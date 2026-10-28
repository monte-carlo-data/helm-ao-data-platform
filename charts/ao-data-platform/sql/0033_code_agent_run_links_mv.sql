CREATE MATERIALIZED VIEW IF NOT EXISTS otel_traces.code_agent_run_links_mv ON CLUSTER '{cluster}'
TO otel_traces.code_agent_run_links
DEFINER = schema_owner SQL SECURITY DEFINER
AS WITH
    toString(SpanAttributes) AS attrs,
    JSONExtractString(attrs, 'montecarlo', 'association_properties', 'run_id') AS associated_run_id,
    coalesce(nullIf(JSONExtractString(attrs, 'montecarlo', 'agent_name'), ''), ServiceName) AS agent_name
SELECT
    agent_name AS service_name,
    associated_run_id AS run_id,
    lower(TraceId) AS trace_id,
    min(Timestamp) AS first_seen,
    max(Timestamp) AS last_seen
FROM otel_traces.otel_traces
WHERE SpanName != 'user_feedback'
  AND JSONExtractString(attrs, 'gen_ai', 'evaluation', 'name') != 'user_feedback'
  AND agent_name != ''
  AND associated_run_id != ''
  AND JSONType(attrs, 'montecarlo', 'association_properties', 'run_id') = 'String'
  AND length(associated_run_id) <= 256
  AND match(TraceId, '^[0-9a-fA-F]{32}$')
  AND lower(TraceId) != repeat('0', 32)
GROUP BY service_name, run_id, trace_id;
