-- Keep this query identical to 0037_code_agent_run_links_native_filters.sql.
CREATE MATERIALIZED VIEW IF NOT EXISTS otel_traces.code_agent_run_links_mv ON CLUSTER '{cluster}'
TO otel_traces.code_agent_run_links
DEFINER = schema_owner SQL SECURITY DEFINER
AS WITH
    ifNull(dynamicElement(SpanAttributes.montecarlo.association_properties.run_id, 'String'), '') AS associated_run_id,
    (SpanName != 'user_feedback'
        AND ifNull(dynamicElement(SpanAttributes.gen_ai.evaluation.name, 'String'), '') != 'user_feedback'
        AND associated_run_id != '' AND length(associated_run_id) <= 256) AS is_run_span,
    -- Read only Monte Carlo attributes to preserve existing agent-name conversion.
    toString(if(is_run_span, SpanAttributes.^montecarlo, CAST('{}' AS JSON))) AS agent_attrs,
    coalesce(nullIf(JSONExtractString(agent_attrs, 'agent_name'), ''), ServiceName) AS agent_name
SELECT
    agent_name AS service_name,
    associated_run_id AS run_id,
    lower(TraceId) AS trace_id,
    min(Timestamp) AS first_seen,
    max(Timestamp) AS last_seen
FROM otel_traces.otel_traces
WHERE is_run_span
  AND agent_name != ''
  AND match(TraceId, '^[0-9a-fA-F]{32}$')
  AND lower(TraceId) != repeat('0', 32)
GROUP BY service_name, run_id, trace_id;
