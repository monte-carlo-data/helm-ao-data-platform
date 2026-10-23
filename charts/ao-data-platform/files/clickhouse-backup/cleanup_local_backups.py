#!/usr/bin/env python3
"""Remove old local backup records while keeping every completed remote backup.

Only this scheduler's native-backup names can be removed. Keep all full backups
from the latest successful UTC dates and their incrementals, including required
bases of any other retained local backup. A completed matching remote backup
must still exist immediately before every local deletion.

The pinned server's dedicated delete endpoint is synchronous. Never resend a
request after an uncertain response. Do not run manual backup, restore, or
remote deletion commands alongside the scheduler; the API has no shared lock
across copies. An idle check reduces races but cannot lock out a separate tool.
https://github.com/Altinity/clickhouse-backup/blob/v2.8.1/pkg/server/server.go
https://github.com/Altinity/clickhouse-backup/blob/v2.8.1/pkg/backup/delete.go
"""

from datetime import datetime, timezone
import json
import math
import time

from backup_common import BackupError, REQUEST_TIMEOUT_SECONDS, SCHEDULED_NAME
from cleanup_backups import (READ_PATHS, catalog, local_record_warnings,
                             plan as validate_remote, scheduled_time)


DELETE_PREFIX = "/backup/delete/local/"
DELETE_TIMEOUT_SECONDS = 300


class LocalCleanupTimeLimit(BackupError):
    """The time budget ended before sending another request."""


