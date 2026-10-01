"""Refuse unsafe migration completion; exercise the real read-only CLI as well."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "check-backup-upgrade.py"
spec = importlib.util.spec_from_file_location("check_backup_upgrade", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def metadata(name, uid=None, **extra):
    return dict(name=name, uid=uid or name + "-uid", resourceVersion="10", **extra)


def fixture():
    chi = {"kind": "ClickHouseInstallation", "metadata": metadata("otel", generation=7),
           "status": {"status": "Completed", "taskID": "task7", "taskIDsCompleted": ["task7"]}}
    storage = {"kind": "ConfigMap", "metadata": metadata("chi-storage-otel"),
               "data": {"status-normalizedCompleted": json.dumps({"metadata": {"generation": 7}})}}
    cron = {"kind": "CronJob", "metadata": metadata("otel-backup"),
            "spec": {"suspend": True, "jobTemplate": {"spec": {"template": {"metadata": {"labels": {"role": "backup"}}}}}}}
    pods, sets = [], []
    for i in range(2):
        uid, revision = f"set{i}-uid", f"set{i}-revision2"
        sets.append({"kind": "StatefulSet", "metadata": metadata(f"set{i}", uid, generation=2),
                     "spec": {"replicas": 1}, "status": {"observedGeneration": 2, "readyReplicas": 1,
                     "currentReplicas": 1, "updatedReplicas": 1, "currentRevision": revision, "updateRevision": revision}})
        mounts, volumes = [], []
        for kind in ("user", "probe"):
            mounts.append({"name": kind, "mountPath": "/etc/clickhouse-backup-auth/" + kind, "readOnly": True})
            volumes.append({"name": kind, "projected": {"sources": [{"configMap": {"name": "otel-backup-auth"}},
                                                                      {"secret": {"name": kind, "optional": True, "items": [
                                                                          {"key": "auth.xml", "path": "users.d/auth.xml"}]}}]}})
        mounts.append({"name": "stores", "mountPath": "/etc/clickhouse-server/config.d/", "readOnly": True})
        volumes.append({"name": "stores", "projected": {"sources": [
            {"configMap": {"name": "chi-otel-common-configd"}},
            {"configMap": {"name": "otel-backup-auth", "items": [
                {"key": "backup-users.xml", "path": "backup-users.xml"}]}}]}})
        pods.append({"kind": "Pod", "metadata": metadata(f"pod{i}", labels={"controller-revision-hash": revision},
                     ownerReferences=[{"kind": "StatefulSet", "uid": uid, "controller": True}]),
                     "spec": {"containers": [{"name": "clickhouse", "volumeMounts": mounts}, {"name": "clickhouse-backup"}], "volumes": volumes},
                     "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                                "containerStatuses": [{"name": name, "ready": True, "state": {"running": {}}}
                                                      for name in ("clickhouse", "clickhouse-backup")]}})
    return [chi, storage, cron, pods, sets, []]


class GateTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()

    def refuses(self, text):
        with self.assertRaisesRegex(checker.CheckFailed, text):
            checker.check_snapshot(*self.data)

    def test_completed_migration_with_two_current_copies_passes(self):
        self.assertEqual(checker.check_snapshot(*self.data), ["pod0", "pod1"])

    def test_paused_schedule_is_required(self):
        for value in (False, None):
            with self.subTest(value=value):
                self.data[2]["spec"]["suspend"] = value
                self.refuses("paused")

    def test_pending_job_with_no_active_status_still_blocks(self):
        self.data[5] = [{"metadata": metadata("pending", ownerReferences=[{"uid": "otel-backup-uid"}]),
                         "spec": {"template": {}}, "status": {}}]
        self.refuses("pending or running")
        self.data[5][0]["status"]["conditions"] = [{"type": "Complete", "status": "True"}]
        checker.check_snapshot(*self.data)

    def test_manually_created_related_job_blocks(self):
        for labels in ({"backup-verification": "otel-backup"}, {}):
            with self.subTest(labels=labels):
                self.data[5] = [{"metadata": metadata("manual", labels=labels), "spec": {"template": {
                    "metadata": {"labels": {"role": "backup"}} if not labels else {}}}}]
                self.refuses("pending or running")

    def test_stale_completed_operator_status_blocks(self):
        self.data[1]["data"]["status-normalizedCompleted"] = json.dumps({"metadata": {"generation": 6}})
        self.refuses("older generation")

    def test_uncompleted_latest_task_blocks(self):
        self.data[0]["status"]["taskID"] = "task8"
        self.refuses("latest.*task")

    def test_aborted_rollout_blocks(self):
        for field, value in (("status", "InProgress"), ("hostsFailed", 1)):
            with self.subTest(field=field):
                self.data = fixture()
                self.data[0]["status"][field] = value
                self.refuses("not completed")

    def test_one_unready_sidecar_blocks_even_if_pod_ready_is_stale(self):
        self.data[3][1]["status"]["containerStatuses"][1]["ready"] = False
        self.refuses("unready container")

    def test_extra_or_missing_pod_blocks(self):
        for count in (1, 3):
            with self.subTest(count=count):
                self.data = fixture()
                self.data[3] = (self.data[3] * 2)[:count]
                self.refuses("exactly two")

    def test_old_pod_revision_blocks(self):
        self.data[3][1]["metadata"]["labels"]["controller-revision-hash"] = "old"
        self.refuses("current StatefulSet revision")

    def test_statefulset_rollout_or_observation_pending_blocks(self):
        for field, value in (("currentRevision", "old"), ("observedGeneration", 1), ("updatedReplicas", 0)):
            with self.subTest(field=field):
                self.data = fixture()
                self.data[4][1]["status"][field] = value
                self.refuses("current StatefulSet revision")

    def test_deleting_pod_blocks(self):
        self.data[3][0]["metadata"]["deletionTimestamp"] = "2026-09-30T00:00:00Z"
        self.refuses("being replaced")

    def test_no_new_mount_blocks_even_with_new_revision_label(self):
        self.data[3][0]["spec"]["containers"][0]["volumeMounts"].pop()
        self.refuses("new Pod template")

    def test_optional_auth_projection_is_required(self):
        self.data[3][0]["spec"]["volumes"][0]["projected"]["sources"][1]["secret"]["optional"] = False
        self.refuses("optional backup Secret")

    def test_previous_nested_config_file_mount_blocks(self):
        mount = self.data[3][0]["spec"]["containers"][0]["volumeMounts"][-1]
        mount.update(mountPath="/etc/clickhouse-server/config.d/backup-users.xml", subPath="backup-users.xml")
        self.refuses("new Pod template")

    def test_previous_auth_projection_path_blocks(self):
        projection = self.data[3][0]["spec"]["volumes"][0]["projected"]["sources"][1]["secret"]
        projection["items"][0]["path"] = "auth.xml"
        self.refuses("optional backup Secret")


class FileChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ("server/users.d", "server/config.d", "auth/user/users.d", "auth/probe/users.d"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "server/users.d/auth-methods.xml").write_text("<clickhouse><users><otel/></users></clickhouse>")
        (self.root / "server/config.d/backup-users.xml").write_text(
            "/etc/clickhouse-backup-auth/user/users.xml /etc/clickhouse-backup-auth/probe/users.xml")
        for kind in ("user", "probe"):
            (self.root / "auth" / kind / "users.xml").write_text("<clickhouse/>")
            (self.root / "auth" / kind / "users.d" / "auth.xml").write_text("<clickhouse/>")

    def run_files(self):
        # Redirect only filesystem paths. Keep literal grep patterns intact so
        # the same boolean script validates real store-file contents.
        script = checker.FILE_CHECK.replace("main=/etc/clickhouse-server", "main=" + str(self.root / "server"))
        script = script.replace("stores=/etc/clickhouse-server", "stores=" + str(self.root / "server"))
        script = script.replace('"/etc/clickhouse-backup-auth/$kind/', '"' + str(self.root / "auth") + '/$kind/')
        result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.stdout, "")
        return result.returncode

    def test_real_boolean_script_accepts_user_store_files(self):
        self.assertEqual(self.run_files(), 0)

    def test_legacy_reference_in_actual_file_refuses(self):
        (self.root / "server/users.d/auth-methods.xml").write_text('<auth_methods incl="backup_auth_methods"/>')
        self.assertNotEqual(self.run_files(), 0)

    def test_empty_main_user_file_refuses(self):
        (self.root / "server/users.d/auth-methods.xml").write_text("")
        self.assertNotEqual(self.run_files(), 0)

    def test_only_placeholder_in_actual_store_file_refuses(self):
        (self.root / "server/config.d/backup-users.xml").write_text("<clickhouse/>")
        self.assertNotEqual(self.run_files(), 0)

    def test_missing_projected_auth_file_refuses(self):
        (self.root / "auth/probe/users.d/auth.xml").unlink()
        self.assertNotEqual(self.run_files(), 0)


FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(args)+'\n')
assert args[:4]==['--context','test-context','--namespace','montecarlo']
command=args[4:]
if command[0]=='exec':
    assert command[2:6]==['-c','clickhouse','--','/bin/sh']
    assert 'cat ' not in command[-1]
    assert 'grep -q' in command[-1]
    if os.environ.get('FAIL_FILES'):
        print('SECRET_SHOULD_NEVER_APPEAR',file=sys.stderr);sys.exit(1)
    sys.exit(0)
assert command[0]=='get', command
assert command[-2:]==['-o','json']
a=json.loads(Path(os.environ['FIXTURE']).read_text())
kind=command[1]
response={'chi':a[0], 'configmap':a[1], 'cronjob':a[2], 'pods':{'items':a[3]}, 'statefulsets':{'items':a[4]}, 'jobs':{'items':a[5]}}[kind]
if os.environ.get('CHANGE_DURING_CHECK') and kind=='chi':
    count=sum(json.loads(line)[4:6]==['get','chi'] for line in Path(os.environ['CALLS']).read_text().splitlines())
    if count>1: response['metadata']['resourceVersion']='11'
print(json.dumps(response))
'''


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "kubectl").write_text(FAKE_KUBECTL)
        (self.root / "kubectl").chmod(0o755)
        self.data = fixture()
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"],
                        CALLS=str(self.root / "calls"), FIXTURE=str(self.root / "fixture.json"))

    def run_cli(self, extra=()):
        Path(self.env["FIXTURE"]).write_text(json.dumps(self.data))
        return subprocess.run([sys.executable, str(SCRIPT), "--context", "test-context", "--namespace", "montecarlo",
                               "--chi", "otel", "--cronjob", "otel-backup", *extra],
                              capture_output=True, text=True, env=self.env)

    def test_success_uses_only_get_and_two_boolean_execs(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "passed")
        calls = [json.loads(line) for line in Path(self.env["CALLS"]).read_text().splitlines()]
        self.assertEqual(sum(call[4] == "exec" for call in calls), 2)
        self.assertEqual({call[4] for call in calls}, {"get", "exec"})
        self.assertNotIn("secret", [call[5] for call in calls if call[4] == "get"])

    def test_actual_file_failure_refuses_without_exposing_output(self):
        self.env["FAIL_FILES"] = "1"
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("file check failed", result.stdout)
        self.assertNotIn("SECRET_SHOULD_NEVER_APPEAR", result.stdout + result.stderr)

    def test_changing_rollout_refuses(self):
        self.env["CHANGE_DURING_CHECK"] = "1"
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("changed during", result.stdout)

    def test_unpaused_schedule_never_executes(self):
        self.data[2]["spec"]["suspend"] = False
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('"exec"', Path(self.env["CALLS"]).read_text())

    def test_invalid_arguments_do_not_call_kubectl(self):
        result = self.run_cli(("--chi", "bad;name"))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(Path(self.env["CALLS"]).exists())

    def test_unexpected_data_fails_closed(self):
        self.data[3][0].pop("spec")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unexpected Kubernetes data", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
