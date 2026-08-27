-- Per-agent completeness watermark for the conversation turn rollup writer
-- (the job that loads ADK/gen_ai conversations into conversations_normalized,
-- which the 0012 MV's Traceloop-only gate cannot serve). After each run's
-- INSERT lands, the writer publishes one row per scanned service_name: every
-- turn whose spans arrived at or before `watermark` has been written. The
-- cursor cannot be derived from the target table — it is arrival-ordered, so
-- max(turn_start) over written rows is not a completeness bound — which is
-- why it is published rather than computed. Keyed by service_name, the
-- writer's scan unit; spans_normalized has no account column, so nothing
-- finer exists to key on.
--
-- Current-state metadata, not time-series (the 0017 llm_worker_info shape):
-- no TTL and no PARTITION BY. A watermark must survive ANY pause — a writer
-- stopped for longer than a retention window must still hold its last cursor,
-- and no row then unambiguously means never published.
--
-- ReplicatedReplacingMergeTree(watermark): the watermark VALUE is the version,
-- so a merge keeps the highest watermark per service_name and a regressed
-- re-publish (a retry or backfill carrying a lower cursor) is discarded by
-- the engine — monotonicity is structural, not a read-side convention. Read
-- idiom: SELECT service_name, max(watermark) ... GROUP BY service_name. The
-- max is the same answer before and after merges, so reads need no ORDER BY /
-- LIMIT 1 BY and no FINAL. `published_at` is liveness and provenance only —
-- read max(published_at) for "is the writer alive?"; same-watermark republish
-- ties resolve arbitrarily, which is fine at seconds scale.
--
-- `watermark` is an EVENT-TIME bound (comparable to turn_start / period_to)
-- whose advancement the writer gates on arrival completeness; the arrival
-- signal itself is the writer's choice (a MATERIALIZED now() column vs
-- system.parts), not this table's concern. One BATCHED INSERT per run covers
-- every scanned service_name — per-service inserts would create a part per
-- row on a high-frequency job. Async replica lag can only under-assert
-- completeness (a replica that has not seen the latest row answers with a
-- lower watermark), which is the fail-safe direction; reads need no
-- select_sequential_consistency.
--
-- The writer runs as `monte_carlo`; the INSERT grant ships in this release
-- (templates/clickhouse-installation.yaml). Files correspond to the monolith
-- test fixture by basename, not ordinal (see 0019's note); the fixture's
-- version of this table is a plain ReplacingMergeTree with no ON CLUSTER —
-- intentional, it runs on a single-node test container.
CREATE TABLE IF NOT EXISTS otel_traces.conversation_rollup_watermarks ON CLUSTER '{cluster}'
(
    `service_name` LowCardinality(String),
    `watermark` DateTime64(9),
    `published_at` DateTime64(9)
)
ENGINE = ReplicatedReplacingMergeTree(watermark)
ORDER BY service_name;
