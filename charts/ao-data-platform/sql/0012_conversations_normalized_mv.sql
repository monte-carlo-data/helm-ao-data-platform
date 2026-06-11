-- Per-root-span projection: one root span (parent_span_id = '') = one turn = one
-- row. Reads FROM spans_normalized, reusing its already-parsed span_attributes,
-- conversation_id, and root-span detection. The projection is purely per-row
-- (stateless), so it works as a streaming MV — turn ordering is reconstructed at
-- read time, not here.
--
-- This is the first two-level MV cascade in the schema
-- (otel_traces -> spans_normalized -> conversations_normalized). It depends on
-- spans_normalized's conversation_id / service_name / token / span_attributes
-- columns; editing 0006's projection (e.g. reordering or renaming those) can
-- silently break this consumer. The end-to-end tests are the guardrail.
--
-- LangGraph/Traceloop only (v1). The root *.workflow span carries
-- traceloop.entity.input (the new user message) and traceloop.entity.output (the
-- final, deduped state.messages for the turn). Extraction matches the validated
-- root-span boundary read: input messages are flat dicts; output messages are
-- LangChain constructor-wrapped (kwargs.*). Agent content may be a plain string
-- or a block array ([{type:text, text:...}]).
--
-- SELECT column order matches conversations_normalized (0008) — MV-to-table
-- inserts position-wise, not by name.
CREATE MATERIALIZED VIEW IF NOT EXISTS otel_traces.conversations_normalized_mv
TO otel_traces.conversations_normalized
AS WITH
    CAST(span_attributes.traceloop.entity.input  AS String) AS _in_j,
    CAST(span_attributes.traceloop.entity.output AS String) AS _out_j,
    JSONExtractArrayRaw(_in_j,  'inputs',  'messages')  AS _in_msgs,
    JSONExtractArrayRaw(_out_j, 'outputs', 'messages')  AS _out_msgs,
    arrayElement(
        arrayFilter(m -> JSONExtractString(m, 'type') = 'human'
                      OR JSONExtractString(m, 'kwargs', 'type') = 'human', _in_msgs),
        -1
    ) AS _human_msg,
    arrayElement(
        arrayFilter(m -> JSONExtractString(m, 'kwargs', 'type') = 'ai'
                      OR JSONExtractString(m, 'type') = 'ai', _out_msgs),
        -1
    ) AS _ai_msg
SELECT
    service_name,
    conversation_id,
    trace_id,
    start_time AS turn_start,
    coalesce(
        nullIf(JSONExtractString(_human_msg, 'content'), ''),
        JSONExtractString(_human_msg, 'kwargs', 'content')
    ) AS user_input,
    multiIf(
        JSONType(_ai_msg, 'kwargs', 'content') = 'Array',
        arrayStringConcat(
            arrayMap(b -> JSONExtractString(b, 'text'),
                     arrayFilter(b -> JSONExtractString(b, 'type') = 'text',
                                 JSONExtractArrayRaw(_ai_msg, 'kwargs', 'content'))),
            char(10)
        ),
        JSONExtractString(_ai_msg, 'kwargs', 'content')
    ) AS agent_response,
    span_attributes,
    span_attributes_keys,
    prompt_tokens,
    completion_tokens,
    status_code,
    workflow
FROM otel_traces.spans_normalized
WHERE parent_span_id = ''
  AND conversation_id != ''
  -- Traceloop format gate: a turn must carry at least one of the entity.*
  -- payloads. An empty user_input or agent_response is a valid turn (e.g. an
  -- agent non-response), so we require either key rather than both — extraction
  -- degrades to '' for whichever side is absent. A root span with neither key
  -- is not a conversation turn and is excluded.
  AND (
    has(span_attributes_keys, 'traceloop.entity.input')
    OR has(span_attributes_keys, 'traceloop.entity.output')
  );
