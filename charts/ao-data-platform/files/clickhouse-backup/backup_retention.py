"""Keep complete daily/weekly backup sets and remove approved expired sets.

Use only inside the scheduler's non-overlapping Job. The API has no shared lock
across copies: manual backup, deletion and recovery work must not overlap it.
Local records are removed while their S3 backup still exists, then the remote
backup is removed from a copy without those records. No uncertain write is
retried. S3 version expiry is a separate bucket setting.
"""

from datetime import datetime, timedelta, timezone
import json
import math
import time

from backup_common import (BackupError, CleanupStopped, RequestNotSent, REQUEST_TIMEOUT_SECONDS,
                           RevisionError, RevisionCheckUnavailable, SCHEDULED_NAME)
from cleanup_backups import (READ_PATHS, catalog, local_record_warnings,
                             plan as validate_remote, scheduled_time)


class RetentionTimeLimit(BackupError):
    """The time budget ended before another request was sent."""


def chain(entries, name):
    result = []
    while name:
        if name in result or name not in entries or not isinstance(entries[name], str):
            raise BackupError("Cleanup stopped: a backup dependency is missing, broken, or cyclic.")
        result.append(name)
        name = entries[name]
    return result


def plan_sets(remote, local, now, daily_days=7, history_days=30, latest_backup=None):
    """Keep UTC days, then the newest successful day per older Monday-Sunday week.

    A day's set includes all full backups created that day and every dependent
    incremental. A set with a recent member is kept whole, even if its full is
    older. Manual and local-only dependencies keep their whole required sets.
    """
    if (type(daily_days) is not int or daily_days < 1
            or type(history_days) is not int or history_days < daily_days):
        raise BackupError("Cleanup needs positive daily_days and history_days at least as large.")
    validate_remote(remote, now, keep_last=1, latest_backup=latest_backup)
    dates = {name: scheduled_time(name, now) for name in remote}
    owned = {name for name, date in dates.items() if date is not None}
    chains = {name: chain(remote, name) for name in remote}
    roots = {name: chains[name][-1] for name in owned}
    day_sets = {}
    for name in owned:
        day_sets.setdefault(dates[roots[name]].date(), set()).add(name)

    warnings = local_record_warnings(remote, local)
    if warnings["broken_local"]:
        raise BackupError("Cleanup stopped: incomplete local records have unknown dependencies.")
    protected = set(remote) - owned
    if latest_backup is not None:
        protected.add(latest_backup)
    for entries in local:
        combined = {**remote, **entries}
        for name, required in entries.items():
            created = scheduled_time(name, now)
            if not isinstance(required, str):
                raise BackupError("Cleanup stopped: local dependencies are unreadable.")
            if name in remote and required != remote[name]:
                raise BackupError("Cleanup stopped: local and remote backup dependencies disagree.")
            if created is not None:
                if (SCHEDULED_NAME.fullmatch(name)[1] == "full") == bool(required):
                    raise BackupError("Cleanup stopped: a local scheduled backup has an invalid dependency.")
                if required:
                    base_time = scheduled_time(required, now)
                    if base_time is None or base_time > created:
                        raise BackupError("Cleanup stopped: a local scheduled backup has an unexpected base.")
            dependencies = chain(combined, name)
            if name not in remote or created is None:
                protected.update(set(dependencies) & set(remote))
    protected.update(ancestor for name in tuple(protected) for ancestor in chains[name])

    daily_cutoff = now.date() - timedelta(days=daily_days - 1)
    history_cutoff = now.date() - timedelta(days=history_days - 1)
    kept_days = {day for day, members in day_sets.items() if members & protected}
    weekly = {}
    for day, members in day_sets.items():
        newest = max(dates[name].date() for name in members)
        if newest >= daily_cutoff:
            kept_days.add(day)
        elif newest >= history_cutoff:
            week = newest - timedelta(days=newest.weekday())
            weekly[week] = max(weekly.get(week, (newest, day)), (newest, day))
    kept_days.update(day for _, day in weekly.values())
    kept = (set(remote) - owned) | set().union(*(day_sets[day] for day in kept_days))
    remaining, ordered = owned - kept, []
    while remaining:
        bases = {remote[name] for name in remaining}
        ready = sorted(remaining - bases, key=lambda name: (dates[name], name), reverse=True)
        if not ready:
            raise BackupError("Cleanup stopped: deletion order could not be established.")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return {"retention_policy": "daily-weekly", "daily_days": daily_days, "history_days": history_days,
            "kept": sorted(kept), "delete": ordered, "deleted": [], **warnings}


