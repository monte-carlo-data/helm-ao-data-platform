-- Support newer OTel GenAI semantic conventions (OpenLLMetry v0.55.0+ /
-- OTel-official instrumentors, e.g. Google ADK) on spans_normalized_mv.
--
-- Supersedes the SELECT defined in 0006_spans_normalized_mv.sql. Shipped as a new
-- migration (not an edit to 0006) because the schema Job re-runs every *.sql on
-- each upgrade and 0006 is CREATE ... IF NOT EXISTS (a no-op on existing clusters),
-- so an in-place edit would reach only fresh installs — the same reasoning as the
-- 0014 DEFINER reparent. ALTER ... MODIFY QUERY swaps the SELECT in place with no
-- view rebuild and no ingestion gap; the output column set is unchanged, so 0007
-- (spans view) and 0012 (conversations_normalized_mv) are unaffected, and the
-- DEFINER pinned by 0014 is preserved (MODIFY QUERY changes only the SELECT).
--
-- Two additive axes on top of 0006, both total / null-safe — every new expression
-- degrades to '' for a missing/malformed JSON path and never raises, so a source
-- INSERT into otel_traces can never be failed by this view (a raising MV halts
-- ingestion cluster-wide):
--   1. Content: the new semconv moved prompt/completion content from per-attribute
--      indexed keys (gen_ai.prompt.{N}.content) to single JSON-array attributes
--      gen_ai.input.messages / gen_ai.output.messages, each an array of
--      {role, parts:[{type, content} | {type:'tool_call', id, name, arguments}]},
--      with a message-level `content` fallback for emitters that omit `parts`.
--   2. Grouping: gen_ai.conversation.id (the OTel-standard conversation key) is
--      appended to the conversation_id coalesce (vendor keys stay first for
--      back-compat), so standard/ADK agents that set only the standard key group
--      into conversations.
--
-- Forward-only: MODIFY QUERY affects new inserts (new-convention spans are only
-- just appearing; nothing material to backfill).
ALTER TABLE otel_traces.spans_normalized_mv ON CLUSTER '{cluster}'
MODIFY QUERY
WITH
    -- Stringified SpanAttributes for dynamic-path JSONExtractString calls.
    toString(SpanAttributes) AS _attrs_json,

    -- New gen_ai semconv message arrays (JSON-array strings; '' when absent).
    JSONExtractString(_attrs_json, 'gen_ai', 'input', 'messages')  AS _input_messages,
    JSONExtractString(_attrs_json, 'gen_ai', 'output', 'messages') AS _output_messages,

    -- =============================================================
    -- Prompt index discovery — union of all formats, sorted/deduped.
    -- =============================================================
    arraySort(arrayDistinct(arrayConcat(
        -- Standard gen_ai semconv (legacy): gen_ai.prompt.{N}.role
        arrayMap(
            k -> toUInt16(extractAll(k, '\\.(\\d+)\\.')[1]),
            arrayFilter(k -> match(k, '^gen_ai\\.prompt\\.\\d+\\.role$'), SpanAttributesKeys)
        ),
        -- OpenInference: llm.input_messages.{N}.message.role
        arrayMap(
            k -> toUInt16(extractAll(k, '\\.(\\d+)\\.')[1]),
            arrayFilter(k -> match(k, '^llm\\.input_messages\\.\\d+\\.message\\.role$'), SpanAttributesKeys)
        ),
        -- New gen_ai semconv (v0.55.0+): gen_ai.input.messages holds all messages in
        -- one JSON array; positions are 0..len-1.
        arrayMap(x -> toUInt16(x), range(toUInt64(JSONLength(_input_messages)))),
        -- Strands: positional 0..N-1 over user-message events
        arrayMap(
            x -> toUInt16(x),
            range(toUInt64(length(arrayFilter(x -> x = 'gen_ai.user.message', `Events.Name`))))
        ),
        -- Snowflake native: single message at idx=0 if any source attr is set
        if(
            coalesce(CAST(SpanAttributes.ai.observability.record_root.input AS Nullable(String)), '') != ''
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.query AS Nullable(String)), '') != ''
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_search.query AS Nullable(String)), '') != ''
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_analyst.messages AS Nullable(String)), '') != '',
            [toUInt16(0)],
            CAST([] AS Array(UInt16))
        )
    ))) AS _prompt_indices,

    -- =============================================================
    -- Completion index discovery — analogous
    -- =============================================================
    arraySort(arrayDistinct(arrayConcat(
        arrayMap(
            k -> toUInt16(extractAll(k, '\\.(\\d+)\\.')[1]),
            arrayFilter(k -> match(k, '^gen_ai\\.completion\\.\\d+\\.role$'), SpanAttributesKeys)
        ),
        arrayMap(
            k -> toUInt16(extractAll(k, '\\.(\\d+)\\.')[1]),
            arrayFilter(k -> match(k, '^llm\\.output_messages\\.\\d+\\.message\\.role$'), SpanAttributesKeys)
        ),
        -- New gen_ai semconv: gen_ai.output.messages JSON array
        arrayMap(x -> toUInt16(x), range(toUInt64(JSONLength(_output_messages)))),
        arrayMap(
            x -> toUInt16(x),
            range(toUInt64(length(arrayFilter(
                x -> x IN ('gen_ai.assistant.message', 'gen_ai.choice'),
                `Events.Name`
            ))))
        ),
        if(
            coalesce(CAST(SpanAttributes.ai.observability.record_root.output AS Nullable(String)), '') != ''
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.response AS Nullable(String)), '') != ''
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.thinking_response AS Nullable(String)), '') != ''
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_search.results AS Nullable(String)), '') != ''
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_analyst.text AS Nullable(String)), '') != '',
            [toUInt16(0)],
            CAST([] AS Array(UInt16))
        )
    ))) AS _completion_indices

