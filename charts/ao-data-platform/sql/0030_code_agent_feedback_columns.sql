-- Defaults keep existing feedback writers compatible. Comments remain in raw spans.
-- Code feedback rows use an empty agent_mcon. Their request_id is the rated
-- trace or run ID, also recorded in target_id with its target_kind. They use
-- code_agent_run_links for run targets, not agent_turn_links.
ALTER TABLE otel_traces.agent_feedback ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS source LowCardinality(String) DEFAULT 'cortex',
    ADD COLUMN IF NOT EXISTS cleared Bool DEFAULT false,
    ADD COLUMN IF NOT EXISTS target_kind LowCardinality(String) DEFAULT 'run',
    ADD COLUMN IF NOT EXISTS target_id String DEFAULT request_id,
    ADD COLUMN IF NOT EXISTS feedback_trace_id String DEFAULT '',
    ADD COLUMN IF NOT EXISTS feedback_span_id String DEFAULT '',
    ADD COLUMN IF NOT EXISTS feedback_service_name String DEFAULT '',
    ADD COLUMN IF NOT EXISTS feedback_span_name String DEFAULT '',
    ADD COLUMN IF NOT EXISTS feedback_span_time DateTime64(9) DEFAULT toDateTime64(0, 9);
