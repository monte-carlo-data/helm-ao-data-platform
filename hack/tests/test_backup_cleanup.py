"""Exercise retention previews and prove all execution paths refuse writes."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import subprocess
from unittest import mock
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
import run_backup as backup
sys.path.pop(0)
NOW = datetime(2026, 9, 29, 20, tzinfo=timezone.utc)


def name(kind, age, suffix="12345678"):
    return f"ao-otel-{kind}-{NOW - timedelta(hours=age):%Y%m%dT%H%M%SZ}-{suffix}"


A, AI = name("full", 72), name("incremental", 68)
B, BI = name("full", 48), name("incremental", 44)
C, CI = name("full", 24), name("incremental", 20)
THREE_GROUPS = {A: "", AI: A, B: "", BI: B, C: "", CI: C}


class SelectionTests(unittest.TestCase):
    def test_scheduler_names_are_recognized_by_cleanup_and_full_base_selection(self):
        from test_backup_scheduler import FakeAPI as SchedulerAPI, FakeProbe
        api = SchedulerAPI()
        scheduler = backup.Scheduler([api], [FakeProbe()])
        with redirect_stdout(io.StringIO()):
            full = scheduler.run(NOW)
            api.created_required = full
            incremental = scheduler.run(NOW + timedelta(hours=1))
        self.assertEqual(cleanup.scheduled_time(full, NOW), NOW)
        self.assertEqual(cleanup.scheduled_time(incremental, NOW + timedelta(hours=1)),
                         NOW + timedelta(hours=1))
        self.assertEqual(backup.full_base(api.catalog, NOW + timedelta(hours=1)), full)
        self.assertEqual(set(cleanup.plan({full: "", incremental: full},
                                         NOW + timedelta(hours=1))["kept"]), {full, incremental})

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
        cases = [({A: "missing"}, "required base is missing"),
                 ({A: CI, CI: A}, "scheduled backup has an invalid dependency"),
                 ({A: "", AI: ""}, "scheduled backup has an invalid dependency"),
                 ({A: "", AI: C, C: ""}, "scheduled backup has an unexpected base"),
                 ({AI: "manual-base", "manual-base": ""}, "scheduled backup has an unexpected base"),
                 ({"manual-one": "manual-two", "manual-two": "manual-one"}, "dependencies contain a cycle")]
        for entries, message in cases:
            with self.subTest(entries=entries), self.assertRaisesRegex(cleanup.BackupError, message):
                cleanup.plan(entries, NOW)

    def test_empty_and_single_backup_catalogs(self):
        self.assertEqual(cleanup.plan({}, NOW), {"keep_last": 2, "keep_days": 0, "kept": [], "delete": []})
        self.assertEqual(cleanup.plan({A: ""}, NOW)["kept"], [A])
        self.assertEqual(cleanup.plan({A: ""}, NOW)["delete"], [])

    def test_non_utc_aware_time_is_rejected(self):
        with self.assertRaisesRegex(cleanup.BackupError, "current time in UTC"):
            cleanup.plan(THREE_GROUPS, NOW.astimezone(timezone(timedelta(hours=1))))

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
    def __init__(self, copies=2):
        self.remote = dict(THREE_GROUPS)
        self.local = [dict(THREE_GROUPS) for _ in range(copies)]
        self.calls = []
        self.actions = [[] for _ in range(copies)]
        self.before_read = None
        self.apis = [FakeAPI(self, index) for index in range(copies)]

    def run(self, **kwargs):
        return cleanup.Cleaner(self.apis, **kwargs).run(NOW, latest_backup=CI)


class FakeAPI:
    def __init__(self, world, index):
        self.world, self.index = world, index
        self.endpoint = f"http://copy{index}:7171"
        self.unavailable = False
        self.broken_local = False
        self.broken_remote = False
        self.remote_override = None
        self.local_descriptions = {}

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
            raise AssertionError(f"Preview read an unexpected endpoint: {path}")
        location = path.rsplit("/", 1)[1]
        entries = (self.remote_override if self.remote_override is not None else world.remote) if location == "remote" else world.local[index]
        desc = "directory, embedded" if location == "remote" else "embedded"
        if (location == "remote" and self.broken_remote) or (location == "local" and self.broken_local):
            desc = "private failure details"
        return [dict(name=n, required=r, location=location,
                     desc=self.local_descriptions.get(n, desc) if location == "local" else desc)
                for n, r in entries.items()]


class PreviewTests(unittest.TestCase):
    def test_preview_only_reads_and_reports_retention(self):
        for copies in (1, 2, 3):
            with self.subTest(copies=copies):
                world = World(copies)
                before = (dict(world.remote), [dict(entries) for entries in world.local])
                result = world.run()
                self.assertEqual(result["mode"], "dry-run")
                self.assertEqual(result["deleted"], [])
                self.assertEqual(result["broken_local"], [])
                self.assertEqual(set(result["kept"]), {C, CI})
                self.assertEqual(set(result["delete"]), {A, AI, B, BI})
                self.assertEqual(set(world.calls), {
                    (index, "GET", path) for index in range(copies)
                    for path in ("/backup/actions", "/backup/list/remote", "/backup/list/local")})
                self.assertEqual((world.remote, world.local), before)

    def test_every_copy_must_be_readable_and_idle(self):
        for copies in (1, 3):
            for index in range(copies):
                for failure in ("unavailable", "broken_remote", "broken_local", "busy", "failed_delete"):
                    with self.subTest(copies=copies, index=index, failure=failure):
                        world = World(copies)
                        if failure == "busy":
                            world.actions[index] = [dict(command="create_remote other", status="in progress")]
                        elif failure == "failed_delete":
                            world.actions[index] = [dict(command="delete remote " + A, status="error")]
                        else:
                            setattr(world.apis[index], failure, True)
                        with self.assertRaises(cleanup.BackupError):
                            world.run()

    def test_third_copy_local_files_are_checked_and_reported(self):
        world = World(3)
        world.local[2][AI] = ""
        with self.assertRaisesRegex(cleanup.BackupError, "dependencies disagree"):
            world.run()
        world.apis[2].local_descriptions[AI] = "broken metadata.json not found"
        del world.remote[AI]
        del world.local[0][AI]
        del world.local[1][AI]
        result = world.run()
        self.assertEqual(result["broken_local"], [{"copy": 2, "name": AI}])
        self.assertEqual(result["local_only"], [{"copy": 2, "name": AI}])
        self.assertEqual(result["deleted"], [])

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
        for path in ("/restart", "/backup/kill", "/backup/watch", "/backup/list/remote?unexpected=1"):
            with self.subTest(path=path), self.assertRaisesRegex(cleanup.BackupError, "preview only"):
                cleaner.request(0, "GET", path)
        self.assertEqual(world.calls, [])

    def test_remote_only_backup_can_be_previewed_without_deleting_it(self):
        world = World()
        for entries in world.local:
            del entries[AI]
        self.assertIn(AI, world.run()["delete"])
        self.assertIn(AI, world.remote)

    def test_owned_local_only_entries_are_reported_without_writes(self):
        for description in ("embedded", "broken metadata.json not found", "parse metadata.json error: private detail"):
            world = World()
            del world.remote[AI]
            world.apis[1].local_descriptions[AI] = description
            if description != "embedded":
                world.local[1][AI] = ""
            with self.subTest(description=description):
                result = world.run()
                self.assertEqual(result["local_only"], [{"copy": 0, "name": AI}, {"copy": 1, "name": AI}])
                self.assertNotIn(AI, result["delete"])
                self.assertEqual(result["deleted"], [])
                self.assertNotIn("private", str(result))
                self.assertTrue(all(method == "GET" for _, method, _ in world.calls))

    def test_foreign_local_only_entry_stops_preview(self):
        world = World()
        world.local[1]["manual-backup"] = ""
        with self.assertRaisesRegex(cleanup.BackupError, "unrelated local metadata has no matching remote"):
            world.run()

    def test_local_and_remote_dependency_mismatch_stops_preview(self):
        world = World()
        world.local[1][AI] = ""
        with self.assertRaisesRegex(cleanup.BackupError, "local and remote backup dependencies disagree"):
            world.run()

    def test_broken_metadata_or_unavailable_copy_stops_preview(self):
        for attribute in ("broken_local", "broken_remote", "unavailable"):
            world = World()
            setattr(world.apis[1], attribute, True)
            with self.subTest(attribute=attribute), self.assertRaises(cleanup.BackupError) as caught:
                world.run()
            self.assertNotIn("private", str(caught.exception))

    def test_known_incomplete_local_files_are_reported_with_healthy_remote_backup(self):
        for description in ("broken metadata.json not found", "parse metadata.json error: private parser details"):
            world = World()
            world.apis[1].local_descriptions[AI] = description
            world.local[1][AI] = ""  # Broken metadata cannot tell us its base.
            with self.subTest(description=description):
                result = world.run()
                self.assertEqual(result["broken_local"], [{"copy": 1, "name": AI}])
                self.assertEqual(set(result["delete"]), {A, AI, B, BI})
                self.assertEqual(result["deleted"], [])
                self.assertNotIn("private", str(result))
                self.assertTrue(all(method == "GET" for _, method, _ in world.calls))

    def test_broken_remote_entry_stops_preview_even_with_known_local_residue(self):
        world = World()
        world.apis[1].local_descriptions[AI] = "broken metadata.json not found"
        world.local[1][AI] = ""
        world.apis[0].broken_remote = True
        with self.assertRaisesRegex(cleanup.BackupError, "metadata is broken"):
            world.run()

    def test_local_entry_repaired_during_preview_is_detected(self):
        world = World()
        world.apis[0].local_descriptions[AI] = "broken metadata.json not found"
        world.local[0][AI] = ""
        reads = 0

        def repair_during_read(index, path):
            nonlocal reads
            if index == 0 and path == "/backup/list/local":
                reads += 1
                if reads == 2:
                    world.apis[0].local_descriptions.clear()
                    world.local[0][AI] = A

        world.before_read = repair_during_read
        with self.assertRaisesRegex(cleanup.BackupError, "changed"):
            world.run()

    def test_disagreeing_remote_catalogues_stop_preview(self):
        for copies in (2, 3):
            with self.subTest(copies=copies):
                world = World(copies)
                world.apis[-1].remote_override = {C: "", CI: C}
                with self.assertRaisesRegex(cleanup.BackupError, "disagree"):
                    world.run()

    def test_older_active_action_is_found_even_when_last_action_succeeded(self):
        world = World()
        world.actions[1] = [dict(command="create_remote ongoing", status="in progress"),
                            dict(command="list remote", status="success")]
        with self.assertRaisesRegex(cleanup.BackupError, "busy"):
            world.run()

    def test_failed_deletion_history_requires_inspection(self):
        for status in ("error", "cancel"):
            world = World()
            world.actions[1] = [dict(command="delete local " + AI, status=status)]
            with self.subTest(status=status), self.assertRaisesRegex(cleanup.BackupError, "previous deletion"):
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

    def test_local_change_after_snapshot_stops_preview(self):
        world = World()
        reads = 0

        def change_after_snapshot(index, path):
            nonlocal reads
            if index == 1 and path == "/backup/list/local":
                reads += 1
                if reads == 2:
                    del world.local[1][AI]

        world.before_read = change_after_snapshot
        with self.assertRaisesRegex(cleanup.BackupError, "backup metadata changed during cleanup"):
            world.run()

    def test_single_and_third_copy_changes_stop_preview(self):
        for copies in (1, 3):
            for location in ("remote", "local"):
                with self.subTest(copies=copies, location=location):
                    world = World(copies)
                    reads = 0

                    def change_after_snapshot(index, path):
                        nonlocal reads
                        if index == copies - 1 and path == "/backup/list/" + location:
                            reads += 1
                            if reads == 2:
                                if location == "remote":
                                    world.apis[index].remote_override = {**world.remote, name("full", 0): ""}
                                else:
                                    del world.local[index][AI]

                    world.before_read = change_after_snapshot
                    with self.assertRaisesRegex(cleanup.BackupError, "backup metadata changed during cleanup"):
                        world.run()

    def test_action_started_during_snapshot_stops_before_selection(self):
        world = World()

        def start_action(index, path):
            if index == 1 and path == "/backup/list/local":
                world.actions[0] = [dict(command="create_remote other", status="in progress")]

        world.before_read = start_action
        with mock.patch.object(cleanup, "plan") as selection:
            with self.assertRaisesRegex(cleanup.BackupError, "copy is busy"):
                world.run()
        selection.assert_not_called()

    def test_action_started_during_final_catalog_read_stops_report(self):
        for copies in (1, 2, 3):
            with self.subTest(copies=copies):
                world = World(copies)
                reads = 0

                def start_action(index, path):
                    nonlocal reads
                    if index == copies - 1 and path == "/backup/list/local":
                        reads += 1
                        if reads == 2:
                            world.actions[index] = [dict(command="create_remote concurrent", status="in progress")]

                world.before_read = start_action
                with self.assertRaisesRegex(cleanup.BackupError, "copy is busy"):
                    world.run()

    def test_deadline_leaves_room_for_a_request(self):
        world = World()
        with self.assertRaisesRegex(cleanup.BackupError, "time limit"):
            world.run(timeout=29)
        self.assertEqual(world.calls, [])

    def test_nonempty_distinct_copies_and_boolean_execute_are_required(self):
        world = World(3)
        with self.assertRaisesRegex(cleanup.BackupError, "at least one"):
            cleanup.Cleaner([])
        for copies in ([world.apis[0], world.apis[0]],
                       [world.apis[0], world.apis[1], world.apis[0]],
                       [world.apis[0], world.apis[1], world.apis[1]]):
            with self.subTest(copies=copies), self.assertRaisesRegex(cleanup.BackupError, "different"):
                cleanup.Cleaner(copies)
        for duplicate in (0, 1):
            world.apis[2].endpoint = world.apis[duplicate].endpoint
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(cleanup.BackupError, "different"):
                cleanup.Cleaner(world.apis)
        del world.apis[2].endpoint
        with self.assertRaisesRegex(cleanup.BackupError, "explicit boolean"):
            cleanup.Cleaner(world.apis).run(NOW, latest_backup=CI, execute="false")
        self.assertEqual(world.calls, [])


class MainCleanupTests(unittest.TestCase):
    def run_main(self, world, **settings):
        output, errors = io.StringIO(), io.StringIO()
        env = dict(BACKUP_CLEANUP_ENABLED="true", BACKUP_KEEP_LAST="1", BACKUP_KEEP_DAYS="3")
        env.update(settings)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.dict(sys.modules, {"cleanup_backups": cleanup}), \
                mock.patch.object(backup, "configured_clients", return_value=(world.apis, [object() for _ in world.apis])), \
                mock.patch.object(backup.Scheduler, "run", return_value=CI), \
                mock.patch.object(cleanup, "datetime", wraps=datetime) as dates, \
                mock.patch.object(sys, "path", [str(DIRECTORY)] + sys.path), \
                redirect_stdout(output), redirect_stderr(errors):
            dates.now.return_value = NOW
            status = backup.main()
        return status, output.getvalue(), errors.getvalue()

    def test_main_uses_real_cleaner_and_distinct_keep_settings(self):
        status, output, errors = self.run_main(World())
        self.assertEqual(status, 0)
        self.assertEqual(errors, "")
        line, *entries = output.splitlines()
        self.assertTrue(line.startswith("Backup cleanup: "))
        result = json.loads(line.removeprefix("Backup cleanup: "))
        self.assertEqual(result["keep_last"], 1)
        self.assertEqual(result["keep_days"], 3)
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["local_only_count"], 0)
        self.assertEqual(result["kept_count"], 6)
        self.assertEqual(len(entries), 6)

    def test_real_cleaner_error_keeps_verified_backup_successful(self):
        for copies in (1, 2, 3):
            with self.subTest(copies=copies):
                world = World(copies)
                world.apis[-1].unavailable = True
                status, output, errors = self.run_main(world)
                self.assertEqual(status, 0)
                self.assertEqual(output, "")
                self.assertEqual(errors, "Backup cleanup preview stopped: Backup API could not be reached.\n")
                self.assertNotIn("Backup failed", errors)
                self.assertNotIn("Traceback", errors)

    def test_real_residue_is_reported_without_failing_backup(self):
        world = World()
        del world.remote[AI]
        status, output, errors = self.run_main(world)
        self.assertEqual(status, 0)
        self.assertIn('"list": "local_only"', output)
        self.assertIn("local files needing attention", errors)
        self.assertNotIn("Backup failed", errors)

    def test_large_report_keeps_lines_short_and_every_name_in_order(self):
        remote = {}
        for day in range(90):
            full = name("full", day * 24 + 4)
            remote[full] = ""
            for hour in (0, 1, 2, 3):
                remote[name("incremental", day * 24 + hour)] = full
        result = cleanup.plan(remote, NOW)
        result.update(mode="dry-run", deleted=[], broken_local=[], local_only=[])
        output = io.StringIO()
        with redirect_stdout(output):
            backup.report_cleanup(result)
        lines = output.getvalue().splitlines()
        self.assertTrue(all(len(line.encode()) < 1024 for line in lines))
        summary = json.loads(lines[0].removeprefix("Backup cleanup: "))
        entries = [json.loads(line.removeprefix("Backup cleanup entry: ")) for line in lines[1:]]
        for key in ("kept", "delete"):
            self.assertEqual([entry["name"] for entry in entries if entry["list"] == key], result[key])
            self.assertEqual(summary[key + "_count"], len(result[key]))
        self.assertEqual(summary["deleted_count"], 0)

    def test_bad_cleanup_settings_do_not_relabel_verified_backup(self):
        for settings in ({"BACKUP_KEEP_LAST": "bad"}, {"BACKUP_KEEP_LAST": "0"},
                         {"BACKUP_KEEP_DAYS": "-1"}, {"BACKUP_CLEANUP_TIMEOUT_SECONDS": "0"}):
            with self.subTest(settings=settings):
                status, output, errors = self.run_main(World(), **settings)
                self.assertEqual(status, 0)
                self.assertEqual(output, "")
                self.assertTrue(errors.startswith("Backup cleanup preview stopped: "))
                self.assertNotIn("Backup failed", errors)

    def test_script_entry_point_runs_real_backup_and_cleaner(self):
        # Replace only file/network boundaries. runpy executes the actual
        # __main__ block, Scheduler, API parsing and Cleaner in a fresh process.
        program = """
