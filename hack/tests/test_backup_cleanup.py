"""Exercise retention previews and prove all execution paths refuse writes."""

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import unittest


DIRECTORY = Path(__file__).resolve().parents[2] / "charts/ao-data-platform/files/clickhouse-backup"
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("backup_cleanup", DIRECTORY / "cleanup_backups.py")
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)
sys.path.pop(0)
NOW = datetime(2026, 9, 29, 20, tzinfo=timezone.utc)


def name(kind, age, suffix="12345678"):
    return f"ao-otel-{kind}-{NOW - timedelta(hours=age):%Y%m%dT%H%M%SZ}-{suffix}"


A, AI = name("full", 72), name("incremental", 68)
B, BI = name("full", 48), name("incremental", 44)
C, CI = name("full", 24), name("incremental", 20)
THREE_GROUPS = {A: "", AI: A, B: "", BI: B, C: "", CI: C}


class SelectionTests(unittest.TestCase):
    def test_count_is_individual_backups_not_full_groups(self):
        result = cleanup.plan(THREE_GROUPS, NOW)
        self.assertEqual(set(result["kept"]), {C, CI})
        self.assertEqual(set(result["delete"]), {A, AI, B, BI})
        self.assertLess(result["delete"].index(AI), result["delete"].index(A))
        self.assertLess(result["delete"].index(BI), result["delete"].index(B))

    def test_required_bases_can_exceed_keep_count(self):
        result = cleanup.plan({A: "", AI: A, B: "", BI: B, C: ""}, NOW)
        self.assertEqual(set(result["kept"]), {B, BI, C})
        self.assertEqual(result["delete"], [AI, A])

    def test_recursive_dependencies_are_kept(self):
        second = name("incremental", 4)
        result = cleanup.plan({A: "", AI: A, second: AI}, NOW, keep_last=1)
        self.assertEqual(set(result["kept"]), {A, AI, second})
        self.assertEqual(result["delete"], [])

    def test_thirty_day_boundary_is_inclusive_and_keeps_older_base(self):
        old = name("full", 24 * 35)
        before = name("incremental", 24 * 30 + 1)
        boundary = name("incremental", 24 * 30)
        result = cleanup.plan({old: "", before: old, boundary: old, C: "", CI: C},
                              NOW, keep_days=30)
        self.assertEqual(set(result["kept"]), {old, boundary, C, CI})
        self.assertEqual(result["delete"], [before])

    def test_count_floor_preserves_backups_when_all_are_older_than_window(self):
        result = cleanup.plan({A: "", AI: A}, NOW, keep_days=1)
        self.assertEqual(set(result["kept"]), {A, AI})

    def test_foreign_backups_and_their_bases_are_preserved(self):
        result = cleanup.plan({**THREE_GROUPS, "manual-backup": AI}, NOW)
        self.assertIn(A, result["kept"])
        self.assertIn(AI, result["kept"])
        self.assertNotIn("manual-backup", result["delete"])

    def test_latest_verified_backup_is_protected_even_outside_count(self):
        result = cleanup.plan(THREE_GROUPS, NOW, latest_backup=AI)
        self.assertIn(AI, result["kept"])
        self.assertIn(A, result["kept"])

    def test_invalid_dependency_graph_stops_selection(self):
        cases = [{A: "missing"}, {A: CI, CI: A}, {A: "", AI: ""},
                 {A: "", AI: C, C: ""}, {"manual-one": "manual-two", "manual-two": "manual-one"}]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(cleanup.BackupError):
                cleanup.plan(entries, NOW)

    def test_future_invalid_and_malformed_scheduled_dates_fail_closed(self):
        for invalid in [name("full", -1), "ao-otel-full-20269999T000000Z-12345678", "ao-otel-unknown"]:
            with self.subTest(invalid=invalid), self.assertRaises(cleanup.BackupError):
                cleanup.plan({invalid: ""}, NOW)

    def test_invalid_settings_and_missing_latest_stop_selection(self):
        for parameters in [dict(keep_last=0), dict(keep_last=True), dict(keep_days=-1),
                           dict(keep_days=1.5), dict(latest_backup="missing")]:
            with self.subTest(parameters=parameters), self.assertRaises(cleanup.BackupError):
                cleanup.plan(THREE_GROUPS, NOW, **parameters)
        with self.assertRaises(cleanup.BackupError):
            cleanup.plan(THREE_GROUPS, NOW.replace(tzinfo=None))


class World:
    def __init__(self):
        self.remote = dict(THREE_GROUPS)
        self.local = [dict(THREE_GROUPS), dict(THREE_GROUPS)]
        self.calls = []
        self.actions = [[], []]
        self.before_read = None
        self.apis = [FakeAPI(self, 0), FakeAPI(self, 1)]

    def run(self, **kwargs):
        return cleanup.Cleaner(self.apis, **kwargs).run(NOW, latest_backup=CI)


