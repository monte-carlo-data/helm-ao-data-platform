"""Exercise local retention and prove remote backups cannot be deleted."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import unittest

DIRECTORY = Path(__file__).resolve().parents[2] / "charts/ao-data-platform/files/clickhouse-backup"
sys.path.insert(0, str(DIRECTORY))
import cleanup_local_backups as cleanup
sys.path.pop(0)

NOW = datetime(2026, 9, 29, 20, tzinfo=timezone.utc)


def name(kind, age, suffix="12345678"):
    return f"ao-otel-{kind}-{NOW - timedelta(hours=age):%Y%m%dT%H%M%SZ}-{suffix}"


A, AI = name("full", 72), name("incremental", 68)
B, BI = name("full", 48), name("incremental", 44)
C, CI = name("full", 24), name("incremental", 20)
GROUPS = {A: "", AI: A, B: "", BI: B, C: "", CI: C}


def names(result, key, copy=0):
    return [entry["name"] for entry in result[key] if entry["copy"] == copy]


class SelectionTests(unittest.TestCase):
    def plan(self, remote=None, local=None, **kwargs):
        remote = dict(GROUPS) if remote is None else remote
        return cleanup.local_plan(remote, [dict(remote)] if local is None else local, NOW, **kwargs)

    def test_two_full_days_and_all_incrementals_are_kept(self):
        result = self.plan()
        self.assertEqual(set(names(result, "kept")), {B, BI, C, CI})
        self.assertEqual(names(result, "delete"), [AI, A])

    def test_duplicate_fulls_on_one_day_do_not_evict_previous_day(self):
        retry, later = name("full", 23), name("incremental", 19)
        result = self.plan({**GROUPS, retry: "", later: retry})
        self.assertEqual(set(names(result, "kept")), {B, BI, C, CI, retry, later})

    def test_distinct_successful_days_not_elapsed_time_or_individual_fulls(self):
        old, incremental = name("full", 240), name("incremental", 239)
        result = self.plan({old: "", incremental: old, C: "", CI: C})
        self.assertEqual(names(result, "delete"), [])
        self.assertEqual(len(names(result, "kept")), 4)

    def test_daily_boundary_uses_utc_date(self):
        day_two = name("full", 25)
        same_day = name("full", 24, "99999999")
        current = name("full", 1)
        result = self.plan({A: "", day_two: "", same_day: "", current: ""})
        self.assertEqual(set(names(result, "kept")), {day_two, same_day, current})
        self.assertEqual(names(result, "delete"), [A])

    def test_latest_verified_backup_and_all_ancestors_are_kept(self):
        nested = name("incremental", 66)
        result = self.plan({**GROUPS, nested: AI}, latest_backup=nested)
        self.assertEqual(names(result, "delete"), [])

    def test_manual_local_backup_keeps_its_older_local_bases(self):
        result = self.plan(local=[{**GROUPS, "manual-copy": AI}])
        self.assertEqual(names(result, "delete"), [])
        self.assertIn("manual-copy", names(result, "kept"))

    def test_manual_remote_only_backup_does_not_need_original_local_records(self):
        result = self.plan({**GROUPS, "manual-copy": AI}, local=[dict(GROUPS)])
        self.assertEqual(names(result, "delete"), [AI, A])

    def test_local_only_backups_and_their_bases_are_kept(self):
        extra = name("incremental", 64)
        result = self.plan(local=[{**GROUPS, extra: AI}])
        self.assertEqual(names(result, "delete"), [])
        self.assertEqual(result["local_only"], [{"copy": 0, "name": extra}])

    def test_broken_metadata_stops_all_deletion_without_logging_parser_details(self):
        result = self.plan(local=[{**GROUPS, AI: None}, dict(GROUPS)])
        self.assertEqual(result["delete"], [])
        self.assertEqual(result["broken_local"], [{"copy": 0, "name": AI}])

    def test_remote_only_backups_are_never_deletion_candidates(self):
        result = self.plan(local=[{A: "", B: "", C: ""}, {}])
        self.assertEqual(result["delete"], [{"copy": 0, "name": A}])

    def test_chain_deletes_dependents_before_bases(self):
        nested = name("incremental", 64)
        result = self.plan({**GROUPS, nested: AI})
        self.assertEqual(names(result, "delete"), [nested, AI, A])

    def test_missing_matching_remote_entry_is_preserved(self):
        result = self.plan({k: v for k, v in GROUPS.items() if k != AI}, local=[dict(GROUPS)])
        self.assertEqual(names(result, "delete"), [])
        self.assertEqual(result["local_only"], [{"copy": 0, "name": AI}])

    def test_invalid_remote_graph_and_names_stop_selection(self):
        cases = [{A: "missing"}, {AI: ""}, {A: "", AI: C, C: ""},
                 {"manual-a": "manual-b", "manual-b": "manual-a"},
                 {name("full", -1): ""}, {"ao-otel-full-20269999T000000Z-12345678": ""},
                 {"ao-otel-invalid": ""}]
        for remote in cases:
            with self.subTest(remote=remote), self.assertRaises(cleanup.BackupError):
                self.plan(remote)

    def test_invalid_local_graph_and_dependencies_stop_selection(self):
        for local in [{**GROUPS, "manual": "missing"}, {**GROUPS, AI: ""},
                      {**GROUPS, "ao-otel-invalid": ""},
                      {**GROUPS, "manual-a": "manual-b", "manual-b": "manual-a"}]:
            with self.subTest(local=local), self.assertRaises(cleanup.BackupError):
                self.plan(local=[local])

    def test_different_valid_local_and_remote_bases_stop_selection(self):
        # Both bases are old enough and present; only their disagreement is unsafe.
        with self.assertRaisesRegex(cleanup.BackupError, "dependencies disagree"):
            self.plan(local=[{**GROUPS, CI: B}])

    def test_invalid_options_and_missing_latest_stop_selection(self):
        for kwargs in [dict(keep_full_backup_days=1), dict(keep_full_backup_days=True),
                       dict(keep_full_backup_days=2.5), dict(latest_backup="missing")]:
            with self.subTest(kwargs=kwargs), self.assertRaises(cleanup.BackupError):
                self.plan(**kwargs)
        with self.assertRaises(cleanup.BackupError):
            cleanup.local_plan(GROUPS, [GROUPS], NOW.replace(tzinfo=None))

    def test_empty_and_one_day_catalogs_do_not_delete(self):
        self.assertEqual(self.plan({}, local=[{}])["delete"], [])
        self.assertEqual(self.plan({A: "", AI: A})["delete"], [])


class World:
    def __init__(self, copies=2):
        self.remote = dict(GROUPS)
        self.local = [dict(GROUPS) for _ in range(copies)]
        self.actions = [[] for _ in range(copies)]
        self.calls = []
        self.before_request = None
        self.after_delete = None
        self.delete_error = None
        self.delete_response = None
        self.remove_local = True
        self.remote_overrides = {}
        self.row_overrides = {}
        self.apis = [FakeAPI(self, i) for i in range(copies)]

    def run(self, execute=False, **kwargs):
        self.output = io.StringIO()
        with redirect_stdout(self.output):
            return cleanup.LocalCleaner(self.apis, **kwargs).run(NOW, latest_backup=CI, execute=execute)

    def writes(self):
        return [call for call in self.calls if call[1] != "GET"]


class FakeAPI:
    def __init__(self, world, index):
        self.world, self.index = world, index
        self.endpoint = f"http://copy{index}:7171"

    def request(self, method, path, **kwargs):
        world, index = self.world, self.index
        world.calls.append((index, method, path, kwargs))
        if world.before_request:
            world.before_request(index, method, path)
        if method == "POST":
            assert path.startswith(cleanup.DELETE_PREFIX), "No remote mutation or general action endpoint is allowed"
            backup = path.removeprefix(cleanup.DELETE_PREFIX)
            assert "?" not in path
            if world.remove_local:
                del world.local[index][backup]
            if world.after_delete:
                world.after_delete(index, backup)
            if world.delete_error:
                raise world.delete_error
            if world.delete_response is not None:
                return world.delete_response
            return [dict(status="success", operation="delete", backup_name=backup, location="local")]
        assert method == "GET" and path in cleanup.READ_PATHS
        if path == "/backup/actions":
            return [dict(row) for row in world.actions[index]]
        location = path.rsplit("/", 1)[1]
        entries = (world.remote_overrides.get(index, world.remote) if location == "remote"
                   else world.local[index])
        rows = []
        for backup, required in entries.items():
            row = dict(name=backup, required=required, location=location, metadata_size=100,
                       desc="directory, embedded" if location == "remote" else "embedded")
            row.update(world.row_overrides.get((index, location, backup), {}))
            rows.append(row)
        return rows


class OperationTests(unittest.TestCase):
    def test_preview_reads_only_and_does_not_change_either_copy_or_remote(self):
        world = World()
        result = world.run()
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["deleted"], [])
        self.assertEqual(world.writes(), [])
        self.assertEqual(world.remote, GROUPS)
        self.assertEqual(world.local, [GROUPS, GROUPS])

    def test_execution_removes_only_selected_local_files_on_every_copy(self):
        for count in (1, 2, 3):
            world = World(count)
            result = world.run(execute=True)
            self.assertEqual(result["mode"], "delete")
            self.assertEqual(result["deleted"], result["delete"])
            self.assertEqual(world.remote, GROUPS)
            for entries in world.local:
                self.assertEqual(set(entries), {B, BI, C, CI})
            self.assertEqual(len(world.writes()), 2 * count)
            self.assertTrue(all(call[3] == {"request_timeout": 300} for call in world.writes()))

    def test_every_copy_must_be_idle_including_during_pre_delete_recheck(self):
        for at_start in (True, False):
            world = World()
            reads = 0
            def busy(index, method, path):
                nonlocal reads
                if index == 1 and path == "/backup/actions":
                    reads += 1
                    if at_start or reads >= 5:
                        world.actions[index] = [dict(command="create_remote other", status="in progress")]
            world.before_request = busy
            with self.assertRaisesRegex(cleanup.BackupError, "busy"):
                world.run(execute=True)
            self.assertEqual(world.writes(), [])

    def test_failed_or_unknown_operation_history_blocks_deletion(self):
        for row in [dict(command="delete local other", status="error"),
                    dict(command="delete remote other", status="cancel"),
                    dict(command="create", status="unknown"), dict(status="success")]:
            world = World()
            world.actions[1] = [row]
            with self.subTest(row=row), self.assertRaises(cleanup.BackupError):
                world.run(execute=True)
            self.assertEqual(world.writes(), [])

    def test_remote_disagreement_stops_before_deletion(self):
        world = World()
        world.remote_overrides[1] = {k: v for k, v in GROUPS.items() if k != AI}
        with self.assertRaisesRegex(cleanup.BackupError, "disagree"):
            world.run(execute=True)
        self.assertEqual(world.writes(), [])

    def test_remote_change_or_disappearance_before_write_stops(self):
        for mutation in ("missing", "metadata"):
            world = World()
            reads = 0
            def change(index, method, path):
                nonlocal reads
                if index == 0 and path == "/backup/list/remote":
                    reads += 1
                    if reads == 3:
                        if mutation == "missing":
                            del world.remote[AI]
                        else:
                            for copy in range(2):
                                world.row_overrides[copy, "remote", AI] = {"metadata_size": 200}
            world.before_request = change
            with self.subTest(mutation=mutation), self.assertRaisesRegex(cleanup.BackupError, "metadata changed"):
                world.run(execute=True)
            self.assertEqual(world.writes(), [])

    def test_new_manual_dependent_before_write_stops(self):
        world = World()
        reads = 0
        def change(index, method, path):
            nonlocal reads
            if index == 0 and path == "/backup/list/local":
                reads += 1
                if reads == 3:
                    world.local[index]["manual"] = A
        world.before_request = change
        with self.assertRaisesRegex(cleanup.BackupError, "metadata changed"):
            world.run(execute=True)
        self.assertEqual(world.writes(), [])

    def test_wrong_or_uncertain_response_is_never_retried(self):
        for response in [[], [dict(status="acknowledged", operation_id="unknown")],
                         [dict(status="success", operation="delete", backup_name=AI, location="remote")]]:
            world = World()
            world.delete_response = response
            with self.subTest(response=response), self.assertRaisesRegex(cleanup.BackupError, "not confirmed"):
                world.run(execute=True)
            self.assertEqual(len(world.writes()), 1)
        world = World()
        world.delete_error = cleanup.BackupError("transport timeout with private response")
        with self.assertRaisesRegex(cleanup.BackupError, "may still be running") as caught:
            world.run(execute=True)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(world.writes()), 1)

    def test_logs_preserve_verified_deletions_before_a_later_failure(self):
        world = World()
        def fail_second(index, backup):
            if backup == A:
                world.delete_error = cleanup.BackupError("request failed")
        world.after_delete = fail_second
        with self.assertRaises(cleanup.BackupError):
            world.run(execute=True)
        rows = [json.loads(line.removeprefix("Backup local cleanup: "))
                for line in world.output.getvalue().splitlines()]
        self.assertEqual(rows, [
            {"event": "delete_requested", "copy": 0, "name": AI},
            {"event": "delete_verified", "copy": 0, "name": AI},
            {"event": "delete_requested", "copy": 0, "name": A},
        ])
        self.assertEqual(len(world.writes()), 2)

    def test_delete_that_did_not_remove_target_stops_further_deletion(self):
        world = World()
        world.remove_local = False
        with self.assertRaisesRegex(cleanup.BackupError, "metadata changed"):
            world.run(execute=True)
        self.assertEqual(len(world.writes()), 1)

    def test_remote_or_other_local_change_after_delete_stops_further_deletion(self):
        for change in ("remote", "local", "busy"):
            world = World()
            def after(index, backup):
                if change == "remote":
                    del world.remote[backup]
                elif change == "local":
                    del world.local[1][CI]
                else:
                    world.actions[1] = [dict(command="restore other", status="in progress")]
            world.after_delete = after
            with self.subTest(change=change), self.assertRaises(cleanup.BackupError):
                world.run(execute=True)
            self.assertEqual(len(world.writes()), 1)

    def test_broken_local_and_local_only_entries_do_not_get_deleted(self):
        for broken in (True, False):
            world = World()
            if broken:
                world.local[0][AI] = ""
                world.row_overrides[0, "local", AI] = {"desc": "broken metadata.json not found"}
            else:
                del world.remote[AI]
            result = world.run(execute=True)
            self.assertEqual(result["deleted"], [])
            self.assertEqual(world.writes(), [])

    def test_unhealthy_remote_cannot_authorize_local_deletion(self):
        world = World()
        for index in range(2):
            world.row_overrides[index, "remote", AI] = {"desc": "broken metadata.json not found"}
        with self.assertRaises(cleanup.BackupError):
            world.run(execute=True)
        self.assertEqual(world.writes(), [])

    def test_unreachable_copy_and_duplicate_or_unreadable_metadata_stop_deletion(self):
        from unittest import mock
        for response in (None, [None], [dict(name=A, required="", location="remote", desc="directory, embedded")] * 2):
            world = World()
            original = world.apis[1].request
            def invalid(method, path, **kwargs):
                if path == "/backup/list/remote":
                    return response
                return original(method, path, **kwargs)
            with mock.patch.object(world.apis[1], "request", side_effect=invalid):
                with self.subTest(response=response), self.assertRaises(cleanup.BackupError):
                    world.run(execute=True)
            self.assertEqual(world.writes(), [])
        world = World()
        with mock.patch.object(world.apis[1], "request", side_effect=cleanup.BackupError("unreachable")):
            with self.assertRaises(cleanup.BackupError):
                world.run(execute=True)
        self.assertEqual(world.writes(), [])

    def test_local_size_change_during_recheck_blocks_write(self):
        world = World()
        reads = 0
        def change(index, method, path):
            nonlocal reads
            if index == 0 and path == "/backup/list/local":
                reads += 1
                if reads == 3:
                    world.row_overrides[index, "local", AI] = {"metadata_size": 999}
        world.before_request = change
        with self.assertRaisesRegex(cleanup.BackupError, "metadata changed"):
            world.run(execute=True)
        self.assertEqual(world.writes(), [])

    def test_time_limit_stops_without_writes(self):
        world = World()
        with self.assertRaisesRegex(cleanup.BackupError, "time limit"):
            world.run(execute=True, timeout=30)
        self.assertEqual(world.writes(), [])

    def test_time_limit_reports_verified_progress_and_next_run_finishes(self):
        world = World()
        clock = [0]
        reads_after_delete = 0
        def expire_after_verification(index, method, path):
            nonlocal reads_after_delete
            if method == "GET" and world.writes():
                reads_after_delete += 1
                if reads_after_delete == 8:
                    clock[0] = 1000
        world.before_request = expire_after_verification
        result = world.run(execute=True, timeout=900, clock=lambda: clock[0])
        self.assertFalse(result["completed"])
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertEqual(result["deleted"], [{"copy": 0, "name": AI}])
        self.assertEqual(len(world.writes()), 1)
        self.assertEqual(world.remote, GROUPS)
        world.before_request = None
        following = world.run(execute=True)
        self.assertTrue(following["completed"])
        self.assertEqual(len(following["deleted"]), 3)
        self.assertEqual(len(world.writes()), 4)

    def test_time_limit_during_post_delete_check_does_not_claim_verified_removal(self):
        world = World()
        clock = [0]
        def expire(index, backup):
            clock[0] = 1000
        world.after_delete = expire
        with self.assertRaisesRegex(cleanup.BackupError, "prevented its follow-up checks"):
            world.run(execute=True, timeout=900, clock=lambda: clock[0])
        self.assertEqual(len(world.writes()), 1)
        self.assertNotIn("delete_verified", world.output.getvalue())

    def test_authorized_delete_cannot_be_resent_even_after_an_uncertain_response(self):
        for error in (None, cleanup.BackupError("uncertain transport result")):
            world = World()
            world.delete_error = error
            cleaner = cleanup.LocalCleaner(world.apis)
            cleaner.pending = (0, AI)
            with self.subTest(error=error):
                if error:
                    with self.assertRaisesRegex(cleanup.BackupError, "not confirmed"):
                        cleaner.request(0, "POST", cleanup.DELETE_PREFIX + AI)
                else:
                    cleaner.request(0, "POST", cleanup.DELETE_PREFIX + AI)
                with self.assertRaisesRegex(cleanup.BackupError, "unexpected API request"):
                    cleaner.request(0, "POST", cleanup.DELETE_PREFIX + AI)
                self.assertEqual(len(world.writes()), 1)

    def test_delete_requires_its_full_timeout_and_verification_time_on_every_copy(self):
        for copies, required in ((1, 450), (2, 570), (3, 690)):
            for timeout in (required - 1, required, required + 1):
                with self.subTest(copies=copies, timeout=timeout):
                    world = World(copies)
                    result = world.run(execute=True, timeout=timeout, clock=lambda: 0)
                    if timeout <= required:
                        self.assertFalse(result["completed"])
                        self.assertEqual(result["stop_reason"], "time_limit")
                        self.assertEqual(result["deleted"], [])
                        self.assertEqual(world.writes(), [])
                        self.assertNotIn("delete_requested", world.output.getvalue())
                    else:
                        self.assertTrue(result["completed"])
                        self.assertEqual(len(result["deleted"]), copies * 2)
                        self.assertTrue(all(call[3] == {"request_timeout": 300}
                                            for call in world.writes()))

    def test_limit_reached_immediately_before_delete_has_no_request_log(self):
        world = World()
        # Three snapshots (four reads per copy) have finished. Expire at the
        # next clock check, immediately before the first delete would be sent.
        result = world.run(execute=True, timeout=900,
                           clock=lambda: 900 if len(world.calls) == 24 else 0)
        self.assertFalse(result["completed"])
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertEqual(world.writes(), [])
        self.assertNotIn("delete_requested", world.output.getvalue())

    def test_near_limit_runs_pause_or_finish_without_unverified_deletions(self):
        outcomes = set()
        for timeout in (*range(60, 200), *range(600, 751)):
            with self.subTest(timeout=timeout):
                world = World()
                clock = [0]
                def advance(index, method, path):
                    clock[0] += 1
                world.before_request = advance
                result = world.run(execute=True, timeout=timeout, clock=lambda: clock[0])
                outcomes.add((result["completed"], bool(result["deleted"])))
                self.assertEqual(len(result["deleted"]), len(world.writes()))
                events = [json.loads(line.removeprefix("Backup local cleanup: "))
                          for line in world.output.getvalue().splitlines()]
                self.assertEqual(sum(row["event"] == "delete_requested" for row in events),
                                 len(world.writes()))
                self.assertEqual(sum(row["event"] == "delete_verified" for row in events),
                                 len(world.writes()))
                self.assertEqual(world.remote, GROUPS)
        self.assertEqual(outcomes, {(False, False), (False, True), (True, True)})

    def test_slow_delete_and_follow_up_reads_fit_the_reserved_time(self):
        world = World()
        clock = [0]
        def advance(index, method, path):
            if method == "POST":
                clock[0] += 299
            elif world.writes():
                clock[0] += 29
            else:
                clock[0] += 1
        world.before_request = advance
        result = world.run(execute=True, timeout=600, clock=lambda: clock[0])
        self.assertFalse(result["completed"])
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertEqual(result["deleted"], [{"copy": 0, "name": AI}])
        self.assertEqual(len(world.writes()), 1)
        self.assertEqual(world.remote, GROUPS)

    def test_direct_writes_and_invalid_options_are_rejected(self):
        world = World()
        cleaner = cleanup.LocalCleaner(world.apis)
        for method, path in [("POST", cleanup.DELETE_PREFIX + A), ("POST", "/backup/actions"),
                             ("POST", "/backup/delete/remote/" + A), ("DELETE", cleanup.DELETE_PREFIX + A),
                             ("GET", "/backup/status")]:
            with self.subTest(method=method, path=path), self.assertRaises(cleanup.BackupError):
                cleaner.request(0, method, path)
        self.assertEqual(world.calls, [])
        with self.assertRaises(cleanup.BackupError):
            cleaner.run(NOW, execute="true")
        with self.assertRaises(cleanup.BackupError):
            cleanup.LocalCleaner([world.apis[0], world.apis[0]])
        for options in [dict(timeout=-1), dict(timeout=True), dict(timeout=float("nan")),
                        dict(timeout=float("inf")), dict(keep_full_backup_days=1)]:
            with self.assertRaises(cleanup.BackupError):
                cleanup.LocalCleaner(world.apis, **options)


if __name__ == "__main__":
    unittest.main()
