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
from unittest import mock

CHART = Path(__file__).resolve().parents[3] / "charts/ao-data-platform"
SQL_DIR = CHART / "sql"
CH_IMAGE = os.getenv("CLICKHOUSE_TEST_IMAGE", "clickhouse/clickhouse-server:26.4.3")
COLLECTOR_JSON_SETTINGS = {
    "input_format_try_infer_dates": "0",
    "input_format_try_infer_datetimes": "0",
}
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


def wait_for_clickhouse(container):
    for _ in range(30):
        try:
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "clickhouse-client",
                    "--query",
                    "SELECT 1",
                ],
                check=False,
                capture_output=True,
                timeout=3,
            )
            if ready.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    raise AssertionError("Disposable ClickHouse server did not become ready.")


class FeedbackReadinessTests(unittest.TestCase):
    @mock.patch(__name__ + ".time.sleep")
    @mock.patch(__name__ + ".subprocess.run")
    def test_retries_a_timed_out_probe_before_success(self, run, sleep):
        run.side_effect = [
            subprocess.TimeoutExpired("clickhouse-client", 3),
            subprocess.CompletedProcess("clickhouse-client", 0),
        ]
        wait_for_clickhouse("test-container")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["timeout"], 3)
        sleep.assert_called_once_with(1)

    @mock.patch(__name__ + ".time.sleep")
    @mock.patch(__name__ + ".subprocess.run")
    def test_repeated_timeouts_stop_after_thirty_probes(self, run, sleep):
        run.side_effect = subprocess.TimeoutExpired("clickhouse-client", 3)
        with self.assertRaisesRegex(AssertionError, "did not become ready"):
            wait_for_clickhouse("test-container")
        self.assertEqual(run.call_count, 30)
        self.assertEqual(sleep.call_count, 30)


class FeedbackDefinitionTests(unittest.TestCase):
    def test_fresh_and_upgrade_view_queries_match(self):
        for create_name, upgrade_name in (
            (
                "0032_code_agent_feedback_mv.sql",
                "0036_code_agent_feedback_collector_categories.sql",
            ),
            (
                "0033_code_agent_run_links_mv.sql",
                "0037_code_agent_run_links_native_filters.sql",
            ),
        ):
            with self.subTest(create=create_name):
                create = (
                    (SQL_DIR / create_name).read_text().split("AS WITH", 1)[1].strip()
                )
                upgrade = (
                    (SQL_DIR / upgrade_name)
                    .read_text()
                    .split("MODIFY QUERY\nWITH", 1)[1]
                    .strip()
                )
                self.assertEqual(create, upgrade)


