"""Opt-in S3 cleanup checks with disposable ClickHouse and MinIO containers.

RUN_BACKUP_RUNTIME=1 HELM=/path/to/helm python3 -m unittest discover \
    -s hack/tests/runtime -p 'test*runtime.py' -v

All credentials and rows are generated here. Containers share an isolated Docker
network namespace, publish no ports, and cannot reach AWS. The versioned case
checks a safe stop on MinIO's stale prefixes and saved-version reads. It does
not prove complete versioned cleanup or AWS lifecycle expiry.
"""

from datetime import datetime, timedelta, timezone
import inspect
import json
import os
import unittest
import uuid

if __package__:
    from . import test_backup_runtime as runtime
    from .test_local_backup_cleanup_runtime import LocalBackupCleanupRuntimeHelpers
else:
    import test_backup_runtime as runtime
    from test_local_backup_cleanup_runtime import LocalBackupCleanupRuntimeHelpers


MINIO_CLIENT_IMAGE = os.environ.get(
    "BACKUP_MINIO_CLIENT_IMAGE",
    "cgr.dev/chainguard/minio-client@sha256:72f9978d415ea8294d78051fb3aee2fd258d449237633f77f100b5367bb5225d",
)


def signed_s3_request(method, target, access_key, secret_key, body=b""):
    """Sign only requests to the isolated S3 fixture, including query strings."""
    from datetime import datetime, timezone
    import hashlib
    import hmac
    import urllib.parse
    import urllib.request

    parts = urllib.parse.urlsplit(target)
    if (parts.scheme or parts.netloc or parts.fragment
            or not (parts.path == "/runtime-backups" or parts.path.startswith("/runtime-backups/"))):
        raise ValueError("Only the local test bucket is allowed")
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/-_.~")
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = "&".join(sorted(
        urllib.parse.quote(key, safe="-_.~") + "=" + urllib.parse.quote(value, safe="-_.~")
        for key, value in pairs
    ))
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body_hash = hashlib.sha256(body).hexdigest()
    headers = {"host": "127.0.0.1:9001", "x-amz-content-sha256": body_hash, "x-amz-date": now}
    signed_headers = ";".join(headers)
    canonical = "\n".join((method, path, query,
                            "".join(key + ":" + value + "\n" for key, value in headers.items()),
                            signed_headers, body_hash))
    scope = now[:8] + "/us-east-1/s3/aws4_request"
    to_sign = "\n".join(("AWS4-HMAC-SHA256", now, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = ("AWS4" + secret_key).encode()
    for part in (now[:8], "us-east-1", "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = ("AWS4-HMAC-SHA256 Credential=" + access_key + "/" + scope
                                + ", SignedHeaders=" + signed_headers + ", Signature=" + signature)
    request = urllib.request.Request("http://127.0.0.1:9001" + path + ("?" + query if query else ""),
                                     method=method, data=body if method in ("PUT", "POST") else None,
                                     headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


@unittest.skipUnless(os.environ.get("RUN_BACKUP_RUNTIME") == "1", "opt-in local Docker test")
class RemoteBackupCleanupRuntimeTests(LocalBackupCleanupRuntimeHelpers, unittest.TestCase):
    def minio_admin(self, storage, credentials, *arguments, policy=None):
        name = "backup-runtime-admin-" + uuid.uuid4().hex[:10]
        self.addCleanup(lambda: runtime.docker("rm", "-f", name, check=False))
        args = ["run", "--rm", "--name", name, "--network", "container:" + storage,
                "--user", "65532:65532", "--read-only",
                "--tmpfs", "/tmp:rw,size=16777216,uid=65532,gid=65532",
                "--env", "MC_HOST_local=http://" + credentials["access_key"] + ":"
                + credentials["secret_key"] + "@127.0.0.1:9001"]
        if policy is not None:
            policy_file = self.root / "test-writer-policy.json"
            policy_file.write_text(json.dumps(policy))
            args += ["--mount", f"type=bind,source={policy_file},target=/policy.json,readonly"]
        runtime.docker(*args, MINIO_CLIENT_IMAGE, "--config-dir", "/tmp/mc", *arguments, timeout=60)

    def restricted_writer(self, storage, admin):
        writer = {"access_key": "writer-" + uuid.uuid4().hex, "secret_key": uuid.uuid4().hex}
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
             "Resource": ["arn:aws:s3:::runtime-backups"]},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                                             "s3:AbortMultipartUpload"],
             "Resource": ["arn:aws:s3:::runtime-backups/*"]},
            {"Effect": "Deny", "Action": ["s3:GetBucketVersioning", "s3:PutBucketVersioning",
                                            "s3:ListBucketVersions", "s3:DeleteObjectVersion"],
             "Resource": ["arn:aws:s3:::runtime-backups", "arn:aws:s3:::runtime-backups/*"]},
        ]}
        self.minio_admin(storage, admin, "admin", "user", "add", "local",
                         writer["access_key"], writer["secret_key"])
        self.minio_admin(storage, admin, "admin", "policy", "create", "local", "backup-writer",
                         "/policy.json", policy=policy)
        self.minio_admin(storage, admin, "admin", "policy", "attach", "local", "backup-writer",
                         "--user", writer["access_key"])
        return writer

    def s3(self, storage, credentials, statement, **environment):
        script = "import hashlib, json, os, urllib.parse, urllib.error, xml.etree.ElementTree as ET\n"
        script += inspect.getsource(signed_s3_request)
        script += "\nkey, secret = os.environ['S3_ACCESS_KEY'], os.environ['S3_SECRET_KEY']\n"
        return self.runtime_python(storage, script + statement, S3_ACCESS_KEY=credentials["access_key"],
                                   S3_SECRET_KEY=credentials["secret_key"], **environment)

    def version_inventory(self, storage, admin):
        return json.loads(self.s3(storage, admin, """
root = ET.fromstring(signed_s3_request('GET', '/runtime-backups?versions=', key, secret))
ns = {'s3': 'http://s3.amazonaws.com/doc/2006-03-01/'}
assert root.findtext('s3:IsTruncated', namespaces=ns) == 'false', 'Version listing was truncated'
result = {'versions': [], 'delete_markers': []}
for tag, field in [('Version', 'versions'), ('DeleteMarker', 'delete_markers')]:
    for entry in root.findall('s3:' + tag, ns):
        row = {'key': entry.findtext('s3:Key', namespaces=ns),
               'version': entry.findtext('s3:VersionId', namespaces=ns)}
        if tag == 'Version':
            target = '/runtime-backups/' + urllib.parse.quote(row['key'], safe='/')
            target += '?' + urllib.parse.urlencode({'versionId': row['version']})
            body = signed_s3_request('GET', target, key, secret)
            row.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        result[field].append(row)
print(json.dumps(result, sort_keys=True))
"""))

    def seed_groups(self, storage, databases, ages, credentials):
        anchor = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        groups = []
        value = 0
        previous = {}
        self.created_backup_objects = {}
        for age in ages:
            group = []
            for hour in (0, 4):
                value += 1
                for database in databases:
                    self.sql(database, f"INSERT INTO otel_traces.runtime_sample VALUES ({value}, 'batch {value}')")
                name = self.scheduler(storage, anchor - timedelta(days=age) + timedelta(hours=hour))["name"]
                group.append(name)
                current = self.snapshot(storage, credentials)
                self.assertEqual({key: current.get(key) for key in previous}, previous,
                                 "A new backup must leave earlier objects unchanged")
                # Native S3 disk files have generated keys, so a backup-name
                # prefix alone would miss the data whose deletion matters.
                self.created_backup_objects[name] = set(current) - set(previous)
                previous = current
            groups.append(group)
        return anchor, groups

    def prepare_databases(self, storage, credentials):
        databases = [self.start_copy(storage, credentials, index) for index in range(2)]
        for database in databases:
            self.sql(database, "CREATE DATABASE otel_traces")
            self.sql(database, "CREATE TABLE otel_traces.runtime_sample (id UInt64, message String) ENGINE=MergeTree ORDER BY id")
        return databases

    def download_records(self, storage, names, copy=1):
        for name in names:
            self.client(storage, """
name = os.environ['BACKUP_NAME']
scheduler.operation(api, 'download', name, '/backup/download/' + name)
print('downloaded')
""", copy=copy, BACKUP_NAME=name)

    def remote_cleanup(self, storage, anchor, latest, execute, expect_minio_stop=False):
        output = self.client(storage, """
from cleanup_backups import Cleaner
apis = [backup.API('http://127.0.0.1:' + str(port), 'backup', 'dummy-api-password') for port in (7171, 7172)]
try:
    result = Cleaner(apis, retention_policy='daily-weekly', daily_days=7, history_days=30, timeout=180).run(
        now=datetime.fromisoformat(os.environ['CLEANUP_TIME']), latest_backup=os.environ['LATEST_BACKUP'],
        execute=os.environ['EXECUTE'] == '1')
except backup.BackupError as error:
    if os.environ['EXPECT_MINIO_STOP'] != '1':
        raise
    result = {'stopped': str(error)}
print(json.dumps(result, sort_keys=True))
""", CLEANUP_TIME=(anchor + timedelta(hours=12)).isoformat(), LATEST_BACKUP=latest,
                             EXECUTE="1" if execute else "0",
                             EXPECT_MINIO_STOP="1" if expect_minio_stop else "0")
        return json.loads(output.splitlines()[-1])

    def group_objects(self, snapshot, names):
        keys = set().union(*(self.created_backup_objects[name] for name in names))
        return {key: value for key, value in snapshot.items() if key in keys}

    def test_remote_cleanup_removes_native_files_and_preserves_recovery(self):
        storage, credentials = self.start_s3()
        databases = self.prepare_databases(storage, credentials)
        anchor, groups = self.seed_groups(storage, databases, (35, 34, 33, 1, 0), credentials)
        names = [name for group in groups for name in group]
        self.download_records(storage, names)
        before = self.snapshot(storage, credentials)

        # Earlier local cleanup may already have removed a set on one or both
        # copies. Leave a third expired set on both to exercise all three cases.
        for copy, keep_days in ((0, 3), (1, 4)):
            output = self.client(storage, """
from cleanup_local_backups import LocalCleaner
result = LocalCleaner([api], keep_full_backup_days=int(os.environ['KEEP_DAYS'])).run(
    latest_backup=os.environ['LATEST_BACKUP'], execute=True)
print(json.dumps(result))
""", copy=copy, KEEP_DAYS=str(keep_days), LATEST_BACKUP=groups[-1][-1])
            self.assertTrue(json.loads(output.splitlines()[-1])["completed"])
        for copy in (0, 1):
            self.assertTrue(set(groups[0]).isdisjoint(self.local_names(storage, copy)))
        self.assertTrue(set(groups[1]).isdisjoint(self.local_names(storage, 0)))
        self.assertTrue(set(groups[1]).issubset(self.local_names(storage, 1)))
        for copy in (0, 1):
            self.assertTrue(set(groups[2]).issubset(self.local_names(storage, copy)))
        self.assertEqual(self.snapshot(storage, credentials), before)

        expired = {name for group in groups[:3] for name in group}
        removed_objects = self.group_objects(before, expired)
        self.assertTrue(removed_objects)
        self.assertTrue(any(key.endswith("serialization.json") for key in removed_objects),
                        "The fixture must include the native file omitted by the old deletion code")
        preview = self.remote_cleanup(storage, anchor, groups[-1][-1], execute=False)
        self.assertEqual(set(preview["delete"]), expired)
        self.assertEqual(preview["deleted"], [])
        self.assertEqual(self.snapshot(storage, credentials), before)

        cleaned = self.remote_cleanup(storage, anchor, groups[-1][-1], execute=True)
        self.assertTrue(cleaned["completed"])
        self.assertEqual(set(cleaned["deleted"]), expired)
        for full, incremental in groups[:3]:
            self.assertLess(cleaned["deleted"].index(incremental), cleaned["deleted"].index(full))
        for copy in (0, 1):
            self.assertEqual(self.local_names(storage, copy), set(names) - expired)
        after = self.snapshot(storage, credentials)
        self.assertEqual(after, {key: value for key, value in before.items() if key not in removed_objects},
                         "Every expired native/catalog object must be gone; retained objects must be unchanged")

        self.sql(databases[1], "CREATE DATABASE recovered_kept")
        self.sql(databases[1], "RESTORE TABLE otel_traces.runtime_sample AS recovered_kept.runtime_sample "
                 "FROM Disk('backups_s3', '" + groups[-1][1] + "')")
        self.assertEqual(self.sql(databases[1], "SELECT count(), sum(id) FROM recovered_kept.runtime_sample"), "10\t55")
        self.sql(databases[1], "INSERT INTO otel_traces.runtime_sample VALUES (11, 'batch 11')")
        next_backup = self.scheduler(storage, anchor + timedelta(hours=8), copy=1)
        catalog = {row["name"]: row for row in next_backup["remote"]}
        self.assertEqual(catalog[next_backup["name"]]["required"], groups[-1][0])
        self.assertTrue(expired.isdisjoint(catalog))
        self.sql(databases[1], "CREATE DATABASE recovered_next")
        self.sql(databases[1], "RESTORE TABLE otel_traces.runtime_sample AS recovered_next.runtime_sample "
                 "FROM Disk('backups_s3', '" + next_backup["name"] + "')")
        self.assertEqual(self.sql(databases[1], "SELECT count(), sum(id) FROM recovered_next.runtime_sample"), "11\t66")
        self.assertEqual(self.sql(databases[0], "SELECT count(), sum(id) FROM otel_traces.runtime_sample"), "10\t55")
        print(json.dumps({"expired_sets": 3, "removed_objects": len(removed_objects),
                          "retained_incremental_restored": True, "next_incremental_restored": True}))

    def test_versioned_minio_stops_on_stale_prefix_and_preserves_versions(self):
        # MinIO returns empty prefixes after versioned deletion, unlike AWS S3:
        # https://github.com/minio/minio/issues/17810
        # The stock API then retains cached metadata for that prefix. Keep the
        # safety check: stop after the first deletion and send no further one.
        storage, admin = self.start_s3()
        self.minio_admin(storage, admin, "version", "enable", "local/runtime-backups")
        writer = self.restricted_writer(storage, admin)
        self.s3(storage, writer, """
try:
    signed_s3_request('GET', '/runtime-backups?versioning=', key, secret)
except urllib.error.HTTPError as exc:
    assert exc.code == 403, exc.code
else:
    raise AssertionError('The backup writer must not inspect bucket versioning')
print('bucket versioning access denied')
""")
        databases = self.prepare_databases(storage, writer)
        anchor, groups = self.seed_groups(storage, databases, (35, 0), admin)
        self.download_records(storage, [name for group in groups for name in group])
        before = self.snapshot(storage, admin)
        versions_before = self.version_inventory(storage, admin)
        deleted_name = groups[0][-1]
        removed_objects = self.group_objects(before, [deleted_name])
        self.assertTrue(removed_objects)
        cleaned = self.remote_cleanup(storage, anchor, groups[-1][-1], execute=True, expect_minio_stop=True)
        self.assertEqual(cleaned, {"stopped": "Cleanup stopped: backup metadata changed; no further deletion was sent."})
        self.assertEqual(self.snapshot(storage, admin),
                         {key: value for key, value in before.items() if key not in removed_objects})
        for copy in (0, 1):
            self.assertEqual(self.local_names(storage, copy),
                             {name for group in groups for name in group} - {deleted_name})
            rows = json.loads(self.client(storage, "print(json.dumps(api.request('GET', '/backup/actions')))", copy=copy))
            expected = ["delete local " + deleted_name]
            if copy == 0:
                expected.append("delete remote " + deleted_name)
            self.assertEqual([row["command"] for row in rows if row["command"].startswith("delete ")], expected)
        self.s3(storage, admin, """
target = '/runtime-backups?' + urllib.parse.urlencode({'list-type': '2', 'prefix': 'test/catalog/', 'delimiter': '/'})
root = ET.fromstring(signed_s3_request('GET', target, key, secret))
ns = {'s3': 'http://s3.amazonaws.com/doc/2006-03-01/'}
prefixes = [row.findtext('s3:Prefix', namespaces=ns) for row in root.findall('s3:CommonPrefixes', ns)]
assert 'test/catalog/' + os.environ['DELETED_NAME'] + '/' in prefixes
print('MinIO retains the empty deleted prefix')
""", DELETED_NAME=deleted_name)

        versions_after = self.version_inventory(storage, admin)
        by_version = lambda rows: {(row["key"], row["version"]): row for row in rows}
        self.assertEqual(by_version(versions_after["versions"]), by_version(versions_before["versions"]),
                         "Ordinary deletion must leave every old version readable with its original bytes")
        before_markers = {(row["key"], row["version"]) for row in versions_before["delete_markers"]}
        after_markers = {(row["key"], row["version"]) for row in versions_after["delete_markers"]}
        self.assertTrue(before_markers.issubset(after_markers))
        self.assertEqual({key for key, _ in after_markers - before_markers}, set(removed_objects))
        selected = next(row for row in versions_after["versions"] if row["key"] in removed_objects)
        self.s3(storage, writer, """
target = '/runtime-backups/' + urllib.parse.quote(os.environ['OBJECT_KEY'], safe='/')
target += '?' + urllib.parse.urlencode({'versionId': os.environ['VERSION_ID']})
try:
    signed_s3_request('DELETE', target, key, secret)
except urllib.error.HTTPError as exc:
    assert exc.code == 403, exc.code
else:
    raise AssertionError('The backup writer must not permanently delete an old version')
print('version deletion denied')
""", OBJECT_KEY=selected["key"], VERSION_ID=selected["version"])
        self.assertEqual(self.version_inventory(storage, admin), versions_after)
        print(json.dumps({"safe_stop_after_one_deletion": True, "preserved_versions": len(versions_after["versions"]),
                          "delete_markers": len(versions_after["delete_markers"]),
                          "writer_version_deletion_denied": True,
                          "complete_aws_versioned_cleanup_tested": False, "aws_lifecycle_expiry_tested": False}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
