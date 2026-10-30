-- Invalid feedback never rejects ordinary spans in the same inserted block.
-- Keep this query identical to 0036_code_agent_feedback_collector_categories.sql.
CREATE MATERIALIZED VIEW IF NOT EXISTS otel_traces.code_agent_feedback_mv ON CLUSTER '{cluster}'
TO otel_traces.agent_feedback
DEFINER = schema_owner SQL SECURITY DEFINER
AS WITH
    ifNull(dynamicElement(SpanAttributes.gen_ai.evaluation.name, 'String'), '') AS evaluation_name,
    -- Guard the value before serialization; WHERE evaluation order is not a gate.
    toString(if(evaluation_name = 'user_feedback', SpanAttributes, CAST('{}' AS JSON))) AS attrs,
    JSONExtractString(attrs, 'gen_ai', 'evaluation', 'score', 'label') AS label,
    JSONHas(attrs, 'gen_ai', 'evaluation', 'score', 'label') AS has_label,
    JSONExtractString(attrs, 'montecarlo', 'feedback', 'trace_id') AS rated_trace_id,
    JSONExtractString(attrs, 'montecarlo', 'feedback', 'run_id') AS rated_run_id,
    JSONHas(attrs, 'montecarlo', 'feedback', 'trace_id') AS has_trace_target,
    JSONHas(attrs, 'montecarlo', 'feedback', 'run_id') AS has_run_target,
    if(has_trace_target, rated_trace_id, TraceId) AS target_trace_id,
    (has_trace_target OR (NOT has_run_target AND ParentSpanId != '')) AS rates_trace,
    coalesce(nullIf(JSONExtractString(attrs, 'montecarlo', 'agent_name'), ''), ServiceName) AS agent_name,
    multiIf(
        JSONType(attrs, 'montecarlo', 'feedback', 'categories') = 'Array',
            JSONExtractArrayRaw(attrs, 'montecarlo', 'feedback', 'categories'),
        JSONType(attrs, 'montecarlo', 'feedback', 'categories') = 'String',
            JSONExtractArrayRaw(JSONExtractString(attrs, 'montecarlo', 'feedback', 'categories')),
        []
    ) AS category_values,
    arrayMap(category -> JSONExtractString(category),
        arrayFilter(category -> JSONType(category) = 'String', category_values)) AS string_categories
SELECT
    agent_name AS service_name,
    '' AS agent_mcon,
    if(rates_trace, lower(target_trace_id), rated_run_id) AS request_id,
    multiIf(label = 'positive', true, label = 'negative', false, NULL) AS positive,
    label != 'none'
        AND JSONType(attrs, 'gen_ai', 'evaluation', 'explanation') = 'String'
        AND JSONExtractString(attrs, 'gen_ai', 'evaluation', 'explanation') != '' AS has_comment,
    -- Match the SDK nonblank rule, including Unicode separators and control whitespace.
    -- Keep the original text of accepted categories.
    if(label = 'none', [], arraySlice(arrayFilter(category ->
        length(category) <= 128 AND match(category, '[^\\s\\p{Z}\\x{000B}\\x{0085}\\x{001C}-\\x{001F}]'),
        string_categories), 1, 20)) AS categories,
    toDateTime64(Timestamp, 6) AS submitted_at,
    now64(6) AS collected_at,
    'code' AS source,
    label = 'none' AS cleared,
    if(rates_trace, 'trace', 'run') AS target_kind,
    request_id AS target_id,
    TraceId AS feedback_trace_id,
    SpanId AS feedback_span_id,
    ServiceName AS feedback_service_name,
    SpanName AS feedback_span_name,
    Timestamp AS feedback_span_time
FROM otel_traces.otel_traces
WHERE evaluation_name = 'user_feedback'
  AND agent_name != ''
  AND NOT (has_trace_target AND has_run_target)
  AND (NOT has_label OR (JSONType(attrs, 'gen_ai', 'evaluation', 'score', 'label') = 'String' AND label IN ('positive', 'negative', 'none')))
  AND (NOT has_trace_target OR JSONType(attrs, 'montecarlo', 'feedback', 'trace_id') = 'String')
  AND (NOT has_run_target OR JSONType(attrs, 'montecarlo', 'feedback', 'run_id') = 'String')
  AND (
      (rates_trace AND match(target_trace_id, '^[0-9a-fA-F]{32}$') AND lower(target_trace_id) != repeat('0', 32))
      OR (NOT rates_trace AND has_run_target AND rated_run_id != '' AND length(rated_run_id) <= 256)
  );
