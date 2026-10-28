-- Defaults keep existing feedback writers compatible. Comments remain in raw spans.
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
