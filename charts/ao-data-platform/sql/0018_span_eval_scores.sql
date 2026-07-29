-- Durable per-span eval scores — the span-grain sibling of
-- conversation_eval_scores (0013), AO-856. Written as a Q4-sibling INSERT in
-- the same LLM batch_group as monitor execution (no separate write service).
-- monitor_uuid distinguishes N monitors scoring the same span. The read path
-- returns the latest row per (service_name, trace_id, span_id, monitor_uuid,
-- eval_type) via LIMIT 1 BY, so a re-score just appends a newer scored_at and
-- the older row is ignored by reads (then TTL'd). Engine/partition/TTL follow
-- the otel_traces llm_results convention; the 30-day TTL DELETE matches the
-- source spans' retention so score rows age out with the spans they annotate
-- (the schema Job's ttlDays ALTER is the source of truth on upgrades).
-- scored_at >= span start_time, so a score is never deleted before its span.
CREATE TABLE IF NOT EXISTS otel_traces.span_eval_scores ON CLUSTER '{cluster}'
(
    `monitor_uuid` UUID,
    `service_name` LowCardinality(String),
    `trace_id` String,
    `span_id` String,
    `eval_type` LowCardinality(String),
    `score` Float64,
    `reasoning` String,
    `batch_id` UUID,
    `scored_at` DateTime64(9)
)
ENGINE = ReplicatedMergeTree
PARTITION BY toYYYYMM(scored_at)
ORDER BY (service_name, trace_id, span_id, monitor_uuid, eval_type)
TTL scored_at + INTERVAL 30 DAY DELETE;
