#!/usr/bin/env python3
"""Opt-in Docker regression for the two-step backup authentication upgrade.

HELM=/path/to/helm RUN_BACKUP_AUTH_UPGRADE=1 python3 <this file> -v
Requires PyYAML, Docker, and locally available ClickHouse 26.4.3, stock backup
2.8.1, and the patched backup image. Override BACKUP_OLD_IMAGE/BACKUP_NEW_IMAGE.
All passwords are generated test values; no Kubernetes or AWS calls are made.
"""

from itertools import permutations
import os
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
from test_backup_chart import one, pod, render, render_secret_template

CH_IMAGE = os.environ.get("CLICKHOUSE_TEST_IMAGE", "clickhouse/clickhouse-server:26.4.3")
OLD_IMAGE = os.environ.get("BACKUP_OLD_IMAGE", "altinity/clickhouse-backup:2.8.1")
NEW_IMAGE = os.environ.get("BACKUP_NEW_IMAGE", "ao-clickhouse-backup:2.8.1-mc.1")
BASE = """<clickhouse><logger><level>error</level></logger>
<max_thread_pool_size>256</max_thread_pool_size><background_schedule_pool_size>16</background_schedule_pool_size>
<background_buffer_flush_schedule_pool_size>4</background_buffer_flush_schedule_pool_size>
<background_message_broker_schedule_pool_size>4</background_message_broker_schedule_pool_size>
<background_distributed_schedule_pool_size>4</background_distributed_schedule_pool_size></clickhouse>"""
# Exact legacy user definition, previously part of users.d/auth-methods.xml.
LEGACY_BACKUP = """<backup><networks><ip>127.0.0.1</ip><ip>::1</ip></networks>
<grants><query>GRANT BACKUP ON otel_traces.*</query>
<query>GRANT SHOW TABLES, SHOW DATABASES ON otel_traces.*</query>
<query>GRANT SELECT ON system.*</query></grants>
<auth_methods incl="backup_auth_methods"/></backup>"""


def docker(*args, check=True, timeout=40):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise AssertionError(f"Docker {args[0]} failed: {result.stderr[-2000:]}")
    return result


def replace(path, value):
    staged = path.with_suffix(".pending")
    staged.write_text(value)
    staged.replace(path)


class Database:
    def __init__(self, root, files, users, shared, optional=None):
        self.root = root
        self.name = "backup-upgrade-" + uuid.uuid4().hex[:10]
        for name in ("config", "users", "shared", "user", "probe", "new-config", "backup-config"):
            (root / name).mkdir(parents=True, exist_ok=True)
        replace(root / "config/base.xml", BASE)
        # Model the operator's read-only common ConfigMap mount exactly. In
        # particular, do not silently put new store paths into the old mount.
        for name, contents in files.items():
            if name == "config.d/backup-users.xml":
                replace(root / "config/backup-users.xml", contents)
        replace(root / "users/auth-methods.xml", users)
        replace(root / "shared/auth.xml", shared)
        mounts = [(root / "config", "/etc/clickhouse-server/config.d"),
                  (root / "users", "/etc/clickhouse-server/users.d"),
                  (root / "shared", "/etc/clickhouse-server/secrets.d/auth-methods.xml/ao-clickhouse-auth-methods")]
        if optional:
            for key in ("user", "probe"):
                replace(root / key / "users.xml", optional[key]["users"])
                replace(root / key / "auth.xml", optional[key]["auth"])
                mounts.append((root / key, "/etc/clickhouse-backup-auth/" + key))
            replace(root / "new-config/backup-users.xml", optional["stores"])
            # A nested file bind into the read-only common directory reproduces
            # the new Pod template's ConfigMap subPath mount.
            mounts.append((root / "new-config/backup-users.xml", "/etc/clickhouse-server/config.d/backup-users.xml"))
        command = ["run", "-d", "--name", self.name, "--network", "none", "--hostname", "localhost",
                   "--cpus", "2", "--memory", "2g", "--env", "CLICKHOUSE_SKIP_USER_SETUP=1",
                   "--tmpfs", "/var/lib/clickhouse:rw,size=536870912",
                   "--tmpfs", "/var/log/clickhouse-server:rw,size=33554432"]
        for source, target in mounts:
            command += ["--mount", f"type=bind,source={source},target={target},readonly"]
        docker(*command, CH_IMAGE)
        self.wait()

    def query(self, query="SELECT 1", user="default", password=None):
        args = ["exec", self.name, "clickhouse-client", "--user", user, "--query", query]
        if password is not None:
            args += ["--password", password]
        return docker(*args, check=False)

    def wait(self):
        for _ in range(40):
            if self.query().returncode == 0:
                return
            if self.state().split()[0] != "true":
                break
            time.sleep(0.25)
        raise AssertionError("ClickHouse did not start with this auth configuration")

    def state(self):
        return docker("inspect", "--format", "{{.State.Running}} {{.State.Pid}} {{.RestartCount}}", self.name).stdout.strip()

    def restart(self):
        docker("restart", "--time", "2", self.name)
        self.wait()

    def close(self):
        docker("rm", "-f", "-v", self.name, check=False)


