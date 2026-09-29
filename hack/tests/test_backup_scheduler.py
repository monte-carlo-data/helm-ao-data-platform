"""Exercise scheduling and uncertain API outcomes without AWS or ClickHouse."""

import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
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


class Clock:
    def __init__(self):
        self.seconds = 0

    def now(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


class FakeAPI:
    def __init__(self, catalog=None, local=None):
        self.catalog = list(catalog or [])
        self.local = list(local or [])
        self.calls = []
        self.unavailable = False
        self.busy = False
        self.table_failure = False
        self.post_failure = False
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
            return [{"status": "acknowledged", "operation": operation,
                     "backup_name": name, "operation_id": "id-1"}]
        if path == "/backup/status":
            if not query:
                return [{"status": "in progress"}] if self.busy else []
            if self.status_read_failures:
                self.status_read_failures -= 1
                raise backup.BackupError("read timed out")
            status = self.statuses[0]
            if len(self.statuses) > 1:
                self.statuses.pop(0)
            if status == "success":
                operation, name, parameters = self.operation
                if operation == "download":
                    self.local.append(dict(name=name, location="local", desc="embedded", required=""))
                elif self.publish_remote:
                    self.catalog.append(remote(name, parameters.get("diff-from-remote", "")))
            return [{"status": status, "operation_id": "id-1", "error": "private error detail"}]
        if path == "/backup/tables":
            if self.table_failure:
                raise backup.BackupError("ClickHouse is down")
            return [{"database": "otel_traces", "name": "spans"}]
        if path == "/backup/list/remote":
            return self.catalog
        if path == "/backup/list/local":
            return self.local
        raise AssertionError((method, path, query))


class SchedulerTests(unittest.TestCase):
    def run_scheduler(self, *apis, timeout=30):
        clock = Clock()
        scheduler = backup.Scheduler(apis, timeout=timeout, poll_seconds=10,
                                     clock=clock.now, sleep=clock.sleep)
        with redirect_stdout(io.StringIO()):
            return scheduler.run(NOW)

    def test_first_run_creates_full_for_all_trace_tables_on_copy_zero(self):
        first, second = FakeAPI(), FakeAPI()
        name = self.run_scheduler(first, second)
        self.assertTrue(name.startswith("ao-otel-full-20260929T080000Z-"))
        self.assertEqual(first.posts, [("POST", "/backup/create_remote", {"name": name, "table": "otel_traces.*"})])
        self.assertEqual(second.posts, [])

    def test_daily_full_is_used_even_if_newer_increment_exists(self):
        full = remote()
        inc = remote("ao-otel-incremental-20260929T040000Z-abcdefgh", FULL)
        first = FakeAPI([full, inc], [dict(full, location="local", desc="embedded")])
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
        first, second = FakeAPI(), FakeAPI([remote()])
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
        first, second = FakeAPI(), FakeAPI()
        second.busy = True
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

    def test_main_reads_password_from_file_without_logging_it(self):
        settings = {"BACKUP_ENDPOINTS": json.dumps([self.url]), "BACKUP_PASSWORD_FILE": "/credentials/password"}
        with mock.patch.dict(backup.os.environ, settings, clear=True), \
                mock.patch.object(backup.Path, "read_text", return_value="private-password") as read, \
                mock.patch.object(backup.Scheduler, "run"), \
                mock.patch.object(backup, "API") as api:
            self.assertEqual(backup.main(), 0)
            read.assert_called_once_with()
            api.assert_called_once_with(self.url, "backup", "private-password")


if __name__ == "__main__":
    unittest.main()
