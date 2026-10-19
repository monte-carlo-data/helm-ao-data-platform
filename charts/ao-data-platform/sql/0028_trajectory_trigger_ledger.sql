-- The trigger spans that made a conversation-scoped trajectory monitor alert.
-- Each run's trigger window overlaps the previous run's, so a failed run's
-- triggers are examined again. The monitor query anti-joins this table, so a
-- trigger alerts once. Triggers that did not alert are not recorded: examining
-- them again gives the same result.
--
-- Written by the monte_carlo user as an INSERT ... SELECT in the same query
-- batch as the monitor query, the same as conversation_eval_scores (0013).
--
-- ReplicatedReplacingMergeTree(recorded_at) collapses a retried run's
-- duplicate rows at merge. The anti-join does not need FINAL: a duplicate key
-- has no effect. The TTL follows clickhouse.ttlDays like the other
-- annotations (the schema Job's ALTER is the source of truth on upgrades): a
-- trigger older than its spans cannot fall in a trigger window again.
CREATE TABLE IF NOT EXISTS otel_traces.trajectory_trigger_ledger ON CLUSTER '{cluster}'
(
    `monitor_uuid` UUID,
    `service_name` LowCardinality(String),
    `conversation_id` String,
    `trace_id` String,
    `span_id` String,
    `trigger_start` DateTime64(9),
    `job_execution_uuid` UUID,
    `recorded_at` DateTime64(9)
)
ENGINE = ReplicatedReplacingMergeTree(recorded_at)
PARTITION BY toYYYYMM(trigger_start)
ORDER BY (monitor_uuid, trace_id, span_id)
TTL trigger_start + INTERVAL 30 DAY DELETE;
