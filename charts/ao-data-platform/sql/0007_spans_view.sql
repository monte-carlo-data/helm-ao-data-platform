-- Read-side view over spans_normalized. Pass-through for everything except
-- prompts/completions, which gain `is_start` / `is_end` flags derived from
-- position within the per-span message array.
--
-- The arrays come out of spans_normalized_mv already sorted by position
-- (arraySort in the MV's index-discovery), so arrayEnumerate's 1-based
-- index aligns with conversation order: i = 1 is the first message,
-- i = length(arr) is the last.
--
-- Tuple key order (is_end, is_start, message, position, role[, tool_calls])
-- matches the JSON output the product expects and the V1 monolith query
-- shape, so downstream comparators see byte-identical strings.
CREATE VIEW IF NOT EXISTS otel_traces.spans
AS SELECT
    service_name,
    trace_id,
    span_id,
    parent_span_id,
    span_name,
    start_time,
    end_time,
    duration_ns,
    status_code,
    status_message,
    exception_type,
    exception_message,
    model,
    workflow,
    task,
    conversation_id,
    is_llm_call,
    is_tool_call,
    prompt_tokens,
    completion_tokens,
    total_tokens,
    has_prompts,
    has_completions,
    resource_attributes,
    resource_attributes_keys,
    span_attributes,
    span_attributes_keys,
    `events.timestamp`,
    `events.name`,
    `events.attributes`,
    `links.trace_id`,
    `links.span_id`,
    `links.trace_state`,
    `links.attributes`,

    arrayMap(
        (m, i) -> CAST((
            i = length(prompts),
            i = 1,
            m.message,
            m.position,
            m.role
        ) AS Tuple(
            is_end   Bool,
            is_start Bool,
            message  String,
            position UInt16,
            role     LowCardinality(String)
        )),
        prompts, arrayEnumerate(prompts)
    ) AS prompts,

    arrayMap(
        (m, i) -> CAST((
            i = length(completions),
            i = 1,
            m.message,
            m.position,
            m.role,
            m.tool_calls
        ) AS Tuple(
            is_end     Bool,
            is_start   Bool,
            message    String,
            position   UInt16,
            role       LowCardinality(String),
            tool_calls Array(Tuple(id String, name LowCardinality(String), arguments String))
        )),
        completions, arrayEnumerate(completions)
    ) AS completions

FROM otel_traces.spans_normalized;
