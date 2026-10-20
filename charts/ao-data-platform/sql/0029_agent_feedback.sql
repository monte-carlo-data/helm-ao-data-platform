-- End-user feedback on agent responses (thumbs up/down, whether a comment was left,
-- and the categories the user picked), written by Monte Carlo (monte_carlo user)
-- for agents whose platform stores feedback outside the trace spans, such as
-- Snowflake Cortex agents. Comment text and the rating user are not stored here.
--
-- Feedback names the rated turn by request_id, not by trace. agent_turn_links maps
-- each request_id to its trace, and reads join the two tables on
-- (service_name, agent_mcon, request_id). A feedback row and its link can land in
-- either order and in different writes.
--
-- service_name matches spans_normalized. agent_mcon tells apart agents that share
-- a service_name. Writes may repeat a row; reads keep the newest submitted_at per
-- trace with LIMIT 1 BY, as for the eval score tables (0013, 0018).
-- The 30-day TTL matches the spans; the schema Job's ttlDays ALTER is the source
-- of truth on upgrades.
CREATE TABLE IF NOT EXISTS otel_traces.agent_feedback ON CLUSTER '{cluster}'
(
    `service_name` LowCardinality(String),
    `agent_mcon` String,
    `request_id` String,
    -- NULL when the feedback carries no usable rating.
    `positive` Nullable(Bool),
    `has_comment` Bool,
    `categories` Array(String) DEFAULT [],
    `submitted_at` DateTime64(6),
    `collected_at` DateTime64(6)
)
ENGINE = ReplicatedMergeTree
PARTITION BY toYYYYMM(submitted_at)
ORDER BY (service_name, agent_mcon, request_id, submitted_at)
TTL submitted_at + INTERVAL 30 DAY DELETE;

-- trace_id uses the same encoding as spans_normalized.trace_id.
CREATE TABLE IF NOT EXISTS otel_traces.agent_turn_links ON CLUSTER '{cluster}'
(
    `service_name` LowCardinality(String),
    `agent_mcon` String,
    `request_id` String,
    `trace_id` String,
    `turn_start_time` DateTime64(6)
)
ENGINE = ReplicatedMergeTree
PARTITION BY toYYYYMM(turn_start_time)
ORDER BY (service_name, agent_mcon, request_id, turn_start_time)
TTL turn_start_time + INTERVAL 30 DAY DELETE;
