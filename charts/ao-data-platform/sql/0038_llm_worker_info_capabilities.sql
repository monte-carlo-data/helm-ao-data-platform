-- The llm-worker lists the request features it supports, so Monte Carlo offers only the
-- models this worker can call. An older worker writes no list and gets the empty default.
ALTER TABLE otel_traces.llm_worker_info ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS capabilities Array(LowCardinality(String)) DEFAULT [];
