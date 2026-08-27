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
-- Idempotent (ADD COLUMN IF NOT EXISTS) so the schema job can re-run it on
-- every upgrade.
ALTER TABLE otel_traces.conversations_normalized ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS turn_duration_seconds Nullable(Float64),
    ADD COLUMN IF NOT EXISTS turn_tokens Nullable(UInt64),
    ADD COLUMN IF NOT EXISTS turn_errors_count Nullable(UInt32);
