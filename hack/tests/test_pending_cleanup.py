"""Check saved S3 deletion recovery across Jobs without contacting services."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import sys
import unittest

DIRECTORY = Path(__file__).resolve().parents[2] / "charts/ao-data-platform/files/clickhouse-backup"
sys.path.insert(0, str(DIRECTORY))
import backup_retention as retention
from backup_common import BackupError, CleanupStopped, RequestNotSent, RevisionError, RevisionCheckUnavailable
from cleanup_store import CleanupRunLease, CleanupStateConflict
from pending_cleanup import PendingRemoteDelete
sys.path.pop(0)

from test_remote_backup_cleanup import World, NOW, OLD, OLD_I, NEW, NEW_I, name


class MemoryStore:
    def __init__(self):
        self.state, self.version, self.writes = {}, 0, []
        self.failure = None

    def load(self):
        return deepcopy(self.state), str(self.version)

    def compare_and_swap(self, version, state):
        if self.failure:
            raise self.failure
        if version != str(self.version):
            raise CleanupStateConflict("Another test Job updated state.")
        self.state = deepcopy(state)
        self.version += 1
        self.writes.append(deepcopy(state))
        return str(self.version)


class PendingFixture(unittest.TestCase):
    def setUp(self):
        self.world = World(local_copies=0)
        self.clock = [1000]
        self.store = MemoryStore()
        self.identities = [{"pod": f"copy-{index}", "uid": f"copy-{index}-uid", "container_id": f"container-{index}",
                            "restart_count": 0, "password_revision": "revision-1"}
                           for index in range(2)]
        self.lease, self.tracker = self.new_job("first-job")
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def identity(self, index):
        return deepcopy(self.identities[index])

    def new_job(self, owner):
        lease = CleanupRunLease(self.store, owner, now=lambda: self.clock[0])
        lease.acquire()
        return lease, PendingRemoteDelete(lease, self.world.apis, self.identity)

    def snapshots(self):
        remote = retention.validated_rows(self.world.apis[0].request("GET", "/backup/list/remote"), "remote")
        local = [retention.validated_rows(api.request("GET", "/backup/list/local"), "local", remote[0])
                 for api in self.world.apis]
        return remote, local

    def prepare(self, target=OLD_I):
        remote, local = self.snapshots()
        self.tracker.prepare(0, target, remote, local, {OLD, OLD_I})
        return deepcopy(self.store.state["pending_delete"])

    def action(self, status="in progress", start="2026-10-22 20:00:00", target=OLD_I):
        row = {"command": "delete remote " + target, "status": status, "start": start}
        self.world.actions[0] = [row]
        return row

    def complete(self):
        self.action("success")
        self.world.remote.pop(OLD_I, None)

    def run_cleaner(self, tracker=None):
        return retention.RemoteCleaner(self.world.apis, tracker=tracker or self.tracker,
                                       clock=lambda: self.clock[0]).run(NOW, latest_backup=NEW_I, execute=True)

    def assert_pending(self, target=OLD_I):
        self.assertEqual(self.store.state["pending_delete"]["name"], target)


class PendingStateTests(PendingFixture):
    def test_empty_state_allows_no_action_and_changes_no_catalog(self):
        row = self.action()
        self.assertEqual(self.tracker.observe(), "none")
        self.assertFalse(self.tracker.allows_action(0, row))
        rows = [{"name": OLD}, {"name": NEW}]
        self.assertEqual(self.tracker.backup_catalog(rows), rows)
        self.assertFalse(self.tracker.confirm())
        self.assertEqual(self.world.writes(), [])

    def test_four_hours_and_a_new_job_preserve_the_exact_running_deletion_without_retry(self):
        self.world.remove_target = False
        self.world.failure = BackupError("The response timed out.")
        self.world.after_write = lambda index, location, target: self.action(target=target)
        with self.assertRaises(CleanupStopped) as caught:
            self.run_cleaner()
        self.assertEqual(caught.exception.result["stop_reason"], "pending")
        self.assertEqual(caught.exception.result["deleted"], [])
        self.assertEqual(len(self.world.writes()), 1)
        saved = deepcopy(self.store.state["pending_delete"])
        self.assertEqual(saved["action_start"], "2026-10-22 20:00:00")
        self.lease.release()
        self.clock[0] += 4 * 60 * 60
        second_lease, second = self.new_job("second-job")
        self.assertIsNot(second, self.tracker)
        self.assertEqual(second.observe(), "in progress")
        self.assertTrue(second.allows_action(0, self.world.actions[0][0]))
        self.assertEqual(second.record, saved)
        self.assertFalse(second.confirm())
        with self.assertRaises(BackupError):
            remote, local = self.snapshots()
            second.prepare(0, OLD_I, remote, local, {OLD, OLD_I})
        self.assertEqual(len(self.world.writes()), 1)
        self.assertEqual(second_lease.owner, "second-job")
        self.complete()
        self.assertTrue(second.confirm())
        self.assertNotIn("pending_delete", self.store.state)
        self.assertEqual(len(self.world.writes()), 1)

    def test_only_matching_copy_command_status_and_start_can_overlap(self):
        self.prepare()
        own = self.action()
        self.assertTrue(self.tracker.allows_action(0, own))
        for index, row in ((1, own), (0, {**own, "command": "delete remote " + OLD}),
                           (0, {**own, "command": "create_remote " + NEW}),
                           (0, {**own, "status": "success"}),
                           (0, {**own, "start": "2026-10-22 20:00:01"})):
            with self.subTest(index=index, row=row):
                self.assertFalse(self.tracker.allows_action(index, row))
        self.assertEqual(self.world.writes(), [])

    def test_changed_pod_container_restart_or_password_revision_blocks_overlap_and_confirmation(self):
        self.prepare()
        row = self.action()
        self.assertTrue(self.tracker.allows_action(0, row))
        original = deepcopy(self.identities[0])
        for field, value in (("uid", "replacement-pod"), ("container_id", "replacement-container"),
                             ("restart_count", 1), ("password_revision", "revision-2")):
            with self.subTest(field=field):
                self.identities[0] = {**original, field: value}
                self.assertEqual(self.tracker.observe(), "server_changed")
                self.assertFalse(self.tracker.allows_action(0, row))
                self.assertFalse(self.tracker.confirm())
                with self.assertRaises(BackupError):
                    self.tracker.before_send(0, OLD_I)
                self.assert_pending()
        self.assertEqual(self.world.writes(), [])

    def test_missing_duplicate_changed_start_and_unknown_status_do_not_prove_a_result(self):
        self.prepare()
        own = self.action()
        self.assertEqual(self.tracker.observe(), "in progress")
        histories = [[], [own, dict(own)], [{**own, "start": "2026-10-22 20:00:01"}],
                     [{**own, "start": ""}], [{**own, "status": "queued"}],
                     [{"command": "create_remote " + NEW, "status": "in progress", "start": own["start"]}]]
        for history in histories:
            with self.subTest(history=history):
                self.world.actions[0] = history
                self.assertEqual(self.tracker.observe(), "unknown")
                self.assertFalse(self.tracker.allows_action(0, own))
                self.assertFalse(self.tracker.confirm())
                self.assert_pending()
        self.assertEqual(self.world.writes(), [])

    def test_error_and_cancel_keep_intent_without_granting_overlap(self):
        self.prepare()
        for status in ("error", "cancel"):
            with self.subTest(status=status):
                row = self.action(status)
                self.assertEqual(self.tracker.observe(), status)
                self.assertFalse(self.tracker.allows_action(0, row))
                self.assertFalse(self.tracker.confirm())
                self.assert_pending()
        self.assertEqual(self.world.writes(), [])

    def test_unreadable_action_history_stops_and_keeps_intent(self):
        self.prepare()
        for history in (None, {}, [None], ["private server text"]):
            with self.subTest(history=history):
                self.world.actions[0] = history
                with self.assertRaises(BackupError) as caught:
                    self.tracker.observe()
                self.assertNotIn("private", str(caught.exception))
                self.assert_pending()

    def test_catalog_excludes_exact_retiring_names_and_preserves_other_rows(self):
        self.prepare()
        names = [OLD, OLD_I, NEW, NEW_I, OLD + "-other", "manual-backup"]
        rows = [{"name": target, "marker": index} for index, target in enumerate(names)]
        before = deepcopy(rows)
        self.assertEqual(self.tracker.backup_catalog(rows), rows[2:])
        self.assertEqual(rows, before)

    def test_prior_same_command_blocks_prepare_even_if_completed(self):
        remote, local = self.snapshots()
        for status in ("in progress", "success", "error", "cancel"):
            with self.subTest(status=status):
                self.action(status)
                with self.assertRaises(BackupError):
                    self.tracker.prepare(0, OLD_I, remote, local, {OLD, OLD_I})
                self.assertNotIn("pending_delete", self.store.state)
        self.assertEqual(self.world.writes(), [])

    def test_prepare_requires_approved_name_and_absent_local_records(self):
        remote, local = self.snapshots()
        with self.assertRaises(BackupError):
            self.tracker.prepare(0, OLD_I, remote, local, {OLD})
        self.world.local[1][OLD_I] = OLD
        remote, local = self.snapshots()
        with self.assertRaises(BackupError):
            self.tracker.prepare(0, OLD_I, remote, local, {OLD, OLD_I})
        self.assertNotIn("pending_delete", self.store.state)
        self.assertEqual(self.world.writes(), [])

    def test_lost_ownership_or_replaced_pending_record_cannot_be_cleared(self):
        self.prepare()
        original = deepcopy(self.store.state)
        self.store.state["run"]["owner"] = "another-job"
        with self.assertRaises(CleanupStateConflict):
            self.tracker.not_sent()
        self.store.state = deepcopy(original)
        self.store.state["pending_delete"]["action_start"] = "another-operation"
        with self.assertRaises(BackupError):
            self.tracker.not_sent()
        self.assert_pending()

    def test_saved_record_rejects_missing_required_fields(self):
        record = self.prepare()
        for field in ("schema", "copy", "name", "identity", "remote", "local", "excluded", "action_start", "created"):
            with self.subTest(field=field):
                malformed = deepcopy(record)
                del malformed[field]
                self.store.state["pending_delete"] = malformed
                with self.assertRaises(BackupError):
                    PendingRemoteDelete(self.lease, self.world.apis, self.identity)
        self.assertEqual(self.world.writes(), [])

    def test_saved_record_rejects_invalid_types_and_missing_verification_data(self):
        record = self.prepare()
        cases = [("schema", True), ("schema", 2), ("copy", True), ("copy", -1), ("copy", 2),
                 ("name", "manual-backup"), ("identity", {}), ("identity", []),
                 ("remote", {}), ("remote", {OLD_I: "invalid-hash"}),
                 ("local", [None, None]), ("local", [{}]),
                 ("excluded", [OLD]), ("excluded", [OLD_I, "manual-backup"]),
                 ("excluded", [OLD_I, OLD_I]), ("action_start", 123),
                 ("created", "not a date"), ("created", "2026-10-22T20:00:00"),
                 ("created", "2026-10-22T20:00:00+01:00"),
                 ("identity", {**record["identity"], "restart_count": True}),
                 ("identity", {**record["identity"], "uid": ""}),
                 ("local", [{NEW: "invalid-hash"}, {}]),
                 ("local", [{OLD_I: record["remote"][OLD_I]}, {}])]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.store.state["pending_delete"] = {**deepcopy(record), field: value}
                with self.assertRaises(BackupError):
                    PendingRemoteDelete(self.lease, self.world.apis, self.identity)
        self.assertEqual(self.world.writes(), [])


class ConfirmationTests(PendingFixture):
    def test_success_requires_remote_and_all_local_absence_before_clearing(self):
        self.prepare()
        self.action("success")
        with self.assertRaises(BackupError):
            self.tracker.confirm()
        self.assert_pending()
        self.world.remote.pop(OLD_I)
        self.world.local[1][OLD_I] = OLD
        with self.assertRaises(BackupError):
            self.tracker.confirm()
        self.assert_pending()
        self.world.local[1].clear()
        self.assertTrue(self.tracker.confirm())
        self.assertNotIn("pending_delete", self.store.state)
        self.assertIsNone(self.tracker.record)
        self.assertEqual(self.world.writes(), [])

    def test_changed_or_missing_existing_remote_metadata_keeps_intent(self):
        self.prepare()
        self.complete()
        baseline = deepcopy(self.world.remote)
        for change in ("size", "missing"):
            with self.subTest(change=change):
                self.world.remote = deepcopy(baseline)
                self.world.row_overrides = {}
                if change == "missing":
                    del self.world.remote[NEW_I]
                else:
                    for copy in (0, 1):
                        self.world.row_overrides[(copy, "remote", NEW)] = {"metadata_size": 101}
                with self.assertRaises(BackupError):
                    self.tracker.confirm()
                self.assert_pending()

    def test_changed_or_missing_existing_local_metadata_keeps_intent(self):
        self.world.local = [{NEW: "", NEW_I: NEW}, {NEW: ""}]
        self.prepare()
        self.complete()
        baseline = deepcopy(self.world.local)
        for change in ("size", "missing"):
            with self.subTest(change=change):
                self.world.local = deepcopy(baseline)
                self.world.row_overrides = {}
                if change == "missing":
                    del self.world.local[1][NEW]
                else:
                    self.world.row_overrides[(1, "local", NEW)] = {"metadata_size": 101}
                with self.assertRaises(BackupError):
                    self.tracker.confirm()
                self.assert_pending()

    def test_new_complete_backups_and_downloaded_base_records_are_allowed(self):
        self.prepare()
        self.complete()
        later = name("incremental", 0, 8)
        self.world.remote[later] = NEW
        self.world.local = [{NEW: "", later: NEW}, {NEW: ""}]
        self.assertTrue(self.tracker.confirm())
        self.assertNotIn("pending_delete", self.store.state)
        self.assertIn(later, self.world.remote)
        self.assertEqual(self.world.writes(), [])

    def test_disagreeing_copies_and_new_incomplete_records_keep_intent(self):
        self.prepare()
        self.complete()
        self.world.remote_overrides[1] = {**self.world.remote, OLD_I: OLD}
        with self.assertRaises(BackupError):
            self.tracker.confirm()
        self.assert_pending()
        self.world.remote_overrides.clear()
        incomplete = name("incremental", 0, 8)
        self.world.local[1][incomplete] = ""
        self.world.row_overrides[(1, "local", incomplete)] = {"desc": "broken metadata.json not found"}
        with self.assertRaises(BackupError):
            self.tracker.confirm()
        self.assert_pending()

    def test_server_change_during_verification_prevents_clear(self):
        self.prepare()
        self.complete()

        def replace_server(index, method, path):
            if path == "/backup/list/local":
                self.identities[0]["restart_count"] = 1

        self.world.before_request = replace_server
        self.assertFalse(self.tracker.confirm())
        self.assert_pending()
        self.assertEqual(self.world.writes(), [])

    def test_lost_action_history_during_verification_prevents_clear(self):
        self.prepare()
        self.complete()

        def lose_history(index, method, path):
            if path == "/backup/list/local":
                self.world.actions[0] = []

        self.world.before_request = lose_history
        self.assertFalse(self.tracker.confirm())
        self.assert_pending()
        self.assertEqual(self.world.writes(), [])

    def test_failed_catalog_read_keeps_pending_without_deletion(self):
        self.prepare()
        self.complete()

        def unavailable(index, method, path):
            if index == 1 and path == "/backup/list/remote":
                raise BackupError("The second copy could not be read.")

        self.world.before_request = unavailable
        with self.assertRaises(BackupError):
            self.tracker.confirm()
        self.assert_pending()
        self.assertEqual(self.world.writes(), [])


class CleanerIntegrationTests(PendingFixture):
    def test_insufficient_time_does_not_save_an_unsent_deletion(self):
        cleaner = retention.RemoteCleaner(self.world.apis, tracker=self.tracker,
                                          timeout=100, clock=lambda: self.clock[0])
        result = cleaner.run(NOW, latest_backup=NEW_I, execute=True)
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertNotIn("pending_delete", self.store.state)
        self.assertEqual(self.world.writes(), [])

    def test_time_expires_after_preparation_without_leaving_a_false_pending_delete(self):
        prepare = self.tracker.prepare

        def slow_prepare(*args):
            prepare(*args)
            self.clock[0] += 2000

        self.tracker.prepare = slow_prepare
        result = self.run_cleaner()
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertNotIn("pending_delete", self.store.state)
        self.assertEqual(self.world.writes(), [])

    def test_intent_is_persisted_before_delete_and_success_after_timeout_is_verified(self):
        observed = []

        def before(index, method, path):
            if method == "POST":
                record = self.store.state["pending_delete"]
                self.assertEqual((record["copy"], record["name"]), (index, path.rsplit("/", 1)[1]))
                self.assertIsNone(record["action_start"])
                self.assertIn(record["name"], record["remote"])
                observed.append(record["name"])

        def finished(index, location, target):
            self.world.actions[index].append({"command": "delete remote " + target,
                                              "start": "2026-10-22 20:00:00", "status": "success"})

        self.world.before_request, self.world.after_write = before, finished
        self.world.failure = BackupError("The successful response was lost.")
        result = self.run_cleaner()
        self.assertTrue(result["completed"])
        self.assertEqual(result["deleted"], [OLD_I, OLD])
        self.assertEqual(observed, [OLD_I, OLD])
        self.assertEqual(len(self.world.writes()), 2)
        self.assertNotIn("pending_delete", self.store.state)
        self.assertEqual(self.world.remote, {NEW: "", NEW_I: NEW})

    def test_failed_state_write_prevents_delete_request(self):
        self.store.failure = CleanupStateConflict("Another Job wrote first.")
        with self.assertRaises(BackupError):
            self.run_cleaner()
        self.assertEqual(self.world.writes(), [])
        self.assertNotIn("pending_delete", self.store.state)

    def test_saved_intent_with_lost_write_response_never_sends_delete(self):
        persist = self.store.compare_and_swap

        def lose_response(version, state):
            persist(version, state)
            raise BackupError("The state write response was lost.")

        self.store.compare_and_swap = lose_response
        with self.assertRaises(BackupError):
            self.run_cleaner()
        self.assert_pending()
        self.assertEqual(self.world.writes(), [])
        self.store.compare_and_swap = persist
        recovered = PendingRemoteDelete(self.lease, self.world.apis, self.identity)
        self.assertEqual(recovered.observe(), "unknown")
        self.assertFalse(recovered.confirm())
        self.assert_pending()
        self.assertEqual(self.world.writes(), [])

    def test_only_proven_unsent_requests_clear_intent(self):
        for failure in (RequestNotSent("Not connected."), RevisionError("Revision changed."),
                        RevisionCheckUnavailable("Revision unavailable.")):
            with self.subTest(failure=type(failure).__name__):
                self.world.remove_target = False
                self.world.failure = failure
                before = len(self.world.writes())
                with self.assertRaises(BackupError):
                    self.run_cleaner()
                self.assertNotIn("pending_delete", self.store.state)
                self.assertEqual(len(self.world.writes()), before + 1)
                self.assertIn(OLD_I, self.world.remote)

    def test_uncertain_error_and_malformed_response_keep_intent_without_retry(self):
        for failure, response in ((BackupError("private server text"), None),
                                  (RuntimeError("private transport text"), None),
                                  (None, []), (None, [{"status": "acknowledged"}])):
            with self.subTest(failure=type(failure).__name__, response=response):
                self.store.state.pop("pending_delete", None)
                self.tracker.refresh()
                self.world.remove_target = False
                self.world.failure, self.world.response = failure, response
                before = len(self.world.writes())
                with self.assertRaises(CleanupStopped) as caught:
                    self.run_cleaner()
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(caught.exception.result["deleted"], [])
                self.assert_pending()
                self.assertEqual(len(self.world.writes()), before + 1)

    def test_successful_action_after_timeout_with_changed_catalog_is_not_verified(self):
        self.world.failure = BackupError("The response timed out.")

        def finished(index, location, target):
            self.action("success", target=target)
            for copy in (0, 1):
                self.world.row_overrides[(copy, "remote", NEW)] = {"metadata_size": 101}

        self.world.after_write = finished
        with self.assertRaises(CleanupStopped) as caught:
            self.run_cleaner()
        self.assertEqual(caught.exception.result["deleted"], [])
        self.assertEqual(caught.exception.result["unverified_deletions"], [
            {"copy": 0, "location": "remote", "name": OLD_I, "response_status": "success"}])
        self.assert_pending()
        self.assertEqual(len(self.world.writes()), 1)

    def test_new_cleaner_never_resends_when_saved_request_has_no_history(self):
        self.prepare()
        second = PendingRemoteDelete(self.lease, self.world.apis, self.identity)
        with self.assertRaises(BackupError):
            self.run_cleaner(second)
        self.assertEqual(self.world.writes(), [])
        self.assert_pending()


if __name__ == "__main__":
    unittest.main()
