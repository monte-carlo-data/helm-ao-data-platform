"""Backup health uses scheduled jobs; external CloudWatch catches missing reports."""

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

from test_backup_chart import one, render


SCRIPT = Path(__file__).resolve().parents[2] / "charts/ao-data-platform/files/clickhouse-backup/check_backup.py"
SPEC = importlib.util.spec_from_file_location("check_backup", SCRIPT)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def iso(when):
    return when.isoformat().replace("+00:00", "Z")


def schedule(success=NOW - timedelta(minutes=10)):
    return {"metadata": {"name": "otel-backup", "uid": "schedule-1",
                         "creationTimestamp": iso(NOW - timedelta(days=1))},
            "spec": {"suspend": False},
            "status": {"lastSuccessfulTime": iso(success)} if success else {}}


def failed_job(when, owner="schedule-1"):
    return {"metadata": {"name": "otel-backup-" + str(int(when.timestamp()) // 60),
                         "ownerReferences": [{"kind": "CronJob", "name": "otel-backup",
                                               "uid": owner, "controller": True}]},
            "status": {"conditions": [{"type": "Failed", "status": "True",
                                         "lastTransitionTime": iso(when)}]}}


def successful_job(when=NOW - timedelta(minutes=10), owner="schedule-1"):
    job = failed_job(when, owner)
    job["status"] = {"completionTime": iso(when), "succeeded": 1,
                     "conditions": [{"type": "Complete", "status": "True"}]}
    return job


class MonitorTests(unittest.TestCase):
    def test_healthy_schedule(self):
        self.assertEqual(monitor.evaluate(schedule(), [successful_job()], NOW, 15300),
                         {"BackupJobFailed": 0, "BackupOverdue": 0, "MonitorHealthy": 1})

    def test_four_hours_plus_allowance(self):
        exact = schedule(NOW - timedelta(seconds=15300))
        jobs = [successful_job(NOW - timedelta(seconds=15300))]
        self.assertEqual(monitor.evaluate(exact, jobs, NOW, 15300)["BackupOverdue"], 0)
        self.assertEqual(monitor.evaluate(exact, jobs, NOW + timedelta(seconds=1), 15300)["BackupOverdue"], 1)

    def test_manual_success_does_not_hide_stopped_schedule(self):
        manual = successful_job(NOW)
        manual["metadata"]["annotations"] = {"cronjob.kubernetes.io/instantiate": "manual"}
        # kubectl --from=cronjob sets the same controller owner. Kubernetes then
        # advances this timestamp even though the scheduler did not create it.
        advanced_by_manual = schedule(NOW)
        stale = successful_job(NOW - timedelta(hours=5))
        self.assertEqual(monitor.evaluate(advanced_by_manual, [stale, manual], NOW, 15300)["BackupOverdue"], 1)

    def test_manual_success_does_not_clear_scheduled_failure(self):
        manual = successful_job(NOW)
        manual["metadata"]["annotations"] = {"cronjob.kubernetes.io/instantiate": "manual"}
        jobs = [successful_job(), failed_job(NOW - timedelta(minutes=5)), manual]
        self.assertEqual(monitor.evaluate(schedule(NOW), jobs, NOW, 15300)["BackupJobFailed"], 1)

    def test_manual_failure_with_same_owner_is_ignored(self):
        manual = failed_job(NOW)
        manual["metadata"]["annotations"] = {"cronjob.kubernetes.io/instantiate": "manual"}
        self.assertEqual(monitor.evaluate(schedule(), [successful_job(), manual], NOW, 15300)["BackupJobFailed"], 0)

    def test_failure_alert_clears_only_after_a_later_scheduled_success(self):
        failed = failed_job(NOW - timedelta(minutes=5))
        self.assertEqual(monitor.evaluate(schedule(), [successful_job(), failed], NOW, 15300)["BackupJobFailed"], 1)
        self.assertEqual(monitor.evaluate(schedule(NOW), [successful_job(NOW), failed], NOW, 15300)["BackupJobFailed"], 0)

    def test_unrelated_or_old_schedule_failure_is_ignored(self):
        self.assertEqual(monitor.evaluate(schedule(), [failed_job(NOW, "old-schedule")], NOW, 15300)["BackupJobFailed"], 0)

    def test_paused_schedule_reports_attention(self):
        paused = schedule()
        paused["spec"]["suspend"] = True
        self.assertEqual(monitor.evaluate(paused, [], NOW, 15300)["BackupOverdue"], 1)

    def test_no_success_uses_creation_time_and_cannot_stay_healthy_forever(self):
        empty = schedule(None)
        self.assertEqual(monitor.evaluate(empty, [], NOW, 15300)["BackupOverdue"], 1)
        empty["metadata"]["creationTimestamp"] = iso(NOW)
        self.assertEqual(monitor.evaluate(empty, [], NOW, 15300)["BackupOverdue"], 0)

    def test_bad_or_missing_status_reports_monitor_failure(self):
        for payload in ({}, schedule()):
            reader = mock.Mock()
            reader.snapshot.return_value = (payload, [successful_job(NOW + timedelta(seconds=1))])
            self.assertEqual(monitor.check(reader, "otel-backup", NOW, 15300), {"MonitorHealthy": 0})

    def test_unreachable_kubernetes_does_not_leak_response_or_token(self):
        reader = mock.Mock()
        reader.snapshot.side_effect = RuntimeError("private-token-body")
        with mock.patch("sys.stderr") as output:
            result = monitor.check(reader, "otel-backup", NOW, 15300)
        self.assertEqual(result, {"MonitorHealthy": 0})
        self.assertNotIn("private-token-body", str(output.mock_calls))

    def test_read_failure_never_publishes_backup_recovery(self):
        reader, client = mock.Mock(), mock.Mock()
        history = [successful_job(NOW - timedelta(hours=5)), failed_job(NOW - timedelta(minutes=10))]
        reader.snapshot.side_effect = [
            (schedule(), history),
            RuntimeError("Kubernetes unavailable"),
            ({}, []),
            (schedule(), history),
            (schedule(), history + [successful_job(NOW)]),
        ]
        for minute in range(5):
            metrics = monitor.check(reader, "otel-backup", NOW + timedelta(minutes=minute), 15300)
            monitor.publish(client, metrics, "test-cluster", "montecarlo", "otel-backup")
        sent = [{item["MetricName"]: item["Value"] for item in call.kwargs["MetricData"]}
                for call in client.put_metric_data.call_args_list]
        self.assertEqual(sent, [
            {"BackupJobFailed": 1, "BackupOverdue": 1, "MonitorHealthy": 1},
            {"MonitorHealthy": 0},
            {"MonitorHealthy": 0},
            {"BackupJobFailed": 1, "BackupOverdue": 1, "MonitorHealthy": 1},
            {"BackupJobFailed": 0, "BackupOverdue": 0, "MonitorHealthy": 1},
        ])

    def test_status_survives_process_restart(self):
        saved = schedule()
        jobs = [successful_job()]
        expected = {"BackupJobFailed": 0, "BackupOverdue": 0, "MonitorHealthy": 1}
        self.assertEqual(monitor.evaluate(saved, jobs, NOW, 15300), expected)
        self.assertEqual(monitor.evaluate(copy.deepcopy(saved), copy.deepcopy(jobs), NOW, 15300), expected)

    def test_missing_scheduled_history_cannot_trust_manual_success_time(self):
        self.assertEqual(monitor.evaluate(schedule(NOW), [], NOW, 15300)["BackupOverdue"], 1)

    def test_scheduled_annotation_and_old_controller_names_are_supported(self):
        job = successful_job()
        self.assertEqual(monitor.evaluate(schedule(), [job], NOW, 15300)["BackupOverdue"], 0)
        job["metadata"]["annotations"] = {"batch.kubernetes.io/cronjob-scheduled-timestamp": iso(NOW - timedelta(minutes=15))}
        self.assertEqual(monitor.evaluate(schedule(), [job], NOW, 15300)["BackupOverdue"], 0)
        job["metadata"]["annotations"]["cronjob.kubernetes.io/instantiate"] = "manual"
        self.assertEqual(monitor.evaluate(schedule(NOW), [job], NOW, 15300)["BackupOverdue"], 1)

    def test_unmarked_custom_job_and_incomplete_success_are_not_scheduled_success(self):
        for mutate in (lambda job: job["metadata"].update(name="copy-switch-test"),
                       lambda job: job["status"].update(conditions=[])):
            job = successful_job(NOW)
            mutate(job)
            self.assertEqual(monitor.evaluate(schedule(NOW), [job], NOW, 15300)["BackupOverdue"], 1)

    def test_paginated_jobs_and_incomplete_reads(self):
        reader = object.__new__(monitor.Kubernetes)
        reader.namespace = "montecarlo"
        reader.get = mock.Mock(side_effect=[schedule(),
                                           {"items": [1], "metadata": {"continue": "next"}},
                                           {"items": [2], "metadata": {}}])
        _, jobs = reader.snapshot("otel-backup")
        self.assertEqual(jobs, [1, 2])
        self.assertEqual(reader.get.call_args.args[1]["continue"], "next")
        reader.get = mock.Mock(side_effect=[schedule(),
                                           {"items": [], "metadata": {"continue": "loop"}},
                                           {"items": [], "metadata": {"continue": "loop"}}])
        with self.assertRaises(monitor.MonitorError):
            reader.snapshot("otel-backup")

    def test_metrics_have_only_the_fixed_namespace_and_dimensions(self):
        client = mock.Mock()
        metrics = monitor.evaluate(schedule(), [], NOW, 15300)
        monitor.publish(client, metrics, "dev-cluster", "montecarlo", "otel-backup")
        request = client.put_metric_data.call_args.kwargs
        self.assertEqual(request["Namespace"], "AO/ClickHouseBackup")
        self.assertEqual(len(request["MetricData"]), 3)
        for item in request["MetricData"]:
            self.assertEqual({d["Name"]: d["Value"] for d in item["Dimensions"]},
                             {"Cluster": "dev-cluster", "Namespace": "montecarlo", "CronJob": "otel-backup"})


class MonitorChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render("clickhouse.backup.monitoring.enabled=true",
                          "clickhouse.backup.monitoring.aws.region=us-west-1",
                          "clickhouse.backup.monitoring.aws.roleArn=arn:aws:iam::123456789012:role/monitor",
                          "clickhouse.backup.monitoring.aws.clusterName=dev-test")

    def test_watcher_is_independent_and_has_no_backup_credentials(self):
        cron = one(self.docs, "CronJob", "otel-backup-monitor")
        self.assertEqual(cron["spec"]["schedule"], "*/5 * * * *")
        pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "clickhouse-backup-monitor")
        self.assertFalse(any("secret" in volume for volume in pod["volumes"]))
        self.assertEqual(cron["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"], 240)
        self.assertNotEqual(cron["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"],
                            "clickhouse-backup-job")

    def test_watcher_can_read_only_its_cronjob_and_job_list(self):
        rules = one(self.docs, "Role", "otel-backup-monitor")["rules"]
        self.assertEqual(rules, [
            {"apiGroups": ["batch"], "resources": ["cronjobs"], "resourceNames": ["otel-backup"], "verbs": ["get"]},
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["list"]}])

    def test_missing_monitor_settings_are_rejected(self):
        with self.assertRaisesRegex(AssertionError, "monitoring.aws.region is required"):
            render("clickhouse.backup.monitoring.enabled=true")

    def test_backup_account_cannot_share_the_monitor_account(self):
        with self.assertRaisesRegex(AssertionError, "must differ from clickhouse-backup-monitor"):
            render("clickhouse.backup.serviceAccount.name=clickhouse-backup-monitor",
                   "clickhouse.backup.monitoring.enabled=true",
                   "clickhouse.backup.monitoring.aws.region=us-west-1",
                   "clickhouse.backup.monitoring.aws.roleArn=arn:aws:iam::123456789012:role/monitor",
                   "clickhouse.backup.monitoring.aws.clusterName=dev-test")
        accounts = [doc["metadata"]["name"] for doc in self.docs if doc["kind"] == "ServiceAccount"]
        self.assertEqual(len(accounts), len(set(accounts)))

    def test_monitor_account_name_is_available_when_monitoring_is_off(self):
        docs = render("clickhouse.backup.serviceAccount.name=clickhouse-backup-monitor")
        account = one(docs, "ServiceAccount", "clickhouse-backup-monitor")
        self.assertEqual(account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"],
                         "arn:aws:iam::123456789012:role/ao-backup-render-test")
        self.assertFalse(any(doc["kind"] == "CronJob" and doc["metadata"]["name"] == "otel-backup-monitor"
                             for doc in docs))

    def test_cleanup_is_explicit_dry_run_and_shares_backup_job(self):
        docs = render("clickhouse.backup.cleanup.enabled=true")
        self.assertEqual(len([doc for doc in docs if doc["kind"] == "CronJob"]), 1)
        job = one(docs, "CronJob", "otel-backup")["spec"]["jobTemplate"]["spec"]
        env = {item["name"]: item.get("value") for item in job["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["BACKUP_CLEANUP_DRY_RUN"], "true")
        self.assertEqual(env["BACKUP_KEEP_LAST"], "2")
        self.assertEqual(job["activeDeadlineSeconds"], 10800 + 1800 + 60)
        self.assertIn("cleanup_backups.py", one(docs, "ConfigMap", "otel-backup-job")["data"])

    def test_bad_cleanup_settings_are_rejected(self):
        for setting in ("clickhouse.replicasCount=1", "clickhouse.backup.cleanup.keepLast=0",
                        "clickhouse.backup.cleanup.keepDays=-1", "clickhouse.backup.cleanup.timeoutSeconds=1"):
            with self.assertRaises(AssertionError):
                render("clickhouse.backup.cleanup.enabled=true", setting)

    def test_deletion_is_rejected_at_chart_render(self):
        for value in ("false", "not-boolean", "1"):
            with self.subTest(value=value), self.assertRaisesRegex(
                    AssertionError, "deletion is unavailable.*dryRun must remain true"):
                render("clickhouse.backup.cleanup.enabled=true", "clickhouse.backup.cleanup.dryRun=" + value)


if __name__ == "__main__":
    unittest.main()
