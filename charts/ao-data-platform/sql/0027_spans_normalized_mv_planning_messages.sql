-- Map Cortex reasoning-step message history into prompts.
--
-- Supersedes 0026. ReasoningAgentStepPlanning / ResponseGeneration spans
-- carry snow.ai.observability.agent.planning.messages: a JSON-array string of
-- "User: ..." / "Assistant: ..." elements holding the conversation the step
-- sent to the model. Each element becomes one prompt, with the prefix moved
-- into role. Spans without a parseable list keep the single planning.query
-- prompt.
--
-- Keep the prompt, text and tool_calls arms in lockstep with the read
-- template (monolith agent_observability_queries.yaml, ``native``). Two
-- differences are deliberate; do not sync them away:
--   * The cortex_search/cortex_analyst arms fold tool-span content into the
--     completion, because spans_normalized has no tool I/O columns. Removing
--     them drops all Cortex tool-span content from ClickHouse.
--   * The nullIf wrappers treat '' as absent. The read template's bare casts
--     let '' win, which is a latent bug on that side.
--
-- All added expressions are total, so the MV never raises. Output columns
-- are unchanged.
--
-- DEPLOYMENT: this is the authoritative copy of this view definition,
-- mirrored in monolith 0020_spans_normalized_mv_planning_messages.sql
-- (test fixture). Supersedes 0026_spans_normalized_mv_planning_tool_calls.sql,
-- whose ALTER ... MODIFY QUERY is emptied by this change (schema-job.yaml
-- re-applies every /sql/*.sql with no ledger, so a superseded ALTER left in
-- place would regress the view on every upgrade).
--
-- Forward-only: MODIFY QUERY affects new inserts only.

ALTER TABLE otel_traces.spans_normalized_mv ON CLUSTER '{cluster}'
MODIFY QUERY
WITH
    -- Stringified SpanAttributes for dynamic-path JSONExtractString calls.
    toString(SpanAttributes) AS _attrs_json,

    -- New gen_ai semconv message arrays (JSON-array strings; '' when absent).
    JSONExtractString(_attrs_json, 'gen_ai', 'input', 'messages')  AS _input_messages,
    JSONExtractString(_attrs_json, 'gen_ai', 'output', 'messages') AS _output_messages,

    -- New gen_ai semconv system prompt. Anthropic, LangChain, and google-genai/ADK
    -- instrumentors split the system prompt out of input.messages into a separate
    -- top-level attribute, gen_ai.system_instructions: a bare parts array
    -- ([{type, content}], no role wrapper). Concatenate its parts' text into one
    -- system-role message (prepended to prompts below) so these agents surface their
    -- system prompt in the same section legacy (gen_ai.prompt.{N}.role=system) agents
    -- already populate. '' when absent, keeping non-emitters byte-identical.
    JSONExtractString(_attrs_json, 'gen_ai', 'system_instructions') AS _system_instructions,
    arrayStringConcat(
        arrayMap(part -> JSONExtractString(part, 'content'), JSONExtractArrayRaw(_system_instructions)),
        ''
    ) AS _system_prompt_text,

    -- Cortex reasoning steps: the message list the step sent to the model, a
    -- JSON-array string of "User: ..." / "Assistant: ..." elements. Empty when
    -- absent or unparseable, so prompts fall back to planning.query.
    JSONExtract(
        coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.messages AS Nullable(String)), ''),
        'Array(String)'
    ) AS _planning_messages,

    -- Snowflake planning step's own tool calls: four parallel JSON arrays, one
    -- entry per call. argument.name[i] is a comma-joined name list and
    -- argument.value[i] the matching comma-joined JSON values.
    coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.tool_selection.name AS Nullable(String)), '') AS _planning_call_names,
    coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.tool_selection.id AS Nullable(String)), '') AS _planning_call_ids,
    coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.tool_selection.argument.name AS Nullable(String)), '') AS _planning_arg_names,
    coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.tool_selection.argument.value AS Nullable(String)), '') AS _planning_arg_values,

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
        -- Cortex reasoning steps: one position per planning.messages element
        arrayMap(i -> toUInt16(i - 1), arrayEnumerate(_planning_messages)),
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
            OR JSONLength(_planning_call_names) > 0
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_search.results AS Nullable(String)), '') != ''
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.tool.cortex_analyst.text AS Nullable(String)), '') != '',
            [toUInt16(0)],
            CAST([] AS Array(UInt16))
        )
    ))) AS _completion_indices

SELECT
    coalesce(
        nullIf(CAST(SpanAttributes.montecarlo.agent_name AS Nullable(String)), ''),
        ServiceName
    ) AS service_name,
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
        CAST(SpanAttributes.llm.token_count.total AS Nullable(UInt32)),
        -- New-semconv emitters (e.g. Google ADK) report input/output but no
        -- total; derive it from the resolved prompt/completion columns so total
        -- renders instead of NULL. A Nullable sum yields NULL unless BOTH sides
        -- are present, and any explicit total above still wins.
        prompt_tokens + completion_tokens
    ) AS total_tokens,

    (coalesce(CAST(SpanAttributes.gen_ai.request.model AS Nullable(String)), '') != '')
        OR (coalesce(CAST(SpanAttributes.llm.model_name AS Nullable(String)), '') != '')
        -- gen_ai.operation.name identifies an LLM span even when request.model is
        -- empty (adk-go / google-genai set model from the client's Name(), blank
        -- behind an OpenAI-compatible gateway). Match the semconv TEXT-GENERATION ops,
        -- not just 'chat' -- 'generate_content' (google-genai/ADK) and
        -- 'text_completion' are equally LLM calls; without them such spans go
        -- unmarked when model is empty.
        --
        -- Deliberately NOT every GenAI op: 'embeddings' is excluded. is_llm_call feeds
        -- count_llm_calls (a trace sort field and a breach-event field), so admitting
        -- embedding spans would move that metric for existing monitors -- a metrics
        -- decision, not a rendering one. Note the FIRST arm above already admits any
        -- span carrying gen_ai.request.model, so an embeddings span that sets a model
        -- still classifies as an LLM call; the exclusion only governs the
        -- classify-from-operation-name path.
        OR (coalesce(CAST(SpanAttributes.gen_ai.operation.name AS Nullable(String)), '') IN ('chat', 'generate_content', 'text_completion'))
        OR (coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.model AS Nullable(String)), '') != '') AS is_llm_call,

    (coalesce(CAST(SpanAttributes.traceloop.span.kind AS Nullable(String)), '') = 'tool')
        OR (coalesce(CAST(SpanAttributes.gen_ai.operation.name AS Nullable(String)), '') = 'execute_tool')
        OR (coalesce(CAST(SpanAttributes.openinference.span.kind AS Nullable(String)), '') = 'TOOL') AS is_tool_call,

    -- system_instructions is a prompt too (it prepends a system message below), so
    -- keep the has_prompts <=> notEmpty(prompts) invariant the list/detail gates rely on.
    notEmpty(_prompt_indices) OR (_system_prompt_text != '') AS has_prompts,
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
    -- across formats. The new gen_ai semconv system prompt
    -- (gen_ai.system_instructions) is prepended as a leading system-role message,
    -- matching legacy agents that carry system as gen_ai.prompt.0.role=system.
    -- Empty array when absent, so non-emitters stay byte-identical.
    -- =============================================================
    arrayConcat(
        if(_system_prompt_text != '',
            [CAST(
                (_system_prompt_text, toUInt16(0), 'system')
                AS Tuple(message String, position UInt16, role LowCardinality(String))
            )],
            CAST([] AS Array(Tuple(message String, position UInt16, role LowCardinality(String))))
        ),
    arrayMap(
        idx -> CAST((
            -- message
            coalesce(
                nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'prompt', toString(idx), 'content'), ''),
                nullIf(JSONExtractString(_attrs_json, 'llm', 'input_messages', toString(idx), 'message', 'content'), ''),
                -- New gen_ai semconv: idx-th message = concatenated text of its `parts`,
                -- where a `tool_call_response` part contributes its serialized `response`
                -- payload (tool results carry no `content`) and any other non-text part
                -- contributes ''. Message-level `content` fallback for emitters that
                -- skip `parts`.
                nullIf(
                    coalesce(
                        nullIf(
                            arrayStringConcat(
                                arrayMap(
                                    part -> if(
                                        JSONExtractString(part, 'type') = 'tool_call_response',
                                        if(
                                            startsWith(JSONExtractRaw(part, 'response'), '"'),
                                            JSONExtractString(part, 'response'),
                                            JSONExtractRaw(part, 'response')
                                        ),
                                        JSONExtractString(part, 'content')
                                    ),
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
                -- Cortex reasoning steps: idx-th planning.messages element with its
                -- "User: " / "Assistant: " prefix moved into role
                nullIf(
                    multiIf(
                        startsWith(_planning_messages[idx + 1], 'User: '), substring(_planning_messages[idx + 1], 7),
                        startsWith(_planning_messages[idx + 1], 'Assistant: '), substring(_planning_messages[idx + 1], 12),
                        _planning_messages[idx + 1]
                    ),
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
                if(startsWith(_planning_messages[idx + 1], 'Assistant: '), 'assistant', NULL),
                'user'
            )
        ) AS Tuple(message String, position UInt16, role LowCardinality(String))),
        _prompt_indices
    )
    ) AS prompts,

    -- =============================================================
    -- Completions: analogous to prompts.
    -- =============================================================
    arrayMap(
        idx -> CAST((
            coalesce(
                nullIf(JSONExtractString(_attrs_json, 'gen_ai', 'completion', toString(idx), 'content'), ''),
                nullIf(JSONExtractString(_attrs_json, 'llm', 'output_messages', toString(idx), 'message', 'content'), ''),
                -- New gen_ai semconv: idx-th message's concatenated text parts, with the
                -- same `tool_call_response` handling as the prompts side above (kept
                -- symmetric; no emitter has been observed putting a tool result here).
                nullIf(
                    coalesce(
                        nullIf(
                            arrayStringConcat(
                                arrayMap(
                                    part -> if(
                                        JSONExtractString(part, 'type') = 'tool_call_response',
                                        if(
                                            startsWith(JSONExtractRaw(part, 'response'), '"'),
                                            JSONExtractString(part, 'response'),
                                            JSONExtractRaw(part, 'response')
                                        ),
                                        JSONExtractString(part, 'content')
                                    ),
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
                        -- Read-template order: the extended-thinking CoT wins over
                        -- the formal response.
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.thinking_response AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.response AS Nullable(String)), ''),
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
                ),
                -- Snowflake planning step, idx=0 only. arguments zips the i-th name
                -- list with the i-th value list into a JSON object when the counts
                -- match and the names are distinct; otherwise it keeps the raw
                -- value text. A call with no argument names gets an empty object.
                if(idx = 0,
                    arrayMap(
                        i -> CAST((
                            JSONExtractString(_planning_call_ids, i),
                            JSONExtractString(_planning_call_names, i),
                            multiIf(
                                JSONExtractString(_planning_arg_names, i) = '',
                                '{}',
                                isValidJSON(concat('[', JSONExtractString(_planning_arg_values, i), ']'))
                                    AND JSONLength(concat('[', JSONExtractString(_planning_arg_values, i), ']'))
                                        = length(splitByChar(',', JSONExtractString(_planning_arg_names, i)))
                                    AND length(arrayDistinct(splitByChar(',', JSONExtractString(_planning_arg_names, i))))
                                        = length(splitByChar(',', JSONExtractString(_planning_arg_names, i))),
                                concat('{', arrayStringConcat(
                                    arrayMap(
                                        (arg_name, j) -> concat(
                                            toJSONString(arg_name),
                                            ':',
                                            JSONExtractRaw(concat('[', JSONExtractString(_planning_arg_values, i), ']'), j)
                                        ),
                                        splitByChar(',', JSONExtractString(_planning_arg_names, i)),
                                        arrayEnumerate(splitByChar(',', JSONExtractString(_planning_arg_names, i)))
                                    ),
                                    ','
                                ), '}'),
                                JSONExtractString(_planning_arg_values, i)
                            )
                        ) AS Tuple(id String, name LowCardinality(String), arguments String)),
                        range(1, toUInt64(JSONLength(_planning_call_names)) + 1)
                    ),
                    CAST([] AS Array(Tuple(id String, name LowCardinality(String), arguments String)))
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
