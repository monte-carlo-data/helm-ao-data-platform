-- Additive: version the monolith↔worker input contract on llm_inputs.
-- 0 = legacy/v0 (Bedrock-shaped), 1 = MC eval contract v1. The worker dispatches
-- on a payload-shape sniff, NOT on this column, so the column is not a migration
-- gate — it exists for future versioning + observability. Idempotent (ADD COLUMN
-- IF NOT EXISTS) so the schema job can re-run it on every upgrade.
ALTER TABLE otel_traces.llm_inputs ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS contract_version UInt8 DEFAULT 0 AFTER tool_config
