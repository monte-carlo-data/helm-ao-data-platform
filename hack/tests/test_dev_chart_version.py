"""Exercise the version printed by the publishing helper without publishing."""

import os
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

    def test_automatic_publishing_requires_lint_and_real_software_checks(self):
        config = yaml.safe_load((ROOT / ".circleci/config.yml").read_text())
        jobs = {name: settings for job in config["workflows"]["build"]["jobs"]
                for name, settings in job.items()}
        for name in ("publish-tag", "publish-dev"):
            with self.subTest(job=name):
                self.assertGreaterEqual(set(jobs[name]["requires"]), {"lint", "backup-runtime"})
        self.assertEqual(jobs["publish-tag"]["filters"]["branches"]["ignore"], "/.*/")
        tag_filter = {"only": "/^v.*/"}
        for name in ("publish-tag", "lint", "backup-runtime"):
            with self.subTest(job=name):
                self.assertEqual(jobs[name]["filters"]["tags"], tag_filter)
        self.assertEqual(jobs["publish-dev"]["filters"]["branches"], {"only": "dev"})
        # No tag filter means CircleCI ignores tags for this branch-only job.
        self.assertNotIn("tags", jobs["publish-dev"]["filters"])

    def test_runtime_selection_reads_the_complete_changed_file_list(self):
        config = yaml.safe_load((ROOT / ".circleci/config.yml").read_text())
        select = next(step["run"]["command"]
                      for step in config["jobs"]["backup-runtime"]["steps"]
                      if isinstance(step, dict) and isinstance(step.get("run"), dict)
                      and step["run"]["name"] == "Select backup runtime checks")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "git").write_text(
                '#!/bin/bash\ncase "$1" in\n'
                'fetch) exit 0 ;;\nmerge-base) echo base ;;\nrev-parse) echo head ;;\n'
                'diff) [ "$DIFF_FAILURE" = 0 ] || exit 128; cat "$CHANGED_FILES" ;;\n'
                '*) exit 99 ;;\nesac\n'
            )
            (path / "circleci-agent").write_text('#!/bin/bash\nprintf "halted" > "$HALT_MARKER"\n')
            for name in ("git", "circleci-agent"):
                (path / name).chmod(0o755)
            changed = path / "changed"
            halted = path / "halted"
            env = {**os.environ, "PATH": str(path) + os.pathsep + os.environ["PATH"],
                   "FORCE_BACKUP_RUNTIME": "false", "PUBLISH_BACKUP_CHART": "false",
                   "CIRCLE_TAG": "", "CIRCLE_BRANCH": "test-branch",
                   "CHANGED_FILES": str(changed), "HALT_MARKER": str(halted)}
            # The first match is followed by far more text than a pipe buffer.
            # With git | grep -q, git can exit on SIGPIPE and incorrectly halt CI.
            large_change = "charts/ao-data-platform/values.yaml\n" + "docs/unrelated-file.md\n" * 100000
            for contents, diff_failure, should_halt in (
                (large_change, "0", False),
                ("docs/README.md\n", "0", True),
                ("", "0", True),
                ("", "1", False),
            ):
                with self.subTest(diff_failure=diff_failure, should_halt=should_halt,
                                  size=len(contents)):
                    halted.unlink(missing_ok=True)
                    changed.write_text(contents)
                    result = subprocess.run(["bash", "-eo", "pipefail", "-c", select],
                                            env={**env, "DIFF_FAILURE": diff_failure},
                                            text=True, capture_output=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(halted.exists(), should_halt, result.stdout + result.stderr)

    def test_rejects_a_branch_or_abbreviated_commit(self):
        for commit in ("dev", "0123456", "not-a-commit"):
            with self.subTest(commit=commit):
                result = self.version("version: 5.2.0\n", commit)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
