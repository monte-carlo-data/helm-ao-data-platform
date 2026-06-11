CREATE TABLE IF NOT EXISTS otel_traces.otel_traces_trace_id_ts (
    TraceId String CODEC(ZSTD(1)),
    Start DateTime CODEC(Delta(4), ZSTD(1)),
    End DateTime CODEC(Delta(4), ZSTD(1)),
    INDEX idx_trace_id TraceId TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = MergeTree
PARTITION BY toDate(Start)
ORDER BY (TraceId, Start)
-- Create-time default; the live retention is set by clickhouse.ttlDays via the
-- schema-job MODIFY TTL step. Keep this 30 in sync with that value's default.
TTL toDate(Start) + toIntervalDay(30)
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
