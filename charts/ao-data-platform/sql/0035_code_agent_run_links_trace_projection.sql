-- Page reads start with trace IDs; ingestion and status checks start with run IDs.
-- Keep both lookup orders over the compact mapping.
ALTER TABLE otel_traces.code_agent_run_links ON CLUSTER '{cluster}'
MODIFY SETTING deduplicate_merge_projection_mode = 'rebuild';

ALTER TABLE otel_traces.code_agent_run_links ON CLUSTER '{cluster}'
ADD PROJECTION IF NOT EXISTS by_trace
(
    SELECT service_name, trace_id, run_id, first_seen, last_seen
    ORDER BY (service_name, trace_id, run_id)
);
