#!/usr/bin/env python3
"""Run one scheduled backup through the pinned clickhouse-backup v2.8.1 API.

The default CronJob schedule runs every four hours.
The first successful run of each UTC day creates a full backup; later runs use
that day's latest full backup as their base. This avoids a chain of incrementals
when moving between copies. Both copies must share the same native S3 backup disk.

API contract: https://github.com/Altinity/clickhouse-backup/blob/v2.8.1/pkg/server/server.go
Responses are newline-separated JSON objects, including asynchronous operation IDs.
No operation is resubmitted after an uncertain response, and no local backup is
deleted: deleting native S3 disk metadata can also remove the remote files.
"""

import base64
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


TABLES = "otel_traces.*"
FULL_NAME = re.compile(r"ao-otel-full-(\d{8}T\d{6}Z)-[0-9a-f]{8}\Z")


class BackupError(Exception):
    """A safe-to-log failure message, without server bodies or credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, endpoint, username, password):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in ("", "/")):
            raise BackupError("Backup endpoint must be an HTTP(S) host without credentials or a path.")
        self.endpoint = endpoint.rstrip("/")
        encoded = base64.b64encode((username + ":" + password).encode()).decode()
        self.authorization = "Basic " + encoded
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, query=None):
        url = self.endpoint + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url, method=method, data=b"" if method == "POST" else None,
            headers={"Authorization": self.authorization, "Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = response.read(32 * 1024 * 1024 + 1)
            if len(payload) > 32 * 1024 * 1024:
                raise BackupError("Backup API response exceeded the size limit.")
            rows = [json.loads(line) for line in payload.splitlines() if line.strip()]
            if any(not isinstance(row, dict) for row in rows):
                raise BackupError("Backup API returned an unexpected response.")
            return rows
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise BackupError(f"Backup API returned HTTP {code}.") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
            raise BackupError("Backup API could not be reached or returned invalid JSON.") from None


def full_base(catalog, now):
    """Only use this schedule's complete embedded full backups from today.

    The API puts broken-backup errors in `desc`, replacing the format. Requiring
    the exact healthy value excludes those entries. Its "directory" format
    depends on s3.compression_format: none in templates/_backup.tpl; keep that
    setting and this check (including the completed-backup check below) in sync.
    A full backup must also have no `required` dependency.
    """
    candidates = []
    for row in catalog:
        name = row.get("name", "")
        match = FULL_NAME.fullmatch(name) if isinstance(name, str) else None
        if (not match or row.get("location") != "remote"
                or row.get("desc") != "directory, embedded" or row.get("required") != ""):
            continue
        try:
            created = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created.date() == now.date() and created <= now:
            candidates.append((created, row["name"]))
    return max(candidates)[1] if candidates else None


class Scheduler:
    def __init__(self, apis, timeout=10800, poll_seconds=10,
                 clock=time.monotonic, sleep=time.sleep):
        self.apis = apis
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.sleep = sleep
        self.deadline = clock() + timeout

    def check_deadline(self):
        if self.clock() >= self.deadline:
            raise BackupError("Backup wait timed out; the server may still be working. No retry was sent.")

    def choose_copy(self):
        # Check every reachable copy before choosing one. A running command may
        # belong to an earlier Job that stopped waiting for its result. v2.8.1
        # /status shows only the last command, which can be a later list call;
        # /actions still includes any earlier command that remains in progress.
        healthy = []
        for index, api in enumerate(self.apis):
            try:
                rows = api.request("GET", "/backup/actions")
            except BackupError:
                continue
            if any(row.get("status") == "in progress" for row in rows):
                raise BackupError("A backup command is already running; no new backup was started.")
            try:
                tables = api.request("GET", "/backup/tables", {"table": TABLES})
                if not tables:
                    continue
                catalog = api.request("GET", "/backup/list/remote")
            except BackupError:
                continue
            healthy.append((index, api, catalog))
        if not healthy:
            raise BackupError("Neither ClickHouse copy could list the trace tables and remote backups.")
        return healthy[0]

    def operation(self, api, operation, name, path, query=None):
        self.check_deadline()
        try:
            rows = api.request("POST", path, query)
        except BackupError:
            raise BackupError(
                f"The {operation} request was not confirmed. It may still run; no retry or switch was sent."
            ) from None
        if (len(rows) != 1 or rows[0].get("status") != "acknowledged"
                or rows[0].get("operation") != operation
                or rows[0].get("backup_name") != name
                or not isinstance(rows[0].get("operation_id"), str)
                or not rows[0]["operation_id"]):
            raise BackupError("Backup request was not confirmed; no retry or switch was sent.")
        operation_id = rows[0]["operation_id"]
        while True:
            self.check_deadline()
            try:
                rows = api.request("GET", "/backup/status", {"operationid": operation_id})
            except BackupError:
                # Repeating a read is safe. Never send a second POST or switch
                # copies because the original operation may still be running.
                rows = []
            if len(rows) == 1 and rows[0].get("operation_id") == operation_id:
                state = rows[0].get("status")
                if state == "success":
                    return
                if state in ("error", "cancel"):
                    raise BackupError(f"The {operation} operation failed; check the selected copy's backup logs.")
                if state != "in progress":
                    raise BackupError("Backup API returned an unknown operation status; no retry was sent.")
            self.sleep(min(self.poll_seconds, max(0, self.deadline - self.clock())))

    def prepare_base(self, api, name):
        def local_entry():
            rows = api.request("GET", "/backup/list/local")
            return next((row for row in rows if row.get("name") == name), None)

        row = local_entry()
        if row is None:
            # On the native S3 disk this downloads pointers and the .backup
            # manifest, not the database payload. Don't add schema=1: it skips
            # that manifest, which the incremental backup needs.
            self.operation(api, "download", name, "/backup/download/" + name)
            row = local_entry()
        if (not row or row.get("desc") != "embedded"
                or row.get("location") != "local" or row.get("required") != ""):
            raise BackupError("The full backup's local metadata is missing or incomplete; no incremental was started.")

    def run(self, now=None):
        now = now or datetime.now(timezone.utc)
        index, api, catalog = self.choose_copy()
        base = full_base(catalog, now)
        kind = "incremental" if base else "full"
        name = f"ao-otel-{kind}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        print(f"Starting {kind} backup {name} on ClickHouse copy {index}.", flush=True)
        if base:
            self.prepare_base(api, base)
        query = {"name": name, "table": TABLES}
        if base:
            query["diff-from-remote"] = base
        self.operation(api, "create_remote", name, "/backup/create_remote", query)
        rows = api.request("GET", "/backup/list/remote")
        if not any(row.get("name") == name and row.get("location") == "remote"
                   and row.get("desc") == "directory, embedded"
                   and row.get("required") == (base or "") for row in rows):
            raise BackupError("The backup operation finished, but its completed entry was not found in S3.")
        print(f"Verified completed {kind} backup {name} in S3.", flush=True)
        return name


def main():
    try:
        endpoints = json.loads(os.environ["BACKUP_ENDPOINTS"])
        if not isinstance(endpoints, list) or not endpoints or not all(isinstance(x, str) for x in endpoints):
            raise BackupError("BACKUP_ENDPOINTS must be a nonempty JSON list of URLs.")
        password = Path(os.environ.get("BACKUP_PASSWORD_FILE", "/credentials/password")).read_text()
        if not password:
            raise BackupError("The mounted backup password is empty.")
        username = os.environ.get("BACKUP_API_USERNAME", "backup")
        timeout = int(os.environ.get("BACKUP_TIMEOUT_SECONDS", "10800"))
        poll_seconds = int(os.environ.get("BACKUP_POLL_SECONDS", "10"))
        if timeout <= 0 or poll_seconds <= 0:
            raise BackupError("Backup timeout and polling interval must be positive seconds.")
        apis = [API(endpoint, username, password) for endpoint in endpoints]
        Scheduler(apis, timeout, poll_seconds).run()
        return 0
    except (KeyError, ValueError, OSError):
        print("Backup failed: check scheduler settings and the mounted password file.", file=sys.stderr)
    except BackupError as error:
        print(f"Backup failed: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