def validated_rows(rows, location, remote=None):
    """Keep full rows for change detection and dependencies for selection."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise BackupError("Local cleanup stopped: backup metadata is unreadable.")
    dependencies = catalog(rows, location, remote)
    return dependencies, {row["name"]: dict(row) for row in rows}


def ancestors(entries, name):
    result = set()
    while name:
        if name in result:
            raise BackupError("Local cleanup stopped: backup dependencies contain a cycle.")
        if name not in entries or not isinstance(entries[name], str):
            raise BackupError("Local cleanup stopped: a required backup cannot be identified.")
        result.add(name)
        name = entries[name]
    return result


def local_plan(remote, local, now, keep_full_backup_days=2, latest_backup=None):
    """Return kept/delete lists of {copy: index, name: backup name} entries.

    keep_full_backup_days counts distinct successful UTC full-backup dates, not
    individual retries or elapsed days. Gaps therefore retain older good days.
    Broken entries have unknown dependencies, so they stop deletion everywhere.
    Local-only entries are kept, along with any local bases they still need.
    """
    if type(keep_full_backup_days) is not int or keep_full_backup_days < 2:
        raise BackupError("Local cleanup must keep at least two successful full-backup days.")
    # Reuse the remote preview's complete dependency/name/time validation,
    # without using its separate remote-retention selection.
    validate_remote(remote, now, keep_last=1, latest_backup=latest_backup)
    created = {name: scheduled_time(name, now) for name in remote}
    full = {name for name in remote if created[name] is not None and not remote[name]}
    dates = set(sorted({created[name].date() for name in full}, reverse=True)[:keep_full_backup_days])
    protected_full = {name for name in full if created[name].date() in dates}
    chains = {name: ancestors(remote, name) for name in remote}
    protected = {name for name in remote if chains[name] & protected_full}
    if latest_backup is not None:
        protected.update(chains[latest_backup])
    warnings = local_record_warnings(remote, local)
    kept, delete = [], []
    for index, entries in enumerate(local):
        for name, required in entries.items():
            timestamp = scheduled_time(name, now)
            if timestamp is not None and required is not None:
                is_full = SCHEDULED_NAME.fullmatch(name)[1] == "full"
                if is_full == bool(required):
                    raise BackupError("Local cleanup stopped: a local scheduled backup has an invalid dependency.")
                if required:
                    base_time = scheduled_time(required, now)
                    if base_time is None or base_time > timestamp:
                        raise BackupError("Local cleanup stopped: a local scheduled backup has an unexpected base.")
            if required is not None and name in remote and required != remote[name]:
                raise BackupError("Local cleanup stopped: local and remote dependencies disagree.")
        if warnings["broken_local"]:
            # An incomplete entry could conceal a dependency on any base.
            keep = set(entries)
        else:
            combined = {**remote, **entries}
            local_chains = {name: ancestors(combined, name) for name in entries}
            owned = {name for name in entries if SCHEDULED_NAME.fullmatch(name)}
            keep = (set(entries) - owned) | (set(entries) - set(remote)) | (set(entries) & protected)
            for name in tuple(keep):
                keep.update(local_chains[name] & set(entries))
        kept.extend({"copy": index, "name": name} for name in sorted(keep))
        remaining = set(entries) - keep
        while remaining:
            bases = {entries[name] for name in remaining}
            ready = sorted(remaining - bases, reverse=True)
            if not ready:
                raise BackupError("Local cleanup stopped: deletion order could not be established.")
            delete.extend({"copy": index, "name": name} for name in ready)
            remaining.difference_update(ready)
    return {"keep_full_backup_days": keep_full_backup_days, "kept": kept, "delete": delete,
            "deleted": [], **warnings}


class LocalCleaner:
    def __init__(self, apis, keep_full_backup_days=2, timeout=1800, clock=time.monotonic):
        if (not apis or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(keep_full_backup_days) is not int or keep_full_backup_days < 2):
            raise BackupError("Local cleanup requires copies, a positive timeout, and at least two full-backup days.")
        endpoints = [api.endpoint for api in apis if getattr(api, "endpoint", None)]
        if len({id(api) for api in apis}) != len(apis) or len(set(endpoints)) != len(endpoints):
            raise BackupError("Local cleanup requires different ClickHouse copy endpoints.")
        self.apis = tuple(apis)
        self.keep_full_backup_days = keep_full_backup_days
        self.timeout = timeout
        self.clock = clock
        self.deadline = clock() + timeout
        self.pending = None

    def request(self, index, method, path):
        remaining = self.deadline - self.clock()
        if remaining <= REQUEST_TIMEOUT_SECONDS:
            raise LocalCleanupTimeLimit("Local cleanup time limit reached; no further deletion was sent.")
        if method == "GET" and path in READ_PATHS:
            return self.apis[index].request(method, path)
        name = path.removeprefix(DELETE_PREFIX)
        if (method != "POST" or not path.startswith(DELETE_PREFIX)
                or not SCHEDULED_NAME.fullmatch(name) or self.pending != (index, name)):
            raise BackupError("Local cleanup refused an unexpected API request.")
        # After deletion, snapshot() reads actions twice and both lists on
        # every copy. Reserve each request's full timeout and one extra read
        # timeout for processing before starting an irreversible operation.
        required = DELETE_TIMEOUT_SECONDS + (4 * len(self.apis) + 1) * REQUEST_TIMEOUT_SECONDS
        if remaining <= required:
            raise LocalCleanupTimeLimit("Not enough cleanup time remains for a deletion and its checks; "
                                        "no further deletion was sent.")
        # Consume authorization before the request. Even an uncertain response
        # cannot cause the same operation to be sent again through this object.
        self.pending = None
        print("Backup local cleanup: " + json.dumps(
            {"event": "delete_requested", "copy": index, "name": name}, sort_keys=True), flush=True)
        try:
            return self.apis[index].request(method, path,
                request_timeout=DELETE_TIMEOUT_SECONDS)
        except BackupError:
            raise BackupError("Local deletion was not confirmed and may still be running; "
                              "no retry or further deletion was sent.") from None

    def idle(self):
        for index in range(len(self.apis)):
            rows = self.request(index, "GET", "/backup/actions")
            if not isinstance(rows, list):
                raise BackupError("Local cleanup stopped: action history is unreadable.")
            for row in rows:
                if (not isinstance(row, dict) or row.get("status") not in ("success", "error", "cancel")
                        or not isinstance(row.get("command"), str) or not row["command"].strip()):
                    raise BackupError("Local cleanup stopped: a copy is busy or action history is unreadable.")
                if row["command"].startswith("delete ") and row["status"] != "success":
                    raise BackupError("Local cleanup stopped: a previous deletion failed or was cancelled.")

    def snapshot(self):
        self.idle()
        remote = [validated_rows(self.request(i, "GET", "/backup/list/remote"), "remote")
                  for i in range(len(self.apis))]
        if any(rows != remote[0] for rows in remote[1:]):
            raise BackupError("Local cleanup stopped: copies disagree about remote backups.")
        local = [validated_rows(self.request(i, "GET", "/backup/list/local"), "local", remote[0][0])
                 for i in range(len(self.apis))]
        self.idle()
        return remote[0], local

    def unchanged(self, remote, local):
        actual_remote, actual_local = self.snapshot()
        if actual_remote != remote or actual_local != local:
            raise BackupError("Local cleanup stopped: backup metadata changed; no further deletion was sent.")

    def run(self, now=None, latest_backup=None, execute=False):
        if type(execute) is not bool:
            raise BackupError("Local cleanup execute must be an explicit boolean.")
        now = now or datetime.now(timezone.utc)
        remote, local = self.snapshot()
        result = local_plan(remote[0], [entries for entries, _ in local], now,
                            self.keep_full_backup_days, latest_backup)
        result["mode"] = "delete" if execute else "dry-run"
        result["completed"] = False
        try:
            self.unchanged(remote, local)
            if execute:
                self.remove_planned(result, remote, local)
        except LocalCleanupTimeLimit:
            self.pending = None
            result["stop_reason"] = "time_limit"
            return result
        result["completed"] = True
        return result

    def remove_planned(self, result, remote, local):
        for entry in result["delete"]:
            index, name = entry["copy"], entry["name"]
            # Both copies must remain idle and every remote backup, every local
            # dependency and all exposed metadata must match the approved plan.
            self.unchanged(remote, local)
            self.pending = (index, name)
            rows = self.request(index, "POST", DELETE_PREFIX + name)
            expected = {"status": "success", "operation": "delete", "backup_name": name, "location": "local"}
            if rows != [expected]:
                raise BackupError("Local deletion response was not confirmed; no retry or further deletion was sent.")
            del local[index][0][name]
            del local[index][1][name]
            try:
                self.unchanged(remote, local)
            except LocalCleanupTimeLimit:
                raise BackupError("Local deletion returned success, but the time limit prevented its "
                                  "follow-up checks; inspect the backup lists before more work.") from None
            result["deleted"].append(dict(entry))
            print("Backup local cleanup: " + json.dumps(
                {"event": "delete_verified", "copy": index, "name": name}, sort_keys=True), flush=True)
