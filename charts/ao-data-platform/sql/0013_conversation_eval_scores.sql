-- Durable per-conversation eval scores. Written as a Q4-sibling INSERT in the
-- same LLM batch_group as monitor execution (no separate write service).
-- monitor_uuid distinguishes N monitors scoring the same conversation. The read
-- path returns the latest row per (service_name, monitor_uuid, conversation_id,
-- eval_type) via LIMIT 1 BY, so a re-score just appends a newer scored_at and the
-- older row is ignored by reads (then TTL'd). Engine/partition/TTL follow the
-- otel_traces llm_results convention; the 30-day TTL DELETE matches the source
-- conversations' retention so score rows age out with the conversations they
-- annotate (the only read path joins by (service_name, conversation_id) — an
-- orphaned score whose conversation has been dropped is unreadable). scored_at >=
-- turn_start, so a score is never deleted before its conversation.
CREATE TABLE IF NOT EXISTS otel_traces.conversation_eval_scores
(
    `monitor_uuid` UUID,
    `service_name` LowCardinality(String),
    `conversation_id` String,
    `eval_type` LowCardinality(String),
    `score` Float64,
    `reasoning` String,
    `batch_id` UUID,
    `scored_at` DateTime64(9)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(scored_at)
ORDER BY (service_name, conversation_id, monitor_uuid, eval_type)
TTL scored_at + INTERVAL 30 DAY DELETE;
