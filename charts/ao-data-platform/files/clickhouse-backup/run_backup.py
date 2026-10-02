#!/usr/bin/env python3
"""Run one scheduled backup through the pinned clickhouse-backup v2.8.1 API.

The default CronJob schedule runs every four hours.
The first successful run of each UTC day creates a full backup; later runs use
that day's latest full backup as their base. This avoids a chain of incrementals
when moving between copies. All copies must share the same native S3 backup disk.

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
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


TABLES = "otel_traces.*"
FULL_NAME = re.compile(r"ao-otel-full-(\d{8}T\d{6}Z)-[0-9a-f]{8}\Z")
REVISION_ANNOTATION = "backup.montecarlodata.com/password-revision"
REPLICA_QUERY = """SELECT t.database AS database, t.name AS table, t.engine AS engine,
    r.table AS replica_table, r.is_readonly AS is_readonly,
    r.is_session_expired AS is_session_expired,
    r.absolute_delay AS absolute_delay
FROM system.tables AS t LEFT JOIN system.replicas AS r
    ON t.database = r.database AND t.name = r.table
WHERE t.database = 'otel_traces' AND startsWith(t.engine, 'Replicated')
SETTINGS output_format_json_quote_64bit_integers = 0
FORMAT JSONEachRow"""


class BackupError(Exception):
    """A safe-to-log failure message, without server bodies or credentials."""


class RevisionError(BackupError):
    """A password rotation must stop the whole Job, rather than trigger failover."""


class RevisionCheckUnavailable(BackupError):
    """Credentials must not be sent until the revision can be checked again."""


class AuthenticationError(BackupError):
    """The backup server rejected a credential; do not send it again."""


class RequestNotSent(BackupError):
    """The request was refused locally or could not establish a connection."""


class ReadUnavailable(BackupError):
    """A temporary failure permits another read, but never another operation."""


class ReplicaError(BackupError):
    """This copy cannot supply a backup, regardless of a short replication wait."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, endpoint, username, password, guard=None, ca_file=None, backup_api=True):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in ("", "/")):
            raise BackupError("Backup endpoint must be an HTTP(S) host without credentials or a path.")
        self.endpoint = endpoint.rstrip("/")
        encoded = base64.b64encode((username + ":" + password).encode()).decode()
        self.authorization = "Basic " + encoded
        handlers = [NoRedirect()]
        if ca_file:
            if parsed.scheme != "https":
                raise BackupError("A database CA file requires an HTTPS endpoint.")
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
        self.opener = urllib.request.build_opener(*handlers)
        self.guard = guard
        self.backup_api = backup_api

    def request(self, method, path, query=None):
        if self.guard:
            self.guard()
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
            if self.backup_api and code in (401, 403):
                raise AuthenticationError(
                    f"Backup API rejected authentication (HTTP {code}). The password may have changed without a revision update. "
                    "The server may have logged the submitted password; follow the password rotation instructions. "
                    "No retry or switch was sent."
                ) from None
            if code in (408, 429) or code >= 500:
                raise ReadUnavailable(f"Backup API returned HTTP {code}.") from None
            raise BackupError(f"Backup API returned HTTP {code}.") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            # A timeout can happen after the server receives the request.
            # Only these connection failures prove it was never contacted.
            if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
                raise RequestNotSent("Backup API connection could not be established; no request was sent.") from None
            raise ReadUnavailable("Backup API response could not be read; the request may have reached the server.") from None
        except ValueError:
            raise BackupError("Backup API returned invalid JSON.") from None


