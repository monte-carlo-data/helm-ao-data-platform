CREATE MATERIALIZED VIEW IF NOT EXISTS otel_traces.otel_traces_trace_id_ts_mv
TO otel_traces.otel_traces_trace_id_ts (
    TraceId String,
    Start DateTime64(9),
    End DateTime64(9)
)
AS SELECT
    TraceId,
    min(Timestamp) AS Start,
    max(Timestamp) AS End
FROM otel_traces.otel_traces
WHERE TraceId != ''
GROUP BY TraceId;
