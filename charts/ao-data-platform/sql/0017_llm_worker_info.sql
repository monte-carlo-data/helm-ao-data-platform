-- Cloud-identification marker. The llm-worker publishes its (cloud, provider) on
-- startup so the monolith can resolve the deployment's cloud-native model pool
-- (aws→bedrock, gcp→vertex, azure→foundry) instead of assuming Bedrock.
-- ReplicatedReplacingMergeTree(updated_at) keeps a single current row per
-- (cloud, provider); the monolith reads the latest via FINAL / argMax(updated_at).
-- No TTL — this is current-state metadata, not time-series. Idempotent for
-- schema-job re-runs. Clustered/HA: ON CLUSTER + path-less Replicated engine
-- (default_replica_path owns the layout), matching the other otel_traces tables.
CREATE TABLE IF NOT EXISTS otel_traces.llm_worker_info ON CLUSTER '{cluster}'
(
    cloud       LowCardinality(String),
    provider    LowCardinality(String),
    updated_at  DateTime DEFAULT now()
)
ENGINE = ReplicatedReplacingMergeTree(updated_at)
ORDER BY (cloud, provider)
