-- Per-agent completeness watermark for the conversation turn rollup writer
-- (the job that loads ADK/gen_ai conversations into conversations_normalized,
-- which the 0012 MV's Traceloop-only gate cannot serve). The writer publishes
-- one row per service_name after each run's INSERT lands: every turn whose
-- spans arrived at or before `watermark` has been written. The cursor cannot
-- be derived from the target table — it is arrival-ordered, so max(turn_start)
-- over written rows is not a completeness bound — which is why it is published
-- rather than computed.
-- The read idiom is the conversation_eval_scores one: the latest row per
-- service_name via LIMIT 1 BY, so re-publishing appends a newer published_at
-- and the older row is ignored by reads (then TTL'd). A paused writer stops
-- publishing, so the watermark HOLDS rather than advancing over a window the
-- job skipped — the absence of a newer row is the pause signal. Keyed by
-- service_name, the writer's scan unit; spans_normalized has no account
-- column, so nothing finer exists to key on.
CREATE TABLE IF NOT EXISTS otel_traces.conversation_rollup_watermarks ON CLUSTER '{cluster}'
(
    `service_name` LowCardinality(String),
    `watermark` DateTime64(9),
    `published_at` DateTime64(9)
)
ENGINE = ReplicatedMergeTree
PARTITION BY toYYYYMM(published_at)
ORDER BY service_name
TTL published_at + INTERVAL 30 DAY DELETE;
