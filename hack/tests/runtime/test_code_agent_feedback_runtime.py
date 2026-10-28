"""Opt-in SQL checks in a disposable ClickHouse container with local Keeper.

RUN_FEEDBACK_RUNTIME=1 python3 -m unittest discover -s hack/tests/runtime \
    -p test_code_agent_feedback_runtime.py -v
Requires Docker. CLICKHOUSE_TEST_IMAGE can override the chart-default image.
No host ports are published; only the container created here is removed.
"""

import json
import os
import re
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

CHART = Path(__file__).resolve().parents[3] / "charts/ao-data-platform"
SQL_DIR = CHART / "sql"
CH_IMAGE = os.getenv("CLICKHOUSE_TEST_IMAGE", "clickhouse/clickhouse-server:26.4.3")
RUNTIME_CONFIG = """<clickhouse>
<logger><level>error</level><console>1</console></logger>
<max_thread_pool_size>256</max_thread_pool_size>
<background_schedule_pool_size>16</background_schedule_pool_size>
<background_buffer_flush_schedule_pool_size>4</background_buffer_flush_schedule_pool_size>
<background_message_broker_schedule_pool_size>4</background_message_broker_schedule_pool_size>
<background_distributed_schedule_pool_size>4</background_distributed_schedule_pool_size>
<keeper_server><tcp_port>9181</tcp_port><server_id>1</server_id>
<log_storage_path>/var/lib/clickhouse/keeper/log</log_storage_path>
<snapshot_storage_path>/var/lib/clickhouse/keeper/snapshots</snapshot_storage_path>
<raft_configuration><server><id>1</id><hostname>127.0.0.1</hostname><port>9234</port>
</server></raft_configuration></keeper_server>
<zookeeper><node><host>127.0.0.1</host><port>9181</port></node></zookeeper>
<remote_servers><feedback_test><shard><replica><host>127.0.0.1</host><port>9000</port>
</replica></shard></feedback_test></remote_servers>
<macros><shard>01</shard><replica>01</replica></macros>
<default_replica_path>/clickhouse/tables/{uuid}/{shard}</default_replica_path>
<default_replica_name>{replica}</default_replica_name>
</clickhouse>"""


