-- Turn rollup storage for the ADK/gen_ai conversation writer. The 0012 MV's
-- admission gate (parent_span_id = '' AND conversation_id != '' AND a
-- traceloop.entity.input|output key) only matches Traceloop/LangGraph-shaped
-- traces, so ADK turns never materialize. A scheduled writer job will roll
-- spans_normalized up per trace and INSERT into conversations_normalized;
-- these three columns hold what the read path supplies today by LEFT JOINing
-- a per-trace rollup of spans_normalized.
-- Nullable so the eventual read switch can coalesce(stored, joined): rows the
-- 0012 MV writes carry no value here, and a 0 default would make that fallback
-- unreachable. New columns are absent from the 0012 MV's SELECT, so MV-written
-- rows land with NULL — the intended steady state until the MV retires.
-- Appended, though position does not bind: the 0012 MV's insert matches the
-- target by output-alias NAME (verified on 26.2 — an alias the table lacks
-- is rejected at the MV's CREATE, and an AFTER-placed column does not shift
-- the mapping). The hazard is a renamed alias or target column, not order.
-- The writer must not emit a trace the 0012 gate admits:
-- conversations_normalized is ReplacingMergeTree with no version column, so an
-- MV row (new columns NULL) and a writer row (new columns set) for the same
-- (service_name, minute, conversation_id, trace_id) dedup to an arbitrary
-- winner. Nothing in the schema enforces that split, and the case it turns on
-- is a dual-instrumented trace: an SDK emitting the gen_ai keys AND a
-- traceloop.entity.input|output key satisfies the 0012 gate and the writer's
-- own shape at once. The two shapes are disjoint today — measured on prod-us1
-- 2026-08-28, 0 of 81,003 root spans carrying a conversation_id over 3 days had
-- both, against 80,694 the gate admits and 100 with gen_ai keys — but that is a
-- property of today's instrumentation, not of this schema, so the writer still
-- owes an explicit exclusion of what 0012 admits.
-- turn_tokens is UInt64 where spans_normalized's token columns are UInt32: it
-- sums the per-span total_tokens over a trace, and summing UInt32 promotes to
-- UInt64. It is not a sum of prompt_tokens and completion_tokens — those
-- re-count context carried into each LLM step.
-- turn_errors_count narrows the rollup's countIf() (UInt64) to UInt32 at
-- insert; reaching 2^32 would need that many errored spans in one trace —
-- not a real input.
-- Idempotent (ADD COLUMN IF NOT EXISTS) so the schema job can re-run it on
-- every upgrade.
ALTER TABLE otel_traces.conversations_normalized ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS turn_duration_seconds Nullable(Float64),
    ADD COLUMN IF NOT EXISTS turn_tokens Nullable(UInt64),
    ADD COLUMN IF NOT EXISTS turn_errors_count Nullable(UInt32);
