-- Reparent the materialized-view DEFINERs onto schema_owner.
--
-- On ClickHouse 26.4 a materialized view created without an explicit clause defaults to
-- SQL SECURITY DEFINER with definer = the creating user (default_materialized_view_sql_security =
-- DEFINER, default_view_definer = CURRENT_USER). Historically the schema Job ran as `otel`, so on
-- already-installed clusters these MVs carry an implicit DEFINER = otel. Once `otel` is tightened to
-- INSERT-only on the raw source table, an otel-definer MV can no longer write its normalized target
-- and the cascade breaks. Pinning the DEFINER to `schema_owner` (which retains SELECT on the sources
-- and INSERT on the targets) keeps the normalization pipeline writing regardless of `otel`'s grants.
--
-- ALTER ... MODIFY SQL SECURITY is an in-place metadata change — no view rebuild, no normalization
-- gap (unlike CREATE OR REPLACE). On a fresh install the MVs are already created by schema_owner, so
-- these statements are a harmless reassertion. This is a migration file (not an edit to 0004/0006/
-- 0012) because the schema Job re-runs every *.sql on each upgrade and the create files are
-- CREATE ... IF NOT EXISTS (no-ops on existing installs).
ALTER TABLE otel_traces.otel_traces_trace_id_ts_mv MODIFY SQL SECURITY DEFINER DEFINER = schema_owner;
ALTER TABLE otel_traces.spans_normalized_mv        MODIFY SQL SECURITY DEFINER DEFINER = schema_owner;
ALTER TABLE otel_traces.conversations_normalized_mv MODIFY SQL SECURITY DEFINER DEFINER = schema_owner;