@unittest.skipUnless(os.getenv("RUN_FEEDBACK_RUNTIME") == "1", "opt-in ClickHouse test")
class FeedbackStorageTests(unittest.TestCase):
    @classmethod
    def execute(cls, sql):
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                cls.container,
                "clickhouse-client",
                "--multiquery",
                "--format",
                "JSONEachRow",
            ],
            input=sql.replace("otel_traces.", cls.database + "."),
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines() if line]

    @classmethod
    def apply(cls, paths):
        # Use the production replicated engines and distributed DDL on one replica.
        for path in paths:
            sql = path.read_text().replace("'{cluster}'", "'feedback_test'")
            sql = sql.replace(
                "CREATE DATABASE IF NOT EXISTS otel_traces",
                "CREATE DATABASE IF NOT EXISTS " + cls.database,
            )
            cls.execute(sql)

    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="feedback-runtime-")
        cls.addClassCleanup(directory.cleanup)
        config = Path(directory.name) / "runtime.xml"
        config.write_text(RUNTIME_CONFIG)
        cls.container = "feedback-runtime-" + uuid.uuid4().hex
        cls.addClassCleanup(
            subprocess.run,
            ["docker", "rm", "--force", cls.container],
            check=False,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                cls.container,
                "--network",
                "none",
                "--hostname",
                "localhost",
                "--memory",
                "2g",
                "--cpus",
                "2",
                "--env",
                "CLICKHOUSE_SKIP_USER_SETUP=1",
                "--mount",
                f"type=bind,source={config},target=/etc/clickhouse-server/config.d/feedback-runtime.xml,readonly",
                CH_IMAGE,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
        for attempt in range(30):
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    cls.container,
                    "clickhouse-client",
                    "--query",
                    "SELECT 1",
                ],
                check=False,
                capture_output=True,
                timeout=3,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise AssertionError("Disposable ClickHouse server did not become ready.")
        cls.database = "feedback_test_" + uuid.uuid4().hex
        cls.execute(
            "CREATE USER schema_owner; GRANT ALL ON otel_traces.* TO schema_owner"
        )
        cls.addClassCleanup(
            cls.execute, "DROP DATABASE IF EXISTS " + cls.database + " SYNC"
        )
        paths = sorted(SQL_DIR.glob("*.sql"))
        cls.apply([path for path in paths if int(path.name[:4]) <= 29])
        cls.execute(
            "INSERT INTO otel_traces.agent_feedback (service_name, agent_mcon, request_id, positive, has_comment, submitted_at, collected_at) VALUES ('upgrade-seed', 'existing-agent', 'request', true, false, now64(6), now64(6))"
        )
        cls.apply([path for path in paths if int(path.name[:4]) > 29])

    def setUp(self):
        self.service = "test_" + uuid.uuid4().hex
        self.target = uuid.uuid4().hex
        self.day = datetime.now(UTC).date()

    def insert(self, rows):
        self.execute(
            "INSERT INTO otel_traces.otel_traces FORMAT JSONEachRow\n"
            + "\n".join(json.dumps(row) for row in rows)
        )

    def span(self, attrs=None, **overrides):
        row = {
            "Timestamp": str(self.day) + " 12:00:00.123456789",
            "TraceId": self.target,
            "SpanId": uuid.uuid4().hex[:16],
            "ParentSpanId": "",
            "SpanName": "agent.workflow",
            "ServiceName": self.service,
            "SpanAttributes": attrs or {},
            "ResourceAttributes": {},
            "Duration": 1_000_000,
            "StatusCode": "Ok",
        }
        row.update(overrides)
        return row

    def feedback(self, **attrs):
        return self.span(
            {"gen_ai.evaluation.name": "user_feedback", **attrs},
            SpanName="user_feedback",
            TraceId=uuid.uuid4().hex,
        )

    def select(self, table, columns="*", suffix=""):
        return self.execute(
            f"SELECT {columns} FROM otel_traces.{table} WHERE service_name = '{self.service}' {suffix}"
        )

    def test_rating_comment_clear_and_raw_locator(self):
        row = self.feedback(
            **{
                "montecarlo.feedback.trace_id": self.target.upper(),
                "gen_ai.evaluation.score.label": "negative",
                "gen_ai.evaluation.explanation": "Wrong table",
                "montecarlo.feedback.categories": ["incorrect", "missing_information"],
            }
        )
        self.insert([row])
        result = self.select("agent_feedback")[0]
        self.assertEqual(
            (
                result["target_kind"],
                result["target_id"],
                result["positive"],
                result["has_comment"],
            ),
            ("trace", self.target, False, True),
        )
        self.assertEqual(result["categories"], ["incorrect", "missing_information"])
        self.assertEqual(result["feedback_trace_id"], row["TraceId"])
        self.assertEqual(result["feedback_span_id"], row["SpanId"])
        self.assertEqual(result["feedback_service_name"], self.service)
        self.assertEqual(result["feedback_span_time"], row["Timestamp"])

        self.assertEqual(result["source"], "code")
        self.assertNotIn("comment", result)
        self.insert(
            [
                self.feedback(
                    **{
                        "montecarlo.feedback.trace_id": self.target,
                        "gen_ai.evaluation.score.label": "none",
                        "gen_ai.evaluation.explanation": "must clear",
                        "montecarlo.feedback.categories": ["incorrect"],
                    }
                )
            ]
        )
        clear = self.select("agent_feedback", suffix="AND cleared = true")[0]
        self.assertEqual(
            (clear["positive"], clear["has_comment"], clear["categories"]),
            (None, False, []),
        )

    def test_categories_accept_native_and_collector_string_arrays(self):
        for encoded, expected in [
            (
                ["incorrect", "missing_information"],
                ["incorrect", "missing_information"],
            ),
            (
                '["incorrect","missing_information"]',
                ["incorrect", "missing_information"],
            ),
            ("not JSON", []),
            ('{"unexpected":true}', []),
            (123, []),
            (None, []),
        ]:
            with self.subTest(encoded=encoded):
                event = self.feedback(
                    **{
                        "montecarlo.feedback.trace_id": self.target,
                        "montecarlo.feedback.categories": encoded,
                    }
                )
                normal = self.span()
                self.insert([event, normal])
                result = self.select(
                    "agent_feedback",
                    "categories",
                    f"AND feedback_span_id = '{event['SpanId']}'",
                )
                self.assertEqual(result, [{"categories": expected}])
                self.assertEqual(
                    self.select(
                        "spans_normalized",
                        "span_id",
                        f"AND span_id = '{normal['SpanId']}'",
                    ),
                    [{"span_id": normal["SpanId"]}],
                )

    def test_comment_only_and_child_target(self):
        child = self.feedback(**{"gen_ai.evaluation.explanation": "Comment only"})
        child.update(TraceId=self.target, ParentSpanId="1234567890abcdef")
        self.insert([child])
        result = self.select("agent_feedback")[0]
        self.assertEqual(
            (result["target_id"], result["positive"], result["has_comment"]),
            (self.target, None, True),
        )

    def test_invalid_feedback_does_not_break_normal_spans_or_become_traces(self):
        invalid_attrs = [
            {},
            {"montecarlo.feedback.trace_id": "bad"},
            {"montecarlo.feedback.trace_id": "0" * 32},
            {"montecarlo.feedback.trace_id": {"nested": "bad"}},
            {"montecarlo.feedback.run_id": 123},
            {"montecarlo.feedback.run_id": "x" * 257},
            {"montecarlo.feedback.run_id": "é" * 129},
            {
                "montecarlo.feedback.trace_id": self.target,
                "montecarlo.feedback.run_id": "run",
            },
            {
                "montecarlo.feedback.trace_id": self.target,
                "gen_ai.evaluation.score.label": "bad",
            },
            {
                "montecarlo.feedback.trace_id": self.target,
                "gen_ai.evaluation.score.label": False,
            },
            {
                "montecarlo.feedback.trace_id": self.target,
                "gen_ai.evaluation.score.label": "Positive",
            },
        ]
        normal = self.span({"gen_ai.evaluation.name": "correctness"})
        malformed_name = self.span(
            {"gen_ai.evaluation.name": ["user_feedback"]}, SpanName="user_feedback"
        )
        self.insert(
            [
                normal,
                malformed_name,
                *[self.feedback(**attrs) for attrs in invalid_attrs],
            ]
        )
        self.assertEqual(self.select("agent_feedback"), [])
        self.assertEqual(
            self.select("spans_normalized", "span_id"), [{"span_id": normal["SpanId"]}]
        )

    def test_run_links_combine_blocks_and_preserve_multiple_traces(self):
        run = str(uuid.uuid4())
        attrs = {"montecarlo.association_properties.run_id": run}
        first = self.span(
            attrs,
            Timestamp=str(self.day) + " 12:00:00.000000001",
            ParentSpanId="1234567890abcdef",
        )
        later = self.span(
            attrs,
            Timestamp=str(self.day) + " 12:00:01.000000002",
            ParentSpanId="1234567890abcdef",
        )
        second_trace = self.span(attrs, TraceId=uuid.uuid4().hex)
        self.execute("SYSTEM STOP MERGES otel_traces.code_agent_run_links")
        try:
            self.insert([first, first])
            self.insert([later, second_trace])
            query = "run_id, trace_id, min(first_seen) AS first, max(last_seen) AS last"
            links = self.select(
                "code_agent_run_links",
                query,
                "GROUP BY run_id, trace_id ORDER BY trace_id",
            )
            self.assertEqual(len(links), 2)
            own = next(row for row in links if row["trace_id"] == self.target)
            self.assertEqual(
                (own["first"], own["last"]), (first["Timestamp"], later["Timestamp"])
            )
        finally:
            self.execute("SYSTEM START MERGES otel_traces.code_agent_run_links")
        self.execute("OPTIMIZE TABLE otel_traces.code_agent_run_links FINAL")
        self.assertEqual(len(self.select("code_agent_run_links")), 2)
        self.insert(
            [
                self.feedback(
                    **{
                        "montecarlo.association_properties.run_id": run,
                        "montecarlo.feedback.run_id": run,
                    }
                )
            ]
        )
        self.assertEqual(len(self.select("code_agent_run_links")), 2)

    def test_feedback_before_trace_and_late_child_do_not_change_trace_bounds(self):
        feedback = self.feedback(
            **{
                "montecarlo.feedback.trace_id": self.target,
                "gen_ai.evaluation.score.label": "positive",
            }
        )
        self.insert([feedback])
        self.assertEqual(len(self.select("agent_feedback")), 1)
        self.assertEqual(self.select("spans_normalized"), [])
        normal = self.span()
        self.insert([normal])
        child = self.feedback(**{"gen_ai.evaluation.score.label": "negative"})
        child.update(
            TraceId=self.target,
            ParentSpanId=normal["SpanId"],
            Timestamp=str(self.day + timedelta(days=1)) + " 12:00:00.123456789",
        )
        self.insert([child])
        bounds = self.execute(
            f"SELECT max(End) AS end FROM otel_traces.otel_traces_trace_id_ts WHERE TraceId = '{self.target}'"
        )
        self.assertEqual(bounds, [{"end": normal["Timestamp"].split(".")[0]}])
        self.assertEqual(len(self.select("spans_normalized")), 1)
        self.assertEqual(len(self.select("agent_feedback")), 2)

    def test_agent_override_matches_normalization(self):
        row = self.feedback(
            **{
                "montecarlo.agent_name": self.service,
                "montecarlo.feedback.run_id": "run-1",
            }
        )
        row["ServiceName"] = "feedback-service"
        self.insert([row])
        result = self.select("agent_feedback")[0]
        self.assertEqual(result["feedback_service_name"], "feedback-service")
        self.assertEqual(result["target_kind"], "run")
        normal = self.span(
            {
                "montecarlo.agent_name": self.service,
                "montecarlo.association_properties.run_id": "run-1",
            },
            ServiceName="trace-service",
        )
        self.insert([normal])
        self.assertEqual(
            self.select("code_agent_run_links", "run_id, trace_id"),
            [{"run_id": "run-1", "trace_id": self.target}],
        )

    def test_upgrade_is_repeatable_and_preserves_existing_writer(self):
        migrated = self.execute(
            "SELECT source, cleared, positive, target_id, target_kind, toUnixTimestamp64Nano(feedback_span_time) AS feedback_time FROM otel_traces.agent_feedback WHERE service_name = 'upgrade-seed'"
        )
        self.assertEqual(
            migrated,
            [
                {
                    "source": "cortex",
                    "cleared": False,
                    "positive": True,
                    "target_id": "request",
                    "target_kind": "run",
                    "feedback_time": 0,
                }
            ],
        )
        self.execute(
            f"INSERT INTO otel_traces.agent_feedback (service_name, agent_mcon, request_id, positive, has_comment, submitted_at, collected_at) VALUES ('{self.service}', 'existing-agent', 'request', true, false, now64(6), now64(6))"
        )
        self.apply(sorted(SQL_DIR.glob("*.sql")))
        result = self.select("agent_feedback")[0]
        self.assertEqual(
            (result["source"], result["cleared"], result["positive"]),
            ("cortex", False, True),
        )
        self.insert([self.feedback(**{"montecarlo.feedback.trace_id": self.target})])
        self.assertEqual(len(self.select("agent_feedback")), 2)
        self.assertEqual(self.select("spans_normalized"), [])

    def test_existing_feedback_view_gets_collector_fix_on_upgrade(self):
        # Simulate an already-installed view. CREATE IF NOT EXISTS alone cannot fix it.
        initial = (SQL_DIR / "0032_code_agent_feedback_mv.sql").read_text()
        self.execute(
            "ALTER TABLE otel_traces.code_agent_feedback_mv MODIFY QUERY WITH"
            + initial.split("AS WITH", 1)[1]
        )
        try:
            before = self.feedback(
                **{
                    "montecarlo.feedback.trace_id": self.target,
                    "montecarlo.feedback.categories": '["incorrect"]',
                }
            )
            self.insert([before])
            self.assertEqual(
                self.select("agent_feedback", "categories"), [{"categories": []}]
            )
            self.apply([SQL_DIR / "0036_code_agent_feedback_collector_categories.sql"])
            after = self.feedback(
                **{
                    "montecarlo.feedback.trace_id": self.target,
                    "montecarlo.feedback.categories": '["incorrect"]',
                }
            )
            self.insert([after])
            self.assertEqual(
                self.select(
                    "agent_feedback",
                    "categories",
                    f"AND feedback_span_id = '{after['SpanId']}'",
                ),
                [{"categories": ["incorrect"]}],
            )
        finally:
            self.apply([SQL_DIR / "0036_code_agent_feedback_collector_categories.sql"])

    def test_configured_feedback_retention_runs_on_replicated_tables(self):
        scope = {"agent_feedback", "agent_turn_links", "code_agent_run_links"}
        statements = dict(
            re.findall(
                r'modify_ttl otel_traces\.(\w+) "([^"]+)"',
                (CHART / "templates/schema-job.yaml").read_text(),
            )
        )
        self.assertTrue(scope <= statements.keys())
        for days in (45, 20):
            for table in sorted(scope):
                expression = statements[table].replace(
                    "{{ .Values.clickhouse.ttlDays }}", str(days)
                )
                self.execute(
                    f"ALTER TABLE otel_traces.{table} ON CLUSTER 'feedback_test' MODIFY TTL {expression} SETTINGS materialize_ttl_after_modify = 0"
                )
                definition = self.execute(f"SHOW CREATE TABLE otel_traces.{table}")[0][
                    "statement"
                ]
                self.assertIn("Replicated", definition)
                self.assertIn(f"toIntervalDay({days})", definition)

    def test_trace_lookup_uses_projection_and_reads_a_bounded_granule(self):
        self.execute(
            "INSERT INTO otel_traces.code_agent_run_links "
            f"SELECT '{self.service}', concat('run-', leftPad(toString(number), 8, '0')), "
            "lower(hex(MD5(toString(number)))), now64(9), now64(9) FROM numbers(100000)"
        )
        # ClickHouse 26.2 does not select this projection for SELECT DISTINCT.
        query = (
            "SELECT run_id, trace_id FROM otel_traces.code_agent_run_links "
            f"WHERE service_name = '{self.service}' "
            "AND trace_id = lower(hex(MD5('50000'))) GROUP BY run_id, trace_id"
        )
        plan = self.execute("EXPLAIN projections = 1, indexes = 1 " + query)
        self.assertIn("by_trace", "\n".join(str(row) for row in plan))
        rows = self.execute(query + " SETTINGS max_rows_to_read = 10000")
        self.assertEqual([row["run_id"] for row in rows], ["run-00050000"])
        self.execute(
            "INSERT INTO otel_traces.code_agent_run_links "
            f"SELECT '{self.service}', 'run-00050000', "
            "lower(hex(MD5('50000'))), now64(9), now64(9)"
        )
        self.execute("OPTIMIZE TABLE otel_traces.code_agent_run_links FINAL")
        merged_plan = self.execute("EXPLAIN projections = 1, indexes = 1 " + query)
        self.assertIn("by_trace", "\n".join(str(row) for row in merged_plan))
        self.assertEqual(
            self.execute(query + " SETTINGS max_rows_to_read = 10000"), rows
        )
        with self.assertRaisesRegex(AssertionError, "TOO_MANY_ROWS"):
            self.execute(
                query
                + " SETTINGS optimize_use_projections = 0, max_rows_to_read = 10000"
            )
