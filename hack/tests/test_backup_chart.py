"""Render checks for backup isolation, password rotation, and job wiring.

Run after `helm dependency build charts/ao-data-platform` with PyYAML installed.
Set HELM to use a Helm binary outside PATH. All credentials here are fake.
"""

import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts/ao-data-platform"
HELM = os.environ.get("HELM", "helm")
RELEASE = "ao-backup-test"


def render(*overrides, backup=True, values_files=(), values=None):
    """Render the chart; `values` is written to a temporary file passed as the last -f."""
    command = [HELM, "template", RELEASE, str(CHART), "--namespace", "montecarlo",
               "-f", str(CHART / "ci/lint-values.yaml")]
    if backup:
        command += ["-f", str(CHART / "ci/backup-values.yaml")]
    for values_file in values_files:
        command += ["-f", str(values_file)]
    with contextlib.ExitStack() as stack:
        if values is not None:
            values_file = stack.enter_context(tempfile.NamedTemporaryFile("w", suffix=".yaml"))
            yaml.safe_dump(values, values_file)
            values_file.flush()
            command += ["-f", values_file.name]
        for override in overrides:
            command += ["--set", override]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def one(documents, kind, name=None):
    matches = [doc for doc in documents if doc["kind"] == kind
               and (name is None or doc["metadata"]["name"] == name)]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {kind} {name}, found {len(matches)}")
    return matches[0]


def pod(chi):
    return chi["spec"]["templates"]["podTemplates"][0]["spec"]


def xml_tree(value):
    """Ignore indentation while comparing the actual user definitions."""
    node = ET.fromstring(value)
    for child in node.iter():
        child.text = (child.text or "").strip() or None
        child.tail = None
    return node


def render_secret_template(template, values):
    """Evaluate ESO's Go-template expressions, including the sentinel guard.

    These expressions use Go template/Sprig operations also available in Helm;
    using the real template engine avoids reimplementing the password guard.
    """
    with tempfile.TemporaryDirectory(prefix="ao-backup-template-") as directory:
        chart = Path(directory)
        (chart / "templates").mkdir()
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: secret-test\nversion: 0.1.0\n")
        (chart / "values.yaml").write_text(yaml.safe_dump({"content": template, "passwords": values}))
        (chart / "templates/result.yaml").write_text(
            'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: result\n'
            'data:\n  result: {{ tpl .Values.content .Values.passwords | quote }}\n'
        )
        output = subprocess.run([HELM, "template", "secret-test", str(chart)],
                                capture_output=True, text=True, check=True).stdout
        return yaml.safe_load(output)["data"]["result"]


class BackupChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.enabled = render()
        cls.disabled = render("clickhouse.backup.enabled=false")
        cls.chi = one(cls.enabled, "ClickHouseInstallation")
        cls.name = cls.chi["metadata"]["name"]

    def test_backups_are_opt_in_and_do_not_change_other_workloads(self):
        default = render(backup=False)
        for documents in (default, self.disabled):
            chi = one(documents, "ClickHouseInstallation")
            self.assertNotIn("clickhouse-backup", [c["name"] for c in pod(chi)["containers"]])
            self.assertNotIn("config.d/backup.xml", chi["spec"]["configuration"]["files"])
            self.assertIsNone(xml_tree(chi["spec"]["configuration"]["files"]["users.d/auth-methods.xml"]).find("users/backup"))
            self.assertFalse(any(doc["kind"] == "CronJob" for doc in documents))
            self.assertFalse(any(doc["metadata"]["name"].endswith("-backup-api") for doc in documents))
            for secret in (doc for doc in documents if doc["kind"] == "ExternalSecret"):
                self.assertFalse(any(item["secretKey"].startswith("backup_") for item in secret["spec"].get("data", [])))

        enabled_by_key = {(d["kind"], d["metadata"]["name"]): d for d in self.enabled}
        for document in self.disabled:
            # Backup enablement changes CHI and its TLS SANs, never other users.
            if document["kind"] == "ClickHouseInstallation":
                continue
            if document["kind"] == "Certificate" and document["metadata"]["name"] == "clickhouse-server-tls":
                continue
            key = (document["kind"], document["metadata"]["name"])
            self.assertEqual(document, enabled_by_key[key], key)

    def test_clickhouse_keeps_existing_users_and_backup_cannot_restore(self):
        files = self.chi["spec"]["configuration"]["files"]
        disabled_files = one(self.disabled, "ClickHouseInstallation")["spec"]["configuration"]["files"]
        self.assertEqual(files["users.d/auth-methods.xml"], disabled_files["users.d/auth-methods.xml"])
        self.assertEqual(one(self.enabled, "ExternalSecret", "ao-clickhouse-auth-methods"),
                         one(self.disabled, "ExternalSecret", "ao-clickhouse-auth-methods"))
        for name, expected in (
            ("ao-clickhouse-backup-credentials", {"GRANT BACKUP ON otel_traces.*", "GRANT SELECT ON system.*", "GRANT SHOW TABLES, SHOW DATABASES ON otel_traces.*"}),
            ("ao-clickhouse-backup-probe-credentials", {"GRANT SELECT ON system.replicas", "GRANT SHOW TABLES, SHOW DATABASES ON otel_traces.*"}),
        ):
            with self.subTest(secret=name):
                auth = one(self.enabled, "ExternalSecret", name)["spec"]["target"]["template"]["data"]["auth.xml"]
                user = xml_tree(render_secret_template(auth, {"password": "fake", "previous": "-"})).find("users/" + ("backup_probe" if "probe" in name else "backup"))
                self.assertEqual({n.text for n in user.findall("grants/query")}, expected)
                if name == "ao-clickhouse-backup-credentials":
                    self.assertEqual({n.text for n in user.findall("networks/ip")}, {"127.0.0.1", "::1"})

    def test_database_and_backup_use_the_same_aws_role_and_data_volume(self):
        account = one(self.enabled, "ServiceAccount", "ci-clickhouse-backup")
        self.assertEqual(account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"],
                         "arn:aws:iam::123456789012:role/ao-backup-render-test")
        spec = pod(self.chi)
        self.assertEqual(spec["serviceAccountName"], account["metadata"]["name"])
        sidecar = next(c for c in spec["containers"] if c["name"] == "clickhouse-backup")
        self.assertEqual(sidecar["image"], "altinity/clickhouse-backup:2.8.1@sha256:08016b048f7e6035c048501315c2a788e5a782f15f168e042c7bd48d5a388cc4")
        self.assertEqual(sidecar["command"], ["/bin/sh", "/scripts/start-backup.sh"])
        data_volume = self.chi["spec"]["defaults"]["templates"]["dataVolumeClaimTemplate"]
        mounts = {mount["mountPath"]: mount for mount in sidecar["volumeMounts"]}
        self.assertEqual(mounts["/var/lib/clickhouse"]["name"], data_volume)
        self.assertNotIn("subPath", mounts["/etc/clickhouse-backup"])
        self.assertTrue(mounts["/etc/clickhouse-backup"]["readOnly"])
        env = {item["name"]: item for item in sidecar["env"]}
        self.assertEqual(env["GOMEMLIMIT"]["value"], "400MiB")
        self.assertNotIn("CLICKHOUSE_TIMEOUT", env)
        self.assertNotIn("API_PASSWORD", env)
        self.assertEqual(mounts["/scripts"]["name"], "backup-startup")
        self.assertTrue(mounts["/scripts"]["readOnly"])
        self.assertEqual(mounts["/tmp/clickhouse-backup"]["name"], "backup-runtime")
        startup = one(self.enabled, "ConfigMap", self.name + "-backup-startup")["data"]["start-backup.sh"]
        self.assertEqual(startup.rstrip(), (CHART / "files/clickhouse-backup/start-backup.sh").read_text().rstrip())
        self.assertEqual(env["BACKUP_PASSWORD_REVISION"]["value"], "revision-1")
        self.assertEqual(mounts["/etc/clickhouse-backup-api"]["name"], "backup-api-credentials")
        self.assertNotIn("subPath", mounts["/etc/clickhouse-backup-api"])
        volumes = {v["name"]: v for v in spec["volumes"]}
        self.assertTrue(volumes["backup-startup"]["configMap"]["optional"])
        self.assertEqual(volumes["backup-runtime"]["emptyDir"]["medium"], "Memory")
        self.assertEqual(spec["securityContext"]["fsGroup"], 101)
        self.assertTrue(volumes["backup-config"]["secret"]["optional"])
        self.assertEqual(volumes["backup-config"]["secret"]["items"], [{"key": "config.yml", "path": "config.yml"}, {"key": "password", "path": "password"}])
        self.assertTrue(volumes["backup-api-credentials"]["secret"]["optional"])
        for forbidden in ("CLICKHOUSE_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(forbidden, env)

        disk = xml_tree(self.chi["spec"]["configuration"]["files"]["config.d/backup.xml"])
        self.assertEqual(disk.findtext("s3/use_environment_credentials"), "0")
        destination = disk.find("storage_configuration/disks/backups_s3")
        self.assertEqual(destination.findtext("type"), "s3")
        self.assertEqual(destination.findtext("use_environment_credentials"), "1")
        self.assertEqual(destination.findtext("skip_access_check"), "true")
        self.assertEqual(destination.findtext("endpoint"),
                         "https://s3.us-west-1.amazonaws.com/ao-backup-render-test/render-test/native/")
        self.assertEqual(disk.findtext("backups/allowed_disk"), "backups_s3")
        self.assertIsNone(destination.find("access_key_id"))
        self.assertIsNone(destination.find("secret_access_key"))

    def test_password_rotation_reaches_users_and_the_startup_config_source(self):
        credentials = one(self.enabled, "ExternalSecret", "ao-clickhouse-backup-credentials")
        configuration = credentials["spec"]["target"]["template"]["data"]["config.yml"]
        # Passwords containing quotes/newlines must remain one YAML string.
        password = 'a: "quoted" password\nwith a second line'
        config = yaml.safe_load(render_secret_template(configuration, {"password": password}))
        self.assertEqual(config["clickhouse"]["password"], password)
        self.assertTrue(config["clickhouse"]["use_embedded_backup_restore"])
        self.assertEqual(config["clickhouse"]["embedded_backup_disk"], "backups_s3")
        self.assertEqual(config["clickhouse"]["timeout"], "4h")
        self.assertFalse(config["clickhouse"]["use_embedded_backup_restore_cluster"])
        self.assertFalse(config["general"]["rbac_backup_always"])
        self.assertFalse(config["api"]["allow_parallel"])
        self.assertFalse(config["api"]["create_integration_tables"])
        self.assertEqual(config["s3"]["compression_format"], "none")
        self.assertEqual(config["s3"]["acl"], "")
        self.assertNotIn("password", config["api"])

        for name in ("ao-clickhouse-backup-credentials", "ao-clickhouse-backup-probe-credentials"):
            secret = one(self.enabled, "ExternalSecret", name)
            auth = secret["spec"]["target"]["template"]["data"]["auth.xml"]
            references = {d["secretKey"]: d["remoteRef"] for d in secret["spec"]["data"]}
            self.assertEqual(set(references), {"password", "previous"})
            self.assertEqual(references["password"]["version"], "AWSCURRENT")
            self.assertEqual(references["previous"]["version"], "AWSCURRENT")
            for previous in ("-", "", "previous-password"):
                with self.subTest(secret=name, previous=previous):
                    password = 'contains <xml> & "quotes"'
                    user = xml_tree(render_secret_template(auth, {"password": password, "previous": previous})).find("users/" + ("backup_probe" if "probe" in name else "backup"))
                    self.assertEqual(user.findtext("auth_methods/current/password"), password)
                    self.assertEqual(user.findtext("auth_methods/previous/password"),
                                     previous if previous == "previous-password" else None)
            empty = xml_tree(render_secret_template(auth, {"password": "", "previous": "previous-password"}))
            self.assertIsNone(empty.find("users/" + ("backup_probe" if "probe" in name else "backup")), "An empty current credential must not create a passwordless user")

    def test_missing_backup_secrets_omit_optional_users(self):
        config = one(self.enabled, "ConfigMap", self.name + "-backup-auth")["data"]
        self.assertNotIn("empty-auth.xml", config)
        volumes = {v["name"]: v for v in pod(self.chi)["volumes"]}
        for kind, username in (("user", "backup"), ("probe", "backup_probe")):
            with self.subTest(kind=kind):
                wrapper = xml_tree(config[kind + ".xml"])
                self.assertIsNone(wrapper.find("users"))
                if kind == "probe":
                    self.assertEqual(wrapper.findtext("profiles/backup_probe/readonly"), "2")
                sources = volumes[f"backup-{kind}-auth"]["projected"]["sources"]
                self.assertEqual(sources[0]["configMap"]["items"], [{"key": kind + ".xml", "path": "users.xml"}])
                self.assertTrue(sources[0]["configMap"]["optional"])
                self.assertTrue(sources[1]["secret"]["optional"])
                self.assertEqual(sources[1]["secret"]["items"], [{"key": "auth.xml", "path": "users.d/auth.xml"}])
        self.assertNotIn("config.d/backup-users.xml", self.chi["spec"]["configuration"]["files"])
        stores = xml_tree(config["backup-users.xml"])
        self.assertEqual({n.findtext("path") for n in stores.findall("user_directories/users_xml")},
                         {f"/etc/clickhouse-backup-auth/{k}/users.xml" for k in ("user", "probe")})
        self.assertEqual(volumes["backup-auth-config"]["projected"]["sources"], [
            {"configMap": {"name": "chi-" + self.name + "-common-configd"}},
            {"configMap": {"name": self.name + "-backup-auth", "optional": True,
                           "items": [{"key": "backup-users.xml", "path": "backup-users.xml"}]}},
        ])
        database = next(c for c in pod(self.chi)["containers"] if c["name"] == "clickhouse")
        self.assertIn({"name": "backup-auth-config",
                       "mountPath": "/etc/clickhouse-server/config.d/",
                       "readOnly": True}, database["volumeMounts"])

    def test_projected_sources_have_no_conflicting_file_paths(self):
        # The API server rejects even optional Secret/ConfigMap sources that
        # target the same file. A Docker bind mount does not catch this rule.
        for volume in pod(self.chi)["volumes"]:
            paths = []
            for source in volume.get("projected", {}).get("sources", []):
                for projection in source.values():
                    for item in projection.get("items", []):
                        path = item["path"]
                        for previous in paths:
                            self.assertFalse(path == previous or path.startswith(previous + "/")
                                             or previous.startswith(path + "/"),
                                             f"Conflicting projected paths in {volume['name']}: {previous}, {path}")
                        paths.append(path)

    def test_api_revision_rolls_pods_and_job_can_only_read_pods(self):
        documents = render("clickhouse.backup.api.passwordRevision=revision-2")
        chi = one(documents, "ClickHouseInstallation")
        annotations = chi["spec"]["templates"]["podTemplates"][0]["metadata"]["annotations"]
        self.assertEqual(annotations["backup.montecarlodata.com/password-revision"], "revision-2")
        spec = one(documents, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        env = {v["name"]: v.get("value") for v in spec["containers"][0]["env"]}
        self.assertEqual(env["BACKUP_PASSWORD_REVISION"], "revision-2")
        self.assertEqual(env["BACKUP_PASSWORD_REVISION_FILE"], "/credentials/revision")
        self.assertEqual(env["BACKUP_PROBE_PASSWORD_FILE"], "/probe-credentials/password")
        role = one(documents, "Role", self.name + "-backup-job")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"],
                                           "resourceNames": json.loads(env["BACKUP_POD_NAMES"])}])
        account = one(documents, "ServiceAccount", spec["serviceAccountName"])
        self.assertNotIn("annotations", account["metadata"])
        binding = one(documents, "RoleBinding", self.name + "-backup-job")
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": account["metadata"]["name"], "namespace": "montecarlo"}])
        self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])

    def test_api_external_secret_preserves_paired_password_and_revision(self):
        docs = render("clickhouse.backup.api.existingSecret=",
                      "clickhouse.backup.api.externalSecret.secretStoreRef.name=ci-placeholder",
                      "clickhouse.backup.api.externalSecret.remoteRef.key=ci-api-pair",
                      "clickhouse.backup.api.externalSecret.remoteRef.version=AWSCURRENT")
        secret = one(docs, "ExternalSecret", self.name + "-backup-api")
        self.assertEqual(secret["spec"]["data"], [{"secretKey": "credentials", "remoteRef": {"key": "ci-api-pair", "version": "AWSCURRENT"}}])
        templates = secret["spec"]["target"]["template"]["data"]
        source = {"credentials": json.dumps({"password": "test-secret", "revision": "old-revision"})}
        self.assertEqual(render_secret_template(templates["password"], source), "test-secret")
        self.assertEqual(render_secret_template(templates["revision"], source), "old-revision")
        for item in ("password", "revision"):
            self.assertEqual(render_secret_template(templates[item], {"credentials": "{}"}), "")
        with self.assertRaisesRegex(AssertionError, "exactly one"):
            render("clickhouse.backup.api.externalSecret.secretStoreRef.name=ci-placeholder",
                   "clickhouse.backup.api.externalSecret.remoteRef.key=ci-api-pair")
        with self.assertRaisesRegex(AssertionError, "remoteRef.property must be empty"):
            render("clickhouse.backup.api.existingSecret=",
                   "clickhouse.backup.api.externalSecret.secretStoreRef.name=ci-placeholder",
                   "clickhouse.backup.api.externalSecret.remoteRef.key=ci-api-pair",
                   "clickhouse.backup.api.externalSecret.remoteRef.property=password")

    def test_api_external_source_accepts_minimal_options_and_a_custom_secret_name(self):
        docs = render("clickhouse.backup.api.existingSecret=",
                      "clickhouse.backup.api.externalSecret.secretStoreRef.name=ci-placeholder",
                      "clickhouse.backup.api.externalSecret.remoteRef.key=ci-api",
                      "clickhouse.backup.api.externalSecret.refreshInterval=5m",
                      "clickhouse.backup.api.secret=custom-backup-api")
        secret = one(docs, "ExternalSecret", "custom-backup-api")
        self.assertEqual(secret["spec"]["refreshInterval"], "5m")
        self.assertEqual(secret["spec"]["secretStoreRef"]["kind"], "ClusterSecretStore")
        self.assertEqual(secret["spec"]["target"]["name"], "custom-backup-api")
        references = []
        for spec in (pod(one(docs, "ClickHouseInstallation")),
                     one(docs, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]):
            references += [v["secret"]["secretName"] for v in spec["volumes"] if "secret" in v]
        self.assertEqual(references.count("custom-backup-api"), 2)

    def test_one_job_runs_every_four_hours_without_blind_retries(self):
        job = one(self.enabled, "CronJob")
        self.assertEqual(job["spec"]["schedule"], "0 */4 * * *")
        self.assertEqual(job["spec"]["timeZone"], "Etc/UTC")
        self.assertEqual(job["spec"]["concurrencyPolicy"], "Forbid")
        template = job["spec"]["jobTemplate"]["spec"]
        self.assertEqual(template["backoffLimit"], 0)
        self.assertEqual(template["template"]["spec"]["restartPolicy"], "Never")
        self.assertTrue(template["template"]["spec"]["automountServiceAccountToken"])
        container = template["template"]["spec"]["containers"][0]
        env = {v["name"]: v.get("value") for v in container["env"]}
        endpoints = json.loads(env["BACKUP_ENDPOINTS"])
        services = [d for d in self.enabled if d["kind"] == "Service"
                    and any(p["port"] == 7171 for p in d["spec"]["ports"])]
        self.assertEqual(endpoints, [f"http://{self.name}-backup-{replica}:7171" for replica in range(2)])
        self.assertEqual({url.split("//")[1].split(":")[0] for url in endpoints},
                         {service["metadata"]["name"] for service in services})
        script = one(self.enabled, "ConfigMap", f"{self.name}-backup-job")["data"]["run_backup.py"]
        self.assertEqual(script.rstrip(), (CHART / "files/clickhouse-backup/run_backup.py").read_text().rstrip())
        compile(script, "rendered-run_backup.py", "exec")

    def test_backup_controls_are_private_and_only_the_job_is_allowed(self):
        services = [d for d in self.enabled if d["kind"] == "Service"
                    and any(p["port"] == 7171 for p in d["spec"]["ports"])]
        self.assertEqual(len(services), 2)
        self.assertEqual({s["spec"]["selector"]["clickhouse.altinity.com/replica"] for s in services}, {"0", "1"})
        for service in services:
            self.assertEqual(service["spec"]["type"], "ClusterIP")
            self.assertNotIn("externalIPs", service["spec"])
            self.assertEqual(service["spec"]["selector"]["clickhouse.altinity.com/chi"], self.name)
        for service in self.chi["spec"]["templates"]["serviceTemplates"]:
            self.assertNotIn(7171, [port["port"] for port in service["spec"]["ports"]])

        policy = one(self.enabled, "NetworkPolicy", f"{self.name}-backup-api")["spec"]
        self.assertEqual(policy["podSelector"]["matchLabels"], {"clickhouse.altinity.com/chi": self.name})
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        normal_ports = set()
        job_labels = one(self.enabled, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
        api_rules = []
        for rule in policy["ingress"]:
            self.assertTrue(rule.get("ports"), "An all-port rule would expose backup controls")
            ports = {port["port"] for port in rule["ports"]}
            if 7171 in ports:
                api_rules.append(rule)
                self.assertEqual(rule["from"], [{"podSelector": {"matchLabels": job_labels}}])
            elif not rule.get("from"):
                normal_ports.update(ports)
        self.assertEqual(len(api_rules), 1)
        required = self.database_listener_ports(self.chi)
        self.assertEqual(required, normal_ports)

    @staticmethod
    def database_listener_ports(chi):
        # Derive customer-facing ports from the rendered CHI, not a second copy
        # of the policy's literals. The operator also exposes replication and
        # metrics (9009/9363), and ClickHouse's default native listener (9000).
        ports = {9009, 9363, 9000}
        for service in chi["spec"]["templates"]["serviceTemplates"]:
            ports.update(p["port"] for p in service["spec"]["ports"])
        for container in pod(chi)["containers"]:
            for key in ("readinessProbe", "livenessProbe"):
                probe = container.get(key, {}).get("httpGet", {})
                if "port" in probe:
                    ports.add(probe["port"])
        # The HTTP listener stays enabled even when the client Service uses TLS.
        ports.add(8123)
        settings = chi["spec"]["configuration"]["settings"]
        ports.update(int(settings[k]) for k in ("https_port", "tcp_port_secure") if k in settings)
        return ports

    def test_every_replica_gets_a_matching_backup_address(self):
        for count in (1, 2, 3):
            with self.subTest(replicas=count):
                docs = render(f"clickhouse.replicasCount={count}")
                chi = one(docs, "ClickHouseInstallation")
                cluster = chi["spec"]["configuration"]["clusters"][0]
                job = one(docs, "CronJob")
                env = {v["name"]: v.get("value") for v in job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
                endpoints = json.loads(env["BACKUP_ENDPOINTS"])
                services = [d for d in docs if d["kind"] == "Service" and any(p["port"] == 7171 for p in d["spec"]["ports"])]
                pod_names = json.loads(env["BACKUP_POD_NAMES"])
                self.assertEqual(pod_names, [f"chi-{chi['metadata']['name']}-{cluster['name']}-0-{i}-0" for i in range(count)])
                role = one(docs, "Role", chi["metadata"]["name"] + "-backup-job")
                self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"], "resourceNames": pod_names}])
                self.assertEqual(len(endpoints), count)
                self.assertEqual(len(services), count)
                self.assertEqual(endpoints, [f"http://{chi['metadata']['name']}-backup-{i}:7171" for i in range(count)])
                self.assertEqual({s["metadata"]["name"] for s in services}, {url.split("//")[1].split(":")[0] for url in endpoints})
                self.assertEqual({s["spec"]["selector"]["clickhouse.altinity.com/replica"] for s in services}, {str(i) for i in range(count)})
                for service in services:
                    self.assertEqual(service["spec"]["selector"]["clickhouse.altinity.com/cluster"], cluster["name"])
                    self.assertEqual(service["spec"]["selector"]["clickhouse.altinity.com/shard"], "0")
                    self.assertTrue(service["spec"]["publishNotReadyAddresses"])

    def test_backup_timeouts_and_job_resources_follow_settings(self):
        docs = render("clickhouse.backup.schedule.timeoutSeconds=7200",
                      "clickhouse.backup.schedule.startingDeadlineSeconds=300",
                      "clickhouse.backup.schedule.maxReplicaDelaySeconds=12",
                      "clickhouse.backup.schedule.freshnessRetrySeconds=0",
                      "clickhouse.backup.schedule.resources.requests.cpu=50m",
                      "clickhouse.backup.schedule.resources.limits.memory=256Mi",
                      "clickhouse.backup.sidecar.goMemoryLimit=300MiB")
        secret = one(docs, "ExternalSecret", "ao-clickhouse-backup-credentials")
        config = yaml.safe_load(render_secret_template(secret["spec"]["target"]["template"]["data"]["config.yml"], {"password": "fake"}))
        self.assertEqual(config["clickhouse"]["timeout"], "4h")
        job = one(docs, "CronJob")
        self.assertEqual(job["spec"]["startingDeadlineSeconds"], 300)
        template = job["spec"]["jobTemplate"]["spec"]
        self.assertEqual(template["activeDeadlineSeconds"], 7260)
        runner = template["template"]["spec"]["containers"][0]
        self.assertEqual(runner["resources"]["requests"]["cpu"], "50m")
        self.assertEqual(runner["resources"]["limits"]["memory"], "256Mi")
        env = {entry["name"]: entry.get("value") for entry in runner["env"]}
        self.assertEqual(env["BACKUP_TIMEOUT_SECONDS"], "7200")
        self.assertEqual(env["BACKUP_MAX_REPLICA_DELAY_SECONDS"], "12")
        self.assertEqual(env["BACKUP_FRESHNESS_RETRY_SECONDS"], "0")
        sidecar = next(c for c in pod(one(docs, "ClickHouseInstallation"))["containers"] if c["name"] == "clickhouse-backup")
        self.assertEqual(next(e["value"] for e in sidecar["env"] if e["name"] == "GOMEMLIMIT"), "300MiB")

    def test_backup_image_mirror_must_keep_the_verified_digest(self):
        image = "mirror.example.org/cache/altinity/clickhouse-backup@sha256:08016b048f7e6035c048501315c2a788e5a782f15f168e042c7bd48d5a388cc4"
        docs = render("clickhouse.backup.sidecar.image=" + image)
        sidecar = next(container for container in pod(one(docs, "ClickHouseInstallation"))["containers"]
                       if container["name"] == "clickhouse-backup")
        self.assertEqual(sidecar["image"], image)

    def test_backup_notes_warn_when_the_job_deadline_reaches_the_next_run(self):
        # Render the real NOTES source as text; helm template omits NOTES.txt.
        source = (CHART / "templates/NOTES.txt").read_text()
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        values["clickhouse"]["backup"]["enabled"] = True
        for timeout in (14339, 14340, 14399, 14400):
            with self.subTest(timeout=timeout):
                values["clickhouse"]["backup"]["schedule"]["timeoutSeconds"] = timeout
                notes = render_secret_template(source, {
                    "Values": values, "Release": {"Namespace": "render-test"},
                })
                self.assertIn(f"scheduler waits up to {timeout}\nseconds", notes)
                self.assertIn(f"Job deadline to {timeout + 60}\nseconds", notes)
                self.assertEqual("WARNING:" in notes, timeout >= 14340)

    def test_database_probe_tls_matches_certificates_and_mounts_only_public_ca(self):
        for tls in (True, False):
            with self.subTest(tls=tls):
                docs = render("tls.enabled=" + str(tls).lower())
                chi = one(docs, "ClickHouseInstallation")
                spec = one(docs, "CronJob")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                env = {v["name"]: v.get("value") for v in spec["containers"][0]["env"]}
                endpoints = json.loads(env["BACKUP_DATABASE_ENDPOINTS"])
                scheme, port = ("https", 8443) if tls else ("http", 8123)
                hosts = [self.name + "-backup-" + str(i) for i in range(2)]
                self.assertEqual(endpoints, [f"{scheme}://{h}:{port}" for h in hosts])
                for host in hosts:
                    service = one(docs, "Service", host)
                    self.assertIn({"name": "database-probe", "port": port, "targetPort": port}, service["spec"]["ports"])
                if tls:
                    names = one(docs, "Certificate", "clickhouse-server-tls")["spec"]["dnsNames"]
                    self.assertTrue(set(hosts) <= set(names))
                    ca = next(v["secret"] for v in spec["volumes"] if v["name"] == "clickhouse-ca")
                    self.assertEqual(ca["items"], [{"key": "ca.crt", "path": "ca.crt"}])
                    self.assertEqual(env["BACKUP_DATABASE_CA_FILE"], "/clickhouse-ca/ca.crt")
                else:
                    self.assertNotIn("BACKUP_DATABASE_CA_FILE", env)
                    self.assertFalse(any(v["name"] == "clickhouse-ca" for v in spec["volumes"]))

    def test_extra_database_listener_does_not_expose_backup_controls(self):
        docs = render("clickhouse.backup.networkPolicy.additionalPorts[0]=9004")
        chi = one(docs, "ClickHouseInstallation")
        policy = one(docs, "NetworkPolicy", f"{chi['metadata']['name']}-backup-api")["spec"]
        public_ports = {p["port"] for r in policy["ingress"] if not r.get("from") for p in r["ports"]}
        self.assertEqual(public_ports, self.database_listener_ports(chi) | {9004})
        self.assertNotIn(7171, public_ports)

    def test_unsafe_or_incomplete_backup_configuration_fails_rendering(self):
        cases = {
            "clickhouse.backup.provider=gke": "only aws",
            "clickhouse.backup.aws.bucket=": "aws.bucket",
            "clickhouse.backup.aws.region=": "aws.region",
            "clickhouse.backup.aws.roleArn=": "aws.roleArn",
            "clickhouse.backup.aws.path=../other": "safe directory names",
            "clickhouse.backup.serviceAccount.name=default": "dedicated account",
            "clickhouse.backup.api.existingSecret=": "exactly one",
            "clickhouse.backup.api.passwordRevision=": "passwordRevision",
            "clickhouse.backup.sidecar.image=": "verified Altinity 2.8.1 digest",
            "clickhouse.backup.sidecar.image=altinity/clickhouse-backup:2.8.1": "verified Altinity 2.8.1 digest",
            "clickhouse.backup.sidecar.image=mirror.example.org/backup@sha256:" + "0" * 64: "verified Altinity 2.8.1 digest",
            "clickhouse.backup.probe.secret=ao-clickhouse-backup-credentials": "separate Secrets",
            "clickhouse.backup.user.secret=ao-clickhouse-auth-methods": "shared auth bundle",
            "clickhouse.backup.probe.secret=ao-clickhouse-otel-credentials": "another ClickHouse user",
            "clickhouse.backup.probe.externalSecret.remoteRef.key=": "backup_probe",
            "clickhouse.backup.api.existingSecret=ao-clickhouse-backup-credentials": "separate",
            "clickhouse.backup.user.secret=": "backup.user.secret",
            "clickhouse.backup.user.externalSecret.remoteRef.key=": "backup",
            "clickhouse.backup.user.externalSecret.secretStoreRef.name=": "secretStoreRef.name",
            "clickhouse.backup.schedule.timeoutSeconds=1": "from 60 to 14400",
            "clickhouse.backup.schedule.timeoutSeconds=14401": "from 60 to 14400",
            "clickhouse.backup.schedule.maxReplicaDelaySeconds=-1": "maxReplicaDelaySeconds must be a nonnegative integer",
            "clickhouse.backup.schedule.maxReplicaDelaySeconds=abc": "maxReplicaDelaySeconds must be a nonnegative integer",
            "clickhouse.backup.schedule.freshnessRetrySeconds=-1": "freshnessRetrySeconds must be a nonnegative integer",
            "clickhouse.backup.schedule.freshnessRetrySeconds=abc": "freshnessRetrySeconds must be a nonnegative integer",
            "clickhouse.backup.networkPolicy.additionalPorts[0]=7171": "protected backup port",
            "clickhouse.backup.networkPolicy.additionalPorts[0]=0": "TCP ports",
            "clickhouse.backup.networkPolicy.additionalPorts[0]=65536": "TCP ports",
        }
        for override, message in cases.items():
            with self.subTest(override=override):
                with self.assertRaisesRegex(AssertionError, message):
                    render(override)


if __name__ == "__main__":
    unittest.main()