class PasswordRevisionGuard:
    """Check projected credentials and every existing copy before sending them."""

    def __init__(self, password, desired_revision, password_file, revision_file, namespace, pod_names):
        if (not desired_revision or not namespace or not isinstance(pod_names, list) or not pod_names
                or not all(isinstance(name, str) and name for name in pod_names)
                or len(set(pod_names)) != len(pod_names)):
            raise RevisionError("Backup password revision and Pod lookup settings are required.")
        self.password = password
        self.desired_revision = desired_revision
        self.password_file = Path(password_file)
        self.revision_file = Path(revision_file)
        self.namespace = namespace
        self.pod_names = pod_names

    def get_pods(self):
        directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        try:
            token = (directory / "token").read_text().strip()
            if not token:
                raise ValueError("empty token")
            context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
            opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
            prefix = "/api/v1/namespaces/" + urllib.parse.quote(self.namespace, safe="") + "/pods/"
            pods = []
            for name in self.pod_names:
                request = urllib.request.Request(
                    "https://kubernetes.default.svc" + prefix + urllib.parse.quote(name, safe=""),
                    headers={"Authorization": "Bearer " + token})
                try:
                    with opener.open(request, timeout=10) as response:
                        payload = response.read(4 * 1024 * 1024 + 1)
                except urllib.error.HTTPError as error:
                    code = error.code
                    error.close()
                    if code == 404:
                        continue
                    raise
                if len(payload) > 4 * 1024 * 1024:
                    raise ValueError("oversized Pod response")
                result = json.loads(payload)
                if not isinstance(result, dict) or result.get("metadata", {}).get("name") != name:
                    raise ValueError("unexpected Pod response")
                pods.append(result)
            return pods
        except urllib.error.HTTPError as error:
            error.close()
            raise RevisionCheckUnavailable("Could not check ClickHouse Pod password revisions; no further backup request was sent.") from None
        except (OSError, ValueError, TypeError, AttributeError, urllib.error.URLError, http.client.HTTPException):
            raise RevisionCheckUnavailable("Could not check ClickHouse Pod password revisions; no further backup request was sent.") from None

    def check_password(self):
        try:
            before = self.revision_file.read_text()
            password = self.password_file.read_text()
            after = self.revision_file.read_text()
        except OSError:
            raise RevisionError("Could not read the mounted backup password revision; no further backup request was sent.") from None
        if before != self.desired_revision or after != before or password != self.password:
            raise RevisionError("The mounted backup password changed or has the wrong revision; no further backup request was sent.")

    def __call__(self, copy_index=None):
        self.check_password()
        copies = set()
        for pod in self.get_pods():
            if (not isinstance(pod, dict) or not isinstance(pod.get("metadata"), dict)
                    or not isinstance(pod["metadata"].get("annotations"), dict)
                    or pod["metadata"]["annotations"].get(REVISION_ANNOTATION) != self.desired_revision):
                raise RevisionError("ClickHouse copies have not all adopted the requested backup password revision; no further backup request was sent.")
            name = pod["metadata"].get("name")
            if name not in self.pod_names:
                raise RevisionError("Could not identify a ClickHouse copy in the Pod response; no further backup request was sent.")
            copies.add(self.pod_names.index(name))
        # Pod lookup can take time; refuse a projected-secret update that
        # arrived while that read was in progress as well.
        self.check_password()
        if copy_index is not None and copy_index not in copies:
            raise RequestNotSent("The selected ClickHouse copy has no Pod; no further backup request was sent.")


def replica_is_fresh(api, replica_count=2, max_delay=5):
    # Inventory comes from system.tables, not just system.replicas: a missing
    # replica row must not make a partially initialized copy look current.
    rows = api.request("GET", "/", {"query": REPLICA_QUERY})
    names = set()
    fresh = True
    for row in rows:
        name = row.get("table")
        if (row.get("database") != "otel_traces" or not isinstance(name, str) or not name
                or name in names or not isinstance(row.get("engine"), str)
                or not row["engine"].startswith("Replicated") or row.get("replica_table") != name):
            raise ReplicaError("A ClickHouse copy has missing or invalid replication metadata.")
        names.add(name)
        for field in ("is_readonly", "is_session_expired"):
            if type(row.get(field)) is not int or row[field] != 0:
                raise ReplicaError("A ClickHouse copy is read-only or has an expired replication session.")
        delay = row.get("absolute_delay")
        if type(delay) is not int or delay < 0:
            raise ReplicaError("A ClickHouse copy has missing or invalid replication delay.")
        # A healthy fetch can briefly queue inserts. Bound their age instead
        # of rejecting ongoing ingestion, and do not gate on queued merges.
        fresh = fresh and delay <= max_delay
    if not names and replica_count > 1:
        raise ReplicaError("No replicated trace tables were found on a multi-copy installation; backups require shared replicated data.")
    return fresh