@unittest.skipUnless(os.getenv("RUN_FEEDBACK_RUNTIME") == "1", "opt-in ClickHouse test")
class FeedbackStorageTests(unittest.TestCase):
    @classmethod
    def execute(cls, sql, *, collector_insert=False):
        settings = (
            [f"--{name}={value}" for name, value in COLLECTOR_JSON_SETTINGS.items()]
            if collector_insert
            else []
        )
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                cls.container,
                "clickhouse-client",
                *settings,
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
        wait_for_clickhouse(cls.container)
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

    def insert(self, rows, *, legacy_inference=False):
        self.execute(
            "INSERT INTO otel_traces.otel_traces FORMAT JSONEachRow\n"
            + "\n".join(json.dumps(row) for row in rows),
            collector_insert=not legacy_inference,
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

    def test_categories_bound_count_and_utf8_bytes(self):
        categories = ["x" * 129, "é" * 65, "x" * 128, "é" * 64]
        categories += [f"category-{index}" for index in range(25)]
        expected = categories[2:22]
        for encoded in (categories, json.dumps(categories)):
            with self.subTest(encoding=type(encoded).__name__):
                event = self.feedback(
                    **{
                        "montecarlo.feedback.trace_id": self.target,
                        "montecarlo.feedback.categories": encoded,
                    }
                )
                self.insert([event])
                self.assertEqual(
                    self.select(
                        "agent_feedback",
                        "categories",
                        f"AND feedback_span_id = '{event['SpanId']}'",
                    ),
                    [{"categories": expected}],
                )

    def test_categories_drop_blank_and_nonstring_entries(self):
        categories = [
            None,
            "",
            1,
            True,
            {"a": 1},
            ["nested"],
            " ",
            "\t\n\v\f\r\u0085\x1c\x1d\x1e\x1f",
            "\u00a0\u2003",
            "ok",
            " padded ",
            "2026-10-29",
        ]
        for encoded in (categories, json.dumps(categories)):
            with self.subTest(encoding=type(encoded).__name__):
                event = self.feedback(
                    **{
                        "montecarlo.feedback.trace_id": self.target,
                        "montecarlo.feedback.categories": encoded,
                    }
                )
                normal = self.span()
                self.insert([event, normal])
                self.assertEqual(
                    self.select(
                        "agent_feedback",
                        "categories",
                        f"AND feedback_span_id = '{event['SpanId']}'",
                    ),
                    [{"categories": ["ok", " padded ", "2026-10-29"]}],
                )
                self.assertEqual(
                    self.select(
                        "spans_normalized",
                        "span_id",
                        f"AND span_id = '{normal['SpanId']}'",
                    ),
                    [{"span_id": normal["SpanId"]}],
                )

    def test_date_shaped_run_ids_link_to_the_same_feedback_target(self):
        values = [
            "2026-10-29",
            "2026-1-5",
            "2026-02-30",
            "2026-10-29 12:00:00",
            "2026-10-29 12:00:00.123456",
            "2026-10-29T12:00:00Z",
            "2026-10-29T14:00:00+02:00",
            "123",
            "true",
            "run-abc",
            str(uuid.uuid4()),
        ]
        for value in values:
            with self.subTest(run_id=value):
                trace_id = uuid.uuid4().hex
                normal = self.span(
                    {"montecarlo.association_properties.run_id": value},
                    TraceId=trace_id,
                )
                feedback = self.feedback(
                    **{
                        "montecarlo.feedback.run_id": value,
                        "gen_ai.evaluation.score.label": "positive",
                    }
                )
                self.insert([normal, feedback])
                result = self.select(
                    "agent_feedback",
                    "target_kind, target_id",
                    f"AND feedback_span_id = '{feedback['SpanId']}'",
                )
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["target_kind"], "run")
                self.assertEqual(result[0]["target_id"], value)
                stored_type = self.execute(
                    "SELECT dynamicType(SpanAttributes.montecarlo.association_properties.run_id) AS stored_type FROM otel_traces.otel_traces "
                    + f"WHERE SpanId = '{normal['SpanId']}'"
                )[0]["stored_type"]
                self.assertEqual(stored_type, "String")
                self.assertEqual(
                    self.select(
                        "code_agent_run_links",
                        "run_id, trace_id",
                        f"AND trace_id = '{trace_id}'",
                    ),
                    [{"run_id": result[0]["target_id"], "trace_id": trace_id}],
                )

    def test_run_ids_survive_mixed_agents_and_separate_feedback_batches(self):
        for value in (
            "2026-10-29",
            "2026-1-5",
            "2026-02-30",
            "2026-10-29 12:00:00.123456",
        ):
            with self.subTest(run_id=value):
                mixed_trace, single_trace = uuid.uuid4().hex, uuid.uuid4().hex
                other = self.span(
                    {"montecarlo.association_properties.run_id": "2026-10-29 12:00:00"},
                    ServiceName="other_" + self.service,
                )
                mixed = self.span(
                    {"montecarlo.association_properties.run_id": value},
                    TraceId=mixed_trace,
                )
                self.insert([other, mixed])
                self.insert(
                    [
                        self.span(
                            {"montecarlo.association_properties.run_id": value},
                            TraceId=single_trace,
                        )
                    ]
                )
                event = self.feedback(**{"montecarlo.feedback.run_id": value})
                self.insert([event])
                joined = self.execute(
                    "SELECT f.target_id, groupUniqArray(l.trace_id) AS traces "
                    "FROM otel_traces.agent_feedback f INNER JOIN otel_traces.code_agent_run_links l "
                    "ON f.service_name = l.service_name AND f.target_id = l.run_id "
                    f"WHERE f.feedback_span_id = '{event['SpanId']}' GROUP BY f.target_id"
                )
                self.assertEqual(len(joined), 1)
                self.assertEqual(joined[0]["target_id"], value)
                self.assertCountEqual(joined[0]["traces"], [mixed_trace, single_trace])

    def test_ingest_preserves_attribute_text_and_typed_telemetry_fields(self):
        value = "2026-10-29 12:00:00.123456"
        row = self.span({"text": value, "count": 12, "fraction": 1.5})
        row.update(
            {
                "ResourceAttributes": {"text": value},
                "Events.Timestamp": [row["Timestamp"]],
                "Events.Name": ["event"],
                "Events.Attributes": [{"text": value}],
                "Links.TraceId": [uuid.uuid4().hex],
                "Links.SpanId": [uuid.uuid4().hex[:16]],
                "Links.TraceState": [""],
                "Links.Attributes": [{"text": value}],
            }
        )
        self.insert([row])
        stored = self.execute(
            "SELECT toString(SpanAttributes) AS span, toString(ResourceAttributes) AS resource, "
            "arrayMap(x -> toString(x), `Events.Attributes`) AS events, "
            "arrayMap(x -> toString(x), `Links.Attributes`) AS links, "
            "toString(Timestamp) AS timestamp, Duration AS duration, "
            "arrayMap(x -> toString(x), `Events.Timestamp`) AS event_times "
            f"FROM otel_traces.otel_traces WHERE SpanId = '{row['SpanId']}'"
        )[0]
        self.assertEqual(
            json.loads(stored["span"]), {"text": value, "count": 12, "fraction": 1.5}
        )
        self.assertEqual(json.loads(stored["resource"]), {"text": value})
        self.assertEqual(
            [json.loads(item) for item in stored["events"]], [{"text": value}]
        )
        self.assertEqual(
            [json.loads(item) for item in stored["links"]], [{"text": value}]
        )
        self.assertEqual(stored["timestamp"], row["Timestamp"])
        self.assertEqual(stored["event_times"], [row["Timestamp"]])
        self.assertEqual(stored["duration"], row["Duration"])

    def test_child_feedback_with_run_target_does_not_rate_its_own_trace(self):
        event = self.feedback(
            **{
                "montecarlo.feedback.run_id": "explicit-run",
                "gen_ai.evaluation.score.label": "positive",
            }
        )
        event["ParentSpanId"] = "1234567890abcdef"
        self.insert([event])
        self.assertEqual(
            self.select("agent_feedback", "target_kind, target_id, request_id"),
            [
                {
                    "target_kind": "run",
                    "target_id": "explicit-run",
                    "request_id": "explicit-run",
                }
            ],
        )

    def test_positive_run_feedback_keeps_target_and_boolean(self):
        event = self.feedback(
            **{
                "montecarlo.feedback.run_id": "run-case-sensitive",
                "gen_ai.evaluation.score.label": "positive",
            }
        )
        self.insert([event])
        result = self.select("agent_feedback")[0]
        self.assertEqual(
            (
                result["target_kind"],
                result["target_id"],
                result["request_id"],
                result["agent_mcon"],
                result["positive"],
            ),
            ("run", "run-case-sensitive", "run-case-sensitive", "", True),
        )
        self.assertIs(result["positive"], True)
        self.assertIs(result["has_comment"], False)

    def test_marker_only_child_and_reserved_name_never_become_normal_spans(self):
        normal = self.span({"montecarlo.association_properties.run_id": "normal-run"})
        child = self.feedback(
            **{"montecarlo.association_properties.run_id": "feedback-run"}
        )
        child.update(
            SpanName="evaluation",
            TraceId=self.target,
            ParentSpanId=normal["SpanId"],
            Timestamp=str(self.day + timedelta(days=1)) + " 12:00:00.123456789",
        )
        reserved = self.span(
            {"montecarlo.association_properties.run_id": "reserved-run"},
            SpanName="user_feedback",
            TraceId=uuid.uuid4().hex,
        )
        self.insert([normal, child, reserved])
        self.assertEqual(
            self.select("spans_normalized", "span_id"), [{"span_id": normal["SpanId"]}]
        )
        self.assertEqual(
            self.select("code_agent_run_links", "run_id"), [{"run_id": "normal-run"}]
        )
        self.assertEqual(
            self.select("agent_feedback", "target_kind, target_id"),
            [{"target_kind": "trace", "target_id": self.target}],
        )
        self.assertEqual(
            self.execute(
                "SELECT TraceId, max(End) AS end FROM otel_traces.otel_traces_trace_id_ts "
                f"WHERE TraceId IN ('{self.target}', '{reserved['TraceId']}') GROUP BY TraceId"
            ),
            [{"TraceId": self.target, "end": normal["Timestamp"].split(".")[0]}],
        )

    def test_run_mapping_rejects_bad_types_lengths_and_trace_ids(self):
        invalid_runs = [123, True, ["run"], {"nested": "run"}, "", "x" * 257, "é" * 129]
        rows = [
            self.span({"montecarlo.association_properties.run_id": run})
            for run in invalid_runs
        ]
        rows.extend(
            self.span(
                {"montecarlo.association_properties.run_id": "valid-run"}, TraceId=trace
            )
            for trace in ("g" * 32, "0" * 32, "abc", "a" * 33)
        )
        normal = self.span({"montecarlo.association_properties.run_id": "é" * 128})
        rows.append(normal)
        self.insert(rows)
        self.assertEqual(
            self.select("code_agent_run_links", "run_id, trace_id"),
            [{"run_id": "é" * 128, "trace_id": self.target}],
        )
        self.assertEqual(len(self.select("spans_normalized")), len(rows))

    def test_nonstring_feedback_markers_remain_ordinary_spans(self):
        values = [123, True, ["user_feedback"], {"name": "user_feedback"}, None]
        rows = [
            self.span(
                {
                    "gen_ai.evaluation.name": value,
                    "montecarlo.association_properties.run_id": f"run-{index}",
                },
                ParentSpanId="1234567890abcdef",
            )
            for index, value in enumerate(values)
        ]
        self.insert(rows)
        self.assertEqual(len(self.select("spans_normalized")), len(rows))
        self.assertEqual(len(self.select("code_agent_run_links")), len(rows))
        self.assertEqual(self.select("agent_feedback"), [])

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
            {"montecarlo.feedback.run_id": ""},
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
        normal = self.span(
            {"gen_ai.evaluation.name": "correctness"}, ParentSpanId="1234567890abcdef"
        )
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
        self.assertEqual(result["target_id"], "run-1")
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

    def test_nonstring_agent_overrides_preserve_existing_conversion(self):
        for name, expected_name in (
            (123, "123"),
            (True, "true"),
            (["agent"], '["agent"]'),
            ({"name": "agent"}, '{"name":"agent"}'),
            (None, self.service),
            ("", self.service),
        ):
            with self.subTest(name=name):
                trace_id = uuid.uuid4().hex
                normal = self.span(
                    {
                        "montecarlo.agent_name": name,
                        "montecarlo.association_properties.run_id": "run",
                    },
                    TraceId=trace_id,
                )
                feedback = self.feedback(
                    **{
                        "montecarlo.agent_name": name,
                        "montecarlo.feedback.trace_id": trace_id,
                    }
                )
                self.insert([normal, feedback])
                expected = [{"service_name": expected_name}]
                self.assertEqual(
                    self.execute(
                        "SELECT service_name FROM otel_traces.code_agent_run_links "
                        f"WHERE trace_id = '{trace_id}'"
                    ),
                    expected,
                )
                self.assertEqual(
                    self.execute(
                        "SELECT service_name FROM otel_traces.agent_feedback "
                        f"WHERE feedback_span_id = '{feedback['SpanId']}'"
                    ),
                    expected,
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
        start = initial.index("    if(label = 'none', [], arraySlice")
        end = initial.index(" AS categories,", start)
        initial = (
            initial[:start]
            + "    JSONExtract(attrs, 'montecarlo', 'feedback', 'categories', 'Array(String)')"
            + initial[end:]
        )
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
                    "montecarlo.feedback.categories": '[null,"",1,"incorrect"]',
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

    def test_upgrade_stops_linking_inferred_run_ids(self):
        # Replace the previous query, which serialized inferred dates as strings.
        self.execute("""
ALTER TABLE otel_traces.code_agent_run_links_mv MODIFY QUERY
WITH
    (SpanName != 'user_feedback'
        AND ifNull(dynamicElement(SpanAttributes.gen_ai.evaluation.name, 'String'), '') != 'user_feedback'
        AND isNotNull(SpanAttributes.montecarlo.association_properties.run_id)) AS has_run_value,
    toString(if(has_run_value, SpanAttributes.^montecarlo.association_properties, CAST('{}' AS JSON))) AS association_attrs,
    JSONExtractString(association_attrs, 'run_id') AS associated_run_id,
    (has_run_value AND JSONType(association_attrs, 'run_id') = 'String'
        AND associated_run_id != '' AND length(associated_run_id) <= 256) AS is_run_span,
    -- Read only Monte Carlo attributes to preserve existing agent-name conversion.
    toString(if(is_run_span, SpanAttributes.^montecarlo, CAST('{}' AS JSON))) AS agent_attrs,
    coalesce(nullIf(JSONExtractString(agent_attrs, 'agent_name'), ''), ServiceName) AS agent_name
SELECT
    agent_name AS service_name,
    associated_run_id AS run_id,
    lower(TraceId) AS trace_id,
    min(Timestamp) AS first_seen,
    max(Timestamp) AS last_seen
FROM otel_traces.otel_traces
WHERE is_run_span
  AND agent_name != ''
  AND match(TraceId, '^[0-9a-fA-F]{32}$')
  AND lower(TraceId) != repeat('0', 32)
GROUP BY service_name, run_id, trace_id;
""")
        try:
            self.insert(
                [
                    self.span({"montecarlo.association_properties.run_id": 123}),
                    self.span({"montecarlo.association_properties.run_id": "before"}),
                ]
            )
            self.assertEqual(
                self.select("code_agent_run_links", "run_id"), [{"run_id": "before"}]
            )
            self.apply([SQL_DIR / "0037_code_agent_run_links_native_filters.sql"])
            self.insert(
                [
                    self.span({"montecarlo.association_properties.run_id": 456}),
                    self.span({"montecarlo.association_properties.run_id": "after"}),
                ]
            )
            self.insert(
                [self.span({"montecarlo.association_properties.run_id": "2026-10-29"})],
                legacy_inference=True,
            )
            self.assertEqual(
                self.select("code_agent_run_links", "run_id", "ORDER BY run_id"),
                [{"run_id": "after"}, {"run_id": "before"}],
            )
            self.insert(
                [self.span({"montecarlo.association_properties.run_id": "2026-10-29"})]
            )
            self.assertEqual(
                self.select("code_agent_run_links", "run_id", "ORDER BY run_id"),
                [{"run_id": "2026-10-29"}, {"run_id": "after"}, {"run_id": "before"}],
            )
        finally:
            self.apply([SQL_DIR / "0037_code_agent_run_links_native_filters.sql"])

    def test_old_collector_date_cannot_alias_a_new_string_run(self):
        for original, rewritten in (
            ("2026-1-5", "2026-01-05"),
            ("2026-02-30", "2026-03-02"),
            ("2026-10-29 12:00:00.123456", "2026-10-29 12:00:00.123456000"),
        ):
            with self.subTest(original=original):
                old_trace, new_trace = uuid.uuid4().hex, uuid.uuid4().hex
                self.insert(
                    [
                        self.span(
                            {"montecarlo.association_properties.run_id": original},
                            TraceId=old_trace,
                        )
                    ],
                    legacy_inference=True,
                )
                raw = self.execute(
                    "SELECT dynamicType(SpanAttributes.montecarlo.association_properties.run_id) AS kind "
                    f"FROM otel_traces.otel_traces WHERE TraceId = '{old_trace}'"
                )
                self.assertTrue(raw[0]["kind"].startswith("Date"), raw)
                self.assertEqual(
                    self.select(
                        "code_agent_run_links",
                        "trace_id",
                        f"AND trace_id = '{old_trace}'",
                    ),
                    [],
                )
                self.insert(
                    [
                        self.span(
                            {"montecarlo.association_properties.run_id": rewritten},
                            TraceId=new_trace,
                        )
                    ]
                )
                self.insert(
                    [self.feedback(**{"montecarlo.feedback.run_id": rewritten})]
                )
                matched = self.execute(
                    "SELECT DISTINCT l.trace_id FROM otel_traces.agent_feedback AS f "
                    "INNER JOIN otel_traces.code_agent_run_links AS l "
                    "ON f.service_name = l.service_name AND f.target_id = l.run_id "
                    f"WHERE f.service_name = '{self.service}' AND f.target_id = '{rewritten}'"
                )
                self.assertEqual(matched, [{"trace_id": new_trace}])

    def test_feedback_table_backlog_rejects_insert_and_recovers(self):
        self.execute("SYSTEM STOP MERGES otel_traces.agent_feedback")
        try:
            self.execute(
                f"INSERT INTO otel_traces.agent_feedback (service_name, agent_mcon, request_id, positive, has_comment, submitted_at, collected_at) VALUES ('{self.service}', 'existing-agent', 'backlog-seed', true, false, now64(6), now64(6))"
            )
            self.execute(
                "ALTER TABLE otel_traces.agent_feedback MODIFY SETTING parts_to_throw_insert = 1, parts_to_delay_insert = 1"
            )
            with self.assertRaises(AssertionError) as failure:
                self.insert([self.span()])
            self.assertIn("TOO_MANY_PARTS", str(failure.exception))
            self.assertIn("code_agent_feedback_mv", str(failure.exception))
        finally:
            self.execute(
                "ALTER TABLE otel_traces.agent_feedback RESET SETTING parts_to_throw_insert, parts_to_delay_insert"
            )
            self.execute("SYSTEM START MERGES otel_traces.agent_feedback")
        # A failed multi-view insert is not an atomic transaction; use a fresh ID.
        retry = self.span()
        self.insert([retry])
        self.assertEqual(
            self.select(
                "spans_normalized", "span_id", f"AND span_id = '{retry['SpanId']}'"
            ),
            [{"span_id": retry["SpanId"]}],
        )

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

    def test_fresh_mapping_has_projection_before_later_migrations(self):
        database = "fresh_mapping_" + uuid.uuid4().hex
        self.execute("CREATE DATABASE " + database)
        try:
            self.execute(
                (SQL_DIR / "0031_code_agent_run_links.sql")
                .read_text()
                .replace("'{cluster}'", "'feedback_test'")
                .replace("otel_traces.", database + ".")
            )
            table = database + ".code_agent_run_links"
            self.execute(
                f"INSERT INTO {table} SELECT 'agent', toString(number), "
                "lower(hex(MD5(toString(number)))), now64(9), now64(9) FROM numbers(100000)"
            )
            definition = self.execute("SHOW CREATE TABLE " + table)[0]["statement"]
            self.assertIn("PROJECTION by_trace", definition)
            self.assertIn("deduplicate_merge_projection_mode = 'rebuild'", definition)
            expected = self.execute(
                "SELECT '50000' AS run_id, lower(hex(MD5('50000'))) AS trace_id"
            )
            self.assertEqual(
                self.execute(
                    f"SELECT run_id, trace_id FROM {table} WHERE service_name = 'agent' "
                    "AND trace_id = lower(hex(MD5('50000'))) GROUP BY run_id, trace_id "
                    "SETTINGS max_rows_to_read = 10000"
                ),
                expected,
            )
        finally:
            self.execute("DROP DATABASE " + database + " SYNC")
