-- Render gen_ai-semconv tool results in the message view.
--
-- A tool result fed back into the follow-up LLM turn arrives in
-- gen_ai.input.messages as a message whose single part is
-- {id, type:'tool_call_response', response:{...}} — the payload sits under
-- `response`, not `content`. The part-concatenation introduced in 0019 reads only
-- `content`, so such a part contributed '' and the whole message normalized to an
-- empty string: the tool result rendered BLANK in the conversation / prompt message
-- view.
--
-- In every emitter captured so far this was a rendering gap, not a data gap: the
-- result also lands on the execute_tool span's tool_call_output. That holds for both
-- Python google-adk turns AND the guide-following adk-go turn — all three carry
-- gcp.vertex.agent.tool_response there. Whether an emitter exists for which the part
-- is the ONLY copy is UNVERIFIED: tool_call_output coalesces just
-- traceloop.entity.output, gen_ai.tool.call.result and gcp.vertex.agent.tool_response,
-- so one setting none of those would lose the result outright — but no such capture
-- has been observed, so treat that as a mechanism to watch, not as history.
--
-- Fix: a `tool_call_response` part now contributes its serialized `response`.
--
-- The rule is FAITHFUL SERIALIZATION of whatever `response` holds, with one
-- exception: a JSON *string* payload is unwrapped so it renders as its text rather
-- than with surrounding quotes. Everything else is emitted as its raw JSON, so a
-- tool that legitimately returns `0`, `false`, `{}`, or `null` shows that value
-- instead of a misleading blank. An absent `response` contributes '' (blank).
--
-- The string case is detected from the RAW form starting with a quote, NOT from
-- `JSONExtractString(...) != ''`. That distinction matters: JSONExtractString
-- returns '' both for "this isn't a string" AND for the genuinely-empty string "",
-- so an emptiness sentinel would send `"response": ""` down the raw path and render
-- it as the literal two-character text `""` — inconsistent with a non-empty string,
-- which renders unquoted. Keyed on the raw form, "" correctly renders blank.
--
-- Keyed on the part `type`, NOT on the message `role`, because the wrapper role is
-- emitter-dependent: Python google-adk emits these as role 'user', ADK-for-Go as
-- role 'tool'. Applied to the completions side too, so the two parallel part-concat
-- blocks stay symmetric; only the input side has an observed emitter.
--
-- Both JSONExtract* variants are total (never raise), preserving this view's
-- headline invariant: a raising SELECT expression fails the source INSERT into
-- otel_traces and silently HALTS span ingestion cluster-wide.
--
-- The system-prompt concat (_system_prompt_text, further down this SELECT) is
-- deliberately left alone: gen_ai.system_instructions is a bare [{type, content}]
-- parts array that never carries a tool result.
--
-- SIZE: `response` is tool-controlled and copied verbatim, so prompts[].message is
-- unbounded here. It amplifies — gen_ai.input.messages on a follow-up turn replays
-- every prior tool result, so a conversation grows O(turns^2) in total prompt bytes —
-- and before this change every one of those parts contributed '', so the whole payload
-- class is new to this column. Left unbounded deliberately: observed tool-span I/O is
-- p95 ~13 KB / max ~23 KB against a 6 MB DC sync-invoke ceiling, so reaching it needs a
-- single response ~250x the observed max. That margin is the whole argument — overflow
-- is NOT handled gracefully downstream, so this is not safe-by-construction. The TRACE
-- export fetches spans in batches of 5 and halves on overflow (5->3->2->1), but a span
-- that still overflows at size 1 is DROPPED and the run aborts. The SPAN export
-- (agent_span_export_service) does a single fetch with no batching and FAILS outright,
-- and its MAX_SPAN_IDS = 1000 request is sized assuming bounded per-span content.
-- If a large-payload emitter
-- ever appears, bound it HERE rather than per-consumer — every downstream derivation
-- (first_prompt/last_prompt/full_prompt, the eval transform prompts, the export) reads
-- this one expression, and capping here keeps the lazy-chunk body_hash self-consistent.
-- The discriminating measurement is the length() distribution of
-- SpanAttributes['gen_ai.input.messages'] on a production cluster.
--
-- Also folds in an is_llm_call classification fix (same MODIFY QUERY rewrite, so it
-- rides here rather than in a second full-body copy of the view): the predicate
-- matched gen_ai.operation.name='chat' only, missing 'generate_content' (google-genai
-- / ADK) and 'text_completion'. Those LLM spans went unmarked whenever
-- gen_ai.request.model was empty -- adk-go sets model from the client's Name(), which
-- is blank behind an OpenAI-compatible gateway. is_tool_call already matched adk-go's
-- 'execute_tool', so this restores the LLM-side parity.
--
-- Supersedes the SELECT defined in 0019_spans_normalized_mv_genai_semconv.sql.
-- Shipped as a new migration (not an edit to 0019) to hold 1:1 numbering parity with
-- the monolith test fixture, where this file is 0013 and an in-place edit is not an
-- option: clickhouse-migrations md5-checksums applied files and raises on any change to
-- one already applied. On the helm side an edit to 0019 WOULD reach existing clusters --
-- schema-job.yaml runs every /sql/*.sql on install and upgrade with no ledger or
-- checksum, and 0019 is itself an ALTER ... MODIFY QUERY, so re-running it re-applies
-- the SELECT. Only 0006, the CREATE ... IF NOT EXISTS that owns this view, is a no-op on
-- an existing cluster. So parity with the monolith numbering is the binding constraint
-- here, not reachability on this side.
-- ALTER ... MODIFY QUERY swaps the SELECT in place with no view rebuild and no
-- ingestion gap; the output column set is unchanged, so 0007 (spans view) and 0012
-- (conversations_normalized_mv) are unaffected, and the DEFINER pinned by 0014 is
-- preserved (MODIFY QUERY changes only the SELECT).
--
-- Forward-only: MODIFY QUERY affects new inserts. There is no backfill, so
-- conversations already materialized keep rendering the tool result blank. Consumers
-- of the rendered shape invalidate their caches via SNAPSHOT_FORMAT_VERSION in the
-- monolith (service/agent_observability/conversation_thread_snapshot.py), bumped
-- alongside this — which is why that bump must land WITH this migration, not before
-- it: on its own it would invalidate every cached snapshot and force a full
-- re-hydration wave while the rendering is still unchanged in production.
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
