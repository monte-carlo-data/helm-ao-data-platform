"""Test the built image using only temporary local Docker resources.

BACKUP_TEST_IMAGE=ao-clickhouse-backup:2.8.1-mc.1 python3 images/clickhouse-backup/test_runtime.py
No Kubernetes or AWS commands are used. All credentials are test strings.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid


IMAGE = os.environ.get("BACKUP_TEST_IMAGE", "ao-clickhouse-backup:2.8.1-mc.1")
DB_PASSWORD = "runtime-database-secret"
API_PASSWORD = "runtime-api-secret"


def docker(*args, check=True, timeout=40):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          check=check, timeout=timeout)


def config_text(host="127.0.0.1", password=DB_PASSWORD):
    return f"""general:
  remote_storage: none
clickhouse:
  host: {host}
  port: 9000
  username: backup
  password: {json.dumps(password)}
  timeout: 10800s
  use_embedded_backup_restore: true
  log_sql_queries: false
api:
  listen: 0.0.0.0:7171
  create_integration_tables: false
  complete_resumable_after_restart: false
"""


class DockerTest(unittest.TestCase):
    def setUp(self):
        self.name = "backup-security-" + uuid.uuid4().hex[:10]
        self.temp = tempfile.TemporaryDirectory(prefix="backup-security-")
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.secret = self.root / "secret"
        for directory in (self.config, self.secret):
            directory.mkdir(mode=0o755)
        self.containers = []
        self.networks = []
        self.volumes = []

    def tearDown(self):
        for name in reversed(self.containers):
            docker("rm", "-f", name, check=False)
        for name in self.networks:
            docker("network", "rm", name, check=False)
        for name in self.volumes:
            docker("volume", "rm", name, check=False)
        self.temp.cleanup()

    def write_config(self, password=DB_PASSWORD, host="127.0.0.1"):
        (self.config / "config.yml").write_text(config_text(host, password))
        (self.config / "password").write_text(password)

    def write_api(self, revision="revision-1", password=API_PASSWORD):
        (self.secret / "password").write_text(password)
        (self.secret / "revision").write_text(revision)

    def start_wrapper(self, *, fake=True, revision="revision-1", network="none"):
        command = ["run", "-d", "--name", self.name, "--platform", "linux/amd64",
                   "--user", "101:101", "--network", network,
                   "--mount", f"type=bind,source={self.config},target=/etc/clickhouse-backup,readonly",
                   "--mount", f"type=bind,source={self.secret},target=/etc/clickhouse-backup-api,readonly",
                   "--env", "BACKUP_PASSWORD_REVISION=" + revision,
                   "--entrypoint", "/usr/local/bin/start-backup.sh"]
        if fake:
            child = self.root / "fake-backup"
            child.write_text(
                '#!/bin/sh\nset -eu\n'
                f'[ "$API_PASSWORD" = "{API_PASSWORD}" ]\n'
                '[ "$API_USERNAME" = backup ]\n'
                'printf started > /tmp/started\nexec sleep 3600\n')
            child.chmod(0o755)
            command += ["--read-only", "--tmpfs", "/tmp",
                        "--mount", f"type=bind,source={child},target=/bin/clickhouse-backup,readonly"]
        self.containers.append(self.name)
        docker(*command, IMAGE)

    def assert_waiting(self):
        time.sleep(2.2)
        self.assertEqual(docker("inspect", "-f", "{{.State.Running}}", self.name).stdout.strip(), "true")
        self.assertEqual(docker("exec", self.name, "test", "-e", "/tmp/started", check=False).returncode, 1)
        self.assert_clean_logs()

    def assert_started(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if docker("exec", self.name, "test", "-e", "/tmp/started", check=False).returncode == 0:
                self.assert_clean_logs()
                return
            time.sleep(0.2)
        self.fail("The wrapper did not start the child with valid credentials")

    def assert_clean_logs(self):
        result = docker("logs", self.name)
        for value in (API_PASSWORD, DB_PASSWORD, "submitted-user-secret", "submitted-password-secret", "query-user-secret", "query-password-secret"):
            self.assertNotIn(value, result.stdout + result.stderr)


class BootstrapTests(DockerTest):
    def test_missing_configuration_or_database_password_keeps_container_alive(self):
        self.write_api()
        self.start_wrapper()
        self.assert_waiting()
        (self.config / "config.yml").write_text(config_text())
        self.assert_waiting()
        (self.config / "password").write_text("")
        self.assert_waiting()
        (self.config / "password").write_text(DB_PASSWORD)
        self.assert_started()
        self.assertEqual((self.config / "config.yml").read_text(), config_text())

    def test_missing_empty_and_mismatched_api_credentials_wait(self):
        self.write_config()
        self.start_wrapper()
        self.assert_waiting()
        self.write_api(password="")
        self.assert_waiting()
        self.write_api(revision="old-revision")
        self.assert_waiting()
        self.write_api()
        self.assert_started()

    def test_empty_database_password_in_yaml_cannot_use_defaults(self):
        self.write_config(password="")
        (self.config / "password").write_text(DB_PASSWORD)
        self.write_api()
        self.start_wrapper()
        self.assert_waiting()
        self.write_config()
        self.assert_started()

    def test_projected_secret_generation_and_read_only_mounts(self):
        self.write_config()
        generation = self.secret / "generation-one"
        generation.mkdir(mode=0o755)
        (generation / "password").write_text(API_PASSWORD)
        (generation / "revision").write_text("revision-1")
        (self.secret / "..data").symlink_to(generation.name)
        for key in ("password", "revision"):
            (self.secret / key).symlink_to("..data/" + key)
        self.start_wrapper()
        self.assert_started()
        for path in ("/etc/clickhouse-backup/password", "/etc/clickhouse-backup-api/password"):
            result = docker("exec", self.name, "sh", "-c", 'echo forbidden >> "$1"', "_", path, check=False)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual((generation / "password").read_text(), API_PASSWORD)

    def test_missing_expected_revision_does_not_start(self):
        self.write_config()
        self.write_api()
        self.start_wrapper(revision="")
        self.assert_waiting()

    def test_direct_server_rejects_an_empty_api_password_before_connecting(self):
        self.write_config()
        result = docker("run", "--rm", "--platform", "linux/amd64", "--network", "none",
                        "--mount", f"type=bind,source={self.config},target=/etc/clickhouse-backup,readonly",
                        "--env", "API_USERNAME=backup", "--env", "API_PASSWORD=",
                        "--entrypoint", "/bin/clickhouse-backup", IMAGE, "server", check=False, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("backup API username and password must be nonempty", result.stdout + result.stderr)
        self.assertNotIn(DB_PASSWORD, result.stdout + result.stderr)


class AuthenticationTests(DockerTest):
    def request(self, suffix="", username=None, password=None, raw_header=None):
        command = ["exec", self.name, "curl", "--silent", "--show-error", "--max-time", "2",
                   "--output", "/dev/null", "--write-out", "%{http_code}"]
        if username is not None:
            command += ["--user", f"{username}:{password}"]
        if raw_header is not None:
            command += ["--header", "Authorization: " + raw_header]
        result = docker(*command, self.url + suffix, check=False, timeout=8)
        if result.returncode:
            raise OSError("Temporary API is not reachable")
        return int(result.stdout)

    def test_real_api_never_logs_submitted_header_or_query_credentials(self):
        network = self.name + "-network"
        self.networks.append(network)
        docker("network", "create", "--internal", network)
        database = self.name + "-database"
        self.containers.append(database)
        docker("run", "-d", "--name", database, "--network", network,
               "--env", "CLICKHOUSE_USER=backup", "--env", "CLICKHOUSE_PASSWORD=" + DB_PASSWORD,
               "clickhouse/clickhouse-server:26.4.3")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if docker("exec", database, "clickhouse-client", "--user", "backup", "--password", DB_PASSWORD,
                      "--query", "SELECT 1", check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            self.fail("Temporary ClickHouse did not start")
        self.write_config(host=database)
        self.write_api()
        self.start_wrapper(fake=False, network=network)
        self.url = "http://127.0.0.1:7171/backup/status"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if self.request(username="backup", password=API_PASSWORD) == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            self.fail("The authenticated API did not start")
        self.assertEqual(self.request(), 401)
        self.assertEqual(self.request(username="backup", password=""), 401)
        self.assertEqual(self.request(username="submitted-user-secret", password="submitted-password-secret"), 401)
        self.assertEqual(self.request(raw_header="Basic malformed-secret-header"), 401)
        self.assertEqual(self.request("?user=query-user-secret&pass=query-password-secret"), 401)
        self.assertEqual(self.request("?user=backup&pass=" + API_PASSWORD), 200)
        self.assert_clean_logs()
        logs = docker("logs", self.name)
        self.assertNotIn("malformed-secret-header", logs.stdout + logs.stderr)
        self.assertNotIn("?user=", logs.stdout + logs.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
