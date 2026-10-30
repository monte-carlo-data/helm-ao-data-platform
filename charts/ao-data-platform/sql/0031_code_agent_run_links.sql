-- A run can have several traces. Each pair remains distinct.
-- GROUP BY in the materialized view combines one inserted block. This engine
-- also combines pairs across blocks. Reads GROUP BY the key before using the
-- times so they remain correct before background merges.
-- No time partition: a run crossing a month must still have one merge key.
CREATE TABLE IF NOT EXISTS otel_traces.code_agent_run_links ON CLUSTER '{cluster}'
(
    service_name LowCardinality(String),
    run_id String,
    trace_id String,
    first_seen SimpleAggregateFunction(min, DateTime64(9)),
    last_seen SimpleAggregateFunction(max, DateTime64(9)),
    PROJECTION by_trace
    (
        SELECT service_name, trace_id, run_id, first_seen, last_seen
        ORDER BY (service_name, trace_id, run_id)
    )
)
ENGINE = ReplicatedAggregatingMergeTree
ORDER BY (service_name, run_id, trace_id)
TTL toDateTime(last_seen) + INTERVAL 30 DAY DELETE
SETTINGS deduplicate_merge_projection_mode = 'rebuild';
