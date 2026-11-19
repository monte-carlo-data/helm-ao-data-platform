"""Render checks for the data backfill Job and its generated SQL.

Run after `helm dependency build charts/ao-data-platform` with PyYAML installed.
Set HELM to use a Helm binary outside PATH.
"""

import hashlib
import re
import unittest

from test_backup_chart import CHART, one, render


BACKFILL_DIR = CHART / "backfill"
BACKFILL_FILES = (
    "spans_normalized_cost_inputs_pending.sql",
    "spans_normalized_cost_inputs_update.sql",
)
# A backfill fills empty columns. It must never remove, replace or copy rows.
FORBIDDEN = re.compile(r"\b(DELETE|DROP|TRUNCATE|INSERT|OPTIMIZE|REPLACE)\b")


def script(job):
    return job["spec"]["template"]["spec"]["containers"][0]["command"][2]


class BackfillChartTests(unittest.TestCase):
    def test_manifest_lists_every_backfill_file_with_its_checksum(self):
        manifest = (BACKFILL_DIR / "MANIFEST.sha256").read_text()
        entries = dict(
            reversed(line.split("  ", 1))
            for line in manifest.splitlines()
            if line and not line.startswith("#")
        )
        present = sorted(p.name for p in BACKFILL_DIR.iterdir() if p.name != "MANIFEST.sha256")
        self.assertEqual(sorted(entries), present)
        for name, digest in entries.items():
            self.assertEqual(hashlib.sha256((BACKFILL_DIR / name).read_bytes()).hexdigest(), digest)

    def test_backfill_sql_only_updates(self):
        for name in BACKFILL_FILES:
            with self.subTest(name=name):
                text = (BACKFILL_DIR / name).read_text()
                self.assertIsNone(FORBIDDEN.search(text))
                self.assertIn("{day:String}", text)
        update = (BACKFILL_DIR / "spans_normalized_cost_inputs_update.sql").read_text()
        self.assertIn("ALTER TABLE otel_traces.spans_normalized\nUPDATE", update)
        self.assertIn("IN PARTITION {day:String}", update)
        self.assertIn("WHERE new_input_tokens IS NULL", update)
        self.assertIn("SETTINGS mutations_sync = 2", update)

    def test_job_runs_each_file_per_day_as_schema_owner(self):
        documents = render(backup=False)
        job = one(documents, "Job", "clickhouse-backfill-1")
        config_map = one(documents, "ConfigMap", "clickhouse-backfill")

        self.assertEqual(sorted(config_map["data"]), sorted(BACKFILL_FILES))
        for name in BACKFILL_FILES:
            self.assertEqual(config_map["data"][name], (BACKFILL_DIR / name).read_text())
        text = script(job)
        self.assertIn("--user schema_owner", text)
        self.assertIn('--param_day="$day" < /backfill/spans_normalized_cost_inputs_pending.sql', text)
        self.assertIn('--param_day="$day" < /backfill/spans_normalized_cost_inputs_update.sql', text)
        self.assertIn("i=30\n", text)
        self.assertIsNone(FORBIDDEN.search(text))
        env = job["spec"]["template"]["spec"]["containers"][0]["env"][0]
        self.assertEqual(env["valueFrom"]["secretKeyRef"]["name"], "ao-clickhouse-schema-owner-credentials")

    def test_job_waits_for_the_view_to_fill_the_columns(self):
        text = script(one(render(backup=False), "Job", "clickhouse-backfill-1"))

        wait = text.index('grep -q "AS new_input_tokens"')
        self.assertLess(wait, text.index("spans_normalized_cost_inputs_update.sql"))

    def test_job_follows_the_retention_window(self):
        text = script(one(render("clickhouse.ttlDays=7", backup=False), "Job", "clickhouse-backfill-1"))

        self.assertIn("i=7\n", text)

    def test_job_uses_tls_when_enabled(self):
        documents = render("tls.enabled=true", backup=False)
        job = one(documents, "Job", "clickhouse-backfill-1")

        self.assertIn("--secure --port 9440", script(job))

    def test_disabled_renders_no_job(self):
        documents = render("clickhouse.backfill.enabled=false", backup=False)

        self.assertFalse([d for d in documents if d["metadata"]["name"] == "clickhouse-backfill-1"])
        self.assertFalse([d for d in documents if d["metadata"]["name"] == "clickhouse-backfill"])


if __name__ == "__main__":
    unittest.main()
