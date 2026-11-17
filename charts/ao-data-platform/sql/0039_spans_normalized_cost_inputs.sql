-- The inputs a span's cost is priced from.
--
-- new_input_tokens counts the input tokens not served from the prompt cache.
-- Cost reads price input from it. prompt_tokens keeps the count each
-- instrumentation reports, which includes cache reads for some of them.
-- cache_read_input_tokens and cache_write_input_tokens are the prompt-cache
-- reads and writes, each priced at its own rate.
-- query_id is the warehouse query a SQL execution span ran. It is '' on
-- every other span.
--
-- The view that fills spans_normalized does not write these columns yet, so
-- they read NULL and '' until it does. Metadata-only: existing parts are not
-- rewritten.
ALTER TABLE otel_traces.spans_normalized ON CLUSTER '{cluster}'
    ADD COLUMN IF NOT EXISTS new_input_tokens Nullable(UInt32),
    ADD COLUMN IF NOT EXISTS cache_read_input_tokens Nullable(UInt32),
    ADD COLUMN IF NOT EXISTS cache_write_input_tokens Nullable(UInt32),
    ADD COLUMN IF NOT EXISTS query_id String DEFAULT '';
