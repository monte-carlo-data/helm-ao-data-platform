-- Gate conversations_normalized admission on extracted content, not entity-key
-- presence. Mirror of the monolith test fixture's
-- 0017_conversations_normalized_mv_format_gate.sql (AO-1156); files correspond
-- to that fixture by basename, not ordinal (this chart carries migrations the
-- fixture lacks, so ordinals drift).
--
-- #15242 widened the Databricks SDK export's entity stamps: an SDK agent_turn
-- root now carries traceloop.entity.input = {"question": ...} and
-- traceloop.entity.output = a bare JSON string, plus a conversation_id via
-- montecarlo.association_properties.thread_id. 0012's gate admits a root on
-- entity-KEY presence, and the LangGraph-shaped extraction
-- (inputs.messages / outputs.messages) yields nothing from those payloads --
-- so every such root materializes a permanently content-free turn row
-- (user_input = '' AND agent_response = '').
--
-- Changes the admission gate only, expression by expression:
--   * The has(span_attributes_keys, 'traceloop.entity.input'/'.output')
--     disjunction stays, demoted from gate to ANDed prefilter: it narrows the
--     row set ahead of the JSON extraction (a best-effort skip, not a
--     guarantee) and admits nothing on its own.
--   * A new conjunct requires the extraction to produce content:
--     (_user_input != '' OR _agent_response != ''). The user_input and
--     agent_response extraction expressions are lifted into the WITH clause
--     as _user_input / _agent_response, referenced by both the SELECT list
--     and the gate, so the gate and the projected columns cannot drift.
-- 0012's partial-turn semantic is preserved: a root whose input extracts
-- non-empty stays admitted with an empty agent_response (an agent
-- non-response is a valid turn), and symmetrically for output-only roots.
--
-- All expressions are total (CAST to String / JSONExtract* degrade to '' on
-- missing or malformed JSON, never raise), so the MV's never-raise invariant
-- holds. Output column set and order unchanged -- the MV's SELECT matches
-- conversations_normalized (0011, columns extended by 0022) by output-alias
-- name, so aliases must not be renamed.
--
-- Under this chart's execution model sql/ is a DESIRED-STATE script set: the
-- schema Job re-runs every file on install AND upgrade. 0012 owns the
-- definition on a fresh install (CREATE ... IF NOT EXISTS); this file carries
-- the one trailing ALTER, so after any full run the MV's query is this
-- file's SELECT on every cluster, and no prior MODIFY QUERY of this view
-- exists to supersede. MODIFY QUERY preserves the DEFINER = schema_owner pin
-- 0014 sets on this MV (verified on ClickHouse 26.2).
--
-- Forward-only, and NO ROLLBACK PATH: MODIFY QUERY affects new inserts only,
-- and a chart rollback does not revert the gate -- 0012's CREATE is a no-op
-- on an existing cluster, so the gate persists until superseded. Rows
-- admitted before this migration stay; the read core's non-empty-side gate
-- in conversation_core.sql filters them.
--
-- One consequence for whoever enables a Traceloop-shaped service in
-- conversation_rollup_writer.enabled_services (empty in every shipped
-- config): a root this gate keeps out of the MV is no longer excluded by the
-- AO-1108 rollup writer's anti-join, so that writer emits it as a
-- content-free row the read core drops. Storage only; nothing reads it.
ALTER TABLE otel_traces.conversations_normalized_mv ON CLUSTER '{cluster}'
MODIFY QUERY
WITH
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
    ) AS _ai_msg,
    coalesce(
        nullIf(JSONExtractString(_human_msg, 'content'), ''),
        JSONExtractString(_human_msg, 'kwargs', 'content')
    ) AS _user_input,
    multiIf(
        JSONType(_ai_msg, 'kwargs', 'content') = 'Array',
        arrayStringConcat(
            arrayMap(b -> JSONExtractString(b, 'text'),
                     arrayFilter(b -> JSONExtractString(b, 'type') = 'text',
                                 JSONExtractArrayRaw(_ai_msg, 'kwargs', 'content'))),
            char(10)
        ),
        JSONExtractString(_ai_msg, 'kwargs', 'content')
    ) AS _agent_response
SELECT
    service_name,
    conversation_id,
    trace_id,
    start_time AS turn_start,
    _user_input AS user_input,
    _agent_response AS agent_response,
    span_attributes,
    span_attributes_keys,
    prompt_tokens,
    completion_tokens,
    status_code,
    workflow
FROM otel_traces.spans_normalized
WHERE parent_span_id = ''
  AND conversation_id != ''
  -- Prefilter, not the gate: narrows the row set ahead of the extraction and
  -- admits nothing on its own. Whether the extraction is actually skipped is
  -- best-effort (short-circuit evaluation), not a guarantee; the gate does
  -- not depend on it.
  AND (
    has(span_attributes_keys, 'traceloop.entity.input')
    OR has(span_attributes_keys, 'traceloop.entity.output')
  )
  -- Format gate (this file): a turn must EXTRACT non-empty content on at
  -- least one side. An empty user_input or agent_response on the other side
  -- is still a valid turn (e.g. an agent non-response). A root whose entity
  -- payloads extract nothing is not a conversation turn and is excluded.
  AND (_user_input != '' OR _agent_response != '');
