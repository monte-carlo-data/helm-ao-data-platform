"""Opt-in local cleanup checks with stock ClickHouse, Altinity, and MinIO.

RUN_BACKUP_RUNTIME=1 HELM=/path/to/helm python3 -m unittest discover \
    -s hack/tests/runtime -p 'test*runtime.py' -v

All data and credentials are generated here. No ports are published, and the
containers share a Docker network namespace with no external network access.
Only the containers and volumes created by this test are removed.
"""

import ast
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
import re
from types import SimpleNamespace
import unittest

import yaml

if __package__:
    from . import test_backup_runtime as runtime
else:
    import test_backup_runtime as runtime


class AlternatePortDatabase(runtime.Database):
    """Run a second database in the same isolated network as the S3 emulator."""

    def __init__(self, *args, tcp_port=9000, http_port=8123, **kwargs):
        self.tcp_port = tcp_port
        super().__init__(*args, **kwargs)
        generation = (self.root / "config" / "..data").resolve()
        config = {path.name: path.read_text() for path in generation.iterdir()}
        settings = runtime.BASE.replace("<http_port>8123</http_port>",
                                        f"<http_port>{http_port}</http_port>")
        settings = settings.replace("</clickhouse>",
                                    f"<tcp_port>{tcp_port}</tcp_port>"
                                    f"<interserver_http_port>{tcp_port + 9}</interserver_http_port>"
                                    "<mysql_port remove=\"remove\"/>"
                                    "<postgresql_port remove=\"remove\"/>"
                                    "</clickhouse>")
        config["runtime.xml"] = settings
        runtime.project(self.root / "config", config)

    def query(self, query="SELECT 1", user="admin", password=None):
        password = self.passwords[user] if password is None else password
        return runtime.docker("exec", self.name, "clickhouse-client", "--port", str(self.tcp_port),
                              "--user", user, "--password", password, "--query", query,
                              check=False, timeout=30)


class LocalBackupCleanupRuntimeHelpers(runtime.BackupRuntimeHelpers):
    """Two-copy fixtures shared by local and remote cleanup checks."""
    def failover_actions(self, rows):
        # Execute only the documented helper's history parser, never its live
        # backup test. Feed it histories returned by the actual stock server.
        path = runtime.CHART.parents[1] / "hack/verify-backup-failover.py"
        module = ast.parse(path.read_text())
        script = next(ast.literal_eval(node.value) for node in module.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "POD_SCRIPT"
                              for target in node.targets))
        names = {"CheckFailed", "check", "actions"}
        definitions = [node for node in ast.parse(script).body
                       if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
        self.assertEqual({node.name for node in definitions}, names)
        namespace = {"re": re}
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), namespace)

        def request(method, endpoint):
            self.assertEqual((method, endpoint), ("GET", "/backup/actions"))
            return rows

        return namespace["actions"](SimpleNamespace(request=request))

    def sql(self, database, statement):
        result = database.query(statement)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def start_copy(self, storage, credentials, index):
        database = AlternatePortDatabase(self.root / f"database-{index}", self.documents, self.passwords,
                                         tcp_port=9000 + index * 10, http_port=8123 + index,
                                         network="container:" + storage, s3_credentials=credentials)
        self.addCleanup(database.close)
        database.start()
        source = self.config_source()
        config = yaml.safe_load(source["config.yml"])
        config["general"]["remote_storage"] = "s3"
        config["s3"].update(endpoint="http://127.0.0.1:9001", bucket="runtime-backups", region="us-east-1",
                            path="test/catalog", force_path_style=True, disable_ssl=True, **credentials)
        config["clickhouse"]["port"] = database.tcp_port
        config["api"]["listen"] = "0.0.0.0:" + str(7171 + index)
        source["config.yml"] = yaml.safe_dump(config)
        sidecar_root = database.root / "sidecar"
        runtime.project(sidecar_root / "config", source)
        runtime.project(sidecar_root / "api", {"password": "dummy-api-password", "revision": "revision-1"})
        sidecar = self.start_wrapper(database, sidecar_root)
        self.client(storage, """
import time
deadline = time.monotonic() + 60
while True:
    try:
        api.request('GET', '/backup/status')
        break
    except backup.BackupError:
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.2)
print('ready')
""", copy=index)
        self.assert_clean_logs(sidecar)
        return database

    def client(self, storage, statement, copy=0, **environment):
        script = """import json, os, sys
from datetime import datetime
sys.path.insert(0, '/test')
import run_backup as backup
index = int(os.environ['TEST_COPY'])
api = backup.API('http://127.0.0.1:' + str(7171 + index), 'backup', 'dummy-api-password')
probe = backup.API('http://127.0.0.1:' + str(8123 + index), 'backup_probe', os.environ['PROBE_PASSWORD'], backup_api=False)
scheduler = backup.Scheduler([api], [probe], timeout=180, poll_seconds=0.1)
"""
        return self.runtime_python(storage, script + statement, TEST_COPY=str(copy),
                                   PROBE_PASSWORD=self.passwords["backup_probe"], **environment)

    def scheduler(self, storage, when, copy=0):
        output = self.client(storage, """
name = scheduler.run(datetime.fromisoformat(os.environ['TEST_TIME']))
print(json.dumps({'name': name, 'remote': api.request('GET', '/backup/list/remote')}))
""", copy=copy, TEST_TIME=when.isoformat())
        return json.loads(output.splitlines()[-1])

    def snapshot(self, storage, credentials):
        script = "import hashlib, json, os, urllib.parse, xml.etree.ElementTree as ET\n"
        script += inspect.getsource(runtime.local_s3_request)
        script += """
key, secret = os.environ['S3_ACCESS_KEY'], os.environ['S3_SECRET_KEY']
listing = ET.fromstring(local_s3_request('GET', '/runtime-backups', key, secret))
ns = {'s3': 'http://s3.amazonaws.com/doc/2006-03-01/'}
assert listing.findtext('s3:IsTruncated', namespaces=ns) == 'false', 'Test object listing was truncated'
objects = {}
for entry in listing.findall('s3:Contents', ns):
    name = entry.findtext('s3:Key', namespaces=ns)
    body = local_s3_request('GET', '/runtime-backups/' + urllib.parse.quote(name, safe='/'), key, secret)
    objects[name] = {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
assert objects, 'The generated backups must create S3 objects'
print(json.dumps(objects, sort_keys=True))
"""
        return json.loads(self.runtime_python(storage, script, S3_ACCESS_KEY=credentials["access_key"],
                                              S3_SECRET_KEY=credentials["secret_key"]))

    def cleanup(self, storage, latest_backup, execute=False):
        output = self.client(storage, """
from cleanup_local_backups import LocalCleaner
apis = [backup.API('http://127.0.0.1:' + str(port), 'backup', 'dummy-api-password') for port in (7171, 7172)]
result = LocalCleaner(apis, keep_full_backup_days=2).run(
    latest_backup=os.environ['LATEST_BACKUP'], execute=os.environ['EXECUTE'] == '1')
print(json.dumps(result, sort_keys=True))
""", LATEST_BACKUP=latest_backup, EXECUTE="1" if execute else "0")
        return json.loads(output.splitlines()[-1])

    def local_names(self, storage, copy):
        return set(json.loads(self.client(storage, "print(json.dumps([row['name'] for row in api.request('GET', '/backup/list/local')]))",
                                          copy=copy)))

    def local_record_bytes(self, database):
        path = self.sql(database, "SELECT path FROM system.disks WHERE name = 'backups_s3' FORMAT TabSeparatedRaw")
        result = runtime.docker("exec", database.name, "du", "-sk", path)
        return int(result.stdout.split()[0]) * 1024


