"""Opt-in Docker checks using public stock images and generated test credentials.

RUN_BACKUP_RUNTIME=1 HELM=/path/to/helm python3 -m unittest discover \
    -s hack/tests/runtime -p test_backup_runtime.py -v
Requires PyYAML and the images below. Containers share isolated network namespaces;
no AWS or Kubernetes calls are made. Only containers created here are removed.
"""

import inspect
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import xml.etree.ElementTree as ET

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_backup_chart import CHART, one, pod, render, render_secret_template

CH_IMAGE = os.environ.get("CLICKHOUSE_TEST_IMAGE", "clickhouse/clickhouse-server:26.4.3")
BACKUP_IMAGE = os.environ.get("BACKUP_TEST_IMAGE", "altinity/clickhouse-backup:2.8.1@sha256:08016b048f7e6035c048501315c2a788e5a782f15f168e042c7bd48d5a388cc4")
PYTHON_IMAGE = os.environ.get("BACKUP_PYTHON_IMAGE", "python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e")
S3_IMAGE = os.environ.get("BACKUP_S3_IMAGE", "chainguard/minio@sha256:0f95aa412a12351a95bb43c3b54b66440eb0aa022bb3f3458942678a489e915b")
WRAPPER = CHART / "files/clickhouse-backup/start-backup.sh"
BASE = """<clickhouse><logger><level>error</level><console>1</console></logger>
<max_thread_pool_size>256</max_thread_pool_size><background_schedule_pool_size>16</background_schedule_pool_size>
<background_buffer_flush_schedule_pool_size>4</background_buffer_flush_schedule_pool_size>
<background_message_broker_schedule_pool_size>4</background_message_broker_schedule_pool_size>
<background_distributed_schedule_pool_size>4</background_distributed_schedule_pool_size>
<listen_host>0.0.0.0</listen_host><http_port>8123</http_port>
</clickhouse>"""
KEEPER = """<clickhouse><keeper_server><tcp_port>9181</tcp_port><server_id>1</server_id>
<log_storage_path>/var/lib/clickhouse/keeper/log</log_storage_path>
<snapshot_storage_path>/var/lib/clickhouse/keeper/snapshots</snapshot_storage_path>
<coordination_settings><operation_timeout_ms>10000</operation_timeout_ms>
<session_timeout_ms>30000</session_timeout_ms></coordination_settings>
<raft_configuration><server><id>1</id><hostname>127.0.0.1</hostname><port>9234</port>
</server></raft_configuration></keeper_server>
<zookeeper><node><host>127.0.0.1</host><port>9181</port></node></zookeeper></clickhouse>"""


def docker(*args, check=True, timeout=45, input=None):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=timeout, input=input)
    if check and result.returncode:
        raise AssertionError(f"Docker {args[0]} failed with exit code {result.returncode}")
    return result


