-- Late child feedback must not extend the rated trace's time bounds.
ALTER TABLE otel_traces.otel_traces_trace_id_ts_mv ON CLUSTER '{cluster}'
MODIFY QUERY
SELECT TraceId, min(Timestamp) AS Start, max(Timestamp) AS End
FROM otel_traces.otel_traces
WHERE TraceId != ''
  AND SpanName != 'user_feedback'
  AND JSONExtractString(toString(SpanAttributes), 'gen_ai', 'evaluation', 'name') != 'user_feedback'
GROUP BY TraceId;
