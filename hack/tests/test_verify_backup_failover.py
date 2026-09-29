"""Run the failover helper against fake kubectl/API responses, never a cluster."""

import argparse
from contextlib import redirect_stdout
import copy
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load("verify_failover", "hack/verify-backup-failover.py")
scheduler = load("run_backup", "charts/ao-data-platform/files/clickhouse-backup/run_backup.py")
FULL = "ao-otel-full-20260929T000000Z-12345678"
NOW = datetime(2026, 9, 29, 0, 30, tzinfo=timezone.utc)


def metadata(name=FULL, location="remote", required=""):
    return {"name":name, "location":location, "required":required,
            "desc":"directory, embedded" if location == "remote" else "embedded"}


class CopyAPI:
    def __init__(self, remote, first=False, unavailable=False):
        self.remote = remote
        self.local = {FULL:metadata(location="local")} if first else {}
        self.history = [{"command":"create_remote " + FULL, "status":"success", "operation_id":"full-id"}] if first else []
        self.posts = []
        self.unavailable = unavailable
        self.fail_post = False

    def request(self, method, path, query=None):
        if self.unavailable:
            raise scheduler.BackupError("not reachable")
        if method == "POST":
            self.posts.append((path, query))
            if self.fail_post:
                raise scheduler.BackupError("uncertain submission")
            operation = "download" if path.startswith("/backup/download/") else "create_remote"
            name = FULL if operation == "download" else query["name"]
            required = "" if operation == "download" else FULL
            self.local[name] = metadata(name, "local", required)
            if operation == "create_remote":
                self.remote[name] = metadata(name, "remote", required)
            identifier = "operation-" + str(len(self.posts))
            self.history.append({"operation_id":identifier, "command":operation + " " + name, "status":"success"})
            return [{"status":"acknowledged", "operation":operation, "backup_name":name, "operation_id":identifier}]
        if path == "/backup/actions":
            return list(self.history)
        if path == "/backup/status":
            return [h for h in self.history if h["operation_id"] == query["operationid"]] if query else self.history[-1:]
        if path == "/backup/tables":
            return [{"database":"otel_traces", "name":"spans"}]
        if path == "/backup/list/remote":
            return list(self.remote.values())
        if path == "/backup/list/local":
            return list(self.local.values())
        raise AssertionError(path)


class PodTests(unittest.TestCase):
    def setUp(self):
        self.remote = {FULL:metadata()}
        self.copies = [CopyAPI(self.remote, first=True), CopyAPI(self.remote)]

    def run_pod(self, expected=FULL):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        fake_datetime = types.ModuleType("datetime")
        fake_datetime.__dict__.update(vars(importlib.import_module("datetime")))
        fake_datetime.datetime = FrozenDateTime
        endpoints = {"http://copy0:7171":self.copies[0], "http://copy1:7171":self.copies[1],
                     "http://127.0.0.1:1":CopyAPI(self.remote, unavailable=True)}
        environment = {"VERIFY_EXPECTED_FULL":expected, "VERIFY_TIMEOUT_SECONDS":"600",
                       "VERIFY_SCRIPT_SHA256":hashlib.sha256(b"installed scheduler").hexdigest(),
                       "BACKUP_ENDPOINTS":json.dumps(list(endpoints)[:2]), "BACKUP_PASSWORD_FILE":"/credentials/password"}
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"run_backup":scheduler, "datetime":fake_datetime}), \
                mock.patch.dict(helper.os.environ, environment, clear=True), \
                mock.patch.object(Path, "read_text", return_value="do-not-log-this-password"), \
                mock.patch.object(Path, "read_bytes", return_value=b"installed scheduler"), \
                mock.patch.object(scheduler, "API", side_effect=lambda endpoint, *args:endpoints[endpoint]), \
                redirect_stdout(output):
            try:
                exec(helper.POD_SCRIPT, {})
            except SystemExit as error:
                self.assertEqual(error.code, 1)
        self.assertNotIn("do-not-log-this-password", output.getvalue())
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0].removeprefix("EVIDENCE "))

    def test_real_scheduler_downloads_then_creates_on_copy_one_only(self):
        result = self.run_pod()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["selected_copy"], 1)
        self.assertEqual((result["remote_before"], result["remote_after"]), (1, 2))
        self.assertEqual(result["required"], FULL)
        self.assertEqual(self.copies[0].posts, [])
        self.assertEqual([p[0] for p in self.copies[1].posts], ["/backup/download/" + FULL, "/backup/create_remote"])

    def test_wrong_full_fails_before_any_post(self):
        self.assertEqual(self.run_pod("ao-otel-full-20260929T001500Z-12345678")["status"], "inconclusive")
        self.assertEqual(self.copies[0].posts + self.copies[1].posts, [])

    def test_active_operation_hidden_by_later_list_still_blocks(self):
        self.copies[0].history.extend([
            {"command":"create_remote another", "status":"in progress", "operation_id":"busy"},
            {"command":"list remote", "status":"success", "operation_id":""},
        ])
        self.assertEqual(self.run_pod()["status"], "inconclusive")
        self.assertEqual(self.copies[0].posts + self.copies[1].posts, [])

    def test_existing_copy_one_metadata_does_not_fake_download_proof(self):
        self.copies[1].local[FULL] = metadata(location="local")
        self.assertEqual(self.run_pod()["status"], "inconclusive")
        self.assertEqual(self.copies[1].posts, [])

    def test_uncertain_download_is_not_retried_and_never_creates_backup(self):
        self.copies[1].fail_post = True
        self.assertEqual(self.run_pod()["status"], "inconclusive")
        self.assertEqual(len(self.copies[1].posts), 1)
        self.assertEqual(self.copies[0].posts, [])

    def test_operation_starting_after_preflight_is_caught_before_post(self):
        original = self.copies[1].request
        reads = 0
        def request(method, path, query=None):
            nonlocal reads
            if path == "/backup/actions":
                reads += 1
                if reads == 3:
                    self.copies[1].history.append({"command":"create_remote concurrent", "status":"in progress", "operation_id":"concurrent"})
            return original(method, path, query)
        self.copies[1].request = request
        self.assertEqual(self.run_pod()["status"], "inconclusive")
        self.assertEqual(self.copies[0].posts + self.copies[1].posts, [])