@unittest.skipUnless(os.environ.get("RUN_BACKUP_AUTH_UPGRADE") == "1", "opt-in local Docker test")
class AuthUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = render("clickhouse.backup.migration.keepSharedCredentials=true", "clickhouse.backup.schedule.suspend=true")
        cls.final = render("clickhouse.backup.schedule.suspend=true")
        cls.passwords = {name: "local-" + uuid.uuid4().hex for name in
                         ("otel", "schema_owner", "llm_worker", "monte_carlo", "backup", "backup_probe")}
        cls.users = one(cls.final, "ClickHouseInstallation")["spec"]["configuration"]["files"]["users.d/auth-methods.xml"]
        legacy = ET.fromstring(cls.users)
        legacy.find("users").append(ET.fromstring(LEGACY_BACKUP))
        cls.old_users = ET.tostring(legacy, encoding="unicode")
        # The old shared template differs only by this legacy subtree. Reusing
        # ordinary identities from the actual render keeps real grants in test.
        cls.old_auth = cls.shared(cls.bridge)
        if "backup_auth_methods" not in cls.old_auth:
            raise AssertionError("Migration render lost the legacy shared credentials")

    @classmethod
    def shared(cls, documents):
        template = one(documents, "ExternalSecret", "ao-clickhouse-auth-methods")["spec"]["target"]["template"]["data"]["auth.xml"]
        values = {name + "_password": password for name, password in cls.passwords.items()}
        return render_secret_template(template, values).replace("\\n", "\n")

    @classmethod
    def optional(cls, documents, missing=False):
        cm = one(documents, "ConfigMap", "otel-backup-auth")["data"]
        result = {"stores": cm["backup-users.xml"]}
        for kind, username, secret in (("user", "backup", "ao-clickhouse-backup-credentials"),
                                       ("probe", "backup_probe", "ao-clickhouse-backup-probe-credentials")):
            template = one(documents, "ExternalSecret", secret)["spec"]["target"]["template"]["data"]["auth.xml"]
            result[kind] = {"users": cm[kind + ".xml"],
                            "auth": cm["empty-auth.xml"] if missing else render_secret_template(template, {"password": cls.passwords[username]})}
        return result

    @classmethod
    def backup_config(cls, documents):
        template = one(documents, "ExternalSecret", "ao-clickhouse-backup-credentials")["spec"]["target"]["template"]["data"]["config.yml"]
        return render_secret_template(template, {"password": cls.passwords["backup"]})

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="backup-auth-upgrade-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sequence = 0

    def db(self, *, old=False, missing=False, documents=None, old_main=False):
        documents = documents or self.bridge
        self.sequence += 1
        files = one(documents, "ClickHouseInstallation")["spec"]["configuration"]["files"]
        for key, text in files.items():
            if key.startswith("config.d/"):
                self.assertNotIn("/etc/clickhouse-backup-auth/", text,
                                 "New auth paths must never reach old Pods through shared files")
        root = self.root / str(self.sequence)
        # Register cleanup before startup so a failing mount/start assertion does
        # not leave a Docker container behind.
        database = Database.__new__(Database)
        self.addCleanup(lambda: database.close() if hasattr(database, "name") else None)
        database.__init__(root, files, self.old_users if old or old_main else self.users,
                          self.old_auth if old or old_main else self.shared(documents),
                          optional=None if old else self.optional(documents, missing))
        return database

    def ordinary(self, database):
        for username in ("otel", "schema_owner", "llm_worker", "monte_carlo"):
            self.assertEqual(database.query(user=username, password=self.passwords[username]).returncode, 0,
                             "Ordinary user became unavailable: " + username)
        self.assertTrue(database.state().startswith("true "))

    def step(self, database, action):
        process = database.state()
        action()
        self.assertEqual(database.query("SYSTEM RELOAD CONFIG").returncode, 0, "Configuration reload failed")
        self.ordinary(database)
        self.assertEqual(database.state(), process, "Configuration reload restarted ClickHouse")
        database.restart()
        self.ordinary(database)

    def test_old_to_migration_every_shared_file_order_and_restart(self):
        # All three externally updated inputs, including the backup helper config.
        for order in permutations(("users", "shared", "sidecar")):
            with self.subTest(order=order):
                database = self.db(old=True)
                config = self.backup_config(self.bridge)
                self.assertEqual(yaml.safe_load(config)["clickhouse"]["timeout"], "4h")
                changes = {
                    "users": lambda: replace(database.root / "users/auth-methods.xml", self.users),
                    "shared": lambda: replace(database.root / "shared/auth.xml", self.shared(self.bridge)),
                    "sidecar": lambda: replace(database.root / "backup-config/config.yml", config),
                }
                for action in order:
                    self.step(database, changes[action])
                database.close()

    def test_migration_to_final_every_shared_file_order_and_restart(self):
        self.assertEqual(pod(one(self.bridge, "ClickHouseInstallation")), pod(one(self.final, "ClickHouseInstallation")),
                         "Finishing migration must not change the Pod template")
        for order in permutations(("users", "shared", "sidecar")):
            with self.subTest(order=order):
                database = self.db()
                changes = {
                    "users": lambda: replace(database.root / "users/auth-methods.xml", self.users),
                    "shared": lambda: replace(database.root / "shared/auth.xml", self.shared(self.final)),
                    "sidecar": lambda: replace(database.root / "backup-config/config.yml", self.backup_config(self.final)),
                }
                for action in order:
                    self.step(database, changes[action])
                    self.assertEqual(database.query(user="backup", password=self.passwords["backup"]).returncode, 0)
                database.close()

    def test_new_pod_can_start_with_stale_main_users_file(self):
        database = self.db(old_main=True)
        self.ordinary(database)
        self.assertEqual(database.query(user="backup", password=self.passwords["backup"]).returncode, 0)
        self.step(database, lambda: replace(database.root / "users/auth-methods.xml", self.users))
        self.assertEqual(database.query(user="backup", password=self.passwords["backup"]).returncode, 0)

    def test_new_pod_with_missing_optional_secrets_starts_and_restarts(self):
        database = self.db(missing=True)
        for _ in range(2):
            self.ordinary(database)
            self.assertEqual(database.query("SELECT count() FROM system.users WHERE name IN ('backup','backup_probe')").stdout.strip(), "0")
            for username in ("backup", "backup_probe"):
                self.assertNotEqual(database.query(user=username).returncode, 0)
            database.restart()

    def test_old_helper_retries_missing_user_and_new_image_overrides_timeout(self):
        database = self.db(old=True)
        config = yaml.safe_load(self.backup_config(self.bridge))
        self.assertEqual(config["clickhouse"]["timeout"], "4h")
        # Only local dependencies: leave embedded-backup settings and 4h timeout
        # intact, disable external storage, point native queries at this container.
        config["general"]["remote_storage"] = "none"
        config["clickhouse"].update(host="127.0.0.1", port=9000, secure=False, skip_verify=False)
        config.setdefault("api", {}).update(username="backup", password="dummy-api", listen="127.0.0.1:7171")
        configpath = database.root / "backup-config/config.yml"
        replace(configpath, yaml.safe_dump(config))
        replace(database.root / "users/auth-methods.xml", self.users)
        self.assertEqual(database.query("SYSTEM RELOAD CONFIG").returncode, 0)
        name = "backup-upgrade-old-" + uuid.uuid4().hex[:10]
        self.addCleanup(lambda: docker("rm", "-f", "-v", name, check=False))
        docker("run", "-d", "--name", name, "--network", "container:" + database.name,
               "--mount", f"type=bind,source={configpath.parent},target=/etc/clickhouse-backup,readonly",
               OLD_IMAGE, "server", "--config", "/etc/clickhouse-backup/config.yml")
        state = docker("inspect", "--format", "{{.State.Running}} {{.State.Pid}} {{.RestartCount}}", name).stdout.strip()
        time.sleep(11)  # Upstream server retries every 5 seconds; observe >2 retries.
        self.assertEqual(docker("inspect", "--format", "{{.State.Running}} {{.State.Pid}} {{.RestartCount}}", name).stdout.strip(), state)
        self.assertTrue(state.startswith("true "))
        logs = docker("logs", name).stdout + docker("logs", name).stderr
        self.assertGreaterEqual(logs.count("Authentication failed"), 2)
        self.assertNotIn("timeout shall be greater", logs)
        # Repair only the dummy DB user and prove the SAME waiting process starts
        # its API, distinguishing a healthy wait from an unrelated stuck process.
        replace(database.root / "users/auth-methods.xml", self.old_users)
        self.assertEqual(database.query("SYSTEM RELOAD CONFIG").returncode, 0)
        for _ in range(25):
            logs = docker("logs", name).stdout + docker("logs", name).stderr
            if "Starting API server" in logs:
                break
            time.sleep(0.4)
        self.assertIn("Starting API server", logs)
        sidecar = next(c for c in pod(one(self.bridge, "ClickHouseInstallation"))["containers"] if c["name"] == "clickhouse-backup")
        timeout = next(e["value"] for e in sidecar["env"] if e["name"] == "CLICKHOUSE_TIMEOUT")
        self.assertEqual(timeout, "10800s")
        output = docker("run", "--rm", "--network", "none", "--env", "CLICKHOUSE_TIMEOUT=" + timeout,
                        "--env", "API_USERNAME=backup", "--env", "API_PASSWORD=dummy-api",
                        "--mount", f"type=bind,source={configpath.parent},target=/etc/clickhouse-backup,readonly",
                        NEW_IMAGE, "print-config", "--config", "/etc/clickhouse-backup/config.yml").stdout
        self.assertEqual(yaml.safe_load(output)["clickhouse"]["timeout"], "10800s")
        docker("run", "--rm", "--network", "none", "--env", "CLICKHOUSE_TIMEOUT=" + timeout,
               "--env", "API_USERNAME=backup", "--env", "API_PASSWORD=dummy-api",
               "--entrypoint", "/usr/local/bin/check-backup-config",
               "--mount", f"type=bind,source={configpath.parent},target=/etc/clickhouse-backup,readonly",
               NEW_IMAGE, "/etc/clickhouse-backup/config.yml")


if __name__ == "__main__":
    unittest.main()
