"""Exercise scheduling and uncertain API outcomes without AWS or ClickHouse."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SCRIPT = Path(__file__).resolve().parents[2] / "charts/ao-data-platform/files/clickhouse-backup/run_backup.py"
SPEC = importlib.util.spec_from_file_location("backup_scheduler", SCRIPT)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
NOW = datetime(2026, 9, 29, 8, tzinfo=timezone.utc)
FULL = "ao-otel-full-20260929T000000Z-12345678"


def remote(name=FULL, required="", **changes):
    return dict(name=name, location="remote", desc="directory, embedded", required=required, **changes)


def fresh_table(name="spans", **changes):
    row = dict(database="otel_traces", table=name, engine="ReplicatedMergeTree", replica_table=name,
               is_readonly=0, is_session_expired=0, inserts_in_queue=0, absolute_delay=0)
    row.update(changes)
    return row


class FakeProbe:
    def __init__(self, rows=None, failure=False):
        self.rows = [fresh_table()] if rows is None else rows
        self.failure = failure
        self.calls = []

    def request(self, method, path, query=None):
        self.calls.append((method, path, query))
        if self.failure:
            raise backup.BackupError("Database probe failed.")
        return self.rows


class Clock:
    def __init__(self):
        self.seconds = 0

    def now(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


class FakeAPI:
    def __init__(self, catalog=None, local=None, history=None, created_required=""):
        self.catalog = list(catalog or [])
        self.local = list(local or [])
        self.history = list(history or [])
        # The server's completed dependency is independent of the submitted
        # diff-from-remote value, so verification must compare the two.
        self.created_required = created_required
        self.calls = []
        self.unavailable = False
        self.table_failure = False
        self.post_failure = False
        self.acknowledgement_changes = {}
        self.acknowledgement_count = 1
        self.statuses = ["success"]
        self.status_read_failures = 0
        self.publish_remote = True
        self.operation = None

    @property
    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]

    def request(self, method, path, query=None):
        self.calls.append((method, path, query))
        if self.unavailable:
            raise backup.BackupError("not reachable")
        if method == "POST":
            if self.post_failure:
                raise backup.BackupError("connection lost after submitting")
            operation = "download" if path.startswith("/backup/download/") else "create_remote"
            name = path.rsplit("/", 1)[1] if operation == "download" else query["name"]
            self.operation = (operation, name, query or {})
            self.operation_id = "id-" + str(len(self.history) + 1)
            acknowledgement = {"status": "acknowledged", "operation": operation,
                               "backup_name": name, "operation_id": self.operation_id}
            self.history.append(dict(acknowledgement, status="in progress"))
            acknowledgement.update(self.acknowledgement_changes)
            return [acknowledgement] * self.acknowledgement_count
        if path == "/backup/actions":
            return list(self.history)
        if path == "/backup/status":
            if not query:
                return self.history[-1:]
            if self.status_read_failures:
                self.status_read_failures -= 1
                raise backup.BackupError("read timed out")
            status = self.statuses[0]
            if len(self.statuses) > 1:
                self.statuses.pop(0)
            if status == "success":
                operation, name, _ = self.operation
                if operation == "download":
                    self.local.append(dict(name=name, location="local", desc="embedded", required=""))
                elif self.publish_remote:
                    self.catalog.append(remote(name, self.created_required))
            for row in self.history:
                if row.get("operation_id") == self.operation_id:
                    row["status"] = status
            return [{"status": status, "operation_id": self.operation_id, "error": "private error detail"}]
        if path == "/backup/tables":
            if self.table_failure:
                raise backup.BackupError("ClickHouse is down")
            self.history.append({"command": "tables", "status": "success"})
            return [{"database": "otel_traces", "name": "spans"}]
        if path == "/backup/list/remote":
            self.history.append({"command": "list remote", "status": "success"})
            return self.catalog
        if path == "/backup/list/local":
            self.history.append({"command": "list local", "status": "success"})
            return self.local
        raise AssertionError((method, path, query))


class SchedulerTests(unittest.TestCase):
    def run_scheduler(self, *apis, timeout=30, probes=None):
        clock = Clock()
        probes = probes if probes is not None else [FakeProbe() for _ in apis]
        scheduler = backup.Scheduler(apis, probes, timeout=timeout, poll_seconds=10,
                                     clock=clock.now, sleep=clock.sleep)
        with redirect_stdout(io.StringIO()):
            return scheduler.run(NOW)

    def test_behind_readonly_expired_or_incomplete_copy_is_not_selected(self):
        bad_rows = [[], [fresh_table(absolute_delay=1)], [fresh_table(inserts_in_queue=1)],
                    [fresh_table(is_readonly=1)], [fresh_table(is_session_expired=1)],
                    [fresh_table(replica_table="")],
                    [fresh_table(), fresh_table("new_table", replica_table="")],
                    [fresh_table(), fresh_table()],
                    [fresh_table(absolute_delay="0")], [fresh_table(inserts_in_queue=None)]]
        for field in ("absolute_delay", "inserts_in_queue", "is_readonly", "is_session_expired"):
            row = fresh_table()
            del row[field]
            bad_rows.append([row])
        for rows in bad_rows:
            with self.subTest(rows=rows):
                first, second = FakeAPI(), FakeAPI()
                self.run_scheduler(first, second, probes=[FakeProbe(rows), FakeProbe()])
                self.assertEqual(first.posts, [])
                self.assertEqual(len(second.posts), 1)

    def test_database_probe_failure_uses_only_a_qualified_copy(self):
        first, second = FakeAPI(), FakeAPI()
        self.run_scheduler(first, second, probes=[FakeProbe(failure=True), FakeProbe()])
        self.assertEqual(first.posts, [])
        self.assertEqual(len(second.posts), 1)

    def test_no_fresh_copy_stops_without_a_backup(self):
        first, second = FakeAPI(), FakeAPI()
        with self.assertRaisesRegex(backup.BackupError, "No ClickHouse copy is caught up"):
            self.run_scheduler(first, second, probes=[FakeProbe([fresh_table(absolute_delay=1)]), FakeProbe([])])
        self.assertEqual(first.posts + second.posts, [])

    def test_copy_that_falls_behind_during_metadata_download_is_not_backed_up(self):
        first, second = FakeAPI([remote()], created_required=FULL), FakeAPI()
        probe = FakeProbe()
        with mock.patch.object(probe, "request", side_effect=[[fresh_table()], [fresh_table(inserts_in_queue=1)]]):
            with self.assertRaisesRegex(backup.BackupError, "no longer caught up"):
                self.run_scheduler(first, second, probes=[probe, FakeProbe()])
        self.assertEqual(first.posts, [("POST", "/backup/download/" + FULL, None)])
        self.assertEqual(second.posts, [])

    def test_pending_merges_do_not_disqualify_a_current_copy(self):
        first = FakeAPI()
        probe = FakeProbe([fresh_table(queue_size=30, merges_in_queue=30)])
        self.run_scheduler(first, probes=[probe])
        self.assertEqual(len(first.posts), 1)
        self.assertIn("FROM system.tables", probe.calls[0][2]["query"])
        self.assertIn("LEFT JOIN system.replicas", probe.calls[0][2]["query"])

    def test_running_work_on_a_behind_copy_still_blocks_a_fresh_copy(self):
        first, second = FakeAPI(), FakeAPI(history=[{"status": "in progress"}])
        with self.assertRaisesRegex(backup.BackupError, "already running"):
            self.run_scheduler(first, second, probes=[FakeProbe(), FakeProbe([fresh_table(absolute_delay=100)])])
        self.assertEqual(first.posts + second.posts, [])

    def test_revision_failure_is_not_treated_as_an_unreachable_copy(self):
        first, second = FakeAPI(), FakeAPI()
        with mock.patch.object(first, "request", side_effect=backup.RevisionError("Password revision changed.")):
            with self.assertRaisesRegex(backup.RevisionError, "Password revision changed"):
                self.run_scheduler(first, second)
        self.assertEqual(first.posts + second.posts, [])

    def test_first_run_creates_full_for_all_trace_tables_on_copy_zero(self):
        first, second = FakeAPI(), FakeAPI()
        name = self.run_scheduler(first, second)
        self.assertTrue(name.startswith("ao-otel-full-20260929T080000Z-"))
        self.assertEqual(first.posts, [("POST", "/backup/create_remote", {"name": name, "table": "otel_traces.*"})])
        self.assertEqual(second.posts, [])

    def test_daily_full_is_used_even_if_newer_increment_exists(self):
        full = remote()
        inc = remote("ao-otel-incremental-20260929T040000Z-abcdefgh", FULL)
        first = FakeAPI([full, inc], [dict(full, location="local", desc="embedded")], created_required=FULL)
        name = self.run_scheduler(first)
        self.assertIn("incremental", name)
        self.assertEqual(len(first.posts), 1)
        self.assertEqual(first.posts[0][2]["diff-from-remote"], FULL)

    def test_new_day_creates_full_even_if_previous_full_is_recent(self):
        first = FakeAPI([remote("ao-otel-full-20260928T235959Z-12345678")])
        self.run_scheduler(first)
        self.assertNotIn("diff-from-remote", first.posts[0][2])

    def test_invalid_fulls_are_not_used(self):
        bad_name = "ao-otel-full-20260929T070000Z-12345678"
        rows = [dict(remote(bad_name), desc="metadata.json is broken, embedded"),
                remote("manual-full-20260929T060000Z-12345678"),
                remote("ao-otel-full-20260929T090000Z-12345678"),
                remote("ao-otel-full-20260929T050000Z-12345678", "missing-base"),
                remote("ao-otel-full-20260929T996099Z-12345678")]
        self.assertIsNone(backup.full_base(rows, NOW))
        self.assertEqual(backup.full_base([remote(), *rows], NOW), FULL)

    def test_latest_completed_full_of_today_wins(self):
        latest = "ao-otel-full-20260929T040000Z-abcdef12"
        self.assertEqual(backup.full_base([remote(), remote(latest)], NOW), latest)

    def test_fallback_downloads_full_metadata_from_shared_remote_catalog(self):
        first, second = FakeAPI(), FakeAPI([remote()], created_required=FULL)
        first.unavailable = True
        self.run_scheduler(first, second)
        self.assertEqual(first.posts, [])
        self.assertEqual(second.posts[0], ("POST", "/backup/download/" + FULL, None))
        self.assertEqual(second.posts[1][2]["diff-from-remote"], FULL)
        self.assertFalse(any("schema" in (query or {}) for _, _, query in second.posts))
        self.assertFalse(any("delete" in path or "restore" in path for _, path, _ in second.calls))

    def test_api_alive_but_clickhouse_down_uses_copy_one(self):
        first, second = FakeAPI(), FakeAPI()
        first.table_failure = True
        self.run_scheduler(first, second)
        self.assertEqual(first.posts, [])
        self.assertEqual(len(second.posts), 1)

    def test_active_operation_on_either_copy_prevents_new_backup(self):
        for busy_index in (0, 1):
            with self.subTest(copy=busy_index):
                apis = [FakeAPI(), FakeAPI()]
                apis[busy_index].history = [{"command": "create_remote", "status": "in progress"}]
                with self.assertRaisesRegex(backup.BackupError, "already running"):
                    self.run_scheduler(*apis)
                self.assertEqual(apis[0].posts + apis[1].posts, [])

    def test_successful_list_does_not_hide_an_earlier_running_backup(self):
        first, second = FakeAPI(), FakeAPI(history=[
            {"command": "create_remote", "status": "in progress"},
            {"command": "list remote", "status": "success"},
        ])
        self.assertEqual(second.request("GET", "/backup/status"), second.history[-1:])
        with self.assertRaisesRegex(backup.BackupError, "already running"):
            self.run_scheduler(first, second)
        self.assertEqual(first.posts + second.posts, [])

    def test_ambiguous_submission_is_never_retried_or_moved(self):
        first, second = FakeAPI(), FakeAPI()
        first.post_failure = True
        with self.assertRaisesRegex(backup.BackupError, "not confirmed"):
            self.run_scheduler(first, second)
        self.assertEqual(len(first.posts), 1)
        self.assertEqual(second.posts, [])

    def test_asynchronous_failure_is_not_success_and_does_not_leak_error(self):
        first, second = FakeAPI(), FakeAPI()
        first.statuses = ["in progress", "error"]
        with self.assertRaises(backup.BackupError) as caught:
            self.run_scheduler(first, second)
        self.assertIn("operation failed", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(first.posts), 1)
        self.assertEqual(second.posts, [])

    def test_cancel_and_unknown_status_stop_without_retry_or_switch(self):
        for status, message in [("cancel", "operation failed"), ("unexpected", "unknown operation status")]:
            with self.subTest(status=status):
                first, second = FakeAPI(), FakeAPI()
                first.statuses = [status]
                with self.assertRaisesRegex(backup.BackupError, message) as caught:
                    self.run_scheduler(first, second)
                self.assertNotIn("private error detail", str(caught.exception))
                self.assertEqual(len(first.posts), 1)
                self.assertEqual(second.posts, [])
                polls = [call for call in first.calls if call[1] == "/backup/status"]
                self.assertEqual(len(polls), 1)

    def test_malformed_acknowledgement_is_never_retried_or_moved(self):
        cases = [
            ({}, 0), ({}, 2), ({"status": "success"}, 1),
            ({"operation": "download"}, 1), ({"backup_name": "another-backup"}, 1),
            ({"operation_id": ""}, 1), ({"operation_id": None}, 1),
        ]
        for changes, count in cases:
            with self.subTest(changes=changes, count=count):
                first, second = FakeAPI(), FakeAPI()
                first.acknowledgement_changes = changes
                first.acknowledgement_count = count
                with self.assertRaisesRegex(backup.BackupError, "not confirmed; no retry or switch"):
                    self.run_scheduler(first, second)
                self.assertEqual(len(first.posts), 1)
                self.assertEqual(second.posts, [])
                self.assertFalse(any(call[1] == "/backup/status" for call in first.calls))

    def test_wait_timeout_does_not_resubmit(self):
        first, second = FakeAPI(), FakeAPI()
        first.statuses = ["in progress"]
        with self.assertRaisesRegex(backup.BackupError, "may still be working"):
            self.run_scheduler(first, second)
        self.assertEqual(len(first.posts), 1)
        self.assertEqual(second.posts, [])

    def test_transient_poll_read_failure_retries_only_the_read(self):
        first = FakeAPI()
        first.status_read_failures = 1
        self.run_scheduler(first)
        self.assertEqual(len(first.posts), 1)

    def test_success_requires_matching_completed_remote_metadata(self):
        first = FakeAPI()
        first.publish_remote = False
        with self.assertRaisesRegex(backup.BackupError, "not found in S3"):
            self.run_scheduler(first)

    def test_completed_incremental_must_record_the_requested_full_dependency(self):
        for recorded in ("", "another-full-backup"):
            with self.subTest(recorded=recorded):
                full = remote()
                first = FakeAPI([full], [dict(full, location="local", desc="embedded")],
                                created_required=recorded)
                second = FakeAPI()
                with self.assertRaisesRegex(backup.BackupError, "not found in S3"):
                    self.run_scheduler(first, second)
                self.assertEqual(len(first.posts), 1)
                self.assertEqual(first.posts[0][2]["diff-from-remote"], FULL)
                self.assertEqual(first.catalog[-1]["required"], recorded)
                self.assertEqual(second.posts, [])

    def test_completed_full_must_not_record_a_dependency(self):
        first, second = FakeAPI(created_required=FULL), FakeAPI()
        with self.assertRaisesRegex(backup.BackupError, "not found in S3"):
            self.run_scheduler(first, second)
        self.assertEqual(len(first.posts), 1)
        self.assertEqual(second.posts, [])

    def test_download_failure_never_starts_incremental(self):
        first = FakeAPI([remote()])
        first.statuses = ["error"]
        with self.assertRaisesRegex(backup.BackupError, "download operation failed"):
            self.run_scheduler(first)
        self.assertEqual(len(first.posts), 1)
        self.assertIn("download", first.posts[0][1])

    def test_broken_existing_local_metadata_stops_without_deleting_it(self):
        first = FakeAPI([remote()], [dict(remote(), location="local", desc="broken")])
        with self.assertRaisesRegex(backup.BackupError, "metadata is missing or incomplete"):
            self.run_scheduler(first)
        self.assertEqual(first.posts, [])


class PasswordRevisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.password_file = Path(self.directory.name) / "password"
        self.revision_file = Path(self.directory.name) / "revision"
        self.password_file.write_text("private-password")
        self.revision_file.write_text("revision-1")
        self.guard = backup.PasswordRevisionGuard("private-password", "revision-1",
                                                  self.password_file, self.revision_file,
                                                  "test", "test=clickhouse")
        self.api = backup.API("http://copy0:7171", "backup", "private-password", guard=self.guard)
        self.api.opener = mock.Mock()

    def pod(self, revision="revision-1", ready=True, index=0):
        return {"metadata": {"annotations": {backup.REVISION_ANNOTATION: revision},
                             "labels": {"clickhouse.altinity.com/replica": str(index)}},
                "status": {"conditions": [{"type": "Ready", "status": str(ready)}]}}

    def assert_refused_without_api_request(self, pods, message):
        with mock.patch.object(self.guard, "list_pods", return_value=pods):
            with self.assertRaisesRegex(backup.RevisionError, message) as caught:
                self.api.request("GET", "/backup/actions")
        self.api.opener.open.assert_not_called()
        self.assertNotIn("private-password", str(caught.exception))

    def test_wrong_revision_on_any_copy_stops_before_credentials_are_sent(self):
        for pods in ([self.pod(), self.pod("old")], [self.pod("old", ready=False), self.pod()],
                     [{"metadata": {}}], [None]):
            with self.subTest(pods=pods):
                self.assert_refused_without_api_request(pods, "not all adopted")

    def test_old_job_refuses_a_new_projected_password_or_revision(self):
        self.password_file.write_text("new-private-password")
        self.assert_refused_without_api_request([self.pod()], "mounted backup password changed")
        self.password_file.write_text("private-password")
        self.revision_file.write_text("revision-2")
        self.assert_refused_without_api_request([self.pod()], "wrong revision")

    def test_password_update_during_pod_lookup_stops_before_api_request(self):
        def rotated_pods():
            self.password_file.write_text("new-private-password")
            return [self.pod()]
        with mock.patch.object(self.guard, "list_pods", side_effect=rotated_pods):
            with self.assertRaisesRegex(backup.RevisionError, "mounted backup password changed"):
                self.api.request("GET", "/backup/actions")
        self.api.opener.open.assert_not_called()

    def test_missing_secret_file_stops_before_api_request(self):
        self.revision_file.unlink()
        self.assert_refused_without_api_request([self.pod()], "Could not read")

    def test_missing_or_not_ready_copy_does_not_bypass_revision_checks(self):
        for pods in ([], [self.pod()], [self.pod(ready=False)]):
            with self.subTest(pods=pods), mock.patch.object(self.guard, "list_pods", return_value=pods):
                self.guard()

    def test_missing_copy_is_skipped_without_sending_its_credentials(self):
        self.api.guard = lambda: self.guard(0)
        with mock.patch.object(self.guard, "list_pods", return_value=[self.pod(index=1)]):
            with self.assertRaisesRegex(backup.BackupError, "has no Pod"):
                self.api.request("GET", "/backup/actions")
            self.guard(1)
        self.api.opener.open.assert_not_called()

    def test_pod_without_a_copy_label_cannot_authorize_an_endpoint(self):
        pod = self.pod()
        del pod["metadata"]["labels"]
        self.assert_refused_without_api_request([pod], "Could not identify")

    def test_kubernetes_lookup_failure_does_not_send_api_credentials(self):
        with mock.patch.object(self.guard, "list_pods", side_effect=backup.RevisionError("Could not check Pods.")):
            with self.assertRaisesRegex(backup.RevisionError, "Could not check"):
                self.api.request("GET", "/backup/actions")
        self.api.opener.open.assert_not_called()

    def test_kubernetes_lookup_uses_verified_tls_and_namespace_selector(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"items": [self.pod()]}).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        verified_context = backup.ssl.create_default_context()
        with mock.patch.object(backup.Path, "read_text", return_value="private-kubernetes-token"), \
                mock.patch.object(backup.ssl, "create_default_context", return_value=verified_context) as context, \
                mock.patch.object(backup.urllib.request, "HTTPSHandler") as handler, \
                mock.patch.object(backup.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(self.guard.list_pods(), [self.pod()])
        context.assert_called_once_with(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        handler.assert_called_once_with(context=context.return_value)
        self.assertTrue(verified_context.check_hostname)
        self.assertEqual(verified_context.verify_mode, backup.ssl.CERT_REQUIRED)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://kubernetes.default.svc/api/v1/namespaces/test/pods?labelSelector=test%3Dclickhouse")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-kubernetes-token")

    def test_incomplete_or_invalid_pod_response_fails_without_printing_body(self):
        for payload in (b"private malformed body", b'{}', b'{"items": [], "metadata": {"continue": "next"}}'):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = payload
            opener = mock.Mock()
            opener.open.return_value = response
            with self.subTest(payload=payload), \
                    mock.patch.object(backup.Path, "read_text", return_value="private-token"), \
                    mock.patch.object(backup.ssl, "create_default_context"), \
                    mock.patch.object(backup.urllib.request, "build_opener", return_value=opener):
                with self.assertRaisesRegex(backup.RevisionError, "Could not check") as caught:
                    self.guard.list_pods()
                self.assertNotIn("private", str(caught.exception))


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append((self.path, self.headers.get("Authorization")))
                status, headers, body = cls.response
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).requests = []
        self.settings = {
            "BACKUP_ENDPOINTS": json.dumps([self.url]),
            "BACKUP_DATABASE_ENDPOINTS": json.dumps([self.url]),
            "BACKUP_PASSWORD_FILE": "/credentials/password",
            "BACKUP_PASSWORD_REVISION": "revision-1",
            "POD_NAMESPACE": "test",
            "BACKUP_POD_SELECTOR": "test=clickhouse",
        }

    def test_reads_ndjson_and_sends_password_only_in_header(self):
        type(self).response = (200, {}, b'{"name":"one"}\n{"name":"two"}\n')
        api = backup.API(self.url, "backup", " secret\n")
        self.assertEqual(api.request("GET", "/backup/list/remote"), [{"name": "one"}, {"name": "two"}])
        path, authorization = self.requests[0]
        self.assertEqual(path, "/backup/list/remote")
        self.assertEqual(base64.b64decode(authorization.split()[1]), b"backup: secret\n")

    def test_http_errors_do_not_include_server_body_or_password(self):
        type(self).response = (500, {}, b"private password and cloud details")
        with self.assertRaises(backup.BackupError) as caught:
            backup.API(self.url, "backup", "private").request("GET", "/backup/list/remote")
        self.assertEqual(str(caught.exception), "Backup API returned HTTP 500.")

    def test_redirects_are_rejected_without_forwarding_credentials(self):
        type(self).response = (302, {"Location": self.url + "/other"}, b"")
        with self.assertRaisesRegex(backup.BackupError, "HTTP 302"):
            backup.API(self.url, "backup", "private").request("GET", "/backup/list/remote")
        self.assertEqual(len(self.requests), 1)

    def test_endpoint_credentials_and_query_strings_are_rejected(self):
        for endpoint in ["http://user:pass@service:7171", "http://service:7171?token=secret",
                         "file:///tmp/something", "http://service/path"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(backup.BackupError):
                backup.API(endpoint, "backup", "password")

    def test_database_probe_uses_the_mounted_ca_without_disabling_verification(self):
        verified_context = backup.ssl.create_default_context()
        with mock.patch.object(backup.ssl, "create_default_context", return_value=verified_context) as context, \
                mock.patch.object(backup.urllib.request, "HTTPSHandler") as handler, \
                mock.patch.object(backup.urllib.request, "build_opener"):
            backup.API("https://copy0:8443", "backup_probe", "private", ca_file="/clickhouse-ca/ca.crt")
        context.assert_called_once_with(cafile="/clickhouse-ca/ca.crt")
        handler.assert_called_once_with(context=context.return_value)
        self.assertTrue(verified_context.check_hostname)
        self.assertEqual(verified_context.verify_mode, backup.ssl.CERT_REQUIRED)
        with self.assertRaisesRegex(backup.BackupError, "requires an HTTPS endpoint"):
            backup.API("http://copy0:8123", "backup_probe", "private", ca_file="/clickhouse-ca/ca.crt")

    def test_main_reads_password_from_file_without_logging_it(self):
        settings = dict(self.settings)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(backup.os.environ, settings, clear=True), \
                mock.patch.object(backup.Path, "read_text", return_value="private-password") as read, \
                mock.patch.object(backup, "PasswordRevisionGuard") as guard, \
                mock.patch.object(backup.Scheduler, "run"), \
                mock.patch.object(backup, "API") as api, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(backup.main(), 0)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(len(api.call_args_list), 2)
            self.assertEqual(api.call_args_list[0].args, (self.url, "backup", "private-password"))
            self.assertEqual(api.call_args_list[1].args, (self.url, "backup_probe", "private-password"))
            copy_guard = api.call_args_list[0].kwargs["guard"]
            self.assertIs(copy_guard, api.call_args_list[1].kwargs["guard"])
            copy_guard()
            guard.return_value.assert_has_calls([mock.call(), mock.call(0)])
        self.assertNotIn("private-password", stdout.getvalue() + stderr.getvalue())

    def test_main_failures_return_one_and_keep_credentials_out_of_output(self):
        cases = [
            ("backup failure", {}, "private-password", backup.BackupError("The backup operation failed."),
             "backup operation failed"),
            ("empty password", {}, "", None, "mounted backup password is empty"),
            ("missing endpoints", {"BACKUP_ENDPOINTS": None}, "private-password", None, "check scheduler settings"),
            ("empty endpoints", {"BACKUP_ENDPOINTS": "[]"}, "private-password", None, "nonempty JSON list"),
            ("object endpoints", {"BACKUP_ENDPOINTS": "{}"}, "private-password", None, "nonempty JSON list"),
            ("nonstring endpoint", {"BACKUP_ENDPOINTS": "[1]"}, "private-password", None, "nonempty JSON list"),
            ("invalid endpoints", {"BACKUP_ENDPOINTS": "not json"}, "private-password", None, "check scheduler settings"),
            ("zero timeout", {"BACKUP_TIMEOUT_SECONDS": "0"}, "private-password", None, "must be positive"),
            ("negative timeout", {"BACKUP_TIMEOUT_SECONDS": "-1"}, "private-password", None, "must be positive"),
            ("invalid timeout", {"BACKUP_TIMEOUT_SECONDS": "abc"}, "private-password", None, "check scheduler settings"),
            ("zero poll", {"BACKUP_POLL_SECONDS": "0"}, "private-password", None, "must be positive"),
            ("negative poll", {"BACKUP_POLL_SECONDS": "-1"}, "private-password", None, "must be positive"),
            ("invalid poll", {"BACKUP_POLL_SECONDS": "abc"}, "private-password", None, "check scheduler settings"),
        ]
        for label, changes, password, failure, message in cases:
            with self.subTest(case=label):
                settings = dict(self.settings)
                settings.update(changes)
                settings = {key: value for key, value in settings.items() if value is not None}
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.dict(backup.os.environ, settings, clear=True), \
                        mock.patch.object(backup.Path, "read_text", return_value=password), \
                        mock.patch.object(backup, "PasswordRevisionGuard"), \
                        mock.patch.object(backup.Scheduler, "run", side_effect=failure) as run, \
                        mock.patch.object(backup, "API"), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(backup.main(), 1)
                self.assertIn(message, stderr.getvalue())
                self.assertNotIn("private-password", stdout.getvalue() + stderr.getvalue())
                self.assertNotIn("private server body", stdout.getvalue() + stderr.getvalue())
                if failure is None:
                    run.assert_not_called()

    def test_main_does_not_print_unreadable_password_file_details(self):
        settings = dict(self.settings)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(backup.os.environ, settings, clear=True), \
                mock.patch.object(backup.Path, "read_text", side_effect=OSError("private-password private server body")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(backup.main(), 1)
        self.assertIn("check scheduler settings", stderr.getvalue())
        self.assertNotIn("private", stdout.getvalue() + stderr.getvalue())

    def test_main_api_failure_does_not_print_password_or_server_body(self):
        type(self).response = (500, {}, b"private server body")
        settings = dict(self.settings)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(backup.os.environ, settings, clear=True), \
                mock.patch.object(backup.Path, "read_text", return_value="private-password"), \
                mock.patch.object(backup, "PasswordRevisionGuard"), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(backup.main(), 1)
        self.assertIn("able to list the trace tables and remote backups", stderr.getvalue())
        self.assertNotIn("private-password", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("private server body", stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
