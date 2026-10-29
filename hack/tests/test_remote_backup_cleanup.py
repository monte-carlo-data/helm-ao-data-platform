"""Test complete-set retention and ordered deletion without contacting a service."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

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

    def test_thirtieth_prior_date_expires_without_a_newer_day_in_its_week(self):
        for age, retained in ((29, True), (30, False)):
            with self.subTest(age=age):
                result = self.plan({**group(0), **group(age)}, local=[{}])
                self.assertEqual(set(group(age)) <= set(result["kept"]), retained)
                self.assertEqual(set(group(age)) <= set(result["delete"]), not retained)

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

    def test_manual_remote_dependency_keeps_whole_set_without_local_records(self):
        result = self.plan({**BASE, "manual-copy": OLD}, local=[{}])
        self.assertEqual(result["delete"], [])
        self.assertEqual(set(result["kept"]), set(BASE) | {"manual-copy"})

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
    def __init__(self, local_copies=2, copies=2):
        self.remote = dict(BASE)
        self.local = [dict(BASE) if i < local_copies else {} for i in range(copies)]
        self.actions = [[] for _ in range(copies)]
        self.versions = [[{"version": "2.8.2"}] for _ in range(copies)]
        self.calls, self.apis = [], [API(self, i) for i in range(copies)]
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

    def test_short_timeout_still_allows_preview(self):
        world = World()
        result = world.run(execute=False, timeout=60, clock=lambda: 0)
        self.assertTrue(result["completed"])
        self.assertEqual(result["delete"], [OLD_I, OLD])
        self.assertEqual(world.writes(), [])

    def test_deletion_reserves_time_for_five_followup_reads_per_copy(self):
        for copies, reserve in ((1, 480), (2, 630), (3, 780)):
            for timeout in (60, reserve - 0.25, reserve):
                with self.subTest(copies=copies, timeout=timeout):
                    world = World(local_copies=copies, copies=copies)
                    result = world.run(timeout=timeout, clock=lambda: 0)
                    self.assertFalse(result["completed"])
                    self.assertEqual(result["stop_reason"], "time_limit")
                    self.assertEqual(result["deleted"], [])
                    self.assertEqual(result.get("unverified_deletions", []), [])
                    self.assertEqual(world.writes(), [])
                    self.assertEqual(world.remote, BASE)
                    self.assertEqual(world.local, [BASE] * copies)
                    self.assertNotIn("delete_requested", world.output.getvalue())

    def test_just_above_reserve_uses_fixed_delete_timeout_and_one_event_per_request(self):
        for copies, timeout in ((1, 480.25), (2, 630.25), (3, 780.25)):
            with self.subTest(copies=copies):
                world = World(local_copies=copies, copies=copies)
                result = world.run(timeout=timeout, clock=lambda: 0)
                self.assertTrue(result["completed"])
                self.assertEqual(result["deleted"], [OLD_I, OLD])
                self.assertTrue(all(call[3] == {"request_timeout": 300} for call in world.writes()))
                self.assertEqual(world.output.getvalue().count('"event": "delete_requested"'),
                                 len(world.writes()))

    def test_request_refused_before_sending_is_not_reported_as_running(self):
        from run_backup import RequestNotSent, RevisionError, RevisionCheckUnavailable
        for error in (RequestNotSent("connection refused; no request was sent"),
                      RevisionError("password changed; no request was sent"),
                      RevisionCheckUnavailable("password check unavailable; no request was sent")):
            world = World()
            original = world.apis[0].request
            def refused(method, path, **options):
                if method == "POST":
                    raise error
                return original(method, path, **options)
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(world.apis[0], "request", side_effect=refused), \
                    self.assertRaises(retention.CleanupStopped) as caught:
                world.run()
            self.assertEqual(caught.exception.result["unverified_deletions"], [])
            self.assertIn("no request was sent", str(caught.exception))
            self.assertNotIn("may still be running", str(caught.exception))
            self.assertEqual(world.writes(), [])
            self.assertIn('"event": "delete_not_sent"', world.output.getvalue())

    def test_deletion_also_reserves_request_guard_time(self):
        for timeout, completed in ((850, False), (851, True)):
            with self.subTest(timeout=timeout):
                world = World()
                for api in world.apis:
                    api.guard_timeout_seconds = 20
                result = world.run(timeout=timeout, clock=lambda: 0)
                self.assertEqual(result["completed"], completed)
                if completed:
                    self.assertEqual(result["deleted"], [OLD_I, OLD])
                    self.assertTrue(all(call[3] == {"request_timeout": 300} for call in world.writes()))
                    self.assertEqual(world.output.getvalue().count('"event": "delete_requested"'),
                                     len(world.writes()))
                else:
                    self.assertEqual(result["stop_reason"], "time_limit")
                    self.assertEqual(result["deleted"], [])
                    self.assertEqual(result.get("unverified_deletions", []), [])
                    self.assertEqual(world.writes(), [])
                    self.assertEqual(world.remote, BASE)
                    self.assertEqual(world.local, [BASE, BASE])
                    self.assertNotIn("delete_requested", world.output.getvalue())

    def test_time_limit_after_final_predelete_read_logs_no_request(self):
        world, clock = World(), [0]
        def expire(index, method, path):
            if len(world.calls) == 30:
                clock[0] = 1800
        world.before_request = expire
        result = world.run(timeout=1800, clock=lambda: clock[0])
        self.assertFalse(result["completed"])
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertEqual(result["deleted"], [])
        self.assertEqual(world.writes(), [])
        self.assertNotIn("delete_requested", world.output.getvalue())
        self.assertEqual(world.remote, BASE)
        self.assertEqual(world.local, [BASE, BASE])

    def test_reserved_time_covers_full_delete_and_followup_request_timeouts(self):
        world, clock = World(0), [0]
        world.after_write = lambda *args: clock.__setitem__(0, clock[0] + 300)
        def read_time(index, method, path):
            if world.writes() and method == "GET":
                clock[0] += 30
        world.before_request = read_time
        result = world.run(timeout=630.25, clock=lambda: clock[0])
        self.assertFalse(result["completed"])
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertEqual(result["deleted"], [OLD_I])
        self.assertEqual(result.get("unverified_deletions", []), [])
        self.assertEqual(len(world.writes()), 1)
        self.assertEqual(world.output.getvalue().count('"event": "delete_verified"'), 1)

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
        with self.assertRaisesRegex(retention.BackupError, "2.8.2 on every copy") as caught:
            world.run()
        self.assertIsInstance(caught.exception, retention.CleanupStopped)
        self.assertEqual(caught.exception.result["deleted"], [])
        self.assertEqual(caught.exception.result["stop_reason"], "error")
        self.assertEqual(caught.exception.result["unverified_deletions"], [
            {"copy": 0, "location": "local", "name": OLD_I, "response_status": "success"}])
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
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                    retention.BackupError, "metadata changed") as caught:
                world.run()
            self.assertIsInstance(caught.exception, retention.CleanupStopped)
            self.assertFalse(caught.exception.result["completed"])
            self.assertEqual(caught.exception.result["stop_reason"], "error")
            self.assertEqual(caught.exception.result["deleted"], [])
            self.assertEqual(caught.exception.result.get("unverified_deletions", []), [])
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
                self.assertIsInstance(caught.exception, retention.CleanupStopped)
                self.assertFalse(caught.exception.result["completed"])
                self.assertEqual(caught.exception.result["stop_reason"], "error")
                self.assertEqual(caught.exception.result["deleted"], [])
                self.assertEqual(caught.exception.result["unverified_deletions"], [
                    {"copy": 0, "location": "local" if copies else "remote",
                     "name": OLD_I, "response_status": "unknown"}])
                self.assertEqual(len(world.writes()), 1)
                self.assertNotIn("delete_verified", world.output.getvalue())
                self.assertTrue(set(group(0)) <= set(world.remote))

    def test_success_response_without_removal_stops(self):
        world = World()
        world.remove_target = False
        with self.assertRaisesRegex(retention.BackupError, "metadata changed") as caught:
            world.run()
        self.assertIsInstance(caught.exception, retention.CleanupStopped)
        self.assertEqual(caught.exception.result["deleted"], [])
        self.assertEqual(caught.exception.result["unverified_deletions"], [
            {"copy": 0, "location": "local", "name": OLD_I, "response_status": "success"}])
        self.assertEqual(len(world.writes()), 1)

    def test_time_limit_after_success_response_preserves_operation_without_claiming_verification(self):
        for copies in (0, 2):
            with self.subTest(local_copies=copies):
                world, clock = World(copies), [0]
                world.after_write = lambda *args: clock.__setitem__(0, 1800)
                with self.assertRaises(retention.CleanupStopped) as caught:
                    world.run(timeout=1800, clock=lambda: clock[0])
                self.assertIsInstance(caught.exception, retention.BackupError)
                result = caught.exception.result
                self.assertFalse(result["completed"])
                self.assertEqual(result["stop_reason"], "error")
                self.assertEqual(result["deleted"], [])
                self.assertEqual(result["unverified_deletions"], [
                    {"copy": 0, "location": "local" if copies else "remote",
                     "name": OLD_I, "response_status": "success"}])
                self.assertEqual(len(world.writes()), 1)
                self.assertNotIn("delete_verified", world.output.getvalue())
                self.assertEqual(OLD_I in world.remote, bool(copies))

    def test_verified_partial_progress_is_reported_and_later_run_finishes(self):
        world, clock, reads = World(0), [0], [0]
        def expire(index, method, path):
            if world.writes() and method == "GET":
                reads[0] += 1
                if reads[0] == 10:
                    clock[0] = 1800
        world.before_request = expire
        result = world.run(timeout=1800, clock=lambda: clock[0])
        self.assertFalse(result["completed"])
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertEqual(result["deleted"], [OLD_I])
        self.assertEqual(result.get("unverified_deletions", []), [])
        self.assertEqual(len(world.writes()), 1)
        world.before_request = None
        following = world.run()
        self.assertEqual(following["deleted"], [OLD])
        self.assertTrue(following["completed"])
        self.assertEqual(len(world.writes()), 2)

    def test_remote_recheck_failure_preserves_verified_progress_and_current_operation(self):
        world = World(0)
        def change(index, location, backup):
            if backup == OLD:
                world.remote["manual-copy"] = ""
        world.after_write = change
        with self.assertRaisesRegex(retention.BackupError, "metadata changed") as caught:
            world.run()
        self.assertIsInstance(caught.exception, retention.CleanupStopped)
        self.assertFalse(caught.exception.result["completed"])
        self.assertEqual(caught.exception.result["stop_reason"], "error")
        self.assertEqual(caught.exception.result["deleted"], [OLD_I])
        self.assertEqual(caught.exception.result["unverified_deletions"], [
            {"copy": 0, "location": "remote", "name": OLD, "response_status": "success"}])
        self.assertEqual(world.output.getvalue().count('"event": "delete_verified"'), 1)
        self.assertEqual(len(world.writes()), 2)

    def test_failed_later_predelete_check_preserves_progress_without_an_unverified_operation(self):
        for failure in ("busy", "changed", "unreachable"):
            with self.subTest(failure=failure):
                world, reads = World(0), [0]
                def fail(index, method, path):
                    if world.writes() and method == "GET":
                        reads[0] += 1
                        if reads[0] == 11:
                            if failure == "busy":
                                world.actions[1] = [{"status": "in progress", "command": "restore other"}]
                            elif failure == "changed":
                                world.remote["manual-copy"] = ""
                            else:
                                raise retention.BackupError("unreachable")
                world.before_request = fail
                with self.assertRaises(retention.CleanupStopped) as caught:
                    world.run()
                self.assertIsInstance(caught.exception, retention.BackupError)
                self.assertFalse(caught.exception.result["completed"])
                self.assertEqual(caught.exception.result["stop_reason"], "error")
                self.assertEqual(caught.exception.result["deleted"], [OLD_I])
                self.assertEqual(caught.exception.result.get("unverified_deletions", []), [])
                self.assertEqual(len(world.writes()), 1)

    def test_uncertain_later_deletion_preserves_earlier_verified_progress(self):
        world = World(0)
        def fail(index, location, backup):
            if backup == OLD:
                world.failure = retention.BackupError("private transport detail")
        world.after_write = fail
        with self.assertRaisesRegex(retention.BackupError, "not confirmed") as caught:
            world.run()
        self.assertIsInstance(caught.exception, retention.CleanupStopped)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(caught.exception.result["deleted"], [OLD_I])
        self.assertEqual(caught.exception.result["unverified_deletions"], [
            {"copy": 0, "location": "remote", "name": OLD, "response_status": "unknown"}])
        self.assertEqual(len(world.writes()), 2)

    def test_unexpected_error_preserves_progress_and_hides_private_details(self):
        for phase, response_status in (("request", "unknown"), ("followup", "success")):
            with self.subTest(phase=phase):
                world = World(0)
                def fail_write(index, location, backup):
                    if phase == "request" and backup == OLD:
                        raise TypeError("private failure details")
                def fail_read(index, method, path):
                    if phase == "followup" and method == "GET" and len(world.writes()) == 2:
                        raise TypeError("private failure details")
                world.after_write, world.before_request = fail_write, fail_read
                with self.assertRaises(retention.CleanupStopped) as caught:
                    world.run()
                self.assertIsInstance(caught.exception, retention.BackupError)
                self.assertIn("TypeError", str(caught.exception))
                self.assertNotIn("private", str(caught.exception))
                self.assertFalse(caught.exception.result["completed"])
                self.assertEqual(caught.exception.result["stop_reason"], "error")
                self.assertEqual(caught.exception.result["deleted"], [OLD_I])
                self.assertEqual(caught.exception.result["unverified_deletions"], [
                    {"copy": 0, "location": "remote", "name": OLD, "response_status": response_status}])
                self.assertEqual(len(world.writes()), 2)

    def test_unapproved_paths_and_repeated_writes_are_rejected(self):
        world = World(0)
        cleaner = retention.RemoteCleaner(world.apis)
        cleaner.execute = True
        cleaner.allowed = {OLD_I}
        cleaner.pending = (0, "remote", OLD_I)
        with redirect_stdout(io.StringIO()):
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