def configured_clients():
    endpoints = json.loads(os.environ["BACKUP_ENDPOINTS"])
    if not isinstance(endpoints, list) or not endpoints or not all(isinstance(x, str) for x in endpoints):
        raise BackupError("BACKUP_ENDPOINTS must be a nonempty JSON list of URLs.")
    database_endpoints = json.loads(os.environ["BACKUP_DATABASE_ENDPOINTS"])
    if (not isinstance(database_endpoints, list) or len(database_endpoints) != len(endpoints)
            or not all(isinstance(x, str) for x in database_endpoints)):
        raise BackupError("BACKUP_DATABASE_ENDPOINTS must contain one database URL per backup endpoint.")
    pod_names = json.loads(os.environ["BACKUP_POD_NAMES"])
    if not isinstance(pod_names, list) or len(pod_names) != len(endpoints):
        raise BackupError("BACKUP_POD_NAMES must contain one Pod name per backup endpoint.")
    password_file = os.environ.get("BACKUP_PASSWORD_FILE", "/credentials/password")
    password = Path(password_file).read_text()
    if not password:
        raise BackupError("The mounted backup password is empty.")
    guard = PasswordRevisionGuard(password, os.environ["BACKUP_PASSWORD_REVISION"], password_file,
                                  os.environ.get("BACKUP_PASSWORD_REVISION_FILE", "/credentials/revision"),
                                  os.environ["POD_NAMESPACE"], pod_names)
    guard()
    probe_password = Path(os.environ.get("BACKUP_PROBE_PASSWORD_FILE", "/probe-credentials/password")).read_text()
    if not probe_password:
        raise BackupError("The mounted backup probe password is empty.")
    guards = [lambda index=index: guard(index) for index in range(len(endpoints))]
    apis = [API(endpoint, os.environ.get("BACKUP_API_USERNAME", "backup"), password, guard=guards[index])
            for index, endpoint in enumerate(endpoints)]
    probes = [API(endpoint, os.environ.get("BACKUP_PROBE_USERNAME", "backup_probe"), probe_password, guard=guards[index],
                  ca_file=os.environ.get("BACKUP_DATABASE_CA_FILE"), backup_api=False)
              for index, endpoint in enumerate(database_endpoints)]
    return apis, probes


def configured_options():
    max_delay = int(os.environ.get("BACKUP_MAX_REPLICA_DELAY_SECONDS", "5"))
    retry_seconds = int(os.environ.get("BACKUP_FRESHNESS_RETRY_SECONDS", "30"))
    if max_delay < 0 or retry_seconds < 0:
        raise BackupError("Backup replication delay and freshness retry settings must be nonnegative seconds.")
    return {"max_replica_delay": max_delay, "freshness_retry_seconds": retry_seconds}


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


def broken_local_entry(row):
    """Recognize the two incomplete-metadata descriptions emitted by v2.8.1."""
    desc = row.get("desc")
    return (row.get("location") == "local" and row.get("required") == ""
            and isinstance(desc, str)
            and (desc == "broken metadata.json not found" or desc.startswith("parse metadata.json error: ")))


