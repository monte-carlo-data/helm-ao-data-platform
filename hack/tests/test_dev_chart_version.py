"""Exercise the version printed by the publishing helper without publishing."""

from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "hack/dev-chart-version.sh"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def run_commands(config, steps, visited=()):
    """Resolve reusable commands so safety checks cover the executed shell."""
    result = []
    for step in steps:
        if isinstance(step, str):
            name, value = step, None
        else:
            name, value = next(iter(step.items()))
        if name == "run":
            result.append(value if isinstance(value, str) else value["command"])
        elif name in config.get("commands", {}):
            if name in visited:
                raise AssertionError("Recursive pipeline command: " + name)
            result.extend(run_commands(config, config["commands"][name]["steps"], (*visited, name)))
    return result


class DevChartVersionTests(unittest.TestCase):
    def version(self, chart_contents, commit=COMMIT):
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory) / "Chart.yaml"
            chart.write_text(chart_contents)
            return subprocess.run(
                ["bash", str(SCRIPT), str(chart), commit],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_uses_chart_version_and_full_commit(self):
        for version in ("5.2.0", "5.3.0", "5.10.1", '"6.0.0"'):
            with self.subTest(version=version):
                result = self.version(f"apiVersion: v2\nversion: {version}\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                base = version.strip('"')
                self.assertEqual(result.stdout.strip(), f"{base}-dev.g{COMMIT}")

    def test_rejects_missing_or_invalid_release_version(self):
        for contents in (
            "name: example\n", "version: latest\n", "version: 5.2\n",
            "version: 05.2.0\n", "version: 5.2.0-rc.1\n",
        ):
            with self.subTest(contents=contents):
                result = self.version(contents)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_manual_publish_requires_opt_in_and_lint(self):
        config = yaml.safe_load((ROOT / ".circleci/config.yml").read_text())
        self.assertEqual(config["parameters"]["publish_dev_chart"], {"type": "boolean", "default": False})
        workflow = config["workflows"]["publish-branch-chart"]
        self.assertEqual(workflow["when"], "<< pipeline.parameters.publish_dev_chart >>")
        jobs = {name: settings for job in workflow["jobs"] for name, settings in job.items()}
        self.assertEqual(set(jobs), {"lint", "backup-runtime", "publish-dev-commit"})
        self.assertEqual(set(jobs["publish-dev-commit"]["requires"]), {"lint", "backup-runtime"})
        for job in jobs.values():
            self.assertEqual(job["filters"]["branches"]["ignore"], ["main", "dev"])
            self.assertEqual(job["filters"]["tags"]["ignore"], "/.*/")

    def test_manual_publish_cannot_update_the_shared_tag(self):
        config = yaml.safe_load((ROOT / ".circleci/config.yml").read_text())
        steps = config["jobs"]["publish-dev-commit"]["steps"]
        commands = run_commands(config, steps)
        self.assertTrue(any("bash hack/dev-chart-version.sh" in command for command in commands))
        self.assertFalse(any("0.0.0-latest" in command for command in commands))
        automatic = config["workflows"]["build"]
        self.assertEqual(automatic["when"], {"not": "<< pipeline.parameters.publish_dev_chart >>"})
        job = next(job["publish-dev"] for job in automatic["jobs"] if "publish-dev" in job)
        self.assertEqual(job["filters"]["branches"]["only"], "dev")

    def test_rejects_a_branch_or_abbreviated_commit(self):
        for commit in ("dev", "0123456", "not-a-commit"):
            with self.subTest(commit=commit):
                result = self.version("version: 5.2.0\n", commit)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
