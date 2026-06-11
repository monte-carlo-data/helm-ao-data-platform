-- Turn-grained conversation source: one row per root-span trace (= one turn).
-- Mirrors the spans_normalized engine/TTL convention. The MV in 0009 does the
-- stateless per-root-span projection; `turn` is derived at read time (a window
-- function over turn_start) rather than stored, so this stays a streaming MV
-- target. span_attributes / span_attributes_keys are carried from the root span
-- to power turn-level attribute filtering downstream — minus the two heaviest
-- content paths (see the span_attributes column below).
CREATE TABLE IF NOT EXISTS otel_traces.conversations_normalized
(
    `service_name` LowCardinality(String),
    `conversation_id` String,
    `trace_id` String,
    `turn_start` DateTime64(9),
    `user_input` String,
    `agent_response` String,
    -- traceloop.entity.input/output are the heaviest payload in the system (the
    -- full running state.messages history). They're already extracted into
    -- user_input/agent_response above, and turn-level attribute filtering
    -- targets other attributes, not conversation content. SKIP them on the target
    -- column to avoid re-storing them; every other attribute is still shredded
    -- into sub-columns for attribute search. span_attributes_keys still lists
    -- both keys, so the 0009 has() root-span guard is unaffected.
    `span_attributes` JSON(
        SKIP `traceloop.entity.input`,
        SKIP `traceloop.entity.output`
    ) CODEC(ZSTD(1)),
    `span_attributes_keys` Array(LowCardinality(String)) CODEC(ZSTD(1)),
    `prompt_tokens` Nullable(UInt32),
    `completion_tokens` Nullable(UInt32),
    -- Root-span status (carried from spans_normalized): 2=Error, 1=Ok, 0=Unset.
    -- Powers conversation-level status (ERROR if any turn's root span errored).
    `status_code` Nullable(UInt8),
    -- Root-span workflow (carried from spans_normalized). Conversation↔workflow
    -- is 1:1, so this is constant per conversation_id and powers the
    -- conversation-level workflow filter.
    `workflow` LowCardinality(String),

    INDEX idx_conversation_id conversation_id TYPE bloom_filter(0.01) GRANULARITY 1,
    INDEX idx_span_attr_keys span_attributes_keys TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = ReplacingMergeTree
PARTITION BY toDate(turn_start)
PRIMARY KEY (service_name, toStartOfMinute(turn_start), conversation_id)
ORDER BY (service_name, toStartOfMinute(turn_start), conversation_id, trace_id)
TTL turn_start + toIntervalDay(30)
SETTINGS ttl_only_drop_parts = 1;