class Scheduler:
    def __init__(self, apis, probes, timeout=10800, poll_seconds=10,
                 clock=time.monotonic, sleep=time.sleep, max_replica_delay=5, freshness_retry_seconds=30):
        if len(apis) != len(probes) or not apis:
            raise BackupError("Each backup copy requires its own database freshness check.")
        self.apis = apis
        self.probes = probes
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.sleep = sleep
        self.deadline = clock() + timeout
        self.max_replica_delay = max_replica_delay
        self.freshness_retry_seconds = freshness_retry_seconds

    def check_deadline(self):
        if self.clock() >= self.deadline:
            raise BackupError("Backup wait timed out; the server may still be working. No retry was sent.")

    def pause(self):
        self.sleep(min(self.poll_seconds, max(0, self.deadline - self.clock())))

    def read(self, api, path, query=None):
        """Retry temporary read failures within the existing Job deadline."""
        while True:
            self.check_deadline()
            try:
                return api.request("GET", path, query)
            except (ReadUnavailable, RevisionCheckUnavailable, RequestNotSent):
                self.pause()

    def wait_for_fresh_copy(self, index):
        deadline = min(self.deadline, self.clock() + self.freshness_retry_seconds)
        while True:
            self.check_deadline()
            if replica_is_fresh(self.probes[index], len(self.apis), self.max_replica_delay):
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(min(self.poll_seconds, max(0, deadline - self.clock())))

    def choose_copy(self, now=None):
        # Check every reachable copy before choosing one. A running command may
        # belong to an earlier Job that stopped waiting for its result. v2.8.1
        # /status shows only the last command, which can be a later list call;
        # /actions still includes any earlier command that remains in progress.
        reachable = []
        for index, api in enumerate(self.apis):
            self.check_deadline()
            try:
                rows = api.request("GET", "/backup/actions")
            except RequestNotSent:
                continue
            if any(row.get("status") == "in progress" for row in rows):
                raise BackupError("A backup command is already running; no new backup was started.")
            if any(row.get("status") not in ("success", "error", "cancel")
                   or not isinstance(row.get("command"), str) or not row["command"].strip() for row in rows):
                raise BackupError("Backup API returned an unexpected action history; could not confirm it is idle, so no new backup was started.")
            reachable.append((index, api))
        healthy, reasons = [], []
        for index, api in reachable:
            try:
                if not self.wait_for_fresh_copy(index):
                    continue
                tables = api.request("GET", "/backup/tables", {"table": TABLES})
                if not tables:
                    raise ReplicaError("No trace tables were found to back up.")
                catalog = api.request("GET", "/backup/list/remote")
            except (RevisionError, RevisionCheckUnavailable, AuthenticationError):
                raise
            except ReplicaError as error:
                reasons.append(str(error))
                continue
            except BackupError:
                self.check_deadline()
                continue
            healthy.append((index, api, catalog))
        if not healthy:
            detail = " " + " ".join(dict.fromkeys(reasons)) if reasons else ""
            raise BackupError("No ClickHouse copy is caught up and able to list the trace tables and remote backups." + detail)
        # Every copy uses the same remote storage. Choose the newest complete
        # full reported there, then inspect local pointers before sending work.
        base = full_base([row for _, _, catalog in healthy for row in catalog],
                         now or datetime.now(timezone.utc))
        if not base:
            index, api, _ = healthy[0]
            return index, api, None
        downloadable = []
        for index, api, _ in healthy:
            row = self.local_entry(api, base)
            if row is None:
                downloadable.append((index, api))
            elif self.usable_base(row):
                return index, api, base
            elif not broken_local_entry(row):
                raise BackupError("The full backup's local metadata is missing or incomplete in an unsupported way; no new backup was started.")
        if downloadable:
            index, api = downloadable[0]
            return index, api, base
        # A failed read never reaches here. Every eligible healthy copy has
        # positively listed a broken entry, so leave it intact and use a new
        # name for one replacement full rather than losing the rest of the day.
        print("Every healthy ClickHouse copy has incomplete local full-backup metadata; creating a replacement full backup.", flush=True)
        index, api, _ = healthy[0]
        return index, api, None

    def local_entry(self, api, name):
        rows = [row for row in self.read(api, "/backup/list/local") if row.get("name") == name]
        if len(rows) > 1:
            raise BackupError("The local backup list contains duplicate names; no new backup was started.")
        return rows[0] if rows else None

    @staticmethod
    def usable_base(row):
        return (row.get("desc") == "embedded" and row.get("location") == "local"
                and row.get("required") == "")

    def operation(self, api, operation, name, path, query=None):
        self.check_deadline()
        try:
            rows = api.request("POST", path, query)
        except (RevisionError, RevisionCheckUnavailable, AuthenticationError, RequestNotSent):
            raise
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
            except (ReadUnavailable, RevisionCheckUnavailable, RequestNotSent):
                # Repeating a read is safe. Never send a second POST or switch
                # copies because the original operation may still be running.
                self.pause()
                continue
            if not rows:
                raise BackupError("The backup server no longer knows this operation; it may have restarted. No retry or switch was sent.")
            if len(rows) == 1 and rows[0].get("operation_id") == operation_id:
                state = rows[0].get("status")
                if state == "success":
                    return
                if state in ("error", "cancel"):
                    raise BackupError(f"The {operation} operation failed; check the selected copy's backup logs.")
                if state != "in progress":
                    raise BackupError("Backup API returned an unknown operation status; no retry was sent.")
            else:
                raise BackupError("Backup API returned an unexpected operation record; no retry or switch was sent.")
            self.pause()

    def prepare_base(self, api, name):
        row = self.local_entry(api, name)
        if row is None:
            # On the native S3 disk this downloads pointers and the .backup
            # manifest, not the database payload. Don't add schema=1: it skips
            # that manifest, which the incremental backup needs.
            self.operation(api, "download", name, "/backup/download/" + name)
            row = self.local_entry(api, name)
        if not row or not self.usable_base(row):
            raise BackupError("The full backup's local metadata is missing or incomplete; no incremental was started.")

    def run(self, now=None):
        now = now or datetime.now(timezone.utc)
        index, api, base = self.choose_copy(now)
        kind = "incremental" if base else "full"
        name = f"ao-otel-{kind}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        print(f"Starting {kind} backup {name} on ClickHouse copy {index}.", flush=True)
        if base:
            self.prepare_base(api, base)
        query = {"name": name, "table": TABLES}
        if base:
            query["diff-from-remote"] = base
        # Downloading a full backup's metadata can take time. Recheck the
        # selected copy immediately before creating the database snapshot.
        if not self.wait_for_fresh_copy(index):
            raise BackupError("The selected ClickHouse copy is no longer caught up; no new backup was started.")
        self.operation(api, "create_remote", name, "/backup/create_remote", query)
        rows = self.read(api, "/backup/list/remote")
        if not any(row.get("name") == name and row.get("location") == "remote"
                   and row.get("desc") == "directory, embedded"
                   and row.get("required") == (base or "") for row in rows):
            raise BackupError("The backup operation finished, but its completed entry was not found in S3.")
        print(f"Verified completed {kind} backup {name} in S3.", flush=True)
        return name


