"""Confirm recovery of incomplete native-backup catalogs with stock software.

RUN_BACKUP_RUNTIME=1 HELM=/path/to/helm python3 -m unittest discover \
    -s hack/tests/runtime -p test_interrupted_upload_runtime.py -v

The fixture removes the final catalog files from otherwise complete uploads to
reproduce an interrupted upload. Native data and the creating copy's complete
local records remain. All operations use disposable, isolated Docker storage.
"""

from datetime import datetime, timedelta, timezone
import inspect
import json
import os
import unittest

if __package__:
    from . import test_backup_runtime as runtime
    from .test_local_backup_cleanup_runtime import LocalBackupCleanupRuntimeHelpers
    from .test_remote_backup_cleanup_runtime import signed_s3_request
else:
    import test_backup_runtime as runtime
    from test_local_backup_cleanup_runtime import LocalBackupCleanupRuntimeHelpers
    from test_remote_backup_cleanup_runtime import signed_s3_request


@unittest.skipUnless(os.environ.get("RUN_BACKUP_RUNTIME") == "1", "opt-in local Docker test")
class InterruptedUploadRuntimeTests(LocalBackupCleanupRuntimeHelpers, unittest.TestCase):
    def start_wrapper(self, *args, **kwargs):
        name = super().start_wrapper(*args, **kwargs)
        if not hasattr(self, "backup_containers"):
            self.backup_containers = []
        self.backup_containers.append(name)
        return name

    def s3(self, storage, credentials, statement, **environment):
        script = "import json, os, urllib.parse\n" + inspect.getsource(signed_s3_request)
        script += "\nkey, secret = os.environ['S3_ACCESS_KEY'], os.environ['S3_SECRET_KEY']\n"
        return self.runtime_python(storage, script + statement,
                                   S3_ACCESS_KEY=credentials["access_key"],
                                   S3_SECRET_KEY=credentials["secret_key"], **environment)

    def prepare_pair(self):
        storage, credentials = self.start_s3()
        databases = [self.start_copy(storage, credentials, index) for index in range(2)]
        for database in databases:
            self.sql(database, "CREATE DATABASE otel_traces")
            self.sql(database, "CREATE TABLE otel_traces.runtime_sample "
                     "(id UInt64, message String) ENGINE=MergeTree ORDER BY id")
            self.sql(database, "INSERT INTO otel_traces.runtime_sample VALUES (1, 'first')")
        day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        day -= timedelta(days=2)
        full = self.scheduler(storage, day)["name"]
        full_objects = self.snapshot(storage, credentials)
        for database in databases:
            self.sql(database, "INSERT INTO otel_traces.runtime_sample VALUES (2, 'second')")
        incremental = self.scheduler(storage, day + timedelta(hours=4))["name"]
        return storage, credentials, databases, full, incremental, full_objects

    def remote_rows(self, storage):
        return json.loads(self.client(storage, "print(json.dumps(api.request('GET', '/backup/list/remote')))"))

    def interrupt_catalog(self, storage, credentials, name):
        before = self.snapshot(storage, credentials)
        keys = ["test/catalog/" + name + "/" + leaf for leaf in (".backup", "metadata.json")]
        self.assertTrue(set(keys).issubset(before))
        self.s3(storage, credentials, """
for name in json.loads(os.environ['OBJECT_KEYS']):
    signed_s3_request('DELETE', '/runtime-backups/' + urllib.parse.quote(name, safe='/'), key, secret)
""", OBJECT_KEYS=json.dumps(keys))
        after = self.snapshot(storage, credentials)
        self.assertEqual(after, {key: value for key, value in before.items() if key not in keys})
        # The fixture started from successful uploads. An actually interrupted
        # upload would not have populated this cache with complete metadata.
        for container in self.backup_containers:
            runtime.docker("exec", container, "sh", "-c",
                           "rm -f /tmp/.clickhouse-backup-metadata.cache.*")
        row = next(row for row in self.remote_rows(storage) if row["name"] == name)
        self.assertNotEqual(row["desc"], "directory, embedded")
        self.assertIn(name, self.local_names(storage, 0))

    def download_and_restore(self, storage, database, names, destination, expected):
        for name in names:
            self.client(storage, """
name = os.environ['BACKUP_NAME']
scheduler.operation(api, 'download', name, '/backup/download/' + name)
""", copy=1, BACKUP_NAME=name)
        self.sql(database, "CREATE DATABASE " + destination)
        self.sql(database, "RESTORE TABLE otel_traces.runtime_sample AS " + destination
                 + ".runtime_sample FROM Disk('backups_s3', '" + names[-1] + "')")
        self.assertEqual(self.sql(database, "SELECT id, message FROM " + destination
                                  + ".runtime_sample ORDER BY id"), expected)

    def test_resume_completes_full_and_incremental_catalogs_without_changing_native_data(self):
        storage, credentials, databases, full, incremental, _ = self.prepare_pair()
        before = self.snapshot(storage, credentials)
        native = {key: value for key, value in before.items() if key.startswith("test/native/")}
        self.assertTrue(native)
        for name, required in ((full, ""), (incremental, full)):
            self.interrupt_catalog(storage, credentials, name)
            output = self.client(storage, """
name = os.environ['BACKUP_NAME']
try:
    scheduler.operation(api, 'upload', name, '/backup/upload/' + name)
except backup.BackupError:
    history = api.request('GET', '/backup/actions')
    errors = [row for row in history if row.get('status') == 'error'
              and name in row.get('command', '')]
    assert errors, history
    assert any('already exists' in str(row).lower() for row in errors), errors
else:
    raise AssertionError('A plain upload must refuse an existing incomplete catalog')
scheduler.operation(api, 'upload', name, '/backup/upload/' + name, {'resume': 'true'})
print(json.dumps(api.request('GET', '/backup/list/remote')))
""", BACKUP_NAME=name)
            rows = {row["name"]: row for row in json.loads(output.splitlines()[-1])}
            self.assertEqual(rows[name]["desc"], "directory, embedded")
            self.assertEqual(rows[name]["required"], required)
            after = self.snapshot(storage, credentials)
            self.assertEqual({key: value for key, value in after.items()
                              if key.startswith("test/native/")}, native)
            self.assertIn("test/catalog/" + name + "/.backup", after)
            self.assertIn("test/catalog/" + name + "/metadata.json", after)
        self.download_and_restore(storage, databases[1], [full], "recovered_full", "1\tfirst")
        self.download_and_restore(storage, databases[1], [incremental], "recovered_incremental",
                                  "1\tfirst\n2\tsecond")
        for database in databases:
            self.assertEqual(self.sql(database, "SELECT count(), sum(id) FROM otel_traces.runtime_sample"),
                             "2\t3")
        print(json.dumps({"resumed_full": True, "resumed_incremental": True,
                          "required_base_preserved": True, "restored_rows": [1, 2],
                          "unchanged_native_objects": len(native)}, sort_keys=True))

    def test_abandonment_removes_catalog_then_native_objects_without_changing_base(self):
        storage, credentials, databases, full, incremental, full_objects = self.prepare_pair()
        before = self.snapshot(storage, credentials)
        incremental_native = {key for key in before if key.startswith("test/native/") and key not in full_objects}
        self.assertTrue(incremental_native)
        self.interrupt_catalog(storage, credentials, incremental)
        broken = self.snapshot(storage, credentials)
        reply = json.loads(self.client(storage, """
print(json.dumps(api.request('POST', '/backup/delete/remote/' + os.environ['BACKUP_NAME'])))
""", BACKUP_NAME=incremental))
        self.assertEqual(reply[0]["status"], "success")
        self.assertNotIn(incremental, {row["name"] for row in self.remote_rows(storage)})
        after_remote = self.snapshot(storage, credentials)
        self.assertEqual({key: value for key, value in after_remote.items() if key.startswith("test/native/")},
                         {key: value for key, value in broken.items() if key.startswith("test/native/")})
        self.assertIn(incremental, self.local_names(storage, 0))
        self.assertEqual({key: after_remote.get(key) for key in full_objects}, full_objects)
        reply = json.loads(self.client(storage, """
print(json.dumps(api.request('POST', '/backup/delete/local/' + os.environ['BACKUP_NAME'])))
""", BACKUP_NAME=incremental))
        self.assertEqual(reply[0]["status"], "success")
        self.assertNotIn(incremental, self.local_names(storage, 0))
        after_local = self.snapshot(storage, credentials)
        self.assertTrue(incremental_native.isdisjoint(after_local))
        self.assertEqual({key: after_local.get(key) for key in full_objects}, full_objects)
        self.download_and_restore(storage, databases[1], [full], "recovered_base", "1\tfirst")
        print(json.dumps({"abandoned_incremental": True,
                          "native_objects_removed_by_local_delete": len(incremental_native),
                          "unchanged_base_objects": len(full_objects), "base_restored_rows": 1},
                         sort_keys=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