SELECT
    ServiceName AS service_name,
    TraceId AS trace_id,
    SpanId AS span_id,
    ParentSpanId AS parent_span_id,
    SpanName AS span_name,
    Timestamp AS start_time,
    addNanoseconds(Timestamp, Duration) AS end_time,
    Duration AS duration_ns,
    CASE StatusCode
        WHEN 'Error' THEN 2
        WHEN 'Ok' THEN 1
        WHEN 'Unset' THEN 0
        ELSE NULL
    END AS status_code,
    StatusMessage AS status_message,

    coalesce(
        CAST(Events.Attributes[indexOf(Events.Name, 'exception')].exception.type AS Nullable(String)),
        ''
    ) AS exception_type,
    coalesce(
        CAST(Events.Attributes[indexOf(Events.Name, 'exception')].exception.message AS Nullable(String)),
        ''
    ) AS exception_message,

    coalesce(
        nullIf(CAST(SpanAttributes.gen_ai.request.model AS Nullable(String)), ''),
        nullIf(CAST(SpanAttributes.llm.model_name AS Nullable(String)), ''),
        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.model AS Nullable(String)), ''),
        ''
    ) AS model,

    coalesce(
        nullIf(CAST(SpanAttributes.montecarlo.workflow AS Nullable(String)), ''),
        nullIf(CAST(SpanAttributes.traceloop.workflow.name AS Nullable(String)), ''),
        ''
    ) AS workflow,

    coalesce(
        nullIf(CAST(SpanAttributes.montecarlo.task AS Nullable(String)), ''),
        nullIf(CAST(SpanAttributes.traceloop.association.properties.langgraph_node AS Nullable(String)), ''),
        ''
    ) AS task,

    -- conversation_id: vendor keys first (back-compat), then the OTel-standard
    -- gen_ai.conversation.id. Any standard/ADK agent that sets only the standard
    -- key now groups into conversations.
    coalesce(
        nullIf(CAST(SpanAttributes.montecarlo.association_properties.thread_id AS Nullable(String)), ''),
        nullIf(CAST(SpanAttributes.session.id AS Nullable(String)), ''),
        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.thread_id AS Nullable(String)), ''),
        nullIf(CAST(SpanAttributes.gen_ai.conversation.id AS Nullable(String)), ''),
        ''
    ) AS conversation_id,

    coalesce(
        CAST(SpanAttributes.gen_ai.usage.prompt_tokens AS Nullable(UInt32)),
        CAST(SpanAttributes.gen_ai.usage.input_tokens AS Nullable(UInt32)),
        CAST(SpanAttributes.llm.token_count.prompt AS Nullable(UInt32)),
        CAST(SpanAttributes.snow.ai.observability.agent.planning.token_count.input AS Nullable(UInt32))
    ) AS prompt_tokens,

    coalesce(
        CAST(SpanAttributes.gen_ai.usage.completion_tokens AS Nullable(UInt32)),
        CAST(SpanAttributes.gen_ai.usage.output_tokens AS Nullable(UInt32)),
        CAST(SpanAttributes.llm.token_count.completion AS Nullable(UInt32)),
        CAST(SpanAttributes.snow.ai.observability.agent.planning.token_count.output AS Nullable(UInt32))
    ) AS completion_tokens,

    coalesce(
        CAST(SpanAttributes.gen_ai.usage.total_tokens AS Nullable(UInt32)),
        CAST(SpanAttributes.llm.usage.total_tokens AS Nullable(UInt32)),
        CAST(SpanAttributes.llm.token_count.total AS Nullable(UInt32))
    ) AS total_tokens,

    (coalesce(CAST(SpanAttributes.gen_ai.request.model AS Nullable(String)), '') != '')
        OR (coalesce(CAST(SpanAttributes.llm.model_name AS Nullable(String)), '') != '')
        OR (coalesce(CAST(SpanAttributes.gen_ai.operation.name AS Nullable(String)), '') = 'chat')
        OR (coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.model AS Nullable(String)), '') != '') AS is_llm_call,

    (coalesce(CAST(SpanAttributes.traceloop.span.kind AS Nullable(String)), '') = 'tool')
        OR (coalesce(CAST(SpanAttributes.gen_ai.operation.name AS Nullable(String)), '') = 'execute_tool')
        OR (coalesce(CAST(SpanAttributes.openinference.span.kind AS Nullable(String)), '') = 'TOOL') AS is_tool_call,

    notEmpty(_prompt_indices) AS has_prompts,
    notEmpty(_completion_indices) AS has_completions,

    ResourceAttributes     AS resource_attributes,
    ResourceAttributesKeys AS resource_attributes_keys,
    SpanAttributes         AS span_attributes,
    SpanAttributesKeys     AS span_attributes_keys,

    `Events.Timestamp`  AS `events.timestamp`,
    `Events.Name`       AS `events.name`,
    `Events.Attributes` AS `events.attributes`,

    `Links.TraceId`     AS `links.trace_id`,
    `Links.SpanId`      AS `links.span_id`,
    `Links.TraceState`  AS `links.trace_state`,
    `Links.Attributes`  AS `links.attributes`,

    -- =============================================================
    -- Prompts: one tuple per discovered position, content/role coalesced
    -- across formats.
    -- =============================================================
    arrayMap(
        idx -> CAST((
            -- message
            coalesce(
                nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'prompt', toString(idx), 'content'), ''),
                nullIf(JSONExtractString(_attrs_json, 'llm', 'input_messages', toString(idx), 'message', 'content'), ''),
                -- New gen_ai semconv: idx-th message = concatenated text of its `parts`
                -- (non-text parts have no `content` and contribute ''), with a
                -- message-level `content` fallback for emitters that skip `parts`.
                nullIf(
                    coalesce(
                        nullIf(
                            arrayStringConcat(
                                arrayMap(
                                    part -> JSONExtractString(part, 'content'),
                                    JSONExtractArrayRaw(_input_messages, toUInt64(idx + 1), 'parts')
                                ),
                                ''
                            ),
                            ''
                        ),
                        JSONExtractString(_input_messages, toUInt64(idx + 1), 'content')
                    ),
                    ''
                ),
                -- Strands: idx-th matching event's `content` attribute
                nullIf(
                    arrayMap(
                        i -> coalesce(CAST(`Events.Attributes`[i].content AS Nullable(String)), ''),
                        arrayFilter(i -> `Events.Name`[i] = 'gen_ai.user.message', arrayEnumerate(`Events.Name`))
                    )[idx + 1],
                    ''
                ),
                -- Snowflake native: single attr, idx=0 only
                if(idx = 0,
                    coalesce(
                        nullIf(CAST(SpanAttributes.ai.observability.record_root.input AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.query AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_search.query AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_analyst.messages AS Nullable(String)), '')
                    ),
                    NULL
                ),
                ''
            ),
            idx,
            -- role: defaults to 'user' for formats that don't carry an explicit role
            coalesce(
                nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'prompt', toString(idx), 'role'), ''),
                nullIf(JSONExtractString(_attrs_json, 'llm', 'input_messages', toString(idx), 'message', 'role'), ''),
                nullIf(JSONExtractString(_input_messages, toUInt64(idx + 1), 'role'), ''),
                'user'
            )
        ) AS Tuple(message String, position UInt16, role LowCardinality(String))),
        _prompt_indices
    ) AS prompts,

    -- =============================================================
    -- Completions: analogous to prompts.
    -- =============================================================
    arrayMap(
        idx -> CAST((
            coalesce(
                nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'completion', toString(idx), 'content'), ''),
                nullIf(JSONExtractString(_attrs_json, 'llm', 'output_messages', toString(idx), 'message', 'content'), ''),
                -- New gen_ai semconv: idx-th message's concatenated text parts
                nullIf(
                    coalesce(
                        nullIf(
                            arrayStringConcat(
                                arrayMap(
                                    part -> JSONExtractString(part, 'content'),
                                    JSONExtractArrayRaw(_output_messages, toUInt64(idx + 1), 'parts')
                                ),
                                ''
                            ),
                            ''
                        ),
                        JSONExtractString(_output_messages, toUInt64(idx + 1), 'content')
                    ),
                    ''
                ),
                nullIf(
                    arrayMap(
                        i -> coalesce(
                            nullIf(CAST(`Events.Attributes`[i].content AS Nullable(String)), ''),
                            nullIf(CAST(`Events.Attributes`[i].message AS Nullable(String)), ''),
                            ''
                        ),
                        arrayFilter(
                            i -> `Events.Name`[i] IN ('gen_ai.assistant.message', 'gen_ai.choice'),
                            arrayEnumerate(`Events.Name`)
                        )
                    )[idx + 1],
                    ''
                ),
                if(idx = 0,
                    coalesce(
                        nullIf(CAST(SpanAttributes.ai.observability.record_root.output AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.response AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.thinking_response AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_search.results AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_analyst.text AS Nullable(String)), '')
                    ),
                    NULL
                ),
                ''
            ),
            idx,
            coalesce(
                nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'completion', toString(idx), 'role'), ''),
                nullIf(JSONExtractString(_attrs_json, 'llm', 'output_messages', toString(idx), 'message', 'role'), ''),
                nullIf(JSONExtractString(_output_messages, toUInt64(idx + 1), 'role'), ''),
                'assistant'
            ),
            -- tool_calls for this completion message. Legacy formats flatten tool
            -- calls into indexed keys (gen_ai.completion.{idx}.tool_calls.{t}.* /
            -- OpenInference); the new gen_ai semconv carries them as `tool_call`
            -- parts of the message. Concatenate both sources — a span uses one
            -- format, so the other contributes an empty array.
            arrayConcat(
                arrayMap(
                    t_idx -> CAST((
                        -- id
                        coalesce(
                            nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'completion', toString(idx), 'tool_calls', toString(t_idx), 'id'), ''),
                            nullIf(JSONExtractString(_attrs_json, 'llm', 'output_messages', toString(idx), 'message', 'tool_calls', toString(t_idx), 'tool_call', 'id'), ''),
                            ''
                        ),
                        -- name
                        coalesce(
                            nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'completion', toString(idx), 'tool_calls', toString(t_idx), 'name'), ''),
                            nullIf(JSONExtractString(_attrs_json, 'llm', 'output_messages', toString(idx), 'message', 'tool_calls', toString(t_idx), 'tool_call', 'function', 'name'), ''),
                            ''
                        ),
                        -- arguments (already a JSON-encoded string at the source)
                        coalesce(
                            nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'completion', toString(idx), 'tool_calls', toString(t_idx), 'arguments'), ''),
                            nullIf(JSONExtractString(_attrs_json, 'llm', 'output_messages', toString(idx), 'message', 'tool_calls', toString(t_idx), 'tool_call', 'function', 'arguments'), ''),
                            ''
                        )
                    ) AS Tuple(id String, name LowCardinality(String), arguments String)),
                    arraySort(arrayDistinct(arrayConcat(
                        -- gen_ai: gen_ai.completion.{idx}.tool_calls.{t_idx}.name
                        arrayMap(
                            k -> toUInt16(extractAll(k, '\\.tool_calls\\.(\\d+)\\.')[1]),
                            arrayFilter(
                                k -> match(k, concat('^gen_ai\\.completion\\.', toString(idx), '\\.tool_calls\\.\\d+\\.name$')),
                                SpanAttributesKeys
                            )
                        ),
                        -- OpenInference: llm.output_messages.{idx}.message.tool_calls.{t_idx}.tool_call.id
                        arrayMap(
                            k -> toUInt16(extractAll(k, '\\.tool_calls\\.(\\d+)\\.')[1]),
                            arrayFilter(
                                k -> match(k, concat('^llm\\.output_messages\\.', toString(idx), '\\.message\\.tool_calls\\.\\d+\\.tool_call\\.id$')),
                                SpanAttributesKeys
                            )
                        )
                    )))
                ),
                -- New gen_ai semconv: `tool_call`-type parts of this message.
                -- `arguments` may be a JSON string or an object at the source, and
                -- we want a clean JSON-encoded string in both cases (matching the
                -- legacy shape): a string value decodes via JSONExtractString, while
                -- an object returns '' from JSONExtractString and falls through to the
                -- JSONExtractRaw fallback below, which serializes it.
                arrayMap(
                    part -> CAST((
                        JSONExtractString(part, 'id'),
                        JSONExtractString(part, 'name'),
                        coalesce(
                            nullIf(JSONExtractString(part, 'arguments'), ''),
                            nullIf(JSONExtractRaw(part, 'arguments'), ''),
                            ''
                        )
                    ) AS Tuple(id String, name LowCardinality(String), arguments String)),
                    arrayFilter(
                        part -> JSONExtractString(part, 'type') = 'tool_call',
                        JSONExtractArrayRaw(_output_messages, toUInt64(idx + 1), 'parts')
                    )
                )
            )
        ) AS Tuple(
            message String,
            position UInt16,
            role LowCardinality(String),
            tool_calls Array(Tuple(id String, name LowCardinality(String), arguments String))
        )),
        _completion_indices
    ) AS completions

FROM otel_traces.otel_traces;
