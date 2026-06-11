CREATE TABLE IF NOT EXISTS otel_traces.llm_batches
(
    batch_id        UUID,
    status          Enum8('pending' = 1, 'complete' = 2),
    total_rows      UInt32 DEFAULT 0,
    completed_rows  UInt32 DEFAULT 0,
    failed_rows     UInt32 DEFAULT 0,
    created_at      DateTime DEFAULT now()
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (batch_id, created_at)
TTL created_at + INTERVAL 30 DAY DELETE
