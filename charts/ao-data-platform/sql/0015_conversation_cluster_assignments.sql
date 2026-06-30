-- Durable per-conversation cluster assignments. Written as a Q4-sibling INSERT
-- in the same LLM batch as classification (no separate write service). space_uuid
-- distinguishes taxonomies (per (account, agent, scope)) assigning the same
-- conversation. The read path collapses turns to conversation grain, INNER JOINs
-- this table on (service_name, conversation_id), and takes the latest row per
-- (service_name, conversation_id, space_uuid) via LIMIT 1 BY — a re-classify just
-- appends a newer classified_at and the older row is ignored by reads (then TTL'd).
-- Engine/partition/TTL follow the conversation_eval_scores (0013) convention; the
-- 30-day TTL DELETE matches the source conversations' retention so assignment rows
-- age out with the conversations they annotate (the only read path joins by
-- (service_name, conversation_id) — an orphaned assignment whose conversation has
-- been dropped is unreadable). "Uncategorized" is a reserved cluster_key, not a
-- Postgres row.
--
-- Latest-wins is NOT enforced by the engine: this is a plain MergeTree, not a
-- ReplacingMergeTree, and classified_at is not in the ORDER BY. Every read MUST
-- `ORDER BY classified_at DESC` before `LIMIT 1 BY (service_name, conversation_id,
-- space_uuid)` to collapse re-classifies to the newest row; a read that omits the
-- sort returns an arbitrary row per grain. (Mirrors the 0013 conversation_eval_scores
-- convention.)
CREATE TABLE IF NOT EXISTS otel_traces.conversation_cluster_assignments
(
    `space_uuid` UUID,
    `service_name` LowCardinality(String),
    `conversation_id` String,
    `taxonomy_version` UInt32,
    `cluster_key` LowCardinality(String),            -- final label; "Uncategorized" if below min_confidence
    `predicted_cluster_key` LowCardinality(String),  -- raw pick before the confidence gate (debug/tuning)
    `confidence` Float32,                            -- classifier self-reported confidence; drives the gate
    `batch_id` UUID,
    `classified_at` DateTime64(9)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(classified_at)
ORDER BY (service_name, conversation_id, space_uuid)
TTL classified_at + INTERVAL 30 DAY DELETE;