import runpy
import sys
from unittest import mock
from test_backup_cleanup import EntryPointTransport
transport = EntryPointTransport()
with mock.patch("urllib.request.build_opener", return_value=transport), \
     mock.patch("ssl.create_default_context"), \
     mock.patch("pathlib.Path.read_text", autospec=True,
                side_effect=lambda path: "one" if path.name == "revision" else "fake-password"):
    runpy.run_path(sys.argv[1], run_name="__main__")
"""
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).parent), str(DIRECTORY)]),
                   POD_NAMESPACE="test",
                   BACKUP_PASSWORD_REVISION="one", BACKUP_CLEANUP_ENABLED="true",
                   BACKUP_CLEANUP_DRY_RUN="true", BACKUP_KEEP_DAYS="3")
        for copies in (1, 2, 3):
            for keep_last in ("1", "0"):
                with self.subTest(copies=copies, keep_last=keep_last):
                    result = subprocess.run([sys.executable, "-c", program, str(DIRECTORY / "run_backup.py")],
                                            env=dict(env, BACKUP_KEEP_LAST=keep_last,
                                                     BACKUP_ENDPOINTS=json.dumps([f"http://copy{i}:7171" for i in range(copies)]),
                                                     BACKUP_DATABASE_ENDPOINTS=json.dumps([f"http://copy{i}:8123" for i in range(copies)]),
                                                     BACKUP_POD_NAMES=json.dumps([f"copy{i}" for i in range(copies)])),
                                            capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Verified completed full backup", result.stdout)
                    if keep_last == "1":
                        self.assertIn('"keep_last": 1', result.stdout)
                        self.assertIn('"keep_days": 3', result.stdout)
                        self.assertEqual(result.stderr, "")
                    else:
                        self.assertIn("Backup cleanup preview stopped: Cleanup needs a positive keep_last", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertNotIn("Backup failed", result.stderr)


class EntryPointTransport:
    """Fake file/network boundaries for the fresh-process entry-point test."""
    def __init__(self):
        self.names = []

    def open(self, request, timeout):
        from urllib.parse import parse_qs, urlsplit
        url = urlsplit(request.full_url)
        if url.hostname == "kubernetes.default.svc":
            payload = {"metadata": {"name": url.path.rsplit("/", 1)[1],
                                   "annotations": {backup.REVISION_ANNOTATION: "one"}}}
            return io.BytesIO(json.dumps(payload).encode())
        if url.port == 8123:
            rows = [dict(database="otel_traces", table="spans", engine="ReplicatedMergeTree",
                         replica_table="spans", is_readonly=0, is_session_expired=0, absolute_delay=0)]
        elif url.path == "/backup/tables":
            rows = [dict(database="otel_traces", table="spans")]
        elif url.path == "/backup/actions":
            rows = []
        elif url.path == "/backup/create_remote":
            self.names.append(parse_qs(url.query)["name"][0])
            rows = [dict(status="acknowledged", operation="create_remote", backup_name=self.names[-1], operation_id="operation")]
        elif url.path == "/backup/status":
            rows = [dict(status="success", operation_id="operation")]
        elif url.path in ("/backup/list/local", "/backup/list/remote"):
            location = url.path.rsplit("/", 1)[1]
            desc = "embedded" if location == "local" else "directory, embedded"
            rows = [dict(name=name, location=location, required="", desc=desc) for name in self.names]
        else:
            raise AssertionError(request.full_url)
        return io.BytesIO("\n".join(json.dumps(row) for row in rows).encode())


class CleanupChartTests(unittest.TestCase):
    def test_cleanup_is_explicit_dry_run_and_shares_backup_job(self):
        from test_backup_chart import render, one
        docs = render("clickhouse.backup.cleanup.enabled=true", "clickhouse.backup.cleanup.keepLast=3",
                      "clickhouse.backup.cleanup.keepDays=7", "clickhouse.backup.cleanup.timeoutSeconds=900")
        self.assertEqual(len([doc for doc in docs if doc["kind"] == "CronJob"]), 1)
        job = one(docs, "CronJob", "otel-backup")["spec"]["jobTemplate"]["spec"]
        env = {item["name"]: item.get("value") for item in job["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual({key: value for key, value in env.items()
                          if key.startswith(("BACKUP_CLEANUP_", "BACKUP_KEEP_"))}, {
            "BACKUP_CLEANUP_ENABLED": "true", "BACKUP_CLEANUP_DRY_RUN": "true",
            "BACKUP_KEEP_LAST": "3", "BACKUP_KEEP_DAYS": "7", "BACKUP_CLEANUP_TIMEOUT_SECONDS": "900"})
        self.assertEqual(job["activeDeadlineSeconds"], 10800 + 900 + 60)
        data = one(docs, "ConfigMap", "otel-backup-job")["data"]
        for script in ("run_backup.py", "backup_common.py", "cleanup_backups.py"):
            self.assertEqual(data[script].rstrip(), (DIRECTORY / script).read_text().rstrip())
            compile(data[script], script, "exec")

    def test_bad_cleanup_settings_are_rejected_with_specific_messages(self):
        from test_backup_chart import render
        cases = [("clickhouse.backup.enabled=false", "cleanup requires clickhouse.backup.enabled")]
        for key, minimum, invalid in (("keepLast", 1, ("0", "-1", "two", "1.5", "true")),
                                      ("keepDays", 0, ("-1", "30d", "0.5", "false")),
                                      ("timeoutSeconds", 60, ("0", "59", "60s", "60.5", "true"))):
            cases.extend((f"clickhouse.backup.cleanup.{key}={value}",
                          f"cleanup.{key} must be an integer >= {minimum}") for value in invalid)
        for setting, message in cases:
            with self.subTest(setting=setting), self.assertRaisesRegex(AssertionError, message):
                render("clickhouse.backup.cleanup.enabled=true", setting)

    def test_minimum_integer_settings_render(self):
        from test_backup_chart import render, one
        docs = render("clickhouse.backup.cleanup.enabled=true", "clickhouse.backup.cleanup.keepLast=1",
                      "clickhouse.backup.cleanup.keepDays=0", "clickhouse.backup.cleanup.timeoutSeconds=60")
        deadline = one(docs, "CronJob", "otel-backup")["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]
        self.assertEqual(deadline, 10800 + 60 + 60)

    def test_deletion_is_rejected_at_chart_render(self):
        from test_backup_chart import render
        for value in ("false", "not-boolean", "1"):
            with self.subTest(value=value), self.assertRaisesRegex(
                    AssertionError, "deletion is unavailable.*dryRun must remain true"):
                render("clickhouse.backup.cleanup.enabled=true", "clickhouse.backup.cleanup.dryRun=" + value)

    def test_cleanup_enabled_requires_a_boolean(self):
        from test_backup_chart import CHART, HELM
        for value in ("true", "false"):
            with self.subTest(value=value):
                result = subprocess.run([HELM, "template", "cleanup-test", str(CHART),
                                         "-f", str(CHART / "ci/lint-values.yaml"),
                                         "-f", str(CHART / "ci/backup-values.yaml"),
                                         "--set-string", "clickhouse.backup.cleanup.enabled=" + value],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("clickhouse.backup.cleanup.enabled must be a boolean", result.stderr)


if __name__ == "__main__":
    unittest.main()
