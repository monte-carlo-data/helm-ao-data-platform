"""Test complete-set retention and ordered deletion without contacting a service."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import sys
import unittest

DIRECTORY = Path(__file__).resolve().parents[2] / "charts/ao-data-platform/files/clickhouse-backup"
sys.path.insert(0, str(DIRECTORY))
import backup_retention as retention
import cleanup_backups as legacy
from cleanup_local_backups import local_plan
sys.path.pop(0)

NOW = datetime(2026, 10, 22, 20, tzinfo=timezone.utc)


def name(kind, days, hour=0, suffix="12345678"):
    created = (NOW - timedelta(days=days)).replace(hour=hour)
    return f"ao-otel-{kind}-{created:%Y%m%dT%H%M%SZ}-{suffix}"


def group(days, suffix="12345678"):
    full, incremental = name("full", days, suffix=suffix), name("incremental", days, 4, suffix)
    return {full: "", incremental: full}


OLD, OLD_I = tuple(group(40))
NEW, NEW_I = tuple(group(0))
BASE = {**group(40), **group(0)}


class SelectionTests(unittest.TestCase):
    def plan(self, remote=None, local=None, **options):
        remote = BASE if remote is None else remote
        local = [dict(remote)] if local is None else local
        return retention.plan_sets(remote, local, NOW, **options)

    def test_daily_days_and_latest_day_in_each_older_monday_sunday_week(self):
        remote = {key: value for age in range(41) for key, value in group(age).items()}
        result = self.plan(remote)
        kept_days = {0, 1, 2, 3, 4, 5, 6, 7, 11, 18, 25}
        self.assertEqual(set(result["kept"]), {key for age in kept_days for key in group(age)})
        for age in set(range(41)) - kept_days:
            full, incremental = group(age)
            self.assertLess(result["delete"].index(incremental), result["delete"].index(full))

    def test_seven_and_thirty_day_boundaries_use_whole_utc_dates(self):
        remote = {**group(0), **group(6), **group(7), **group(8), **group(29), **group(30)}
        result = self.plan(remote)
        self.assertTrue(set(group(6)) <= set(result["kept"]))
        self.assertTrue(set(group(7)) <= set(result["kept"]))
        self.assertTrue(set(group(8)) <= set(result["delete"]))
        self.assertTrue(set(group(29)) <= set(result["kept"]))
        self.assertTrue(set(group(30)) <= set(result["delete"]))

    def test_multiple_fulls_on_a_selected_day_keep_every_family(self):
        retries = group(7, "aaaaaaaa")
        result = self.plan({**group(0), **group(7), **group(8), **retries})
        self.assertTrue((set(group(7)) | set(retries)) <= set(result["kept"]))
        self.assertTrue(set(group(8)) <= set(result["delete"]))

    def test_recent_incremental_keeps_its_old_full_and_all_siblings(self):
        recent = name("incremental", 1)
        result = self.plan({**BASE, recent: OLD_I})
        self.assertEqual(result["delete"], [])
        self.assertEqual(set(result["kept"]), set(BASE) | {recent})

    def test_newest_verified_old_set_is_protected_despite_long_gap(self):
        result = self.plan({**group(40), **group(60)}, latest_backup=OLD_I)
        self.assertEqual(set(result["kept"]), set(group(40)))
        self.assertEqual(set(result["delete"]), set(group(60)))

    def test_whole_set_is_kept_when_manual_remote_requires_one_member(self):
        result = self.plan({**BASE, "manual-copy": OLD})
        self.assertEqual(result["delete"], [])
        self.assertIn("manual-copy", result["kept"])

    def test_manual_and_local_only_records_preserve_remote_bases(self):
        for record in ("manual-copy", name("incremental", 39)):
            with self.subTest(record=record):
                result = self.plan(local=[{**BASE, record: OLD}])
                self.assertEqual(result["delete"], [])
                self.assertEqual(result["local_only"], [{"copy": 0, "name": record}])

    def test_old_local_second_day_does_not_override_remote_expiry(self):
        self.assertEqual(local_plan(BASE, [BASE], NOW)["delete"], [])
        self.assertEqual(self.plan(latest_backup=NEW_I)["delete"], [OLD_I, OLD])

    def test_invalid_remote_or_local_dependencies_stop_selection(self):
        cases = [({**BASE, OLD: NEW}, [BASE]), (BASE, [{**BASE, OLD_I: NEW}]),
                 (BASE, [{**BASE, "manual-copy": "missing"}]),
                 (BASE, [{**BASE, "manual-a": "manual-b", "manual-b": "manual-a"}]),
                 (BASE, [{**BASE, OLD_I: None}]), (BASE, [{**BASE, OLD_I: ""}])]
        for remote, local in cases:
            with self.subTest(remote=remote, local=local), self.assertRaises(retention.BackupError):
                self.plan(remote, local)

    def test_distinct_valid_local_base_must_match_remote(self):
        remote = {**BASE, **group(41)}
        with self.assertRaisesRegex(retention.BackupError, "dependencies disagree"):
            self.plan(remote, [{**remote, OLD_I: name("full", 41)}])

    def test_missed_days_do_not_invent_backups_or_shorten_weekly_sets(self):
        result = self.plan({**group(0), **group(9), **group(13), **group(20)})
        self.assertEqual(result["delete"], [])

    def test_invalid_policy_settings_and_missing_latest_are_rejected(self):
        for options in (dict(daily_days=0), dict(daily_days=True), dict(history_days=6),
                        dict(history_days=30.0), dict(latest_backup="missing")):
            with self.subTest(options=options), self.assertRaises(retention.BackupError):
                self.plan(**options)
        with self.assertRaises(retention.BackupError):
            retention.plan_sets(BASE, [BASE], NOW.replace(tzinfo=None))


class World:
    def __init__(self, local_copies=2):
        self.remote = dict(BASE)
        self.local = [dict(BASE) if i < local_copies else {} for i in range(2)]
        self.actions = [[], []]
        self.versions = [[{"version": "2.8.2"}], [{"version": "2.8.2"}]]
        self.calls, self.apis = [], [API(self, 0), API(self, 1)]
        self.before_request = self.after_write = None
        self.failure = self.response = None
        self.remove_target = True
        self.remote_overrides, self.row_overrides = {}, {}

    def run(self, execute=True, **options):
        self.output = io.StringIO()
        with redirect_stdout(self.output):
            return legacy.Cleaner(self.apis, retention_policy="daily-weekly", **options).run(
                NOW, latest_backup=NEW_I, execute=execute)

    def writes(self):
        return [row for row in self.calls if row[1] == "POST"]


class API:
    def __init__(self, world, index):
        self.world, self.index = world, index
        self.endpoint = f"http://copy{index}:7171"

    def request(self, method, path, **options):
        w, index = self.world, self.index
        w.calls.append((index, method, path, options))
        if w.before_request:
            w.before_request(index, method, path)
        if method == "POST":
            location, backup = path.split("/")[-2:]
            if location == "local":
                assert backup in w.remote, "Removing local records must preserve a matching S3 backup"
                target = w.local[index]
            else:
                assert all(backup not in entries for entries in w.local), "Remote deletion requires no local records"
                target = w.remote
            if w.remove_target:
                del target[backup]
            if w.after_write:
                w.after_write(index, location, backup)
            if w.failure:
                raise w.failure
            if w.response is not None:
                return w.response
            return [{"status": "success", "operation": "delete", "backup_name": backup, "location": location}]
        if path == "/backup/version":
            return w.versions[index]
        if path == "/backup/actions":
            return w.actions[index]
        location = path.rsplit("/", 1)[1]
        entries = w.remote_overrides.get(index, w.remote) if location == "remote" else w.local[index]
        rows = []
        for backup, required in entries.items():
            row = dict(name=backup, required=required, location=location, metadata_size=100,
                       desc="directory, embedded" if location == "remote" else "embedded")
            row.update(w.row_overrides.get((index, location, backup), {}))
            rows.append(row)
        return rows


class ExecutionTests(unittest.TestCase):
    def test_preview_preserves_all_files_and_does_not_require_new_version(self):
        world = World()
        world.versions = [[{"version": "2.8.1"}], [{"version": "2.8.1"}]]
        result = world.run(execute=False)
        self.assertEqual(result["delete"], [OLD_I, OLD])
        self.assertEqual(result["deleted"], [])
        self.assertEqual(world.writes(), [])
        self.assertFalse(any(row[2] == "/backup/version" for row in world.calls))
        self.assertEqual(world.remote, BASE)

    def test_all_one_or_no_local_copies_are_handled_before_remote_deletion(self):
        for copies in (0, 1, 2):
            with self.subTest(copies=copies):
                world = World(copies)
                result = world.run()
                self.assertTrue(result["completed"])
                self.assertEqual(result["deleted"], [OLD_I, OLD])
                self.assertEqual(world.remote, group(0))
                expected = [(i, "local", backup) for backup in (OLD_I, OLD) for i in range(copies)]
                actual = [(i, path.split("/")[-2], path.split("/")[-1]) for i, _, path, _ in world.writes()]
                self.assertEqual([x for x in actual if x[1] == "local"], expected)
                self.assertEqual([x for x in actual if x[1] == "remote"], [(0, "remote", OLD_I), (0, "remote", OLD)])
                self.assertTrue(all(backup not in entries for backup in (OLD, OLD_I) for entries in world.local))

    def test_mixed_wrong_or_unreadable_versions_stop_before_writes(self):
        for version in ([{"version": "2.8.1"}], [{"version": "2.9.0"}], [], None, [{"version": "private-text"}]):
            with self.subTest(version=version):
                world = World()
                world.versions[1] = version
                with self.assertRaisesRegex(retention.BackupError, "2.8.2 on every copy"):
                    world.run()
                self.assertEqual(world.writes(), [])

    def test_version_change_after_one_action_stops_further_deletion(self):
        world = World()
        world.after_write = lambda *args: world.versions.__setitem__(1, [{"version": "2.8.1"}])
        with self.assertRaisesRegex(retention.BackupError, "2.8.2 on every copy"):
            world.run()
        self.assertEqual(len(world.writes()), 1)
        self.assertEqual(world.remote, BASE)

    def test_busy_or_failed_deletion_history_stops_without_writes(self):
        for row in ({"status": "in progress", "command": "restore other"},
                    {"status": "error", "command": "delete local other"},
                    {"status": "cancel", "command": "delete remote other"}, {}):
            with self.subTest(row=row):
                world = World()
                world.actions[1] = [row]
                with self.assertRaises(retention.BackupError):
                    world.run()
                self.assertEqual(world.writes(), [])

    def test_unavailable_copy_or_disagreed_remote_catalog_stops_without_writes(self):
        from unittest.mock import patch
        world = World()
        with patch.object(world.apis[1], "request", side_effect=retention.BackupError("unreachable")):
            with self.assertRaises(retention.BackupError):
                world.run()
        self.assertEqual(world.writes(), [])
        world = World()
        world.remote_overrides[1] = group(0)
        with self.assertRaisesRegex(retention.BackupError, "disagree"):
            world.run()
        self.assertEqual(world.writes(), [])

    def test_broken_local_metadata_stops_all_deletion(self):
        world = World()
        world.row_overrides[1, "local", OLD] = {"desc": "broken metadata.json not found"}
        with self.assertRaisesRegex(retention.BackupError, "incomplete local"):
            world.run()
        self.assertEqual(world.writes(), [])

    def test_changed_size_or_new_dependency_before_write_stops(self):
        for mutation in ("size", "manual", "missing"):
            world = World()
            reads = 0
            def change(index, method, path):
                nonlocal reads
                if index == 0 and path == "/backup/list/remote":
                    reads += 1
                    if reads == 3:
                        if mutation == "size":
                            for i in range(2):
                                world.row_overrides[i, "remote", OLD_I] = {"metadata_size": 200}
                        elif mutation == "manual":
                            world.local[1]["manual-copy"] = OLD
                        else:
                            del world.remote[OLD_I]
            world.before_request = change
            with self.subTest(mutation=mutation), self.assertRaisesRegex(retention.BackupError, "metadata changed"):
                world.run()
            self.assertEqual(world.writes(), [])

    def test_uncertain_or_unexpected_response_never_retries_or_continues(self):
        for copies in (0, 2):
            for failure, response in ((retention.BackupError("private transport detail"), None),
                                      (None, []), (None, [{"status": "acknowledged"}])):
                world = World(copies)
                world.failure, world.response = failure, response
                with self.subTest(copies=copies, failure=failure, response=response), self.assertRaisesRegex(
                        retention.BackupError, "not confirmed") as caught:
                    world.run()
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(len(world.writes()), 1)
                self.assertNotIn("delete_verified", world.output.getvalue())
                self.assertTrue(set(group(0)) <= set(world.remote))

    def test_success_response_without_removal_stops(self):
        world = World()
        world.remove_target = False
        with self.assertRaisesRegex(retention.BackupError, "metadata changed"):
            world.run()
        self.assertEqual(len(world.writes()), 1)

    def test_time_limit_after_remote_removal_does_not_claim_verified_completion(self):
        world, clock = World(0), [0]
        world.after_write = lambda *args: clock.__setitem__(0, 1000)
        result = world.run(timeout=120, clock=lambda: clock[0])
        self.assertFalse(result["completed"])
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertEqual(result["deleted"], [])
        self.assertEqual(len(world.writes()), 1)
        self.assertNotIn("delete_verified", world.output.getvalue())

    def test_verified_partial_progress_is_reported_and_later_run_finishes(self):
        world, clock, reads = World(0), [0], [0]
        def expire(index, method, path):
            if world.writes() and method == "GET":
                reads[0] += 1
                if reads[0] == 10:
                    clock[0] = 1000
        world.before_request = expire
        result = world.run(timeout=120, clock=lambda: clock[0])
        self.assertFalse(result["completed"])
        self.assertEqual(result["deleted"], [OLD_I])
        self.assertEqual(len(world.writes()), 1)
        world.before_request = None
        following = world.run()
        self.assertEqual(following["deleted"], [OLD])
        self.assertTrue(following["completed"])
        self.assertEqual(len(world.writes()), 2)

    def test_remote_recheck_failure_preserves_preceding_verified_event(self):
        world = World(0)
        def change(index, location, backup):
            if backup == OLD:
                world.remote["manual-copy"] = ""
        world.after_write = change
        with self.assertRaisesRegex(retention.BackupError, "metadata changed"):
            world.run()
        self.assertEqual(world.output.getvalue().count('"event": "delete_verified"'), 1)
        self.assertEqual(len(world.writes()), 2)

    def test_unapproved_paths_and_repeated_writes_are_rejected(self):
        world = World(0)
        cleaner = retention.RemoteCleaner(world.apis)
        cleaner.execute = True
        cleaner.allowed = {OLD_I}
        cleaner.pending = (0, "remote", OLD_I)
        cleaner.request(0, "POST", "/backup/delete/remote/" + OLD_I)
        cleaner.pending = (0, "remote", OLD_I)
        with self.assertRaisesRegex(retention.BackupError, "unexpected API request"):
            cleaner.request(0, "POST", "/backup/delete/remote/" + OLD_I)
        for method, path in (("POST", "/backup/actions"), ("DELETE", "/backup/delete/remote/" + OLD),
                             ("POST", "/backup/delete/remote/" + OLD + "?force=true")):
            with self.subTest(method=method, path=path), self.assertRaises(retention.BackupError):
                cleaner.request(0, method, path)
        self.assertEqual(len(world.writes()), 1)

    def test_execution_requires_latest_verified_name_and_legacy_mode_remains_preview_only(self):
        world = World()
        with self.assertRaisesRegex(retention.BackupError, "latest verified"):
            retention.RemoteCleaner(world.apis).run(NOW, execute=True)
        with self.assertRaisesRegex(retention.BackupError, "count-and-age supports preview only"):
            legacy.Cleaner(world.apis).run(NOW, execute=True)
        self.assertEqual(world.calls, [])

    def test_invalid_options_and_duplicate_copies_are_rejected(self):
        world = World()
        for options in (dict(timeout=0), dict(timeout=float("nan")), dict(timeout=True),
                        dict(daily_days=0), dict(history_days=6), dict(history_days=True)):
            with self.subTest(options=options), self.assertRaises(retention.BackupError):
                retention.RemoteCleaner(world.apis, **options)
        with self.assertRaises(retention.BackupError):
            retention.RemoteCleaner([world.apis[0], world.apis[0]])
        with self.assertRaises(retention.BackupError):
            retention.RemoteCleaner(world.apis).run(NOW, NEW_I, execute="true")
        self.assertEqual(world.calls, [])


if __name__ == "__main__":
    unittest.main()