def main():
    try:
        timeout = int(os.environ.get("BACKUP_TIMEOUT_SECONDS", "10800"))
        poll_seconds = int(os.environ.get("BACKUP_POLL_SECONDS", "10"))
        if timeout <= 0 or poll_seconds <= 0:
            raise BackupError("Backup timeout and polling interval must be positive seconds.")
        options = configured_options()
        cleanup_enabled = os.environ.get("BACKUP_CLEANUP_ENABLED", "false") == "true"
        if cleanup_enabled and os.environ.get("BACKUP_CLEANUP_DRY_RUN", "true") != "true":
            raise BackupError("Backup deletion is unavailable with stock clickhouse-backup 2.8.1; cleanup supports preview only.")
        apis, probes = configured_clients()
        name = Scheduler(apis, probes, timeout, poll_seconds, **options).run()
        if cleanup_enabled:
            # Running in this same job prevents two scheduled cleanups or a
            # scheduled backup and cleanup from overlapping. A cleanup error
            # fails the job so monitoring reports it even though upload passed.
            from cleanup_backups import Cleaner
            cleaner = Cleaner(apis, keep_last=int(os.environ.get("BACKUP_KEEP_LAST", "2")),
                              keep_days=int(os.environ.get("BACKUP_KEEP_DAYS", "0")),
                              timeout=int(os.environ.get("BACKUP_CLEANUP_TIMEOUT_SECONDS", "1800")))
            result = cleaner.run(latest_backup=name, execute=False)
            print("Backup cleanup: " + json.dumps(result, sort_keys=True), flush=True)
            if result["broken_local"]:
                raise BackupError("The backup and cleanup preview completed, but incomplete local backup files need attention. "
                                  "See broken_local in the preview; no files were deleted.")
        return 0
    except (KeyError, ValueError, OSError):
        print("Backup failed: check scheduler settings and the mounted password file.", file=sys.stderr)
    except BackupError as error:
        print(f"Backup failed: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    # Cleanup imports this error type; keep one copy when this file is executed.
    sys.modules.setdefault("run_backup", sys.modules[__name__])
    sys.exit(main())
