CREATE TABLE IF NOT EXISTS otel_traces.llm_results
(
    batch_id        UUID,
    row_id          UUID,
    response        String,
    status          Enum8('complete' = 1, 'failed' = 2),
    error           String DEFAULT '',
    created_at      DateTime DEFAULT now()
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (batch_id, row_id)
TTL created_at + INTERVAL 30 DAY DELETE
