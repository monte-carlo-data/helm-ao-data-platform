"""Keep scheduled uploads safe while an earlier S3 deletion is still running."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from test_backup_scheduler import backup, Clock, FakeAPI, FakeProbe, FULL, NOW, remote

DIRECTORY = Path(__file__).resolve().parents[2] / "charts/ao-data-platform/files/clickhouse-backup"
sys.path.insert(0, str(DIRECTORY))
from cleanup_store import CleanupRunLease, CleanupStateConflict
from pending_cleanup import PendingRemoteDelete, fingerprint
sys.path.pop(0)


IDENTITY = {"pod": "copy-0", "uid": "copy-uid", "container_id": "container://backup-1",
            "restart_count": 0, "password_revision": "revision-1"}


class MemoryStore:
    def __init__(self, state):
        self.state, self.version = copy.deepcopy(state), 1

    def load(self):
        return copy.deepcopy(self.state), str(self.version)

    def compare_and_swap(self, version, state):
        if version != str(self.version):
            raise CleanupStateConflict("Cleanup state changed.")
        self.state = copy.deepcopy(state)
        self.version += 1
        return str(self.version)


def tracked_delete(apis, index=0):
    record = {"schema": 1, "copy": index, "name": FULL, "identity": IDENTITY,
              "action_start": "2026-09-29T01:00:00Z", "remote": {FULL: fingerprint(remote())},
              "created": "2026-09-29T01:00:00+00:00",
              "local": [{} for _ in apis], "excluded": [FULL]}
    store = MemoryStore({"pending_delete": record})
    lease = CleanupRunLease(store, "job-1", now=lambda: 1)
    lease.acquire()
    return PendingRemoteDelete(lease, apis, lambda _: dict(IDENTITY))


def action(**changes):
    return {"command": "delete remote " + FULL, "status": "in progress",
            "start": "2026-09-29T01:00:00Z", **changes}


class PendingSchedulerTests(unittest.TestCase):
    def run_scheduler(self, apis, pending):
        clock = Clock()
        scheduler = backup.Scheduler(apis, [FakeProbe() for _ in apis], timeout=30,
                                     clock=clock.now, sleep=clock.sleep, pending_cleanup=pending)
        with redirect_stdout(io.StringIO()):
            return scheduler.run(NOW)

    def test_only_registered_delete_on_either_copy_can_overlap_backup(self):
        for index in (0, 1):
            with self.subTest(copy=index):
                apis = [FakeAPI([remote()]), FakeAPI([remote()])]
                apis[index].history.append(action())
                pending = tracked_delete(apis, index)
                self.run_scheduler(apis, pending)
                posts = [request for api in apis for request in api.posts]
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0][1], "/backup/create_remote")
                self.assertNotIn("diff-from-remote", posts[0][2])
                self.assertIsNotNone(pending.refresh())

    def test_registered_delete_does_not_allow_another_active_command_on_either_copy(self):
        for index in (0, 1):
            with self.subTest(copy=index):
                apis = [FakeAPI(), FakeAPI()]
                apis[0].history.append(action())
                apis[index].history.append(action(command="create_remote another-backup"))
                with self.assertRaisesRegex(backup.BackupError, "already running"):
                    self.run_scheduler(apis, tracked_delete(apis))
                self.assertEqual(apis[0].posts + apis[1].posts, [])

    def test_same_delete_on_unregistered_copy_is_blocked(self):
        apis = [FakeAPI(), FakeAPI(history=[action()])]
        with self.assertRaisesRegex(backup.BackupError, "already running"):
            self.run_scheduler(apis, tracked_delete(apis, 0))
        self.assertEqual(apis[0].posts + apis[1].posts, [])

    def test_missing_changed_or_duplicate_action_identity_blocks_backup(self):
        histories = ([action(start="")], [action(start="later")], [action(), action()])
        for index in (0, 1):
            for history in histories:
                with self.subTest(copy=index, history=history):
                    apis = [FakeAPI(), FakeAPI()]
                    apis[index].history = history
                    with self.assertRaisesRegex(backup.BackupError, "already running"):
                        self.run_scheduler(apis, tracked_delete(apis, index))
                    self.assertEqual(apis[0].posts + apis[1].posts, [])

    def test_changed_server_or_failed_pending_check_blocks_active_delete_on_both_copies(self):
        for index in (0, 1):
            for unavailable in (False, True):
                with self.subTest(copy=index, unavailable=unavailable):
                    apis = [FakeAPI(), FakeAPI()]
                    apis[index].history = [action()]
                    pending = tracked_delete(apis, index)
                    pending.identity = mock.Mock(return_value=dict(IDENTITY, restart_count=1))
                    if unavailable:
                        pending.identity.side_effect = backup.BackupError("Identity check unavailable.")
                    with self.assertRaises(backup.BackupError):
                        self.run_scheduler(apis, pending)
                    self.assertEqual(apis[0].posts + apis[1].posts, [])

    def test_pending_set_is_excluded_from_every_copy_before_choosing_full_base(self):
        apis = [FakeAPI([remote()]), FakeAPI([remote()])]
        pending = tracked_delete(apis)
        self.run_scheduler(apis, pending)
        self.assertEqual(len(apis[0].posts), 1)
        self.assertEqual(apis[1].posts, [])
        self.assertNotIn("diff-from-remote", apis[0].posts[0][2])
        self.assertFalse(any(path.startswith("/backup/download/")
                             for api in apis for _, path, _ in api.calls))


class PendingMainTests(unittest.TestCase):
    def run_main(self, pending, backup_failure=None, settings=None, release_failure=None, acquire_failure=None):
        lease, tracker_module, local, remote_module = [mock.Mock() for _ in range(4)]
        lease.assert_owned = mock.Mock()
        lease.acquire.side_effect = acquire_failure
        tracker_module.PendingRemoteDelete.return_value = pending
        lease.release.side_effect = release_failure
        apis = [FakeAPI(), FakeAPI()]
        output, errors = io.StringIO(), io.StringIO()
        environment = {"BACKUP_CLEANUP_ENABLED": "true", "BACKUP_CLEANUP_DRY_RUN": "false",
                       "BACKUP_LOCAL_CLEANUP_ENABLED": "true", "BACKUP_LOCAL_CLEANUP_DRY_RUN": "false"}
        environment.update(settings or {})
        with mock.patch.dict(backup.os.environ, environment, clear=True), \
                mock.patch.dict(sys.modules, {"pending_cleanup": tracker_module,
                                              "cleanup_local_backups": local,
                                              "cleanup_backups": remote_module}), \
                mock.patch.object(backup, "configured_clients", return_value=(apis, [FakeProbe(), FakeProbe()])), \
                mock.patch.object(backup, "configured_cleanup_lease", return_value=lease), \
                mock.patch.object(backup, "Scheduler") as scheduler, \
                redirect_stdout(output), redirect_stderr(errors):
            scheduler.return_value.run.return_value = FULL
            scheduler.return_value.run.side_effect = backup_failure
            result = backup.main()
        return result, lease, apis, scheduler, local, remote_module, output.getvalue(), errors.getvalue()

    def test_pending_delete_skips_both_cleaners_after_verified_backup(self):
        pending = mock.Mock()
        pending.observe.return_value = "in progress"
        pending.refresh.return_value = {"name": FULL}
        result, lease, apis, scheduler, local, remote, output, errors = self.run_main(pending)
        self.assertEqual(result, 0)
        lease.acquire.assert_called_once_with()
        lease.release.assert_called_once_with()
        self.assertTrue(all(api.mutation_guard == lease.assert_owned for api in apis))
        self.assertTrue(all(api.mutation_guard_timeout_seconds == 10 for api in apis))
        self.assertIs(scheduler.call_args.kwargs["pending_cleanup"], pending)
        local.LocalCleaner.assert_not_called()
        remote.Cleaner.assert_not_called()
        pending.confirm.assert_not_called()
        report = json.loads(output.splitlines()[-1].split(": ", 1)[1])
        self.assertEqual(report, {"event": "cleanup_skipped", "reason": "pending_remote_delete",
                                  "local": True, "remote": True})
        self.assertEqual(errors, "")

    def test_unreadable_pending_observation_or_refresh_preserves_success_and_skips_cleanup(self):
        pending = mock.Mock()
        pending.observe.side_effect = ValueError("private server response")
        pending.refresh.side_effect = ValueError("private saved state")
        result, lease, _, scheduler, local, remote, output, errors = self.run_main(pending)
        self.assertEqual(result, 0)
        scheduler.return_value.run.assert_called_once_with()
        lease.release.assert_called_once_with()
        local.LocalCleaner.assert_not_called()
        remote.Cleaner.assert_not_called()
        self.assertIn('"status": "unavailable"', output)
        self.assertNotIn("private", output + errors)

    def test_disabling_cleanup_preserves_unfinished_record_and_does_not_retry_delete(self):
        pending = mock.Mock()
        pending.observe.return_value = "in progress"
        pending.refresh.return_value = {"name": FULL}
        result, _, apis, _, local, remote, _, _ = self.run_main(
            pending, settings={"BACKUP_CLEANUP_ENABLED": "false"})
        self.assertEqual(result, 0)
        pending.confirm.assert_not_called()
        self.assertEqual(apis[0].posts + apis[1].posts, [])
        local.LocalCleaner.assert_not_called()
        remote.Cleaner.assert_not_called()

    def test_successful_pending_action_requires_confirmation_before_cleanup(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                pending = mock.Mock()
                pending.observe.return_value = "success"
                pending.confirm.return_value = confirmed
                pending.refresh.return_value = None if confirmed else {"name": FULL}
                result, _, _, _, local, remote, _, _ = self.run_main(
                    pending, settings={"BACKUP_LOCAL_CLEANUP_ENABLED": "false"})
                self.assertEqual(result, 0)
                pending.confirm.assert_called_once_with()
                local.LocalCleaner.assert_not_called()
                if confirmed:
                    self.assertIs(remote.Cleaner.call_args.kwargs["tracker"], pending)
                else:
                    remote.Cleaner.assert_not_called()

    def test_failed_backup_releases_lease_and_never_starts_cleanup(self):
        pending = mock.Mock()
        pending.observe.return_value = "none"
        result, lease, _, _, local, remote, _, _ = self.run_main(
            pending, backup_failure=backup.BackupError("Upload failed."))
        self.assertEqual(result, 1)
        lease.release.assert_called_once_with()
        local.LocalCleaner.assert_not_called()
        remote.Cleaner.assert_not_called()

    def test_existing_run_owner_blocks_scheduler_before_backup(self):
        pending = mock.Mock()
        result, lease, apis, scheduler, local, remote, _, _ = self.run_main(
            pending, acquire_failure=CleanupStateConflict("Another cleanup run owns the schedule."))
        self.assertEqual(result, 1)
        scheduler.assert_not_called()
        pending.observe.assert_not_called()
        lease.release.assert_called_once_with()
        self.assertEqual(apis[0].posts + apis[1].posts, [])
        local.LocalCleaner.assert_not_called()
        remote.Cleaner.assert_not_called()

    def test_release_failure_does_not_hide_verified_backup_or_print_response(self):
        pending = mock.Mock()
        pending.observe.return_value = "none"
        pending.refresh.return_value = None
        result, _, _, _, _, _, output, errors = self.run_main(
            pending, settings={"BACKUP_LOCAL_CLEANUP_ENABLED": "false", "BACKUP_CLEANUP_ENABLED": "false"},
            release_failure=OSError("private response"))
        self.assertEqual(result, 0)
        self.assertIn("could not be released", errors)
        self.assertNotIn("private", output + errors)


class OwnershipAndIdentityTests(unittest.TestCase):
    def test_configured_lease_uses_actual_job_deadline_and_current_pod_identity(self):
        settings = {"BACKUP_CLEANUP_STATE_NAME": "backup-state", "POD_NAMESPACE": "test-space",
                    "POD_NAME": "backup-job", "POD_UID": "job-uid", "BACKUP_JOB_TIMEOUT_SECONDS": "7200"}
        with mock.patch.dict(backup.os.environ, settings, clear=True), \
                mock.patch("cleanup_store.KubernetesCleanupStore") as store:
            lease = backup.configured_cleanup_lease()
        store.assert_called_once_with("test-space", "backup-state")
        self.assertEqual(lease.duration, 7200)
        self.assertEqual(json.loads(lease.owner), {"pod": "backup-job", "uid": "job-uid"})
        store.return_value.get_pod.return_value = None
        self.assertTrue(lease.owner_finished(lease.owner))
        store.return_value.get_pod.assert_called_once_with("backup-job")

    def test_mutation_guard_runs_after_password_guard_and_stops_before_post(self):
        events = []
        password_guard = lambda: events.append("password")

        def expired():
            events.append("ownership")
            raise CleanupStateConflict("Cleanup run ownership expired.")

        api = backup.API("http://copy-0:7171", "backup", "private-password",
                         guard=password_guard, mutation_guard=expired)
        api.opener = mock.Mock()
        with self.assertRaisesRegex(backup.BackupError, "ownership expired"):
            api.request("POST", "/backup/create_remote", {"name": FULL})
        self.assertEqual(events, ["password", "ownership"])
        api.opener.open.assert_not_called()

    def test_password_guard_failure_prevents_ownership_check_and_post(self):
        mutation_guard = mock.Mock()
        api = backup.API("http://copy-0:7171", "backup", "private-password",
                         guard=mock.Mock(side_effect=backup.RevisionError("Password revision changed.")),
                         mutation_guard=mutation_guard)
        api.opener = mock.Mock()
        with self.assertRaises(backup.RevisionError):
            api.request("POST", "/backup/create_remote", {"name": FULL})
        mutation_guard.assert_not_called()
        api.opener.open.assert_not_called()

    def test_reads_do_not_require_mutation_ownership(self):
        mutation_guard = mock.Mock(side_effect=CleanupStateConflict("Expired."))
        api = backup.API("http://copy-0:7171", "backup", "private-password", mutation_guard=mutation_guard)
        api.opener = mock.Mock()
        api.opener.open.return_value.__enter__ = mock.Mock(return_value=mock.Mock(read=lambda _: b"{}"))
        api.opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        self.assertEqual(api.request("GET", "/backup/actions"), [{}])
        mutation_guard.assert_not_called()

    @staticmethod
    def pod():
        return {"metadata": {"name": "copy-0", "uid": "copy-uid",
                             "annotations": {backup.REVISION_ANNOTATION: "revision-1"}},
                "status": {"phase": "Running", "containerStatuses": [
                    {"name": "clickhouse-backup", "ready": True, "state": {"running": {}},
                     "containerID": "container://backup-1", "restartCount": 0}]}}

    def test_server_identity_is_read_afresh_and_tracks_container_restart(self):
        first, second = self.pod(), self.pod()
        second["status"]["containerStatuses"][0].update(containerID="container://backup-2", restartCount=1)
        store = mock.Mock(get_pod=mock.Mock(side_effect=[first, second]))
        settings = {"BACKUP_POD_NAMES": '["copy-0"]', "BACKUP_PASSWORD_REVISION": "revision-1"}
        with mock.patch.dict(backup.os.environ, settings, clear=True):
            self.assertEqual(backup.cleanup_server_identity(store, 0), IDENTITY)
            self.assertEqual(backup.cleanup_server_identity(store, 0),
                             dict(IDENTITY, container_id="container://backup-2", restart_count=1))
        self.assertEqual(store.get_pod.call_args_list, [mock.call("copy-0"), mock.call("copy-0")])

    def test_missing_unready_or_changed_revision_cannot_identify_delete_server(self):
        pods = [None]
        for field, value in (("ready", False), ("state", {"terminated": {}}),
                             ("containerID", ""), ("restartCount", True)):
            pod = self.pod()
            pod["status"]["containerStatuses"][0][field] = value
            pods.append(pod)
        changed = self.pod()
        changed["metadata"]["annotations"][backup.REVISION_ANNOTATION] = "revision-2"
        pods.append(changed)
        settings = {"BACKUP_POD_NAMES": '["copy-0"]', "BACKUP_PASSWORD_REVISION": "revision-1"}
        for pod in pods:
            with self.subTest(pod=pod), mock.patch.dict(backup.os.environ, settings, clear=True):
                with self.assertRaisesRegex(backup.BackupError, "Could not confirm the running backup server"):
                    backup.cleanup_server_identity(mock.Mock(get_pod=mock.Mock(return_value=pod)), 0)

    def test_only_missing_replaced_or_finished_previous_job_releases_ownership(self):
        owner = json.dumps({"pod": "backup-job", "uid": "job-uid"})
        for pod, expected in (
                (None, True),
                ({"metadata": {"name": "backup-job", "uid": "new-uid"}, "status": {"phase": "Running"}}, True),
                ({"metadata": {"name": "backup-job", "uid": "new-uid"}}, True),
                ({"metadata": {"name": "backup-job", "uid": "job-uid"}, "status": {"phase": "Succeeded"}}, True),
                ({"metadata": {"name": "backup-job", "uid": "job-uid"}, "status": {"phase": "Failed"}}, True),
                ({"metadata": {"name": "backup-job", "uid": "job-uid", "deletionTimestamp": "now"},
                  "status": {"phase": "Running"}}, False)):
            with self.subTest(pod=pod):
                self.assertIs(backup.cleanup_owner_finished(mock.Mock(get_pod=mock.Mock(return_value=pod)), owner),
                              expected)


if __name__ == "__main__":
    unittest.main()
