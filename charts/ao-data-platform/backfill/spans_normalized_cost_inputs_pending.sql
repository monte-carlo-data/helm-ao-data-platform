-- Generated upstream. Do not edit.
-- Fills the span cost inputs on rows written before the view filled them.
-- Run once per day partition: clickhouse-client --param_day=YYYY-MM-DD
SELECT count()
FROM otel_traces.spans_normalized
WHERE toDate(start_time) = {day:String}
    AND new_input_tokens IS NULL
    AND cache_read_input_tokens IS NULL
    AND cache_write_input_tokens IS NULL
    AND query_id = ''
    AND (prompt_tokens IS NOT NULL OR hasAny(span_attributes_keys, ['montecarlo.cost.new_input_tokens', 'montecarlo.cost.cache_read_input_tokens', 'montecarlo.cost.cache_write_input_tokens', 'montecarlo.cost.query_id', 'gen_ai.usage.cache_read_input_tokens', 'gen_ai.usage.cache_read.input_tokens', 'gen_ai.usage.cache_creation_input_tokens', 'gen_ai.usage.cache_creation.input_tokens', 'gen_ai.usage.cache_write_input_tokens']))
    AND (coalesce(
        accurateCastOrNull(span_attributes.montecarlo.cost.new_input_tokens, 'UInt32'),
        CAST(multiIf(
            prompt_tokens IS NULL,
            NULL,
            prompt_tokens + completion_tokens + coalesce(coalesce(accurateCastOrNull(span_attributes.montecarlo.cost.cache_read_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_read_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_read.input_tokens, 'UInt32')), 0) + coalesce(coalesce(accurateCastOrNull(span_attributes.montecarlo.cost.cache_write_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_creation_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_creation.input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_write_input_tokens, 'UInt32')), 0) = total_tokens,
            toInt64(prompt_tokens),
            greatest(toInt64(prompt_tokens) - toInt64(coalesce(coalesce(accurateCastOrNull(span_attributes.montecarlo.cost.cache_read_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_read_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_read.input_tokens, 'UInt32')), 0) + coalesce(coalesce(accurateCastOrNull(span_attributes.montecarlo.cost.cache_write_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_creation_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_creation.input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_write_input_tokens, 'UInt32')), 0)), 0)
        ) AS Nullable(UInt32))
    ) IS NOT NULL
        OR coalesce(accurateCastOrNull(span_attributes.montecarlo.cost.cache_read_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_read_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_read.input_tokens, 'UInt32')) IS NOT NULL
        OR coalesce(accurateCastOrNull(span_attributes.montecarlo.cost.cache_write_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_creation_input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_creation.input_tokens, 'UInt32'), accurateCastOrNull(span_attributes.gen_ai.usage.cache_write_input_tokens, 'UInt32')) IS NOT NULL
        OR coalesce(CAST(span_attributes.montecarlo.cost.query_id AS Nullable(String)), '') != '')
