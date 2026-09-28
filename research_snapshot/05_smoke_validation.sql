-- ============================================================
-- PRE-ACTIVATION SMOKE VALIDATION — Prospective OOS V3
-- ============================================================
-- READ ONLY.  Never mutates data, schema, or configuration.
--
-- Run BEFORE activation:
--   psql -U postgres -d trad_bot -f 05_smoke_validation.sql
--
-- Also usable POST-ACTIVATION by changing the --section markers.
-- ============================================================

\echo '============================================================='
\echo '  PROSPECTIVE OOS V3 — SMOKE VALIDATION'
\echo '  READ-ONLY.  No mutations.'
\echo '============================================================='
\echo ''

-- ============================================================
-- SECTION 1: STRUCTURAL INTEGRITY
-- ============================================================
\echo '=== 1. STRUCTURAL INTEGRITY ==='
\echo ''

-- 1a. Required tables exist
\echo '--- 1a. Required tables ---'
SELECT
    'research.prospective_experiment' AS object,
    (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema='research' AND table_name='prospective_experiment') AS exists,
    'table' AS type
UNION ALL
SELECT
    'research.prospective_observation',
    (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema='research' AND table_name='prospective_observation'),
    'table'
UNION ALL
SELECT
    'research.prospective_outcome',
    (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema='research' AND table_name='prospective_outcome'),
    'table'
UNION ALL
SELECT
    'research.v_prospective_accumulation',
    (SELECT COUNT(*) FROM information_schema.views
     WHERE table_schema='research' AND table_name='v_prospective_accumulation'),
    'view';

-- 1b. Exactly 6 expected experiments
\echo ''
\echo '--- 1b. Experiment registry (expect exactly 6) ---'
SELECT
    experiment_id,
    scanner_name,
    direction,
    experiment_type,
    status,
    started_at,
    threshold
FROM research.prospective_experiment
ORDER BY experiment_id;

-- 1c. Registry counts
\echo ''
\echo '--- 1c. Registry count ---'
SELECT
    COUNT(*) AS total_experiments,
    COUNT(*) FILTER (WHERE experiment_id IN (
        'SRR_LONG_BASELINE_V1', 'ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1',
        'ME_SHORT_GEOM_C_V1', 'VC_SHORT_BB_WIDTH_V1', 'LR_SHORT_GATE_V1'
    )) AS expected_count,
    COUNT(*) FILTER (WHERE experiment_id NOT IN (
        'SRR_LONG_BASELINE_V1', 'ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1',
        'ME_SHORT_GEOM_C_V1', 'VC_SHORT_BB_WIDTH_V1', 'LR_SHORT_GATE_V1'
    )) AS unexpected_count
FROM research.prospective_experiment;

-- 1d. No duplicates
\echo ''
\echo '--- 1d. Duplicate check ---'
SELECT experiment_id, COUNT(*) AS cnt
FROM research.prospective_experiment
GROUP BY experiment_id
HAVING COUNT(*) > 1;

-- ============================================================
-- SECTION 2: PRE-ACTIVATION BOUNDARY
-- ============================================================
\echo ''
\echo '=== 2. PRE-ACTIVATION BOUNDARY ==='
\echo ''
\echo 'All six experiments must have started_at IS NULL:'
SELECT
    experiment_id,
    status,
    started_at,
    CASE WHEN started_at IS NULL THEN 'SAFE' ELSE 'ALREADY ACTIVE' END AS boundary
FROM research.prospective_experiment
WHERE experiment_id IN (
    'SRR_LONG_BASELINE_V1', 'ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1',
    'ME_SHORT_GEOM_C_V1', 'VC_SHORT_BB_WIDTH_V1', 'LR_SHORT_GATE_V1'
)
ORDER BY experiment_id;

-- ============================================================
-- SECTION 3: OBSERVER SMOKE VISIBILITY
-- ============================================================
\echo ''
\echo '=== 3. OBSERVER SMOKE VISIBILITY ==='
\echo ''
\echo 'Per-experiment observation summary:'
SELECT
    pe.experiment_id,
    pe.status,
    pe.started_at,
    COUNT(po.observation_id) AS observations,
    COUNT(DISTINCT po.source_signal_id) AS unique_source_signals,
    COUNT(po.observation_id) FILTER (WHERE po.rule_passed) AS rule_passed,
    COUNT(po.observation_id) FILTER (WHERE NOT po.rule_passed) AS rule_control,
    MIN(po.signal_time) AS min_signal_time,
    MAX(po.signal_time) AS max_signal_time
FROM research.prospective_experiment pe
LEFT JOIN research.prospective_observation po ON po.experiment_id = pe.experiment_id
GROUP BY pe.experiment_id, pe.status, pe.started_at
ORDER BY pe.experiment_id;

