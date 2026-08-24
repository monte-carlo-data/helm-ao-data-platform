-- Extract Cortex planning tool-use output as the completion.
--
-- A Cortex ReasoningAgentStepPlanning span that goes straight to tool calls
-- carries no free-form output: planning.response / planning.thinking_response
-- are absent, and the step's output lives in planning.tool_execution.results /
-- planning.tool_selection.name. The Snowflake-side read query that serves
-- this data renders those as the completion via its deeper COALESCE
-- fallbacks. The MV did not extract them, so such spans projected empty
-- completions on the ClickHouse surface -- content loss vs the read path,
-- caught by a golden-trace parity check between the read path and this view.
--
-- Changes the completion extraction (idx=0 Snowflake-native arm) to a
-- seven-arm COALESCE:
--   record_root.output -> planning.thinking_response -> planning.response
--   -> planning.tool_execution.results -> planning.tool_selection.name
--   -> tool.cortex_search.results -> tool.cortex_analyst.text
-- and the matching _completion_indices discovery arm to check the same
-- seven attributes for non-emptiness (an OR, so its internal order does not
-- affect behavior).
--
-- The first five arms -- the LLM namespace -- mirror that read query's
-- completion COALESCE in the same order. This also swaps thinking_response
-- ahead of response (0006..0020 had them reversed): the read query
-- deliberately prefers the extended-thinking chain-of-thought over the
-- formal output when both are present.
--
-- The last two arms (agent.tool.cortex_search.results,
-- agent.tool.cortex_analyst.text) are an MV-only fold of the tool
-- namespace, deliberately absent from the read query: spans_normalized
-- has no tool_call_input/tool_call_output columns to hold tool content
-- separately, so the MV folds it into the completion instead of
-- dropping it. Do not remove these two arms to "sync" with the read
-- query -- doing so silently drops all Cortex tool-span content from the
-- ClickHouse surface.
--
-- A third registered divergence: empty-string handling. The MV nullIf-wraps
-- every arm, so '' counts as absent and falls through to the next arm; the
-- read query's bare ::STRING casts inside COALESCE let '' win and render
-- an empty completion (e.g. record_root.output = '' alongside a populated
-- thinking_response). The MV side is authoritative: treating '' as absent is
-- intended, and the read query's bare casts are the latent bug (predates
-- this migration; the wrapper style comes from 0020). Do not strip the
-- nullIf wrappers to "sync" with the read query.
--
-- Keep the LLM-namespace prefix in lockstep with that read query's
-- completion COALESCE; do not remove the tool arms or the nullIf wrappers.
--
-- SIZE: tool_execution.results is tool-controlled and copied verbatim, so
-- completions[].message is unbounded here too, feeding
-- first_completion/last_completion/full_completion. Two pieces of 0020's SIZE
-- note carry over unchanged: cap it HERE, not per-consumer, and the
-- downstream-overflow behavior (consumers of this column do not degrade
-- gracefully: an oversized span can be dropped outright or fail its whole
-- export). Two pieces do NOT carry over: gen_ai.input.messages replays every
-- prior tool result on each follow-up turn, so its O(turns^2) growth has no
-- equivalent here -- planning.tool_execution.results is a single per-span
-- value with no replay -- and 0020's observed p95/max magnitudes were
-- measured on gen_ai tool-span I/O, not on this attribute. The
-- discriminating measurement, unmeasured so far, is the length()
-- distribution of
-- CAST(SpanAttributes.snow.ai.observability.agent.planning.tool_execution.results
-- AS Nullable(String)) on a production cluster (SpanAttributes is a JSON
-- column: dotted path access, not map brackets). tool_selection.name is a
-- short JSON array of tool names.
--
-- All added expressions are total (CAST Nullable / nullIf / coalesce), so the
-- MV's never-raise invariant holds (a raising SELECT expression fails the
-- source INSERT into otel_traces and silently HALTS span ingestion
-- cluster-wide). Output column set unchanged, so the 0007 spans view and 0012
-- conversations_normalized_mv are unaffected, and the DEFINER pinned by 0014
-- is preserved (MODIFY QUERY changes only the SELECT).
--
-- Supersedes the SELECT defined in
-- 0020_spans_normalized_mv_tool_call_response.sql, and that file's ALTER has
-- been REMOVED rather than left to re-run. schema-job.yaml applies every
-- /sql/*.sql on install and upgrade with no ledger or checksum, so a
-- superseded ALTER ... MODIFY QUERY is not merely redundant: re-running it
-- restores the older SELECT until this file re-applies, and the view is
-- insert-triggered and forward-only, so a Cortex planning tool-use span
-- ingested in that window keeps an empty completion for good. Measured on a
-- two-replica cluster when 0020 superseded 0019: the view lost, then
-- regained, the newer handling on every upgrade pass. Hence the invariant
-- for this directory -- exactly one file carries the current definition of a
-- view: 0006 (CREATE ... IF NOT EXISTS, which owns it on a fresh install)
-- plus one trailing ALTER for clusters where that CREATE is a no-op. Adding
-- a 0022 that rewrites this SELECT means emptying THIS file's statement the
-- same way.
--
-- Shipped as a new file rather than an edit to 0020: a superseded ordinal is
-- never reused, so the chart's history stays readable and numbering stays
-- aligned with the source schema set this directory mirrors, which is
-- append-only by construction. 0020 still exists and holds its ordinal; only
-- its superseded statement is gone.
--
-- Forward-only: MODIFY QUERY affects new inserts. Rows materialized before
-- this migration deploys keep empty completions for these spans.
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
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.tool_execution.results AS Nullable(String)), '') != ''
            OR coalesce(CAST(SpanAttributes.snow.ai.observability.agent.planning.tool_selection.name AS Nullable(String)), '') != ''
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
                        -- the formal response; the tool-use fallbacks cover planning
                        -- steps that emit only the structured tool call.
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.thinking_response AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.response AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.tool_execution.results AS Nullable(String)), ''),
                        nullIf(CAST(SpanAttributes.snow.ai.observability.agent.planning.tool_selection.name AS Nullable(String)), ''),
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
