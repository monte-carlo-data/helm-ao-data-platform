"""API validation of the chart's database Pod template; creates no objects.

Opt in with BACKUP_KUBERNETES_CONTEXT and BACKUP_KUBERNETES_NAMESPACE.
Run with HELM set when Helm is not on PATH. The test uses fake CI credentials.
Docker cannot catch Kubernetes projection validation. This is a manual check
before releasing changes to backup volumes or mounts, not an automatic CI
publishing requirement. It does not
exercise the ClickHouse operator or prove that kubelet can start a container.
Run the Docker checks separately; operator integration requires its own cluster test.
"""

import copy
import os
from pathlib import Path
import subprocess
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_backup_chart import one, render


CONTEXT = os.environ.get("BACKUP_KUBERNETES_CONTEXT")
NAMESPACE = os.environ.get("BACKUP_KUBERNETES_NAMESPACE")


def statefulset(documents):
    chi = one(documents, "ClickHouseInstallation")
    template = copy.deepcopy(chi["spec"]["templates"]["podTemplates"][0])
    template.pop("name", None)
    labels = {"app.kubernetes.io/name": "backup-volume-validation"}
    template.setdefault("metadata", {})["labels"] = labels
    claims = copy.deepcopy(chi["spec"]["templates"]["volumeClaimTemplates"])
    for claim in claims:
        claim["metadata"] = {"name": claim.pop("name")}
    # Claim templates satisfy data-volume references exactly as in the operator's
    # StatefulSet. Server validation does not create/attach claims or start Pods.
    return {"apiVersion": "apps/v1", "kind": "StatefulSet",
            "metadata": {"name": "backup-volume-validation", "namespace": NAMESPACE},
            "spec": {"replicas": 1, "serviceName": "backup-volume-validation",
                     "selector": {"matchLabels": labels}, "template": template,
                     "volumeClaimTemplates": claims}}


@unittest.skipUnless(CONTEXT and NAMESPACE, "Set explicit Kubernetes context and namespace for server dry-run")
class BackupKubernetesValidation(unittest.TestCase):
    def validate(self, manifest):
        return subprocess.run(
            ["kubectl", "--context", CONTEXT, "--namespace", NAMESPACE,
             "create", "--dry-run=server", "--validate=true", "-f", "-", "-o", "name"],
            input=yaml.safe_dump(manifest), text=True, capture_output=True, timeout=45,
        )

    def test_database_template_passes_server_validation(self):
        result = self.validate(statefulset(render()))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_rejects_the_previous_duplicate_auth_file_projection(self):
        manifest = statefulset(render())
        volumes = manifest["spec"]["template"]["spec"]["volumes"]
        auth = next(v for v in volumes if v["name"] == "backup-user-auth")
        auth["projected"]["sources"][0]["configMap"]["items"].append(
            {"key": "empty-auth.xml", "path": "users.d/auth.xml"})
        result = self.validate(manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting duplicate paths", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
