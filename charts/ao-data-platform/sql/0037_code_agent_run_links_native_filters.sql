-- Apply native filters to already-installed run mappings on every upgrade.
-- Keep this query identical to 0033_code_agent_run_links_mv.sql.
ALTER TABLE otel_traces.code_agent_run_links_mv ON CLUSTER '{cluster}'
MODIFY QUERY
WITH
    (SpanName != 'user_feedback'
        AND ifNull(dynamicElement(SpanAttributes.gen_ai.evaluation.name, 'String'), '') != 'user_feedback'
        AND isNotNull(SpanAttributes.montecarlo.association_properties.run_id)) AS has_run_value,
    -- JSON can infer Date/DateTime for string IDs; read their serialized string form.
    toString(if(has_run_value, SpanAttributes.^montecarlo.association_properties, CAST('{}' AS JSON))) AS association_attrs,
    JSONExtractString(association_attrs, 'run_id') AS associated_run_id,
    (has_run_value AND JSONType(association_attrs, 'run_id') = 'String'
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
