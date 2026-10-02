"""Run the failover helper against fake kubectl/API responses, never a cluster."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "charts/ao-data-platform/files/clickhouse-backup"))
scheduler = load("run_backup", "charts/ao-data-platform/files/clickhouse-backup/run_backup.py")
sys.path.pop(0)
FULL = "ao-otel-full-20260929T000000Z-12345678"
NOW = datetime(2026, 9, 29, 0, 30, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def metadata(name=FULL, location="remote", required=""):
    return {"name":name, "location":location, "required":required,
            "desc":"directory, embedded" if location == "remote" else "embedded"}


class CopyAPI:
    def __init__(self, remote, first=False, unavailable=False):
        self.remote = remote
        self.local = {FULL:metadata(location="local")} if first else {}
        self.history = [{"command":"create_remote " + FULL, "status":"success", "operation_id":"full-id"}] if first else []
        self.posts = []
        self.calls = []
        self.unavailable = unavailable
        self.fail_post = False

    def request(self, method, path, query=None):
        self.calls.append((method, path, query))
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


class ProbeAPI:
    def __init__(self):
        self.delay = 0

    def request(self, method, path, query=None):
        return [{"database": "otel_traces", "table": "spans", "engine": "ReplicatedMergeTree",
                 "replica_table": "spans", "is_readonly": 0, "is_session_expired": 0,
                 "absolute_delay": self.delay}]


class PodTests(unittest.TestCase):
    def setUp(self):
        self.remote = {FULL:metadata()}
        self.copies = [CopyAPI(self.remote, first=True), CopyAPI(self.remote)]
        self.probes = [ProbeAPI(), ProbeAPI()]
        self.revision_failure = None

    def run_pod(self, expected=FULL, settings=None):
        fake_datetime = types.ModuleType("datetime")
        fake_datetime.__dict__.update(vars(importlib.import_module("datetime")))
        fake_datetime.datetime = FrozenDateTime
        endpoints = {"http://copy0:7171":self.copies[0], "http://copy1:7171":self.copies[1],
                     "http://copy0:8123":self.probes[0], "http://copy1:8123":self.probes[1]}
        environment = {"VERIFY_EXPECTED_FULL":expected, "VERIFY_TIMEOUT_SECONDS":"600",
                       "VERIFY_SCRIPT_SHA256":hashlib.sha256(b"installed scheduler").hexdigest(),
                       "BACKUP_ENDPOINTS":json.dumps(list(endpoints)[:2]), "BACKUP_PASSWORD_FILE":"/credentials/password",
                       "BACKUP_DATABASE_ENDPOINTS": json.dumps(list(endpoints)[2:]),
                       "BACKUP_PASSWORD_REVISION": "revision-1", "POD_NAMESPACE": "test",
                       "BACKUP_POD_NAMES": json.dumps(["chi-otel-otel-0-0-0", "chi-otel-otel-0-1-0"]),
                       "BACKUP_PROBE_PASSWORD_FILE": "/probe-credentials/password",
                       "BACKUP_MAX_REPLICA_DELAY_SECONDS": "5", "BACKUP_FRESHNESS_RETRY_SECONDS": "0"}
        environment.update(settings or {})
        passwords = {"/credentials/password": "backup-pw", "/probe-credentials/password": "probe-pw"}
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"run_backup":scheduler, "datetime":fake_datetime}), \
                mock.patch.dict(helper.os.environ, environment, clear=True), \
                mock.patch.object(Path, "read_text", autospec=True,
                                  side_effect=lambda path: passwords[str(path)]) as read, \
                mock.patch.object(Path, "read_bytes", return_value=b"installed scheduler"), \
                mock.patch.object(scheduler, "API", side_effect=lambda endpoint, *args, **kwargs:endpoints[endpoint]) as api, \
                mock.patch.object(scheduler, "PasswordRevisionGuard", return_value=mock.Mock(side_effect=self.revision_failure)), \
                redirect_stdout(output):
            try:
                exec(helper.POD_SCRIPT, {})
            except SystemExit as error:
                self.assertEqual(error.code, 1)
        if not self.revision_failure:
            self.assertEqual(read.call_args_list, [mock.call(Path(path)) for path in passwords])
            self.assertEqual([call.args for call in api.call_args_list], [
                ("http://copy0:7171", "backup", "backup-pw"),
                ("http://copy1:7171", "backup", "backup-pw"),
                ("http://copy0:8123", "backup_probe", "probe-pw"),
                ("http://copy1:8123", "backup_probe", "probe-pw")])
        for password in passwords.values():
            self.assertNotIn(password, output.getvalue())
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0].removeprefix("EVIDENCE "))

    def assert_inconclusive(self, result, reason, requests_sent=0):
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["reason"], reason)
        self.assertEqual(result["requests_sent"], requests_sent)
        self.assertEqual(result["instruction"], "Stop and inspect; do not rerun automatically.")

    def test_real_scheduler_downloads_then_creates_on_copy_one_only(self):
        result = self.run_pod()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["selected_copy"], 1)
        self.assertEqual((result["remote_before"], result["remote_after"]), (1, 2))
        self.assertEqual(result["required"], FULL)
        self.assertEqual(self.copies[0].posts, [])
        self.assertEqual([p[0] for p in self.copies[1].posts], ["/backup/download/" + FULL, "/backup/create_remote"])

    def test_helper_cannot_bypass_password_revision_guard(self):
        self.revision_failure = scheduler.RevisionError("Password revision changed.")
        self.assert_inconclusive(self.run_pod(), "Password revision changed.")
        self.assertEqual(self.copies[0].calls + self.copies[1].calls, [])

    def test_helper_cannot_back_up_a_copy_that_is_behind(self):
        self.probes[1].delay = 6
        self.assert_inconclusive(
            self.run_pod(),
            "No ClickHouse copy is caught up and able to list the trace tables and remote backups.")
        self.assertEqual(self.copies[0].posts + self.copies[1].posts, [])

    def test_helper_uses_the_installed_lag_allowance(self):
        self.probes[1].delay = 6
        result = self.run_pod(settings={"BACKUP_MAX_REPLICA_DELAY_SECONDS": "6"})
        self.assertEqual(result["status"], "passed")
        self.assertEqual(self.copies[0].posts, [])

    def test_wrong_full_fails_before_any_post(self):
        self.assert_inconclusive(
            self.run_pod("ao-otel-full-20260929T001500Z-12345678"),
            "The expected full backup is not today's latest healthy full backup.")
        self.assertEqual(self.copies[0].posts + self.copies[1].posts, [])

    def test_changed_base_at_scheduler_selection_stops_before_any_post(self):
        with mock.patch.object(scheduler, "full_base", side_effect=[FULL, None]):
            self.assert_inconclusive(self.run_pod(), "The scheduler selected a different full backup.")
        self.assertEqual(self.copies[0].posts + self.copies[1].posts, [])

    def test_active_operation_hidden_by_later_list_still_blocks(self):
        self.copies[0].history.extend([
            {"command":"create_remote another", "status":"in progress", "operation_id":"busy"},
            {"command":"list remote", "status":"success", "operation_id":""},
        ])
        self.assert_inconclusive(self.run_pod(), "A backup command is already running; stop and inspect it.")
        self.assertEqual(self.copies[0].posts + self.copies[1].posts, [])

    def test_existing_copy_one_metadata_does_not_fake_download_proof(self):
        self.copies[1].local[FULL] = metadata(location="local")
        self.assert_inconclusive(
            self.run_pod(), "Copy 1 already has this full backup; use a future full to test downloading it.")
        self.assertEqual(self.copies[1].posts, [])

    def test_uncertain_download_is_not_retried_and_never_creates_backup(self):
        self.copies[1].fail_post = True
        self.assert_inconclusive(
            self.run_pod(),
            "The download request was not confirmed. It may still run; no retry or switch was sent.",
            requests_sent=1)
        self.assertEqual(len(self.copies[1].posts), 1)
        self.assertEqual(self.copies[0].posts, [])

    def test_operation_starting_after_preflight_is_caught_before_post(self):
        original = scheduler.Scheduler.prepare_base
        def prepare_base(instance, api, name):
            self.copies[1].history.append({"command":"create_remote concurrent", "status":"in progress", "operation_id":"concurrent"})
            return original(instance, api, name)
        with mock.patch.object(scheduler.Scheduler, "prepare_base", prepare_base):
            result = self.run_pod()
        self.assert_inconclusive(result, "A backup command is already running; stop and inspect it.")
        self.assertEqual(self.copies[0].posts + self.copies[1].posts, [])

    def test_unexpected_exception_uses_generic_reason_without_error_details(self):
        with mock.patch.object(scheduler, "full_base", side_effect=ValueError("private error details")):
            result = self.run_pod()
        self.assert_inconclusive(result, "Verification failed; inspect the Job before continuing.")
        self.assertNotIn("private error details", json.dumps(result))
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
        self.calls = []
        self.terminal = {"status":{
            "conditions":[{"type":"Complete", "status":"True"}], "succeeded":1,
            "startTime":"2026-09-29T00:30:00Z", "completionTime":"2026-09-29T00:31:00Z"}}
        self.evidence = {"status":"passed", "selected_copy":1,
                         "backup":"ao-otel-incremental-20260929T003000Z-87654321",
                         "required":FULL, "remote_before":1, "remote_after":2,
                         "downloaded_full_metadata":True, "copy0_mutations":0}
        self.logs = "EVIDENCE " + json.dumps(self.evidence) + "\n"

    def kubectl(self, args, *command):
        self.calls.append(command)
        if command[:2] == ("get", "cronjob"):
            return json.dumps(self.cron)
        if command[:2] == ("get", "jobs"):
            return json.dumps({"items":self.jobs})
        if command[:2] == ("get", "configmap"):
            return json.dumps({"data":{"run_backup.py":"installed scheduler"}})
        if command[:2] == ("create", "-f"):
            manifest = json.loads(Path(command[2]).read_text())
            self.assertEqual(manifest["kind"], "Job")
            self.assertEqual(manifest["metadata"]["name"], self.args.job_name)
            return "created"
        if command[:2] == ("get", "job"):
            return json.dumps(self.terminal)
        if command[0] == "logs":
            return self.logs
        raise AssertionError(command)

    def run_cli(self, directory, *, run=True, overrides=None):
        values = {"context":"test", "namespace":"montecarlo", "cronjob":"otel-backup",
                  "expected-full":FULL, "job-name":"verify-one", "output-dir":str(directory)}
        values.update(overrides or {})
        argv = ["verify"]
        for key, value in values.items():
            argv.extend(["--" + key, str(value)])
        if run:
            argv.append("--run")
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(helper, "kubectl", side_effect=self.kubectl), \
                mock.patch.object(helper, "datetime", FrozenDateTime), \
                mock.patch.object(sys, "argv", argv), \
                redirect_stdout(output), redirect_stderr(errors):
            result = helper.main()
        return result, output.getvalue(), errors.getvalue()

    def assert_no_create(self):
        self.assertFalse(any(command[0] == "create" for command in self.calls), self.calls)

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
        with mock.patch.object(helper, "kubectl", side_effect=self.kubectl), self.assertRaisesRegex(
                helper.CheckFailed, "^A related backup Job is still pending or running\\. Stop and inspect it first\\.$"):
            helper.prepare(self.args, NOW)

    def test_job_template_cannot_start_parallel_backup_pods(self):
        self.cron["spec"]["jobTemplate"]["spec"]["parallelism"] = 2
        with mock.patch.object(helper, "kubectl", side_effect=self.kubectl), self.assertRaisesRegex(
                helper.CheckFailed, "^The test requires exactly one Pod and one completion\\.$"):
            helper.prepare(self.args, NOW)

    def test_schedule_start_margin_blocks_late_cronjob_window(self):
        with mock.patch.object(helper, "kubectl", side_effect=self.kubectl), self.assertRaisesRegex(
                helper.CheckFailed,
                "^Choose a time at least 15 minutes after the last run and 15 minutes before the next, including the test timeout\\.$"):
            helper.prepare(self.args, NOW.replace(minute=14))

    def test_run_success_returns_zero_and_writes_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            result, output, errors = self.run_cli(directory)
            self.assertEqual((result, errors), (0, ""))
            evidence_path = Path(directory) / "verify-one.evidence.json"
            evidence = json.loads(evidence_path.read_text())
            expected = dict(self.evidence, job="verify-one", context="test", namespace="montecarlo",
                            started="2026-09-29T00:30:00Z", completed="2026-09-29T00:31:00Z",
                            scheduler_sha256=hashlib.sha256(b"installed scheduler").hexdigest())
            self.assertEqual(evidence, expected)
            self.assertEqual(json.loads(output.splitlines()[-1]), expected)
            self.assertEqual([c for c in self.calls if c[0] == "create"],
                             [("create", "-f", str(Path(directory) / "verify-one.run.json"))])

    def test_changed_prepared_file_refuses_run_without_creating_job(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_cli(directory, run=False)[0], 0)
            self.assert_no_create()
            prepared = Path(directory) / "verify-one.prepared.json"
            manifest = json.loads(prepared.read_text())
            manifest["spec"]["template"]["spec"]["containers"][0]["image"] = "changed-after-review"
            prepared.write_text(json.dumps(manifest))
            self.calls.clear()
            result, _, errors = self.run_cli(directory)
            self.assertEqual(result, 1)
            self.assertEqual(errors.strip(),
                             "The live Job template or scheduler changed after preparation. Stop and review it.")
            self.assert_no_create()
            self.assertFalse((Path(directory) / "verify-one.run.json").exists())

    def test_missing_or_duplicate_evidence_refuses_success(self):
        for count in (0, 2):
            with self.subTest(evidence_lines=count), tempfile.TemporaryDirectory() as directory:
                self.calls.clear()
                self.logs = "ordinary log output\n" + ("EVIDENCE " + json.dumps(self.evidence) + "\n") * count
                result, _, errors = self.run_cli(directory)
                self.assertEqual(result, 1)
                self.assertEqual(errors.strip(), "No single final evidence record was found. Stop and inspect the Job.")
                self.assertFalse((Path(directory) / "verify-one.evidence.json").exists())
                self.assertEqual(sum(c[0] == "create" for c in self.calls), 1)

    def test_existing_terminal_job_name_refuses_run(self):
        self.jobs = [{"metadata":{"name":"verify-one"},
                      "status":{"conditions":[{"type":"Complete", "status":"True"}]},
                      "spec":{"template":{"metadata":{"labels":{"component":"unrelated"}}}}}]
        with tempfile.TemporaryDirectory() as directory:
            result, _, errors = self.run_cli(directory)
            self.assertEqual(result, 1)
            self.assertEqual(errors.strip(),
                             "That Job name already exists. Inspect it; do not create a replacement automatically.")
            self.assert_no_create()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_arguments_fail_before_any_kubectl_call(self):
        cases = [
            ({"job-name":"Invalid-Name"}, "Use a Kubernetes Job name of at most 63 lowercase letters, digits, and hyphens."),
            ({"job-name":"a" * 64}, "Use a Kubernetes Job name of at most 63 lowercase letters, digits, and hyphens."),
            ({"expected-full":"latest"}, "Expected-full must be an exact full-backup name from this scheduler."),
            ({"timeout-seconds":59}, "Timeout must be between 60 and 3600 seconds."),
            ({"timeout-seconds":3601}, "Timeout must be between 60 and 3600 seconds."),
        ]
        for overrides, reason in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                self.calls.clear()
                result, _, errors = self.run_cli(directory, overrides=overrides)
                self.assertEqual(result, 1)
                self.assertEqual(errors.strip(), reason)
                self.assertEqual(self.calls, [])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_failed_job_cannot_pass_because_it_printed_passed_evidence(self):
        self.terminal = {"status":{"conditions":[{"type":"Failed", "status":"True"}], "succeeded":0}}
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_cli(directory)[0], 1)
            result = json.loads((Path(directory) / "verify-one.evidence.json").read_text())
            self.assertEqual(result["status"], "inconclusive")
            self.assertEqual(result["reason"],
                             "The Job did not finish successfully. Stop and inspect; do not rerun automatically.")
            self.assertEqual(result["context"], "test")


if __name__ == "__main__":
    unittest.main()
