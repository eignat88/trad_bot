-- ============================================================================
-- ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1
-- READ-ONLY supplemental extraction (experiment-scoped generic tables)
-- SAFETY: SELECT / \copy (SELECT ...) TO only.
-- ============================================================================

\set QUIET on
\pset pager off

-- Generic prospective_outcome rows belonging to this experiment (documentation
-- only: this study's authoritative outcome source is
-- research.me_reverse_long_v3_execution_outcome).
\copy (SELECT to_jsonb(p) AS row_json FROM research.prospective_outcome p WHERE p.experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS' ORDER BY p.observation_id) TO '/tmp/me_v3_eval_20261022/10_prospective_outcome_v3.csv' WITH CSV HEADER

-- Completed-event ledger rows belonging to this experiment.
\copy (SELECT * FROM research.prospective_completed_event c WHERE c.experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS' ORDER BY c.setup_event_id) TO '/tmp/me_v3_eval_20261022/11_completed_events_v3.csv' WITH CSV HEADER

-- Constraint inventory for integrity checks.
\copy (SELECT conrelid::regclass::text AS table_name, conname, pg_get_constraintdef(oid) AS constraint_def FROM pg_constraint WHERE connamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'research') AND conrelid::regclass::text IN ('prospective_observation','me_reverse_long_v3_execution_outcome','prospective_experiment') ORDER BY conrelid::regclass::text, conname) TO '/tmp/me_v3_eval_20261022/12_constraints.csv' WITH CSV HEADER
