"""Render checks for backup isolation, password rotation, and job wiring.

Run after `helm dependency build charts/ao-data-platform` with PyYAML installed.
Set HELM to use a Helm binary outside PATH. All credentials here are fake.
"""

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


def render(*overrides, backup=True):
    command = [HELM, "template", RELEASE, str(CHART), "--namespace", "montecarlo",
               "-f", str(CHART / "ci/lint-values.yaml")]
    if backup:
        command += ["-f", str(CHART / "ci/backup-values.yaml")]
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
            # Only ClickHouse and its shared password bundle need modification.
            if document["kind"] == "ClickHouseInstallation":
                continue
            if document["kind"] == "ExternalSecret" and "auth.xml" in document["spec"].get("target", {}).get("template", {}).get("data", {}):
                continue
            key = (document["kind"], document["metadata"]["name"])
            self.assertEqual(document, enabled_by_key[key], key)

    def test_clickhouse_keeps_existing_users_and_backup_cannot_restore(self):
        files = self.chi["spec"]["configuration"]["files"]
        users = xml_tree(files["users.d/auth-methods.xml"])
        backup = users.find("users/backup")
        self.assertIsNotNone(backup)
        self.assertEqual({n.text for n in backup.findall("networks/ip")}, {"127.0.0.1", "::1"})
        grants = [n.text for n in backup.findall("grants/query")]
        self.assertIn("GRANT BACKUP ON otel_traces.*", grants)
        for grant in grants:
            for forbidden in (" DROP ", " INSERT ", " CREATE ", " ALTER ", " ALL ", "GRANT OPTION"):
                self.assertNotIn(forbidden, grant)
        users.find("users").remove(backup)
        disabled_files = one(self.disabled, "ClickHouseInstallation")["spec"]["configuration"]["files"]
        self.assertEqual(ET.tostring(users), ET.tostring(xml_tree(disabled_files["users.d/auth-methods.xml"])))

    def test_database_and_backup_use_the_same_aws_role_and_data_volume(self):
        account = one(self.enabled, "ServiceAccount", "ci-clickhouse-backup")
        self.assertEqual(account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"],
                         "arn:aws:iam::123456789012:role/ao-backup-render-test")
        spec = pod(self.chi)
        self.assertEqual(spec["serviceAccountName"], account["metadata"]["name"])
        sidecar = next(c for c in spec["containers"] if c["name"] == "clickhouse-backup")
        self.assertEqual(sidecar["image"], "altinity/clickhouse-backup:2.8.1")
        data_volume = self.chi["spec"]["defaults"]["templates"]["dataVolumeClaimTemplate"]
        mounts = {mount["mountPath"]: mount for mount in sidecar["volumeMounts"]}
        self.assertEqual(mounts["/var/lib/clickhouse"]["name"], data_volume)
        self.assertNotIn("subPath", mounts["/etc/clickhouse-backup"])
        self.assertTrue(mounts["/etc/clickhouse-backup"]["readOnly"])
        env = {item["name"]: item for item in sidecar["env"]}
        self.assertEqual(env["API_PASSWORD"]["valueFrom"]["secretKeyRef"],
                         {"name": "ci-backup-api", "key": "password"})
        for forbidden in ("CLICKHOUSE_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(forbidden, env)

        disk = xml_tree(self.chi["spec"]["configuration"]["files"]["config.d/backup.xml"])
        destination = disk.find("storage_configuration/disks/backups_s3")
        self.assertEqual(destination.findtext("type"), "s3")
        self.assertEqual(destination.findtext("use_environment_credentials"), "1")
        self.assertEqual(destination.findtext("endpoint"),
                         "https://s3.us-west-1.amazonaws.com/ao-backup-render-test/render-test/native/")
        self.assertEqual(disk.findtext("backups/allowed_disk"), "backups_s3")
        self.assertIsNone(destination.find("access_key_id"))
        self.assertIsNone(destination.find("secret_access_key"))

    def test_password_rotation_reaches_the_user_and_reloadable_backup_config(self):
        credentials = one(self.enabled, "ExternalSecret", "ao-clickhouse-backup-credentials")
        configuration = credentials["spec"]["target"]["template"]["data"]["config.yml"]
        # Passwords containing quotes/newlines must remain one YAML string.
        password = 'a: "quoted" password\nwith a second line'
        config = yaml.safe_load(render_secret_template(configuration, {"password": password}))
        self.assertEqual(config["clickhouse"]["password"], password)
        self.assertTrue(config["clickhouse"]["use_embedded_backup_restore"])
        self.assertEqual(config["clickhouse"]["embedded_backup_disk"], "backups_s3")
        self.assertFalse(config["clickhouse"]["use_embedded_backup_restore_cluster"])
        self.assertFalse(config["general"]["rbac_backup_always"])
        self.assertFalse(config["api"]["allow_parallel"])
        self.assertFalse(config["api"]["create_integration_tables"])
        self.assertEqual(config["s3"]["compression_format"], "none")
        self.assertEqual(config["s3"]["acl"], "")
        self.assertNotIn("password", config["api"])

        bundle = next(d for d in self.enabled if d["kind"] == "ExternalSecret"
                      and "auth.xml" in d["spec"].get("target", {}).get("template", {}).get("data", {}))
        references = {d["secretKey"]: d["remoteRef"] for d in bundle["spec"]["data"]}
        disabled_bundle = one(self.disabled, "ExternalSecret", bundle["metadata"]["name"])
        self.assertEqual({key: value for key, value in references.items() if not key.startswith("backup_")},
                         {item["secretKey"]: item["remoteRef"] for item in disabled_bundle["spec"]["data"]})
        self.assertEqual(references["backup_password"], credentials["spec"]["data"][0]["remoteRef"])
        self.assertEqual(references["backup_previous"],
                         {"key": "ci-backup-previous", "property": "password", "version": "AWSCURRENT"})
        auth = bundle["spec"]["target"]["template"]["data"]["auth.xml"]
        for previous in ("-", "", "previous-password"):
            with self.subTest(previous=previous):
                values = {key: "current-password" for key in references}
                values["backup_previous"] = previous
                methods = xml_tree(render_secret_template(auth, values)).find("backup_auth_methods")
                self.assertEqual(methods.findtext("current/password"), "current-password")
                self.assertEqual(methods.findtext("previous/password"),
                                 "previous-password" if previous == "previous-password" else None)

    def test_one_job_runs_every_four_hours_without_blind_retries(self):
        job = one(self.enabled, "CronJob")
        self.assertEqual(job["spec"]["schedule"], "0 */4 * * *")
        self.assertEqual(job["spec"]["timeZone"], "Etc/UTC")
        self.assertEqual(job["spec"]["concurrencyPolicy"], "Forbid")
        template = job["spec"]["jobTemplate"]["spec"]
        self.assertEqual(template["backoffLimit"], 0)
        self.assertEqual(template["template"]["spec"]["restartPolicy"], "Never")
        self.assertFalse(template["template"]["spec"]["automountServiceAccountToken"])
        container = template["template"]["spec"]["containers"][0]
        env = {v["name"]: v["value"] for v in container["env"]}
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
        self.assertTrue({8123, 8443, 9000, 9440, 9009, 9363}.issubset(normal_ports))

    def test_unsafe_or_incomplete_backup_configuration_fails_rendering(self):
        cases = {
            "clickhouse.backup.provider=gke": "only aws",
            "clickhouse.backup.aws.bucket=": "aws.bucket",
            "clickhouse.backup.aws.region=": "aws.region",
            "clickhouse.backup.aws.roleArn=": "aws.roleArn",
            "clickhouse.backup.aws.path=../other": "safe directory names",
            "clickhouse.backup.serviceAccount.name=default": "dedicated account",
            "clickhouse.backup.api.existingSecret=": "separate Secret",
            "clickhouse.backup.api.existingSecret=ao-clickhouse-backup-credentials": "separate",
            "clickhouse.backup.secret=": "backup.secret",
            "clickhouse.backup.externalSecret.remoteRef.key=": "backup",
            "clickhouse.backup.externalSecret.secretStoreRef.name=": "secretStoreRef.name",
            "clickhouse.backup.schedule.timeoutSeconds=1": "at least 60",
        }
        for override, message in cases.items():
            with self.subTest(override=override):
                with self.assertRaisesRegex(AssertionError, message):
                    render(override)


if __name__ == "__main__":
    unittest.main()