class CLITests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(context="test", namespace="montecarlo", cronjob="otel-backup",
                                       expected_full=FULL, job_name="verify-one", timeout_seconds=600)
        self.cron = {"metadata":{"uid":"cron-uid"}, "spec":{
            "schedule":"0 */4 * * *", "timeZone":"Etc/UTC", "suspend":False, "concurrencyPolicy":"Forbid",
            "jobTemplate":{"spec":{"template":{"metadata":{"labels":{"component":"backup-job"}},
                "spec":{"restartPolicy":"Never", "containers":[{"name":"backup", "env":[]}],
                        "volumes":[{"name":"script", "configMap":{"name":"live-scheduler"}}]}}}}}}
        self.jobs = []

    def kubectl(self, args, *command):
        if command[:2] == ("get", "cronjob"):
            return json.dumps(self.cron)
        if command[:2] == ("get", "jobs"):
            return json.dumps({"items":self.jobs})
        if command[:2] == ("get", "configmap"):
            return json.dumps({"data":{"run_backup.py":"installed scheduler"}})
        raise AssertionError(command)

    def test_prepare_only_reads_and_preserves_network_labels(self):
        with mock.patch.object(helper, "kubectl", side_effect=self.kubectl):
            manifest = helper.prepare(self.args, NOW)
        self.assertNotIn("ownerReferences", manifest["metadata"])
        self.assertEqual(manifest["spec"]["backoffLimit"], 0)
        self.assertEqual(manifest["spec"]["activeDeadlineSeconds"], 600)
        self.assertEqual(manifest["spec"]["template"]["metadata"]["labels"], {"component":"backup-job"})

    def test_pending_job_with_no_active_count_blocks(self):
        self.jobs = [{"metadata":{"name":"pending"}, "status":{},
                      "spec":{"template":{"metadata":{"labels":{"component":"backup-job"}}}}}]
        with mock.patch.object(helper, "kubectl", side_effect=self.kubectl), self.assertRaises(helper.CheckFailed):
            helper.prepare(self.args, NOW)

    def test_job_template_cannot_start_parallel_backup_pods(self):
        self.cron["spec"]["jobTemplate"]["spec"]["parallelism"] = 2
        with mock.patch.object(helper, "kubectl", side_effect=self.kubectl), self.assertRaises(helper.CheckFailed):
            helper.prepare(self.args, NOW)

    def test_schedule_start_margin_blocks_late_cronjob_window(self):
        with mock.patch.object(helper, "kubectl", side_effect=self.kubectl), self.assertRaises(helper.CheckFailed):
            helper.prepare(self.args, NOW.replace(minute=14))

    def test_failed_job_cannot_pass_because_it_printed_passed_evidence(self):
        terminal = {"status":{"conditions":[{"type":"Failed", "status":"True"}], "succeeded":0}}
        manifest = {"spec":{"template":{"spec":{"containers":[{"env":[{"name":"VERIFY_SCRIPT_SHA256", "value":"abc"}]}]}}}}
        def kubectl(args, *command):
            if command[0] == "create": return "created"
            if command[0] == "get": return json.dumps(terminal)
            if command[0] == "logs": return 'EVIDENCE {"status":"passed"}\n'
            raise AssertionError(command)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(helper, "prepare", return_value=manifest), \
                mock.patch.object(helper, "kubectl", side_effect=kubectl), \
                mock.patch.object(sys, "argv", ["verify", "--context", "test", "--namespace", "montecarlo",
                    "--cronjob", "otel-backup", "--expected-full", FULL, "--job-name", "verify-one",
                    "--output-dir", directory, "--run"]), redirect_stdout(io.StringIO()):
            self.assertEqual(helper.main(), 1)
            result = json.loads((Path(directory) / "verify-one.evidence.json").read_text())
            self.assertEqual(result["status"], "inconclusive")
            self.assertEqual(result["context"], "test")


if __name__ == "__main__":
    unittest.main()
