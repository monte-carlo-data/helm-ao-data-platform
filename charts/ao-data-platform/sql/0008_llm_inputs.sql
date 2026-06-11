CREATE TABLE IF NOT EXISTS otel_traces.llm_inputs
(
    batch_id        UUID,
    row_id          UUID,
    model_id        LowCardinality(String),
    prompt          String,
    params          String DEFAULT '{}',
    tool_config     String DEFAULT '',
    created_at      DateTime DEFAULT now()
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (batch_id, row_id)
TTL created_at + INTERVAL 30 DAY DELETE
