#!/usr/bin/env python3
"""Preview retention after a verified backup without deleting any backup files.

Call inside the same non-overlapping CronJob. Both copies and every dependency
must be readable. Only this scheduler's names are eligible; unrelated backups
and required bases are preserved.

Stock clickhouse-backup 2.8.1 skips native serialization.json objects during
S3 deletion. This module therefore rejects execution before making any API
request and contains no deletion operation. Keep the tool's automatic retention
and S3 lifecycle expiration disabled for the backup prefixes.
Source: https://github.com/Altinity/clickhouse-backup/blob/v2.8.1/pkg/backup/delete.go
"""

from datetime import datetime, timedelta, timezone
import re
import time

from run_backup import BackupError


SCHEDULED_NAME = re.compile(r"ao-otel-(full|incremental)-(\d{8}T\d{6}Z)-[0-9a-f]{8}\Z")
DELETION_UNAVAILABLE = "Backup deletion is unavailable with stock clickhouse-backup 2.8.1; cleanup supports preview only."


def scheduled_time(name, now):
    match = SCHEDULED_NAME.fullmatch(name)
    if not match:
        if name.startswith("ao-otel-"):
            raise BackupError("Cleanup stopped: a scheduled backup name is malformed.")
        return None
    try:
        created = datetime.strptime(match[2], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise BackupError("Cleanup stopped: a backup timestamp is invalid.") from None
    if created > now:
        raise BackupError("Cleanup stopped: a backup timestamp is in the future.")
    return created


def catalog(rows, location):
    """Keep only stable API fields; error bodies and credentials never enter logs."""
    result = {}
    for row in rows:
        name, required = row.get("name"), row.get("required")
        if (not isinstance(name, str) or not name or name in result
                or not isinstance(required, str) or row.get("location") != location
                or row.get("desc") != ("directory, embedded" if location == "remote" else "embedded")):
            raise BackupError("Cleanup stopped: backup metadata is broken, duplicated, or unsupported.")
        result[name] = required
    return result


def plan(remote, now, keep_last=2, keep_days=0, latest_backup=None):
    """Keep newest individual backups, the age window, and all required bases."""
    if (type(keep_last) is not int or keep_last < 1
            or type(keep_days) is not int or keep_days < 0):
        raise BackupError("Cleanup needs a positive keep_last and nonnegative keep_days.")
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise BackupError("Cleanup needs the current time in UTC.")
    dates = {name: scheduled_time(name, now) for name in remote}
    owned = {name for name, created in dates.items() if created is not None}
    for name, required in remote.items():
        if required and required not in remote:
            raise BackupError("Cleanup stopped: a backup's required base is missing.")
        if name in owned:
            is_full = SCHEDULED_NAME.fullmatch(name)[1] == "full"
            if is_full == bool(required):
                raise BackupError("Cleanup stopped: a scheduled backup has an invalid dependency.")
            if required and (required not in owned or dates[required] > dates[name]):
                raise BackupError("Cleanup stopped: a scheduled backup has an unexpected base.")

    # Validate every chain, including unrelated backups that may use our bases.
    ancestors = {}
    for name in remote:
        chain, required = set(), name
        while required:
            if required in chain:
                raise BackupError("Cleanup stopped: backup dependencies contain a cycle.")
            chain.add(required)
            required = remote[required]
        ancestors[name] = chain

    newest = sorted(owned, key=lambda name: (dates[name], name), reverse=True)
    if latest_backup is not None and latest_backup not in owned:
        raise BackupError("Cleanup stopped: the newly verified backup is missing.")
    kept = set(newest[:keep_last]) | (set(remote) - owned)
    if latest_backup:
        kept.add(latest_backup)
    if keep_days:
        cutoff = now - timedelta(days=keep_days)
        kept.update(name for name in owned if dates[name] >= cutoff)
    kept = set().union(*(ancestors[name] for name in kept)) if kept else set()

    # List a dependent before its base, even when timestamps are equal.
    remaining, ordered = owned - kept, []
    while remaining:
        bases = {remote[name] for name in remaining}
        ready = sorted(remaining - bases, key=lambda name: (dates[name], name), reverse=True)
        if not ready:
            raise BackupError("Cleanup stopped: deletion order could not be established.")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return {"keep_last": keep_last, "keep_days": keep_days,
            "kept": sorted(owned & kept), "delete": ordered}


class Cleaner:
    def __init__(self, apis, keep_last=2, keep_days=0, timeout=600, clock=time.monotonic):
        if len(apis) != 2 or timeout <= 0:
            raise BackupError("Cleanup requires both ClickHouse copies and a positive timeout.")
        if apis[0] is apis[1] or (getattr(apis[0], "endpoint", None)
                                  and apis[0].endpoint == getattr(apis[1], "endpoint", None)):
            raise BackupError("Cleanup requires two different ClickHouse copy endpoints.")
        self.apis = apis
        self.keep_last = keep_last
        self.keep_days = keep_days
        self.clock = clock
        self.deadline = clock() + timeout

    def request(self, index, method, path):
        if method != "GET":
            raise BackupError(DELETION_UNAVAILABLE)
        # The shared API's request timeout is 30 seconds. Do not start a request
        # without room for it; callers also bound this Cleaner by the Job limit.
        if self.clock() + 30 > self.deadline:
            raise BackupError("Cleanup preview time limit reached; no files were changed.")
        return self.apis[index].request(method, path)

    def idle(self):
        for index in range(2):
            for row in self.request(index, "GET", "/backup/actions"):
                state, command = row.get("status"), row.get("command")
                if state not in ("success", "error", "cancel") or not isinstance(command, str) or not command:
                    raise BackupError("Cleanup stopped: a copy is busy or its action history is unreadable.")
                if command.startswith("delete ") and state != "success":
                    raise BackupError("Cleanup stopped: a previous deletion failed; inspect it first.")

    def snapshot(self):
        self.idle()
        remote = [catalog(self.request(i, "GET", "/backup/list/remote"), "remote") for i in range(2)]
        if remote[0] != remote[1]:
            raise BackupError("Cleanup stopped: the copies disagree about remote backups.")
        local = [catalog(self.request(i, "GET", "/backup/list/local"), "local") for i in range(2)]
        for entries in local:
            if any(name not in remote[0] or required != remote[0][name] for name, required in entries.items()):
                raise BackupError("Cleanup stopped: local metadata has no matching remote backup; inspect it first.")
        self.idle()
        return remote[0], local

    def unchanged(self, remote, local):
        # Do not report a retention decision from a catalog that changed while read.
        self.idle()
        for index in range(2):
            if (catalog(self.request(index, "GET", "/backup/list/remote"), "remote") != remote
                    or catalog(self.request(index, "GET", "/backup/list/local"), "local") != local[index]):
                raise BackupError("Cleanup stopped: backup metadata changed during cleanup.")
        self.idle()

    def run(self, now=None, latest_backup=None, execute=False):
        if type(execute) is not bool:
            raise BackupError("Cleanup execute must be an explicit boolean.")
        if execute:
            raise BackupError(DELETION_UNAVAILABLE)
        now = now or datetime.now(timezone.utc)
        remote, local = self.snapshot()
        result = plan(remote, now, self.keep_last, self.keep_days, latest_backup)
        result.update(mode="dry-run", deleted=[])
        self.unchanged(remote, local)
        return result