-- ============================================================
-- SECTION 4: ME A/B/C PAIRING
-- ============================================================
\echo ''
\echo '=== 4. ME A/B/C PAIRING ==='
\echo ''
\echo 'Source signals with incomplete A/B/C set:'
SELECT
    po.source_signal_id,
    array_agg(DISTINCT po.experiment_id ORDER BY po.experiment_id) AS variants,
    COUNT(DISTINCT po.experiment_id) AS variant_count
FROM research.prospective_observation po
WHERE po.experiment_id IN ('ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1', 'ME_SHORT_GEOM_C_V1')
GROUP BY po.source_signal_id
HAVING COUNT(DISTINCT po.experiment_id) != 3
   OR array_agg(DISTINCT po.experiment_id ORDER BY po.experiment_id) !=
      ARRAY['ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1', 'ME_SHORT_GEOM_C_V1'];

-- Total ME source signals
\echo ''
\echo '--- ME pairing summary ---'
SELECT
    COUNT(DISTINCT po.source_signal_id) AS total_me_sources,
    COUNT(DISTINCT po.source_signal_id) FILTER (
        WHERE po.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1','ME_SHORT_GEOM_C_V1')
        AND po.source_signal_id IN (
            SELECT source_signal_id FROM research.prospective_observation
            WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1','ME_SHORT_GEOM_C_V1')
            GROUP BY source_signal_id HAVING COUNT(DISTINCT experiment_id) = 3
        )
    ) AS fully_paired_sources
FROM research.prospective_observation po
WHERE po.experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1','ME_SHORT_GEOM_C_V1');

-- ============================================================
-- SECTION 5: OUTCOME/MATURITY VISIBILITY
-- ============================================================
\echo ''
\echo '=== 5. OUTCOME/MATURITY VISIBILITY ==='
\echo ''
SELECT
    pe.experiment_id,
    COUNT(DISTINCT po.observation_id) AS observations,
    COUNT(DISTINCT po2.observation_id) AS outcomes,
    COUNT(po2.observation_id) FILTER (WHERE po2.mfe_15m IS NOT NULL)  AS m15m,
    COUNT(po2.observation_id) FILTER (WHERE po2.mfe_30m IS NOT NULL)  AS m30m,
    COUNT(po2.observation_id) FILTER (WHERE po2.mfe_60m IS NOT NULL)  AS m60m,
    COUNT(po2.observation_id) FILTER (WHERE po2.mfe_120m IS NOT NULL) AS m120m,
    COUNT(po2.observation_id) FILTER (WHERE po2.mfe_240m IS NOT NULL) AS m240m,
    COUNT(po2.observation_id) FILTER (WHERE po2.is_final) AS finalized,
    COUNT(po2.observation_id) FILTER (WHERE po2.ambiguous_intrabar) AS ambiguous,
    MAX(po.signal_time) AS latest_observation,
    MAX(po2.updated_at) AS latest_outcome
FROM research.prospective_experiment pe
LEFT JOIN research.prospective_observation po ON po.experiment_id = pe.experiment_id
LEFT JOIN research.prospective_outcome po2 ON po2.observation_id = po.observation_id
GROUP BY pe.experiment_id
ORDER BY pe.experiment_id;

-- ============================================================
-- SECTION 6: VC FILTER VALIDATION
-- ============================================================
\echo ''
\echo '=== 6. VC FILTER VALIDATION ==='
\echo ''
\echo 'Frozen literal: bb_width_percentile < 0.569723'
\echo 'Check stored observations against frozen threshold:'
SELECT
    CASE
        WHEN (po.features->>'bb_width_percentile')::numeric < 0.569723
             AND po.rule_passed = TRUE THEN 'CORRECT_PASS'
        WHEN (po.features->>'bb_width_percentile')::numeric >= 0.569723
             AND po.rule_passed = FALSE THEN 'CORRECT_CONTROL'
        WHEN po.features->>'bb_width_percentile' IS NULL THEN 'MISSING_FEATURE'
        ELSE 'MISMATCH'
    END AS validation,
    COUNT(*) AS cnt
FROM research.prospective_observation po
WHERE po.experiment_id = 'VC_SHORT_BB_WIDTH_V1'
GROUP BY validation;

