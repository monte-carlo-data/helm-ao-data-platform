CREATE TABLE IF NOT EXISTS otel_traces.spans_normalized
(
    `service_name` LowCardinality(String),
    `trace_id` String,
    `span_id` String,
    `parent_span_id` String,
    `span_name` LowCardinality(String),
    `start_time` DateTime64(9),
    `end_time` DateTime64(9),
    `duration_ns` UInt64,
    `status_code` Nullable(UInt8),
    `status_message` String,
    `exception_type` LowCardinality(String),
    `exception_message` String,
    `model` LowCardinality(String),
    `workflow` LowCardinality(String),
    `task` LowCardinality(String),
    `conversation_id` String,
    `is_llm_call` Bool,
    `is_tool_call` Bool,
    `prompt_tokens` Nullable(UInt32),
    `completion_tokens` Nullable(UInt32),
    `total_tokens` Nullable(UInt32),
    `has_prompts` Bool,
    `has_completions` Bool,
    `resource_attributes` JSON CODEC(ZSTD(1)),
    `resource_attributes_keys` Array(LowCardinality(String)) CODEC(ZSTD(1)),
    `span_attributes` JSON CODEC(ZSTD(1)),
    `span_attributes_keys` Array(LowCardinality(String)) CODEC(ZSTD(1)),
    `events` Nested (
        `timestamp` DateTime64(9),
        `name` LowCardinality(String),
        `attributes` JSON
    ) CODEC(ZSTD(1)),
    `links` Nested (
        `trace_id` String,
        `span_id` String,
        `trace_state` String,
        `attributes` JSON
    ) CODEC(ZSTD(1)),
    `prompts` Array(Tuple(
        `message`  String,
        `position` UInt16,
        `role`     LowCardinality(String)
    )) CODEC(ZSTD(1)),
    `completions` Array(Tuple(
        `message`    String,
        `position`   UInt16,
        `role`       LowCardinality(String),
        `tool_calls` Array(Tuple(
            `id`        String,
            `name`      LowCardinality(String),
            `arguments` String
        ))
    )) CODEC(ZSTD(1)),

    INDEX idx_trace_id trace_id TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_span_id span_id TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_conversation_id conversation_id TYPE bloom_filter GRANULARITY 1,
    INDEX idx_res_attr_keys  resource_attributes_keys TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_span_attr_keys span_attributes_keys     TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = ReplacingMergeTree
PARTITION BY toDate(start_time)
PRIMARY KEY (service_name, toStartOfMinute(start_time), xxHash32(trace_id))
ORDER BY (service_name, toStartOfMinute(start_time), xxHash32(trace_id), span_id)
SAMPLE BY xxHash32(trace_id)
-- Create-time default; the live retention is set by clickhouse.ttlDays via the
-- schema-job MODIFY TTL step. Keep this 30 in sync with that value's default.
TTL start_time + toIntervalDay(30)
SETTINGS
    ttl_only_drop_parts = 1,
    prewarm_mark_cache = 1,
    prewarm_primary_key_cache = 1;