def validated_rows(rows, location, remote=None):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise BackupError("Cleanup stopped: backup metadata is unreadable.")
    return catalog(rows, location, remote), {row["name"]: dict(row) for row in rows}


class RemoteCleaner:
    def __init__(self, apis, daily_days=7, history_days=30, timeout=1800, clock=time.monotonic):
        if (not apis or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise BackupError("Cleanup requires copies and a positive time limit.")
        if (type(daily_days) is not int or daily_days < 1
                or type(history_days) is not int or history_days < daily_days):
            raise BackupError("Cleanup needs positive daily_days and history_days at least as large.")
        endpoints = [api.endpoint for api in apis if getattr(api, "endpoint", None)]
        if len({id(api) for api in apis}) != len(apis) or len(set(endpoints)) != len(endpoints):
            raise BackupError("Cleanup requires different ClickHouse copy endpoints.")
        self.apis = tuple(apis)
        self.daily_days, self.history_days = daily_days, history_days
        self.clock, self.deadline = clock, clock() + timeout
        self.pending = None
        self.unverified = None
        self.execute = False
        self.allowed = set()
        self.attempted = set()

    def request(self, index, method, path):
        remaining = self.deadline - self.clock()
        if remaining <= REQUEST_TIMEOUT_SECONDS + getattr(self.apis[index], "guard_timeout_seconds", 0):
            raise RetentionTimeLimit("Cleanup time limit reached before the next request.")
        if method == "GET" and path in READ_PATHS | {"/backup/version"}:
            return self.apis[index].request(method, path)
        pieces = path.split("/")
        if (method != "POST" or len(pieces) != 5 or pieces[:3] != ["", "backup", "delete"]
                or pieces[3] not in ("local", "remote") or not SCHEDULED_NAME.fullmatch(pieces[4])
                or pieces[4] not in self.allowed or not self.execute
                or self.pending != (index, pieces[3], pieces[4])
                or self.pending in self.attempted):
            raise BackupError("Cleanup refused an unexpected API request.")
        # Reserve a full deletion request, all five follow-up reads per copy,
        # and one spare read allowance before sending a mutation. Include the
        # password revision lookups which each API performs before its request.
        guard_seconds = (getattr(self.apis[index], "guard_timeout_seconds", 0)
                         + 5 * sum(getattr(api, "guard_timeout_seconds", 0) for api in self.apis))
        required = 300 + (5 * len(self.apis) + 1) * REQUEST_TIMEOUT_SECONDS + guard_seconds
        if remaining <= required:
            raise RetentionTimeLimit("Cleanup time limit reached; no further deletion was sent.")
        self.attempted.add(self.pending)
        self.pending = None
        event = {"copy": index, "location": pieces[3], "name": pieces[4]}
        print("Backup cleanup: " + json.dumps({"event": "delete_requested", **event}, sort_keys=True), flush=True)
        try:
            rows = self.apis[index].request(method, path, request_timeout=300)
        except (RequestNotSent, RevisionError, RevisionCheckUnavailable):
            print("Backup cleanup: " + json.dumps(
                {"event": "delete_not_sent", **event}, sort_keys=True), flush=True)
            raise
        except BackupError:
            self.unverified = {**event, "response_status": "unknown"}
            raise BackupError("Deletion was not confirmed and may still be running; "
                              "no retry or further deletion was sent.") from None
        except Exception:
            # An unexpected transport error does not prove the request stayed local.
            self.unverified = {**event, "response_status": "unknown"}
            raise
        self.unverified = {**event, "response_status": "unknown"}
        return rows

    def idle(self):
        for index in range(len(self.apis)):
            rows = self.request(index, "GET", "/backup/actions")
            if not isinstance(rows, list):
                raise BackupError("Cleanup stopped: action history is unreadable.")
            for row in rows:
                if (not isinstance(row, dict) or row.get("status") not in ("success", "error", "cancel")
                        or not isinstance(row.get("command"), str) or not row["command"].strip()):
                    raise BackupError("Cleanup stopped: a copy is busy or action history is unreadable.")
                if row["command"].startswith("delete ") and row["status"] != "success":
                    raise BackupError("Cleanup stopped: a previous deletion failed or was cancelled.")

    def snapshot(self):
        self.idle()
        if self.execute:
            for index in range(len(self.apis)):
                if self.request(index, "GET", "/backup/version") != [{"version": "2.8.2"}]:
                    raise BackupError("S3 deletion requires clickhouse-backup 2.8.2 on every copy.")
        remote = [validated_rows(self.request(i, "GET", "/backup/list/remote"), "remote")
                  for i in range(len(self.apis))]
        if any(rows != remote[0] for rows in remote[1:]):
            raise BackupError("Cleanup stopped: copies disagree about remote backups.")
        local = [validated_rows(self.request(i, "GET", "/backup/list/local"), "local", remote[0][0])
                 for i in range(len(self.apis))]
        self.idle()
        return remote[0], local

    def unchanged(self, remote, local):
        actual_remote, actual_local = self.snapshot()
        if actual_remote != remote or actual_local != local:
            raise BackupError("Cleanup stopped: backup metadata changed; no further deletion was sent.")

    def remove(self, index, location, name, remote, local, result):
        if location == "remote" and any(name in entries[0] for entries in local):
            raise BackupError("Cleanup refused S3 deletion while local records remain.")
        self.unchanged(remote, local)
        self.pending = (index, location, name)
        event = {"copy": index, "location": location, "name": name}
        rows = self.request(index, "POST", "/backup/delete/" + location + "/" + name)
        if rows != [{"status": "success", "operation": "delete", "backup_name": name, "location": location}]:
            raise BackupError("Deletion response was not confirmed; no retry or further deletion was sent.")
        self.unverified["response_status"] = "success"
        target = local[index] if location == "local" else remote
        del target[0][name]
        del target[1][name]
        self.unchanged(remote, local)
        if location == "remote":
            result["deleted"].append(name)
        self.unverified = None
        print("Backup cleanup: " + json.dumps({"event": "delete_verified", **event}, sort_keys=True), flush=True)

    def run(self, now=None, latest_backup=None, execute=False):
        if type(execute) is not bool:
            raise BackupError("Cleanup execute must be an explicit boolean.")
        if execute and latest_backup is None:
            raise BackupError("S3 deletion requires the latest verified backup name.")
        self.execute = execute
        now = now or datetime.now(timezone.utc)
        remote, local = self.snapshot()
        result = plan_sets(remote[0], [entries for entries, _ in local], now,
                           self.daily_days, self.history_days, latest_backup)
        self.allowed = set(result["delete"])
        result.update(mode="delete" if execute else "dry-run", completed=False, unverified_deletions=[])
        try:
            self.unchanged(remote, local)
            if execute:
                for name in result["delete"]:
                    for index, entries in enumerate(local):
                        if name in entries[0]:
                            self.remove(index, "local", name, remote, local, result)
                    # Both copies have no local record for this exact backup.
                    # Upstream can now remove native data before the S3 catalog.
                    self.remove(0, "remote", name, remote, local, result)
        except Exception as error:
            self.pending = None
            if isinstance(error, RetentionTimeLimit) and self.unverified is None:
                result["stop_reason"] = "time_limit"
                return result
            result["stop_reason"] = "error"
            if self.unverified is not None:
                result["unverified_deletions"].append(dict(self.unverified))
            if isinstance(error, RetentionTimeLimit):
                message = "Deletion returned success, but follow-up checks did not finish; no further deletion was sent."
            elif isinstance(error, BackupError):
                message = str(error)
            else:
                message = f"Unexpected {type(error).__name__}; no further deletion was sent."
            raise CleanupStopped(message, result) from None
        result["completed"] = True
        return result
