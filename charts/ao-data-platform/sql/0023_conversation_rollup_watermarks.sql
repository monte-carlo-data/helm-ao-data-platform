-- Per-service completeness watermark for the conversation turn rollup writer
-- (the job that loads ADK/gen_ai conversations into conversations_normalized,
-- which the 0012 MV's Traceloop-only gate cannot serve). After each run's
-- INSERT lands, the writer publishes one row per scanned service_name: every
-- turn whose spans arrived at or before `watermark` has been written. The
-- cursor cannot be derived from the target table — it is arrival-ordered, so
-- max(turn_start) over written rows is not a completeness bound — which is
-- why it is published rather than computed. Keyed by service_name, the
-- writer's scan unit; spans_normalized has no account column and carries no
-- agent identifier, so nothing finer exists to key on, and two agents sharing
-- one service_name share one cursor.
--
-- Current-state metadata, not time-series (the 0017 llm_worker_info shape):
-- no TTL and no PARTITION BY. A watermark must survive ANY pause — a writer
-- stopped for longer than a retention window must still hold its last cursor,
-- and no row then unambiguously means never published.
--
-- ReplicatedReplacingMergeTree(watermark): the watermark VALUE is the version,
-- so a merge keeps the highest watermark per service_name and discards a
-- regressed re-publish (a retry or backfill carrying a lower cursor). The
-- discard happens AT MERGE: until one runs, the regressed row stays queryable
-- in its own part, so monotonicity comes from the read, not the engine. Read
-- idiom: SELECT service_name, max(watermark) ... GROUP BY service_name. The
-- max is the same answer before and after merges, so reads need no ORDER BY /
-- LIMIT 1 BY and no FINAL. `published_at` is provenance. A merge keeps only
-- the highest-watermark row, so a regressed re-publish's later publish time
-- disappears with its row — which is the signal that a regressing publisher
-- leaves behind: max(published_at) reads stale while the writer is healthy.
-- Same-watermark republish ties resolve last-wins at merge. The same version
-- rule makes an over-advanced cursor unfixable by re-publish: a lower
-- watermark loses both the max() read and the merge by design. Correcting one
-- is an operator action; the README's note on operating this table has the
-- steps.
--
-- `published_at` defaults to now64(9) so a writer that omits the column still
-- records a real publish time rather than the epoch, which would read as a
-- writer that has never run. The default is evaluated once per inserted block,
-- so one batched publish shares one timestamp. It cannot make the column a
-- liveness read: whether an identical re-send restamps the column is engine
-- dedup behavior this file does not assert — treat the stamp as free to
-- freeze for the full duration of a hold, the deliberate-pause case included.
-- Liveness comes from the writer's run telemetry, not from this column.
-- This release creates the table, so every install gets the column
-- with its default; a later edit to this file would apply to fresh installs
-- only (0019's note).
-- `watermark` takes no default by design — it is an event-time bound the
-- writer is expected to state, and no wall-clock fallback could be correct,
-- because it would assert completeness the run never earned. Nothing enforces
-- that: an INSERT naming only service_name stores the epoch, which loses
-- every merge and every max().
--
-- `watermark` is an EVENT-TIME bound (comparable to turn_start) whose
-- advancement the writer gates on arrival completeness; the arrival
-- signal itself is the writer's choice (a MATERIALIZED now() column vs
-- system.parts), not this table's concern. One BATCHED INSERT per install per
-- tick covers every service whose rollup completed and verified — per-service
-- inserts would create a part per row on a high-frequency job. Async replica lag can only under-assert
-- completeness (a replica that has not seen the latest row answers with a
-- lower watermark), which is the fail-safe direction; reads of this table need
-- no select_sequential_consistency. That covers the cursor read alone; a
-- consumer joining this cursor to conversations_normalized owes more, stated
-- as an operator fact in the README's note on operating this table.
--
-- The writer runs as `monte_carlo`; the INSERT grant ships in this release
-- (templates/clickhouse-installation.yaml). Files correspond to that fixture
-- by basename, not ordinal (0019's note); its version of this table is a
-- plain ReplacingMergeTree with no ON CLUSTER — intentional, it runs on a
-- single-node test container.
CREATE TABLE IF NOT EXISTS otel_traces.conversation_rollup_watermarks ON CLUSTER '{cluster}'
(
    `service_name` LowCardinality(String),
    `watermark` DateTime64(9),
    `published_at` DateTime64(9) DEFAULT now64(9)
)
ENGINE = ReplicatedReplacingMergeTree(watermark)
ORDER BY service_name;
