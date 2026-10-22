-- ============================================================================
-- ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1
-- READ-ONLY frozen cohort extraction for experiment
--   MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS
--
-- Target: production PostgreSQL `trad_bot` (schema `research`).
-- SAFETY: this script performs ONLY SELECT / \copy (SELECT ...) TO statements.
--         No INSERT/UPDATE/DELETE/DDL. Client-side \copy writes CSV files to
--         the output directory given below (default /tmp/me_v3_eval_20261022).
--
-- Usage (on VPS):
--   sudo -u postgres psql -d trad_bot -P pager=off \
--     -f 01_extract_frozen_cohort.sql
--
-- Deterministic ordering: every result set is ordered by its primary key.
-- ============================================================================

\set QUIET on
\pset pager off

-- 01. Extraction environment / audit timestamp --------------------------------
\copy (SELECT now() AS db_now_utc, current_database() AS db_name, current_user AS db_user, version() AS pg_version, (SELECT max(updated_at) FROM research.me_reverse_long_v3_execution_outcome) AS max_outcome_updated_at, (SELECT max(created_at) FROM research.prospective_observation WHERE experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS') AS max_observation_created_at) TO '/tmp/me_v3_eval_20261022/01_db_meta.csv' WITH CSV HEADER

-- 02. Prospective experiment registry rows (all experiments, full row jsonb) --
\copy (SELECT experiment_id, version, scanner_name, direction, experiment_type, status, started_at, threshold, minimum_n, minimum_symbols, primary_metric, filter_rule, entry_rule, stop_rule, target_rule, position_sizing, fee_assumption, discovery_source, paired_with, created_at, updated_at, to_jsonb(e) AS row_json FROM research.prospective_experiment e ORDER BY experiment_id) TO '/tmp/me_v3_eval_20261022/02_prospective_experiment_all.csv' WITH CSV HEADER

-- 03. Direction-level lifecycle state -----------------------------------------
\copy (SELECT to_jsonb(d) AS row_json FROM research.prospective_experiment_direction_state d ORDER BY to_jsonb(d)::text) TO '/tmp/me_v3_eval_20261022/03_direction_state.csv' WITH CSV HEADER

-- 04. FROZEN COHORT: every V3 observation joined 1:1 to its dedicated outcome --
\copy (SELECT o.observation_id, o.experiment_id, o.source_signal_id, o.source_observation_id, o.symbol, o.direction, o.signal_time, o.reference_price, o.invalidation_price, o.target_1, o.target_2, o.score, o.variant_entry, o.variant_stop, o.variant_target, o.rule_passed, o.filter_reason, o.gate_result, o.features ->> 'exhaustion_magnitude' AS exhaustion_magnitude_raw, o.features AS features_json, o.parameters AS parameters_json, o.market_regime, o.created_at AS observation_created_at, x.status AS outcome_status, x.filter_group, x.execution_entry_at, x.execution_entry_price, x.execution_stop_price, x.execution_target_price, x.exit_at, x.exit_price, x.exit_reason, x.gross_r, x.net_r_normal, x.net_r_elevated, x.ambiguous, x.coverage_reason, x.candle_count, x.evaluated_at, x.created_at AS outcome_created_at, x.updated_at AS outcome_updated_at FROM research.prospective_observation o LEFT JOIN research.me_reverse_long_v3_execution_outcome x ON x.observation_id = o.observation_id AND x.experiment_id = o.experiment_id WHERE o.experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS' ORDER BY o.observation_id) TO '/tmp/me_v3_eval_20261022/04_cohort_full.csv' WITH CSV HEADER

-- 05. Dedicated outcome rows without a matching observation (expect 0) --------
\copy (SELECT x.observation_id, x.experiment_id, x.status, x.filter_group, x.evaluated_at FROM research.me_reverse_long_v3_execution_outcome x LEFT JOIN research.prospective_observation o ON o.observation_id = x.observation_id WHERE o.observation_id IS NULL ORDER BY x.observation_id) TO '/tmp/me_v3_eval_20261022/05_outcome_orphans.csv' WITH CSV HEADER

-- 06. Generic research.prospective_outcome rows (NOT this study's source of
--     truth; extracted only to document presence/absence) ---------------------
\copy (SELECT to_jsonb(p) AS row_json FROM research.prospective_outcome p ORDER BY to_jsonb(p)::text) TO '/tmp/me_v3_eval_20261022/06_prospective_outcome_all.csv' WITH CSV HEADER

-- 07. Completed-event ledger rows --------------------------------------------
\copy (SELECT to_jsonb(c) AS row_json FROM research.prospective_completed_event c ORDER BY to_jsonb(c)::text) TO '/tmp/me_v3_eval_20261022/07_completed_events.csv' WITH CSV HEADER

-- 08. Column inventory of the audited tables (integrity reference) ------------
\copy (SELECT table_name, ordinal_position, column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema = 'research' AND table_name IN ('prospective_observation','prospective_experiment','prospective_experiment_direction_state','prospective_outcome','prospective_completed_event','me_reverse_long_v3_execution_outcome') ORDER BY table_name, ordinal_position) TO '/tmp/me_v3_eval_20261022/08_column_inventory.csv' WITH CSV HEADER

-- 09. Row counts of all audited tables ---------------------------------------
\copy (SELECT 'prospective_observation_all' AS table_name, count(*) AS row_count FROM research.prospective_observation UNION ALL SELECT 'prospective_observation_v3', count(*) FROM research.prospective_observation WHERE experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS' UNION ALL SELECT 'me_reverse_long_v3_execution_outcome_all', count(*) FROM research.me_reverse_long_v3_execution_outcome UNION ALL SELECT 'prospective_experiment', count(*) FROM research.prospective_experiment UNION ALL SELECT 'prospective_outcome_all', count(*) FROM research.prospective_outcome UNION ALL SELECT 'prospective_completed_event_all', count(*) FROM research.prospective_completed_event ORDER BY table_name) TO '/tmp/me_v3_eval_20261022/09_row_counts.csv' WITH CSV HEADER