def replace(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(".pending")
    staged.write_text(value)
    staged.replace(path)


def project(directory, files):
    """Use the same ..data/key links as a Kubernetes projected directory."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    generation = directory / ("..generation-" + uuid.uuid4().hex[:8])
    generation.mkdir(mode=0o755)
    for key, value in files.items():
        replace(generation / key, value)
    names = {Path(key).parts[0] for key in files}
    for child in directory.iterdir():
        if not child.name.startswith("..") and child.name not in names:
            child.unlink()
    pending = directory / "..pending"
    pending.symlink_to(generation.name, target_is_directory=True)
    pending.replace(directory / "..data")
    for name in names:
        link = directory / name
        if not link.is_symlink():
            link.symlink_to("..data/" + name, target_is_directory=(generation / name).is_dir())


def local_s3_request(method, path, access_key, secret_key):
    """Sign a request to the disposable MinIO server using only the stdlib."""
    from datetime import datetime, timezone
    import hashlib
    import hmac
    import urllib.request

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body_hash = hashlib.sha256(b"").hexdigest()
    headers = {"host": "127.0.0.1:9001", "x-amz-content-sha256": body_hash, "x-amz-date": now}
    signed_headers = ";".join(headers)
    canonical = "\n".join((method, path, "", "".join(key + ":" + value + "\n" for key, value in headers.items()),
                            signed_headers, body_hash))
    scope = now[:8] + "/us-east-1/s3/aws4_request"
    to_sign = "\n".join(("AWS4-HMAC-SHA256", now, scope, hashlib.sha256(canonical.encode()).hexdigest()))
    key = ("AWS4" + secret_key).encode()
    for part in (now[:8], "us-east-1", "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = ("AWS4-HMAC-SHA256 Credential=" + access_key + "/" + scope
                                + ", SignedHeaders=" + signed_headers + ", Signature=" + signature)
    request = urllib.request.Request("http://127.0.0.1:9001" + path, method=method,
                                     data=b"" if method == "PUT" else None, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read()


class Database:
    def __init__(self, root, documents, passwords, *, missing=False, access_check=False, keeper=False,
                 network="none", s3_credentials=None):
        self.root, self.passwords = root, passwords
        self.name = "backup-runtime-db-" + uuid.uuid4().hex[:10]
        self.started = False
        self.network, self.s3_credentials = network, s3_credentials
        self.data_volume = self.name + "-data" if s3_credentials else None
        self.auth_volumes = {kind: self.name + "-" + kind for kind in ("user", "probe")}
        self.documents = documents
        root.mkdir(parents=True)
        files = one(documents, "ClickHouseInstallation")["spec"]["configuration"]["files"]
        # Keep every rendered config file, including the real S3 disk. Filtering
        # only the auth-store file would hide database startup failures in S3.
        config = {name.removeprefix("config.d/"): value for name, value in files.items()
                  if name.startswith("config.d/")}
        self.rendered_config_keys = set(config)
        config["runtime.xml"] = BASE
        if keeper:
            config["keeper.xml"] = KEEPER
        auth = one(documents, "ConfigMap", "otel-backup-auth")["data"]
        config["backup-users.xml"] = auth["backup-users.xml"]
        if access_check:
            disk = ET.fromstring(config["backup.xml"])
            setting = disk.find("storage_configuration/disks/backups_s3/skip_access_check")
            if setting is None:
                raise AssertionError("Rendered backup disk lost skip_access_check")
            setting.text = "false"
            config["backup.xml"] = ET.tostring(disk, encoding="unicode")
        if s3_credentials:
            disk = ET.fromstring(config["backup.xml"])
            disk.find("storage_configuration/disks/backups_s3/endpoint").text = "http://127.0.0.1:9001/runtime-backups/test/native/"
            config["backup.xml"] = ET.tostring(disk, encoding="unicode")
        project(root / "config", config)
        project(root / "users", {name.removeprefix("users.d/"): value for name, value in files.items()
                                  if name.startswith("users.d/")})
        shared = one(documents, "ExternalSecret", "ao-clickhouse-auth-methods")["spec"]["target"]["template"]["data"]["auth.xml"]
        values = {name + "_password": password for name, password in passwords.items()}
        values.update({item["secretKey"]: "-" for item in one(documents, "ExternalSecret", "ao-clickhouse-auth-methods")["spec"]["data"]
                       if item["secretKey"].endswith("_previous")})
        project(root / "shared", {"auth.xml": render_secret_template(shared, values).replace("\\n", "\n")})
        include = ET.fromstring(files["users.d/auth-methods.xml"]).findtext("include_from")
        self.mounts = [(root / "config", "/etc/clickhouse-server/config.d"),
                       (root / "users", "/etc/clickhouse-server/users.d"),
                       (root / "shared", str(Path(include).parent))]
        for kind in ("user", "probe"):
            project(root / kind, {"users.xml": auth[kind + ".xml"]})
            self.mounts.append((root / kind, "/etc/clickhouse-backup-auth/" + kind))
        if not missing:
            self.credentials("valid")

    def credentials(self, phase):
        base = one(self.documents, "ConfigMap", "otel-backup-auth")["data"]
        for kind, username, secret in (("user", "backup", "ao-clickhouse-backup-credentials"),
                                       ("probe", "backup_probe", "ao-clickhouse-backup-probe-credentials")):
            files = {"users.xml": base[kind + ".xml"]}
            if phase != "missing":
                password = {"valid": self.passwords[username], "empty": "",
                            "special": 'dummy <xml> & "password"'}[phase]
                template = one(self.documents, "ExternalSecret", secret)["spec"]["target"]["template"]["data"]["auth.xml"]
                files["users.d/auth.xml"] = render_secret_template(template, {"password": password, "previous": "-"})
            project(self.root / kind, files)
        if self.started:
            self.sync_credentials()

    def sync_credentials(self):
        # Write rotating projections on Docker's Linux filesystem. Host bind
        # mounts on macOS do not reliably preserve stat() across ..data swaps.
        payload = {}
        args = ["run", "--rm", "-i", "--name", self.name + "-projector", "--network", "none"]
        for kind, volume in self.auth_volumes.items():
            args += ["--mount", f"type=volume,source={volume},target=/projection/{kind}"]
            generation = (self.root / kind / "..data").resolve()
            payload[kind] = {str(path.relative_to(generation)): path.read_text()
                             for path in generation.rglob("*") if path.is_file()}
        script = "import json, sys, uuid\nfrom pathlib import Path\n" + inspect.getsource(replace) + inspect.getsource(project)
        script += "\nfor kind, files in json.load(sys.stdin).items():\n    project(Path('/projection') / kind, files)\n"
        docker(*args, PYTHON_IMAGE, "python", "-c", script, input=json.dumps(payload))

    def start(self, wait=True):
        self.sync_credentials()
        args = ["run", "-d", "--name", self.name, "--network", self.network,
                "--cpus", "2", "--memory", "2g", "--env", "CLICKHOUSE_SKIP_USER_SETUP=1",
                "--env", "AWS_EC2_METADATA_DISABLED=true",
                "--env", "AWS_ACCESS_KEY_ID=" + (self.s3_credentials or {}).get("access_key", "dummy"),
                "--env", "AWS_SECRET_ACCESS_KEY=" + (self.s3_credentials or {}).get("secret_key", "dummy"),
                "--tmpfs", "/var/log/clickhouse-server:rw,size=33554432"]
        if self.data_volume:
            args += ["--mount", f"type=volume,source={self.data_volume},target=/var/lib/clickhouse"]
        else:
            args += ["--tmpfs", "/var/lib/clickhouse:rw,size=536870912"]
        if self.network == "none":
            args += ["--hostname", "localhost"]
        for source, target in self.mounts:
            if source.name in self.auth_volumes:
                args += ["--mount", f"type=volume,source={self.auth_volumes[source.name]},target={target},readonly"]
            else:
                args += ["--mount", f"type=bind,source={source},target={target},readonly"]
        docker(*args, CH_IMAGE)
        self.started = True
        if wait:
            self.wait()

    def query(self, query="SELECT 1", user="admin", password=None):
        password = self.passwords[user] if password is None else password
        return docker("exec", self.name, "clickhouse-client", "--user", user,
                      "--password", password, "--query", query, check=False, timeout=8)

    def running(self):
        return docker("inspect", "--format", "{{.State.Running}}", self.name).stdout.strip() == "true"

    def wait(self):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.query().returncode == 0:
                return
            if not self.running():
                break
            time.sleep(0.5)
        raise AssertionError("ClickHouse did not start with the rendered configuration")

    def restart(self):
        docker("restart", "--time", "2", self.name)
        self.wait()

    def close(self):
        docker("rm", "-f", "-v", self.name, self.name + "-projector", check=False)
        for volume in self.auth_volumes.values():
            docker("volume", "rm", volume, check=False)
        if self.data_volume:
            docker("volume", "rm", self.data_volume, check=False)


@unittest.skipUnless(os.environ.get("RUN_BACKUP_RUNTIME") == "1", "opt-in local Docker test")
class BackupRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = render("tls.enabled=false", "clickhouse.admin.enabled=true",
                               "clickhouse.admin.externalSecret.secretStoreRef.name=ci-placeholder",
                               "clickhouse.admin.externalSecret.remoteRef.key=ci-admin")
        cls.passwords = {name: "dummy-" + uuid.uuid4().hex for name in
                         ("admin", "otel", "schema_owner", "llm_worker", "monte_carlo", "backup", "backup_probe")}

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="backup-runtime-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sequence = 0

    def db(self, **options):
        self.sequence += 1
        database = Database(self.root / str(self.sequence), self.documents, self.passwords, **options)
        self.addCleanup(database.close)
        database.start(wait=not options.get("access_check"))
        return database

    def ordinary_users_work(self, database):
        for user in ("otel", "schema_owner", "llm_worker", "monte_carlo"):
            self.assertEqual(database.query(user=user).returncode, 0, "Ordinary user login failed: " + user)
        self.assertTrue(database.running())

    def test_missing_optional_credentials_preserve_ordinary_users_on_start_and_restart(self):
        database = self.db(missing=True)
        for _ in range(2):
            self.ordinary_users_work(database)
            for user in ("backup", "backup_probe"):
                self.assertNotEqual(database.query(user=user, password="").returncode, 0)
            database.restart()

    def test_optional_credentials_arrive_change_and_disappear_without_passwordless_users(self):
        database = self.db(missing=True)
        for phase in ("valid", "empty", "special", "missing"):
            with self.subTest(phase=phase):
                database.credentials(phase)
                self.assertEqual(database.query("SYSTEM RELOAD CONFIG").returncode, 0)
                for restart in (False, True):
                    if restart:
                        database.restart()
                    self.ordinary_users_work(database)
                    for user in ("backup", "backup_probe"):
                        self.assertNotEqual(database.query(user=user, password="").returncode, 0)
                        self.assertNotEqual(database.query(user=user, password="-").returncode, 0)
                        if phase in ("valid", "special"):
                            password = self.passwords[user] if phase == "valid" else 'dummy <xml> & "password"'
                            self.assertEqual(database.query(user=user, password=password).returncode, 0)
                        else:
                            count = database.query("SELECT count() FROM system.users WHERE name='" + user + "'")
                            self.assertEqual(count.stdout.strip(), "0")

    def test_unreachable_s3_does_not_block_startup_but_negative_control_fails(self):
        database = self.db()
        self.assertIn("backup.xml", database.rendered_config_keys)
        expected = {Path(name).name for name in one(self.documents, "ClickHouseInstallation")["spec"]["configuration"]["files"]
                    if name.startswith("config.d/")}
        self.assertEqual(database.rendered_config_keys, expected)
        self.ordinary_users_work(database)
        database.restart()
        failing = self.db(access_check=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and failing.running():
            time.sleep(0.5)
        self.assertFalse(failing.running(), "Without skip_access_check the unreachable disk must fail startup")
        logs = docker("logs", failing.name)
        self.assertIn("backups_s3", logs.stdout + logs.stderr)

    def probe_result(self, database, replicas=2):
        script = """import json, os, sys
sys.path.insert(0, '/test')
import run_backup as backup
api = backup.API('http://127.0.0.1:8123', 'backup_probe', os.environ['PROBE_PASSWORD'], backup_api=False)
rows = api.request('GET', '/', {'query': backup.REPLICA_QUERY})
try:
    fresh = backup.replica_is_fresh(api, replica_count=int(sys.argv[1]))
    print(json.dumps({'fresh': fresh, 'rows': rows}))
except backup.ReplicaError:
    print(json.dumps({'metadata_error': True, 'rows': rows}))
"""
        result = docker("run", "--rm", "--network", "container:" + database.name,
                        "--env", "PROBE_PASSWORD=" + self.passwords["backup_probe"],
                        "--mount", f"type=bind,source={CHART / 'files/clickhouse-backup'},target=/test,readonly",
                        PYTHON_IMAGE, "python", "-c", script, str(replicas))
        return json.loads(result.stdout)

    def test_real_http_probe_query_with_rendered_grants_and_replicated_table(self):
        database = self.db(keeper=True)
        self.assertEqual(database.query("CREATE DATABASE otel_traces").returncode, 0)
        self.assertEqual(database.query("CREATE TABLE otel_traces.plain (id UInt64) ENGINE=MergeTree ORDER BY id").returncode, 0)
        self.assertEqual(self.probe_result(database, replicas=1), {"fresh": True, "rows": []})
        self.assertEqual(self.probe_result(database), {"metadata_error": True, "rows": []})
        self.assertEqual(database.query("CREATE TABLE otel_traces.replicated (id UInt64) ENGINE=ReplicatedMergeTree('/test/replicated', 'one') ORDER BY id").returncode, 0)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.probe_result(database)
            if result.get("fresh"):
                break
            time.sleep(0.5)
        self.assertTrue(result.get("fresh"), "The real HTTP freshness query must accept the caught-up table")
        self.assertEqual([row["table"] for row in result["rows"]], ["replicated"])
        for row in result["rows"]:
            for field in ("is_readonly", "is_session_expired", "absolute_delay"):
                self.assertIs(type(row[field]), int)
        self.assertNotEqual(database.query("SELECT * FROM otel_traces.plain", user="backup_probe").returncode, 0)

    def start_wrapper(self, database, *, revision="revision-1"):
        name = "backup-runtime-api-" + uuid.uuid4().hex[:10]
        config, api = self.root / "config", self.root / "api"
        config.mkdir(mode=0o755, exist_ok=True)
        api.mkdir(mode=0o755, exist_ok=True)
        project(self.root / "scripts", {"start-backup.sh": WRAPPER.read_text()})
        self.addCleanup(lambda: docker("rm", "-f", "-v", name, check=False))
        args = ["run", "-d", "--name", name, "--network", "container:" + database.name,
               "--user", "101:101", "--read-only", "--tmpfs", "/tmp:rw,size=33554432,uid=101,gid=101",
               "--tmpfs", "/tmp/clickhouse-backup:rw,size=1048576,uid=101,gid=101",
               "--env", "BACKUP_PASSWORD_REVISION=" + revision,
               "--mount", f"type=bind,source={config},target=/etc/clickhouse-backup,readonly",
               "--mount", f"type=bind,source={api},target=/etc/clickhouse-backup-api,readonly",
               "--mount", f"type=bind,source={self.root / 'scripts'},target=/scripts,readonly",
               "--entrypoint", "/bin/sh"]
        if database.data_volume:
            args += ["--mount", f"type=volume,source={database.data_volume},target=/var/lib/clickhouse"]
        docker(*args, BACKUP_IMAGE, "/scripts/start-backup.sh")
        return name

    def config_source(self, password=None):
        template = one(self.documents, "ExternalSecret", "ao-clickhouse-backup-credentials")["spec"]["target"]["template"]["data"]["config.yml"]
        config = yaml.safe_load(render_secret_template(template, {"password": self.passwords["backup"]}))
        # Keep the rendered embedded-backup and stock timeout settings. All
        # operations here are local; no remote catalog is needed for API checks.
        config["general"]["remote_storage"] = "none"
        return {"config.yml": yaml.safe_dump(config), "password": self.passwords["backup"] if password is None else password}

    def update_projection(self, directory, files):
        project(directory, files)
        # The next poll must explain this update, not an earlier missing key.
        return datetime.now(timezone.utc).isoformat()

    def assert_waiting(self, name, reason, *, since=None):
        since = since or docker("inspect", "--format", "{{.State.StartedAt}}", name).stdout.strip()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            logs = docker("logs", "--since", since, name)
            if "Waiting for backup credentials: " + reason + "." in logs.stdout + logs.stderr:
                break
            time.sleep(0.2)
        else:
            self.fail("The startup wrapper never reported the current waiting reason: " + reason)
        # Observe at least two complete 2-second polls after the wrapper starts.
        deadline = time.monotonic() + 4.2
        while time.monotonic() < deadline:
            self.assertEqual(docker("inspect", "--format", "{{.State.Running}}", name).stdout.strip(), "true")
            command = docker("exec", name, "cat", "/proc/1/cmdline").stdout
            self.assertIn("start-backup.sh", command, "The child started before required credentials existed")
            time.sleep(0.3)
        self.assert_clean_logs(name)

    def request_status(self, database, path="/backup/status", password="dummy-api-password"):
        script = """import base64, sys, urllib.request, urllib.error
request = urllib.request.Request('http://127.0.0.1:7171' + sys.argv[1])
if sys.argv[2]:
    request.add_header('Authorization', 'Basic ' + base64.b64encode(('backup:' + sys.argv[2]).encode()).decode())
try:
    print(urllib.request.urlopen(request, timeout=2).status)
except urllib.error.HTTPError as error:
    print(error.code)
"""
        result = docker("run", "--rm", "--network", "container:" + database.name,
                        PYTHON_IMAGE, "python", "-c", script, path, password, check=False, timeout=10)
        return int(result.stdout.strip()) if result.returncode == 0 else None

    def assert_started(self, name, database):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.request_status(database) == 200:
                self.assert_clean_logs(name)
                self.assertEqual(self.request_status(database, password=""), 401)
                return
            time.sleep(0.5)
        self.fail("The stock authenticated backup API did not start")

    def assert_clean_logs(self, name):
        logs = docker("logs", name)
        for password in [*self.passwords.values(), "dummy-api-password", "rotated-api-password"]:
            self.assertNotIn(password, logs.stdout + logs.stderr)

    def test_stock_wrapper_waits_for_database_configuration_and_password(self):
        database = self.db()
        project(self.root / "api", {"password": "dummy-api-password", "revision": "revision-1"})
        name = self.start_wrapper(database)
        self.assert_waiting(name, "database configuration is missing")
        source = self.config_source()
        # The rendered source may omit/empty its YAML password: the separate
        # nonempty file supplies CLICKHOUSE_PASSWORD to the stock process.
        config = yaml.safe_load(source["config.yml"])
        config["clickhouse"]["password"] = ""
        source["config.yml"] = yaml.safe_dump(config)
        since = self.update_projection(self.root / "config", {"config.yml": source["config.yml"]})
        self.assert_waiting(name, "database password is missing", since=since)
        since = self.update_projection(self.root / "config", self.config_source(password=""))
        self.assert_waiting(name, "database password is empty", since=since)
        invalid = dict(source, **{"config.yml": "clickhouse: [" + self.passwords["backup"]})
        since = self.update_projection(self.root / "config", invalid)
        self.assert_waiting(name, "database configuration is not valid", since=since)
        # Positive case: an empty YAML password is valid when the separate
        # password file is present and nonempty.
        self.assertEqual(yaml.safe_load(source["config.yml"])["clickhouse"]["password"], "")
        self.assertTrue(source["password"])
        project(self.root / "config", source)
        self.assert_started(name, database)

    def test_stock_wrapper_waits_for_empty_or_mismatched_api_credentials(self):
        database = self.db()
        project(self.root / "config", self.config_source())
        name = self.start_wrapper(database)
        self.assert_waiting(name, "API revision is missing or does not match")
        since = self.update_projection(self.root / "api", {"password": "", "revision": "revision-1"})
        self.assert_waiting(name, "API password is empty", since=since)
        since = self.update_projection(self.root / "api", {"password": "dummy-api-password", "revision": "old-revision"})
        self.assert_waiting(name, "API revision is missing or does not match", since=since)
        since = self.update_projection(self.root / "api", {"password": "dummy-api-password", "revision": "revision-1\n"})
        self.assert_waiting(name, "API revision is missing or does not match", since=since)
        project(self.root / "api", {"password": "dummy-api-password", "revision": "revision-1"})
        self.assert_started(name, database)

    def test_stock_wrapper_requires_an_expected_revision(self):
        database = self.db()
        project(self.root / "config", self.config_source())
        project(self.root / "api", {"password": "dummy-api-password", "revision": "revision-1"})
        name = self.start_wrapper(database, revision="")
        self.assert_waiting(name, "expected API revision is missing")

    def test_stock_api_keeps_startup_credentials_and_configuration_until_restart(self):
        database = self.db()
        source = self.config_source()
        project(self.root / "config", source)
        project(self.root / "api", {"password": "dummy-api-password", "revision": "revision-1"})
        name = self.start_wrapper(database)
        self.assert_started(name, database)
        snapshot = docker("exec", name, "cat", "/tmp/clickhouse-backup/config.yml").stdout
        self.assertEqual(yaml.safe_load(snapshot), yaml.safe_load(source["config.yml"]))
        changed = yaml.safe_load(source["config.yml"])
        changed["clickhouse"].update(host="127.0.0.2", password="rotated-database-password")
        project(self.root / "config", {"config.yml": yaml.safe_dump(changed), "password": "rotated-database-password"})
        project(self.root / "api", {"password": "rotated-api-password", "revision": "revision-2"})
        self.assertEqual(docker("exec", name, "cat", "/tmp/clickhouse-backup/config.yml").stdout, snapshot)
        self.assertEqual(self.request_status(database), 200)
        self.assertEqual(self.request_status(database, path="/backup/tables"), 200)
        for path in ("/etc/clickhouse-backup/password", "/etc/clickhouse-backup-api/password"):
            self.assertNotEqual(docker("exec", name, "sh", "-c", 'echo forbidden >> "$1"', "_", path, check=False).returncode, 0)
        self.assert_clean_logs(name)

    def runtime_python(self, namespace, script, **environment):
        name = "backup-runtime-client-" + uuid.uuid4().hex[:10]
        self.addCleanup(lambda: docker("rm", "-f", name, check=False))
        args = ["run", "--rm", "--name", name, "--network", "container:" + namespace,
                "--mount", f"type=bind,source={CHART / 'files/clickhouse-backup'},target=/test,readonly"]
        for key, value in environment.items():
            args += ["--env", key + "=" + value]
        result = docker(*args, PYTHON_IMAGE, "python", "-c", script, timeout=240, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_real_scheduler_creates_full_then_incremental_with_stock_api_and_s3(self):
        storage = "backup-runtime-s3-" + uuid.uuid4().hex[:10]
        credentials = {"access_key": "runtime-" + uuid.uuid4().hex, "secret_key": uuid.uuid4().hex}
        self.addCleanup(lambda: docker("rm", "-f", "-v", storage, check=False))
        # Every service shares this isolated network namespace. No test service
        # can reach AWS, and no ports are published on the developer's machine.
        docker("run", "-d", "--name", storage, "--network", "none", "--memory", "512m",
               "--tmpfs", "/data:rw,size=268435456,uid=65532,gid=65532",
               "--env", "MINIO_ROOT_USER=" + credentials["access_key"],
               "--env", "MINIO_ROOT_PASSWORD=" + credentials["secret_key"],
               S3_IMAGE, "server", "/data", "--address", ":9001", "--console-address", ":9002")
        s3_environment = {"S3_ACCESS_KEY": credentials["access_key"], "S3_SECRET_KEY": credentials["secret_key"]}
        s3_script = "import json, os, time, urllib.request\n" + inspect.getsource(local_s3_request)
        self.runtime_python(storage, s3_script + """
deadline = time.monotonic() + 30
while True:
    try:
        with urllib.request.urlopen('http://127.0.0.1:9001/minio/health/ready', timeout=2) as response:
            assert response.status == 200
        break
    except OSError:
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.2)
local_s3_request('PUT', '/runtime-backups', os.environ['S3_ACCESS_KEY'], os.environ['S3_SECRET_KEY'])
""", **s3_environment)
        database = self.db(network="container:" + storage, s3_credentials=credentials)
        for query in ("CREATE DATABASE otel_traces",
                      "CREATE TABLE otel_traces.runtime_sample (id UInt64, message String) ENGINE=MergeTree ORDER BY id",
                      "INSERT INTO otel_traces.runtime_sample VALUES (1, 'first batch')"):
            self.assertEqual(database.query(query).returncode, 0)
        source = self.config_source()
        config = yaml.safe_load(source["config.yml"])
        config["general"]["remote_storage"] = "s3"
        config["s3"].update(endpoint="http://127.0.0.1:9001", bucket="runtime-backups", region="us-east-1",
                            path="test/catalog", force_path_style=True, disable_ssl=True, **credentials)
        source["config.yml"] = yaml.safe_dump(config)
        project(self.root / "config", source)
        project(self.root / "api", {"password": "dummy-api-password", "revision": "revision-1"})
        name = self.start_wrapper(database)
        self.assert_started(name, database)
        scheduler_script = """import json, os, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, '/test')