class FakeAPI:
    def __init__(self, world, index):
        self.world, self.index = world, index
        self.unavailable = False
        self.broken_local = False
        self.broken_remote = False
        self.remote_override = None

    def request(self, method, path):
        world, index = self.world, self.index
        world.calls.append((index, method, path))
        if method != "GET":
            raise AssertionError("Preview must never send a write request")
        if self.unavailable:
            raise cleanup.BackupError("Backup API could not be reached.")
        if world.before_read:
            world.before_read(index, path)
        if path == "/backup/actions":
            return list(world.actions[index])
        if path not in ("/backup/list/remote", "/backup/list/local"):
            raise AssertionError("Preview must not depend on a patched tool version")
        location = path.rsplit("/", 1)[1]
        entries = (self.remote_override if self.remote_override is not None else world.remote) if location == "remote" else world.local[index]
        desc = "directory, embedded" if location == "remote" else "embedded"
        if (location == "remote" and self.broken_remote) or (location == "local" and self.broken_local):
            desc = "private failure details"
        return [dict(name=n, required=r, location=location, desc=desc) for n, r in entries.items()]


class PreviewTests(unittest.TestCase):
    def test_preview_only_reads_and_reports_retention(self):
        world = World()
        before = (dict(world.remote), [dict(entries) for entries in world.local])
        result = world.run()
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["deleted"], [])
        self.assertEqual(set(result["delete"]), {A, AI, B, BI})
        self.assertTrue(world.calls)
        self.assertTrue(all(method == "GET" for _, method, _ in world.calls))
        self.assertEqual((world.remote, world.local), before)

    def test_execute_is_rejected_before_any_request_even_with_verified_backup(self):
        for latest in (None, CI):
            world = World()
            with self.subTest(latest=latest), self.assertRaisesRegex(cleanup.BackupError, "preview only"):
                cleanup.Cleaner(world.apis).run(NOW, latest_backup=latest, execute=True)
            self.assertEqual(world.calls, [])

    def test_direct_mutation_request_is_also_rejected(self):
        world = World()
        cleaner = cleanup.Cleaner(world.apis)
        for method in ("POST", "DELETE", "PUT"):
            with self.subTest(method=method), self.assertRaisesRegex(cleanup.BackupError, "preview only"):
                cleaner.request(0, method, "/backup/delete/remote/" + A)
        self.assertEqual(world.calls, [])

    def test_remote_only_backup_can_be_previewed_without_deleting_it(self):
        world = World()
        for entries in world.local:
            del entries[AI]
        self.assertIn(AI, world.run()["delete"])
        self.assertIn(AI, world.remote)

    def test_orphan_local_record_stops_preview(self):
        world = World()
        del world.remote[AI]
        with self.assertRaisesRegex(cleanup.BackupError, "no matching remote"):
            world.run()

    def test_local_and_remote_dependency_mismatch_stops_preview(self):
        world = World()
        world.local[1][AI] = ""
        with self.assertRaises(cleanup.BackupError):
            world.run()

    def test_broken_metadata_or_unavailable_copy_stops_preview(self):
        for attribute in ("broken_local", "broken_remote", "unavailable"):
            world = World()
            setattr(world.apis[1], attribute, True)
            with self.subTest(attribute=attribute), self.assertRaises(cleanup.BackupError) as caught:
                world.run()
            self.assertNotIn("private", str(caught.exception))

    def test_disagreeing_remote_catalogues_stop_preview(self):
        world = World()
        world.apis[1].remote_override = {C: "", CI: C}
        with self.assertRaisesRegex(cleanup.BackupError, "disagree"):
            world.run()

    def test_older_active_action_is_found_even_when_last_action_succeeded(self):
        world = World()
        world.actions[1] = [dict(command="create_remote ongoing", status="in progress"),
                            dict(command="list remote", status="success")]
        with self.assertRaisesRegex(cleanup.BackupError, "busy"):
            world.run()

    def test_failed_deletion_history_requires_inspection(self):
        world = World()
        world.actions[1] = [dict(command="delete local " + AI, status="error")]
        with self.assertRaisesRegex(cleanup.BackupError, "previous deletion"):
            world.run()

    def test_unknown_or_missing_action_fields_stop_preview(self):
        for action in (dict(status="unknown", command="create"), dict(status="success"), dict(command="create")):
            world = World()
            world.actions[1] = [action]
            with self.subTest(action=action), self.assertRaises(cleanup.BackupError):
                world.run()

    def test_metadata_change_after_snapshot_stops_preview(self):
        world = World()
        reads = 0
        def change_after_snapshot(index, path):
            nonlocal reads
            if path == "/backup/list/remote":
                reads += 1
                if reads == 3:
                    world.remote[name("full", 0)] = ""
        world.before_read = change_after_snapshot
        with self.assertRaisesRegex(cleanup.BackupError, "changed"):
            world.run()

    def test_deadline_leaves_room_for_a_request(self):
        world = World()
        with self.assertRaisesRegex(cleanup.BackupError, "time limit"):
            world.run(timeout=29)
        self.assertEqual(world.calls, [])

    def test_both_distinct_copies_and_boolean_execute_are_required(self):
        world = World()
        with self.assertRaisesRegex(cleanup.BackupError, "both ClickHouse"):
            cleanup.Cleaner(world.apis[:1])
        with self.assertRaisesRegex(cleanup.BackupError, "different"):
            cleanup.Cleaner([world.apis[0], world.apis[0]])
        for api in world.apis:
            api.endpoint = "http://same-copy:7171"
        with self.assertRaisesRegex(cleanup.BackupError, "different"):
            cleanup.Cleaner(world.apis)
        del world.apis[1].endpoint
        with self.assertRaisesRegex(cleanup.BackupError, "explicit boolean"):
            cleanup.Cleaner(world.apis).run(NOW, latest_backup=CI, execute="false")
        self.assertEqual(world.calls, [])


if __name__ == "__main__":
    unittest.main()