@unittest.skipUnless(os.environ.get("RUN_BACKUP_RUNTIME") == "1", "opt-in local Docker test")
class LocalBackupCleanupRuntimeTests(LocalBackupCleanupRuntimeHelpers, unittest.TestCase):
    def test_local_cleanup_preserves_s3_recovery_and_next_incremental(self):
        storage, credentials = self.start_s3()
        databases = [self.start_copy(storage, credentials, index) for index in range(2)]
        for database in databases:
            self.sql(database, "CREATE DATABASE otel_traces")
            self.sql(database, "CREATE TABLE otel_traces.runtime_sample (id UInt64, message String) ENGINE=MergeTree ORDER BY id")
        day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
        groups = []
        for offset in range(3):
            pair = []
            for hour in (0, 4):
                value = offset * 2 + (1 if hour == 0 else 2)
                for database in databases:
                    self.sql(database, f"INSERT INTO otel_traces.runtime_sample VALUES ({value}, 'batch {value}')")
                result = self.scheduler(storage, day + timedelta(days=offset, hours=hour))
                pair.append(result["name"])
            groups.append(pair)
        names = {name for group in groups for name in group}
        # The second copy holds downloaded native-backup records, as it would
        # after using backups originally created by the first copy.
        for name in sorted(names):
            self.client(storage, """
name = os.environ['BACKUP_NAME']
scheduler.operation(api, 'download', name, '/backup/download/' + name)
print('downloaded')
""", copy=1, BACKUP_NAME=name)
        for copy in range(2):
            self.assertEqual(self.local_names(storage, copy), names)
        disk_bytes_before = [self.local_record_bytes(database) for database in databases]
        before = self.snapshot(storage, credentials)
        preview = self.cleanup(storage, groups[-1][-1])
        expected_deleted = {(copy, name) for copy in range(2) for name in groups[0]}
        self.assertEqual(preview["mode"], "dry-run")
        self.assertEqual({(item["copy"], item["name"]) for item in preview["delete"]}, expected_deleted)
        self.assertEqual(preview["deleted"], [])
        for copy in range(2):
            self.assertEqual(self.local_names(storage, copy), names)
        self.assertEqual(self.snapshot(storage, credentials), before)
        cleaned = self.cleanup(storage, groups[-1][-1], execute=True)
        self.assertEqual(cleaned["mode"], "delete")
        self.assertEqual({(item["copy"], item["name"]) for item in cleaned["deleted"]}, expected_deleted)
        for copy in range(2):
            self.assertEqual(self.local_names(storage, copy), names - set(groups[0]))
            history = json.loads(self.client(
                storage, "print(json.dumps(api.request('GET', '/backup/actions')))", copy=copy))
            deleted = [row for row in history if row["command"].startswith("delete local ")]
            self.assertEqual({row["command"] for row in deleted},
                             {"delete local " + name for name in groups[0]})
            self.assertTrue(all(row["status"] == "success" and not row.get("operation_id")
                                for row in deleted))
            accepted = self.failover_actions(history)
            self.assertEqual({row[1] for row in accepted if row[1].startswith("delete local ")},
                             {row["command"] for row in deleted},
                             "The copy-switch check must retain and accept completed local deletions")
        disk_bytes_after = [self.local_record_bytes(database) for database in databases]
        for old, current in zip(disk_bytes_before, disk_bytes_after):
            self.assertLess(current, old, "Local cleanup must free backup records on each database disk")
        self.assertEqual(self.snapshot(storage, credentials), before,
                         "Local cleanup must leave every S3 object and its contents unchanged")

        # Download and restore an old incremental whose original local records
        # were removed from both copies. Use an empty, separate database.
        oldest_incremental = groups[0][1]
        for name in groups[0]:
            self.client(storage, """
name = os.environ['BACKUP_NAME']
scheduler.operation(api, 'download', name, '/backup/download/' + name)
print('downloaded')
""", copy=1, BACKUP_NAME=name)
        self.sql(databases[1], "CREATE DATABASE recovered_old")
        self.sql(databases[1], "RESTORE TABLE otel_traces.runtime_sample AS recovered_old.runtime_sample "
                 "FROM Disk('backups_s3', '" + oldest_incremental + "')")
        self.assertEqual(self.sql(databases[1], "SELECT id, message FROM recovered_old.runtime_sample ORDER BY id"),
                         "1\tbatch 1\n2\tbatch 2")
        self.assertEqual(self.sql(databases[1], "SELECT count(), sum(id) FROM otel_traces.runtime_sample"), "6\t21")
        self.assertEqual(self.snapshot(storage, credentials), before)

        # A later incremental must also work when the current day's local full
        # records are absent on the selected copy. Remove its local incremental
        # first because the stock tool refuses deletion of a base it needs.
        # The scheduler then downloads the missing full-backup records.
        latest_full = groups[-1][0]
        for name in reversed(groups[-1]):
            reply = json.loads(self.client(storage, """
print(json.dumps(api.request('POST', '/backup/delete/local/' + os.environ['BACKUP_NAME'])))
""", copy=1, BACKUP_NAME=name))
            self.assertEqual(reply, [{"status": "success", "operation": "delete", "backup_name": name, "location": "local"}])
        self.assertNotIn(latest_full, self.local_names(storage, 1))
        self.assertEqual(self.snapshot(storage, credentials), before)
        self.sql(databases[1], "INSERT INTO otel_traces.runtime_sample VALUES (7, 'batch 7')")
        next_backup = self.scheduler(storage, day + timedelta(days=2, hours=8), copy=1)
        catalog = {row["name"]: row for row in next_backup["remote"]}
        self.assertEqual(catalog[next_backup["name"]]["required"], latest_full)
        self.assertIn(latest_full, self.local_names(storage, 1))
        self.assertIn(next_backup["name"], self.local_names(storage, 1))
        after = self.snapshot(storage, credentials)
        self.assertGreater(len(after), len(before))
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(self.sql(databases[0], "SELECT count(), sum(id) FROM otel_traces.runtime_sample"), "6\t21")
        self.assertEqual(self.sql(databases[1], "SELECT count(), sum(id) FROM otel_traces.runtime_sample"), "7\t28")
        self.sql(databases[1], "CREATE DATABASE recovered_next")
        self.sql(databases[1], "RESTORE TABLE otel_traces.runtime_sample AS recovered_next.runtime_sample "
                 "FROM Disk('backups_s3', '" + next_backup["name"] + "')")
        self.assertEqual(self.sql(databases[1], "SELECT count(), sum(id) FROM recovered_next.runtime_sample"), "7\t28")
        print(json.dumps({"backup_groups": len(groups), "local_records_removed": len(cleaned["deleted"]),
                          "unchanged_s3_objects": len(before), "restored_rows": 2,
                          "local_bytes_removed_by_copy": [old - current for old, current in zip(disk_bytes_before, disk_bytes_after)],
                          "next_incremental_with_downloaded_base": True}, sort_keys=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