import run_backup as backup
api = backup.API('http://127.0.0.1:7171', 'backup', 'dummy-api-password')
probe = backup.API('http://127.0.0.1:8123', 'backup_probe', os.environ['PROBE_PASSWORD'], backup_api=False)
now = datetime.fromisoformat(os.environ['TEST_DAY']) + timedelta(hours=int(os.environ['TEST_HOUR']))
name = backup.Scheduler([api], [probe], timeout=180, poll_seconds=0.2).run(now)
print(json.dumps({'name': name, 'remote': api.request('GET', '/backup/list/remote')}))
"""
        environment = {"PROBE_PASSWORD": self.passwords["backup_probe"],
                       "TEST_DAY": datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()}
        full = json.loads(self.runtime_python(database.name, scheduler_script, TEST_HOUR="0", **environment).splitlines()[-1])
        self.assertEqual(database.query("INSERT INTO otel_traces.runtime_sample VALUES (2, 'second batch')").returncode, 0)
        incremental = json.loads(self.runtime_python(database.name, scheduler_script, TEST_HOUR="4", **environment).splitlines()[-1])
        self.assertTrue(full["name"].startswith("ao-otel-full-"))
        self.assertTrue(incremental["name"].startswith("ao-otel-incremental-"))
        catalog = {row["name"]: row for row in incremental["remote"]}
        self.assertEqual(set(catalog), {full["name"], incremental["name"]})
        for backup_name, required in ((full["name"], ""), (incremental["name"], full["name"])):
            with self.subTest(backup=backup_name):
                self.assertEqual(catalog[backup_name]["desc"], "directory, embedded")
                self.assertEqual(catalog[backup_name]["location"], "remote")
                self.assertEqual(catalog[backup_name]["required"], required)
                metadata = json.loads(self.runtime_python(storage, s3_script + """
print(local_s3_request('GET', '/runtime-backups/test/catalog/' + os.environ['BACKUP_NAME'] + '/metadata.json',
                      os.environ['S3_ACCESS_KEY'], os.environ['S3_SECRET_KEY']).decode())
""", BACKUP_NAME=backup_name, **s3_environment))
                self.assertEqual(metadata["backup_name"], backup_name)
                self.assertEqual(metadata.get("required_backup", ""), required)
                self.assertIn("embedded", metadata["tags"])
                self.assertEqual(metadata["tables"], [{"database": "otel_traces", "table": "runtime_sample"}])
        self.assert_clean_logs(name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
