-- ============================================================================
-- ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1
-- READ-ONLY constraint + index inventory (schema-qualified, fixed filter)
-- SAFETY: SELECT / \copy (SELECT ...) TO only.
-- ============================================================================

\set QUIET on
\pset pager off

\copy (SELECT c.conrelid::regclass::text AS table_name, c.conname, pg_get_constraintdef(c.oid) AS constraint_def FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid WHERE t.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'research') AND t.relname IN ('prospective_observation','me_reverse_long_v3_execution_outcome','prospective_experiment') ORDER BY t.relname, c.conname) TO '/tmp/me_v3_eval_20261022/12_constraints.csv' WITH CSV HEADER

\copy (SELECT t.relname AS table_name, i.relname AS index_name, ix.indisunique AS is_unique, pg_get_indexdef(ix.indexrelid) AS index_def FROM pg_index ix JOIN pg_class t ON t.oid = ix.indrelid JOIN pg_class i ON i.oid = ix.indexrelid WHERE t.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'research') AND t.relname IN ('prospective_observation','me_reverse_long_v3_execution_outcome','prospective_experiment') ORDER BY t.relname, i.relname) TO '/tmp/me_v3_eval_20261022/13_indexes.csv' WITH CSV HEADER