-- ============================================================
-- SECTION 7: ME GEOMETRY VALIDATION
-- ============================================================
\echo ''
\echo '=== 7. ME GEOMETRY VALIDATION ==='
\echo ''
\echo 'A vs B stop comparison (B should be wider/equal):'
SELECT
    po.source_signal_id,
    MAX(CASE WHEN po.experiment_id = 'ME_SHORT_GEOM_A_V1' THEN ABS(po.reference_price - po.invalidation_price) END) AS risk_dist_A,
    MAX(CASE WHEN po.experiment_id = 'ME_SHORT_GEOM_B_V1'
         THEN ABS(po.reference_price - COALESCE(po.variant_stop, po.invalidation_price)) END) AS risk_dist_B,
    MAX(CASE WHEN po.experiment_id = 'ME_SHORT_GEOM_A_V1' THEN ABS(po.reference_price - po.target_1) END) AS target_dist_A,
    MAX(CASE WHEN po.experiment_id = 'ME_SHORT_GEOM_B_V1'
         THEN ABS(po.reference_price - COALESCE(po.variant_target, po.target_1)) END) AS target_dist_B
FROM research.prospective_observation po
WHERE po.experiment_id IN ('ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1')
GROUP BY po.source_signal_id
ORDER BY po.source_signal_id
LIMIT 10;

\echo ''
\echo 'C delayed entry check (C entry should differ from A entry):'
SELECT
    po.source_signal_id,
    MAX(CASE WHEN po.experiment_id = 'ME_SHORT_GEOM_A_V1' THEN po.reference_price END) AS A_entry_price,
    MAX(CASE WHEN po.experiment_id = 'ME_SHORT_GEOM_C_V1' THEN po.variant_entry END) AS C_entry_price,
    MAX(CASE WHEN po.experiment_id = 'ME_SHORT_GEOM_A_V1' THEN po.reference_price END) -
    MAX(CASE WHEN po.experiment_id = 'ME_SHORT_GEOM_C_V1' THEN po.variant_entry END) AS entry_diff
FROM research.prospective_observation po
WHERE po.experiment_id IN ('ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_C_V1')
GROUP BY po.source_signal_id
ORDER BY po.source_signal_id
LIMIT 10;

-- ============================================================
-- SECTION 8: SRR/LR DISPOSITION FIELDS
-- ============================================================
\echo ''
\echo '=== 8. SRR/LR DISPOSITION FIELDS ==='
\echo ''
\echo 'SRR_LONG_BASELINE_V1 observations (first 5):'
SELECT
    po.observation_id, po.symbol, po.signal_time, po.score,
    po.rule_passed, po.filter_reason, po.gate_result
FROM research.prospective_observation po
WHERE po.experiment_id = 'SRR_LONG_BASELINE_V1'
ORDER BY po.signal_time
LIMIT 5;

\echo ''
\echo 'LR_SHORT_GATE_V1 observations (first 5):'
SELECT
    po.observation_id, po.symbol, po.signal_time, po.score,
    po.rule_passed, po.filter_reason, po.gate_result
FROM research.prospective_observation po
WHERE po.experiment_id = 'LR_SHORT_GATE_V1'
ORDER BY po.signal_time
LIMIT 5;

-- ============================================================
-- SECTION 9: ACCUMULATION VIEW CHECK
-- ============================================================
\echo ''
\echo '=== 9. ACCUMULATION VIEW ==='
\echo ''
SELECT * FROM research.v_prospective_accumulation ORDER BY experiment_id;

-- ============================================================
-- SECTION 10: POST-ACTIVATION BOUNDARY CHECK
-- ============================================================
\echo ''
\echo '=== 10. POST-ACTIVATION CHECK (run AFTER activation) ==='
\echo ''
\echo 'All six must have started_at IS NOT NULL:'
SELECT
    experiment_id,
    started_at,
    CASE WHEN started_at IS NOT NULL THEN 'ACTIVE' ELSE 'NOT_ACTIVATED' END AS status
FROM research.prospective_experiment
WHERE experiment_id IN (
    'SRR_LONG_BASELINE_V1', 'ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1',
    'ME_SHORT_GEOM_C_V1', 'VC_SHORT_BB_WIDTH_V1', 'LR_SHORT_GATE_V1'
)
ORDER BY experiment_id;

\echo ''
\echo 'No outcomes from pre-activation observations:'
SELECT
    po.experiment_id,
    COUNT(*) AS total_observations,
    COUNT(po2.observation_id) AS outcomes_from_pre_activation
FROM research.prospective_observation po
LEFT JOIN research.prospective_outcome po2 ON po2.observation_id = po.observation_id
WHERE po.experiment_id IN (
    'SRR_LONG_BASELINE_V1', 'ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1',
    'ME_SHORT_GEOM_C_V1', 'VC_SHORT_BB_WIDTH_V1', 'LR_SHORT_GATE_V1'
)
AND po.signal_time < (
    SELECT started_at FROM research.prospective_experiment
    WHERE experiment_id = po.experiment_id
)
GROUP BY po.experiment_id;

\echo ''
\echo '============================================================='
\echo '  SMOKE VALIDATION COMPLETE'
\echo '============================================================='
