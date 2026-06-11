CREATE MATERIALIZED VIEW IF NOT EXISTS otel_traces.spans_normalized_mv
TO otel_traces.spans_normalized
AS WITH
    -- Stringified SpanAttributes for dynamic-path JSONExtractString calls.
    -- The JSON-type version requires constant path args, so we serialize
    -- once per row and let the String version handle the dynamic index.
    -- V1 relied on Map(String,String) lookups for the same purpose.
    toString(SpanAttributes) AS _attrs_json,

    -- =============================================================
    -- Prompt index discovery — union of all formats, sorted/deduped.
    -- Each format contributes a list of UInt16 positions; arrayConcat
    -- + arrayDistinct + arraySort produces the per-span ordered set.
    -- =============================================================
    arraySort(arrayDistinct(arrayConcat(
        -- Standard gen_ai semconv: gen_ai.prompt.{N}.role
        arrayMap(
            k -> toUInt16(extractAll(k, '\\.(\\d+)\\.')[1]),
            arrayFilter(k -> match(k, '^gen_ai\\.prompt\\.\\d+\\.role$'), SpanAttributesKeys)
        ),
        -- OpenInference: llm.input_messages.{N}.message.role
        arrayMap(
            k -> toUInt16(extractAll(k, '\\.(\\d+)\\.')[1]),
            arrayFilter(k -> match(k, '^llm\\.input_messages\\.\\d+\\.message\\.role$'), SpanAttributesKeys)
        ),
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

    coalesce(
        nullIf(CAST(SpanAttributes.montecarlo.association_properties.thread_id AS Nullable(String)), ''),
        nullIf(CAST(SpanAttributes.session.id AS Nullable(String)), ''),
        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.thread_id AS Nullable(String)), ''),
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
    -- across formats. JSONExtractString takes path components as strings,
    -- which sidesteps the numeric-path-segment issue (`gen_ai.prompt.0`)
    -- and lets the position index be dynamic.
    -- =============================================================
    arrayMap(
        idx -> CAST((
            -- message
            coalesce(
                nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'prompt', toString(idx), 'content'), ''),
                nullIf(JSONExtractString(_attrs_json, 'llm', 'input_messages', toString(idx), 'message', 'content'), ''),
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
                'user'
            )
        ) AS Tuple(message String, position UInt16, role LowCardinality(String))),
        _prompt_indices
    ) AS prompts,

    -- =============================================================
    -- Completions: analogous to prompts. Strands has two event names
    -- (`gen_ai.assistant.message` carries `content`; `gen_ai.choice`
    -- carries `message`) — coalesce both at extraction time.
    -- =============================================================
    arrayMap(
        idx -> CAST((
            coalesce(
                nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'completion', toString(idx), 'content'), ''),
                nullIf(JSONExtractString(_attrs_json, 'llm', 'output_messages', toString(idx), 'message', 'content'), ''),
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
                'assistant'
            ),
            -- tool_calls for this completion message — discovers per-message
            -- tool_call indices across formats, then builds (id, name, args)
            -- tuples. gen_ai uses {id,name,arguments}; OpenInference adds an
            -- extra `tool_call` indirection and groups name/args under
            -- `function`.
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
