"""Checks for the per-table grants rendered from sql/grants.yaml into the users config.

Run after `helm dependency build charts/ao-data-platform` with PyYAML installed.
Set HELM to use a Helm binary outside PATH.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from test_backup_chart import CHART, HELM, one, xml_tree


GRANTS_FILE = "sql/grants.yaml"
# Each ClickHouse user paired with the grants.yaml role that supplies its table grants.
USER_ROLES = (("otel", "ingest"), ("llm_worker", "llm_worker"), ("monte_carlo", "monte_carlo"))
NAMED_TABLE_GRANT = re.compile(r"^GRANT .+ ON otel_traces\.(?!\*$)\S+$")


def helm_template(chart):
    """Render with restrictGrants on so every role's grants are included."""
    command = [HELM, "template", "grants-test", str(chart), "--namespace", "montecarlo",
               "-f", str(chart / "ci/lint-values.yaml"), "--kube-version", "1.30.0",
               "--set", "clickhouse.otel.restrictGrants=true"]
    return subprocess.run(command, text=True, capture_output=True, check=False)


def users_xml(documents):
    chi = one(documents, "ClickHouseInstallation")
    return xml_tree(chi["spec"]["configuration"]["files"]["users.d/auth-methods.xml"])


class TableGrantsTests(unittest.TestCase):
    def test_rendered_table_grants_equal_grants_yaml(self):
        result = helm_template(CHART)
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        users = users_xml(documents)
        grants = yaml.safe_load((CHART / GRANTS_FILE).read_text())
        for username, role in USER_ROLES:
            with self.subTest(user=username, role=role):
                expected = {f"GRANT {', '.join(privileges)} ON otel_traces.{table}"
                            for table, privileges in grants[role].items()}
                queries = [n.text for n in users.findall(f"users/{username}/grants/query")]
                rendered = {q for q in queries if NAMED_TABLE_GRANT.match(q)}
                self.assertTrue(expected)
                self.assertEqual(rendered, expected)
                if username == "monte_carlo":
                    self.assertIn("GRANT SELECT ON otel_traces.*", queries)
                    self.assertTrue(any(q.startswith("GRANT READ ON REMOTE") for q in queries), queries)

    def test_invalid_grants_file_fails_rendering(self):
        real = yaml.safe_load((CHART / GRANTS_FILE).read_text())

        def mutated(change):
            grants = yaml.safe_load(yaml.safe_dump(real))
            change(grants)
            return yaml.safe_dump(grants)

        def drop_role(grants):
            del grants["ingest"]

        def set_table(role, table, privileges):
            return lambda grants: grants[role].__setitem__(table, privileges)

        def set_role(role, value):
            return lambda grants: grants.__setitem__(role, value)

        # None deletes the file; a string is written verbatim; a callable mutates the real data.
        cases = {
            "missing file": (None, "missing or is not valid YAML"),
            "invalid yaml": (": : :", "missing or is not valid YAML"),
            "missing role": (mutated(drop_role), "has no ingest role"),
            "invalid table name": (
                mutated(set_table("llm_worker", "llm_inputs</query><query>GRANT ALL ON *.*", ["SELECT"])),
                "invalid table name"),
            "unknown privilege": (mutated(set_table("llm_worker", "llm_inputs", ["ALTER"])), "unknown privilege"),
            "empty privilege list": (mutated(set_table("llm_worker", "llm_inputs", [])), "no privileges"),
            "null privilege list": (mutated(set_table("llm_worker", "llm_inputs", None)), "no privileges"),
            "empty role": (mutated(set_role("llm_worker", {})), "has no tables"),
            "unused role": (mutated(set_role("reader", {"otel_traces": ["SELECT"]})), "unknown role"),
        }
        for name, (content, message) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory(prefix="ao-grants-") as directory:
                chart = Path(directory) / "chart"
                shutil.copytree(CHART, chart)
                target = chart / GRANTS_FILE
                if content is None:
                    target.unlink()
                else:
                    target.write_text(content)
                result = helm_template(chart)
                self.assertNotEqual(result.returncode, 0, "render unexpectedly succeeded")
                self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main()
