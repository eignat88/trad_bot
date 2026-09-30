-- ============================================================
-- PROSPECTIVE OOS GLOBAL PRELIMINARY ANALYSIS
-- COMPREHENSIVE READ-ONLY AUDIT
-- ============================================================
-- Run on VPS as:
--   sudo -u postgres psql -d trad_bot -f /opt/trad_bot/docs/research/vps_analysis.sql
-- ============================================================

\echo '============================================================='
\echo '  PROSPECTIVE OOS GLOBAL PRELIMINARY ANALYSIS'
\echo '  READ-ONLY AUDIT — ' || NOW()::text
\echo '============================================================='
\echo ''

-- ============================================================
-- 0. ANALYSIS CUTOFF
-- ============================================================
\echo '=== 0. ANALYSIS CUTOFF ==='
SELECT NOW() AS analysis_cutoff_utc;
\echo ''

-- ============================================================
-- 1. STRUCTURAL CHECK
-- ============================================================
\echo '=== 1. STRUCTURAL CHECK ==='
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
    'research.research_signal',
    (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema='research' AND table_name='research_signal'),
    'table';
\echo ''

-- ============================================================
-- 2. EXECUTIVE SUMMARY
-- ============================================================
\echo '=== 2. EXECUTIVE SUMMARY ==='
\echo ''

-- 2a. Total experiments
SELECT 'Total experiments: ' || COUNT(*)::text FROM research.prospective_experiment;

-- 2b. By status
SELECT status, COUNT(*) AS cnt
FROM research.prospective_experiment
GROUP BY status ORDER BY cnt DESC;

-- 2c. With/without started_at
SELECT
    COUNT(*) FILTER (WHERE started_at IS NOT NULL) AS with_started_at,
    COUNT(*) FILTER (WHERE started_at IS NULL) AS without_started_at
FROM research.prospective_experiment;

-- 2d. Observations summary
SELECT
    COUNT(*) AS total_observations,
    COUNT(DISTINCT experiment_id) AS experiments_with_observations
FROM research.prospective_observation;

-- 2e. Outcomes summary
SELECT
    COUNT(*) AS total_outcomes,
    COUNT(DISTINCT observation_id) AS observations_with_outcomes
FROM research.prospective_outcome;

-- 2f. By maturity class (post-T0 finalized)
WITH post_t0 AS (
    SELECT
        po.observation_id,
        po.experiment_id,
        po.signal_time
    FROM research.prospective_observation po
    JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
    WHERE pe.started_at IS NOT NULL
      AND po.signal_time >= pe.started_at
),
finalized_post_t0 AS (
    SELECT
        pt.experiment_id,
        COUNT(*) AS n_finalized
    FROM post_t0 pt
    JOIN research.prospective_outcome po2 ON po2.observation_id = pt.observation_id
    WHERE po2.is_final = TRUE
    GROUP BY pt.experiment_id
)
SELECT
    CASE
        WHEN n_finalized = 0 THEN 'N=0 (NO_DATA)'
        WHEN n_finalized BETWEEN 1 AND 9 THEN 'N=1-9 (TOO_EARLY)'
        WHEN n_finalized BETWEEN 10 AND 29 THEN 'N=10-29 (EARLY)'
        WHEN n_finalized BETWEEN 30 AND 49 THEN 'N=30-49 (PRELIMINARY)'
        WHEN n_finalized >= 50 THEN 'N>=50 (ANALYSIS_READY)'
    END AS maturity_class,
    COUNT(*) AS experiments_in_class
FROM finalized_post_t0
GROUP BY maturity_class
ORDER BY maturity_class;

\echo ''

-- ============================================================
-- 3. FULL INVENTORY TABLE
-- ============================================================
\echo '=== 3. FULL INVENTORY TABLE ==='
SELECT
    pe.experiment_id,
    pe.scanner_name,
    pe.direction,
    pe.experiment_type,
    pe.status,
    pe.started_at,
    -- Total observations
    (SELECT COUNT(*) FROM research.prospective_observation po
     WHERE po.experiment_id = pe.experiment_id) AS total_obs,
    -- Pre-T0 observations
    (SELECT COUNT(*) FROM research.prospective_observation po
     WHERE po.experiment_id = pe.experiment_id
       AND pe.started_at IS NOT NULL
       AND po.signal_time < pe.started_at) AS pre_t0_obs,
    -- Post-T0 observations
    (SELECT COUNT(*) FROM research.prospective_observation po
     WHERE po.experiment_id = pe.experiment_id
       AND pe.started_at IS NOT NULL
       AND po.signal_time >= pe.started_at) AS post_t0_obs,
    -- Total outcomes
    (SELECT COUNT(*) FROM research.prospective_outcome po2
     JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
     WHERE po.experiment_id = pe.experiment_id) AS total_outcomes,
    -- Post-T0 outcomes
    (SELECT COUNT(*) FROM research.prospective_outcome po2
     JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
     WHERE po.experiment_id = pe.experiment_id
       AND pe.started_at IS NOT NULL
       AND po.signal_time >= pe.started_at) AS post_t0_outcomes,
    -- Finalized post-T0
    (SELECT COUNT(*) FROM research.prospective_outcome po2
     JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
     WHERE po.experiment_id = pe.experiment_id
       AND pe.started_at IS NOT NULL
       AND po.signal_time >= pe.started_at
       AND po2.is_final = TRUE) AS post_t0_finalized,
    -- Pending post-T0
    (SELECT COUNT(*) FROM research.prospective_outcome po2
     JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
     WHERE po.experiment_id = pe.experiment_id
       AND pe.started_at IS NOT NULL
       AND po.signal_time >= pe.started_at
       AND po2.is_final = FALSE) AS post_t0_pending,
    -- Unique symbols post-T0
    (SELECT COUNT(DISTINCT po.symbol) FROM research.prospective_observation po
     WHERE po.experiment_id = pe.experiment_id
       AND pe.started_at IS NOT NULL
       AND po.signal_time >= pe.started_at) AS unique_symbols_post_t0,
    -- First post-T0 signal
    (SELECT MIN(po.signal_time) FROM research.prospective_observation po
     WHERE po.experiment_id = pe.experiment_id
       AND pe.started_at IS NOT NULL
       AND po.signal_time >= pe.started_at) AS first_post_t0_signal,
    -- Last post-T0 signal
    (SELECT MAX(po.signal_time) FROM research.prospective_observation po
     WHERE po.experiment_id = pe.experiment_id
       AND pe.started_at IS NOT NULL
       AND po.signal_time >= pe.started_at) AS last_post_t0_signal,
    -- Promotion orphans (observations without outcomes)
    (SELECT COUNT(*) FROM research.prospective_observation po
     WHERE po.experiment_id = pe.experiment_id
       AND NOT EXISTS (
           SELECT 1 FROM research.prospective_outcome po2
           WHERE po2.observation_id = po.observation_id
       )) AS promotion_orphans
FROM research.prospective_experiment pe
ORDER BY pe.experiment_id;

\echo ''

-- ============================================================
-- 4. T0 PURITY CHECK
-- ============================================================
\echo '=== 4. T0 PURITY CHECK ==='
SELECT
    pe.experiment_id,
    pe.started_at,
    COUNT(*) AS total_observations,
    COUNT(*) FILTER (WHERE pe.started_at IS NULL) AS invalid_t0,
    COUNT(*) FILTER (WHERE pe.started_at IS NOT NULL AND po.signal_time < pe.started_at) AS pre_t0,
    COUNT(*) FILTER (WHERE pe.started_at IS NOT NULL AND po.signal_time >= pe.started_at) AS post_t0
FROM research.prospective_experiment pe
LEFT JOIN research.prospective_observation po ON po.experiment_id = pe.experiment_id
GROUP BY pe.experiment_id, pe.started_at
ORDER BY pe.experiment_id;

\echo ''

-- ============================================================
-- 5. OUTCOME MATURITY CLASSIFICATION
-- ============================================================
\echo '=== 5. OUTCOME MATURITY CLASSIFICATION ==='
WITH post_t0_finalized AS (
    SELECT
        po.experiment_id,
        COUNT(*) AS n_finalized
    FROM research.prospective_observation po
    JOIN research.prospective_outcome po2 ON po2.observation_id = po.observation_id
    JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
    WHERE pe.started_at IS NOT NULL
      AND po.signal_time >= pe.started_at
      AND po2.is_final = TRUE
    GROUP BY po.experiment_id
)
SELECT
    pe.experiment_id,
    COALESCE(ptf.n_finalized, 0) AS n_finalized,
    CASE
        WHEN COALESCE(ptf.n_finalized, 0) = 0 THEN 'NO_DATA'
        WHEN ptf.n_finalized BETWEEN 1 AND 9 THEN 'TOO_EARLY'
        WHEN ptf.n_finalized BETWEEN 10 AND 29 THEN 'EARLY'
        WHEN ptf.n_finalized BETWEEN 30 AND 49 THEN 'PRELIMINARY'
        WHEN ptf.n_finalized >= 50 THEN 'ANALYSIS_READY'
    END AS maturity_class,
    CASE
        WHEN COALESCE(ptf.n_finalized, 0) < 10 THEN 10
        WHEN ptf.n_finalized BETWEEN 10 AND 29 THEN 30
        WHEN ptf.n_finalized BETWEEN 30 AND 49 THEN 50
        WHEN ptf.n_finalized >= 50 THEN 100
    END AS next_checkpoint,
    CASE
        WHEN COALESCE(ptf.n_finalized, 0) < 10 THEN 10 - COALESCE(ptf.n_finalized, 0)
        WHEN ptf.n_finalized BETWEEN 10 AND 29 THEN 30 - ptf.n_finalized
        WHEN ptf.n_finalized BETWEEN 30 AND 49 THEN 50 - ptf.n_finalized
        WHEN ptf.n_finalized >= 50 THEN 100 - ptf.n_finalized
    END AS samples_remaining
FROM research.prospective_experiment pe
LEFT JOIN post_t0_finalized ptf ON ptf.experiment_id = pe.experiment_id
ORDER BY pe.experiment_id;

\echo ''

-- ============================================================
-- 6. DATA INTEGRITY CHECK
-- ============================================================
\echo '=== 6. DATA INTEGRITY CHECK ==='

-- 6a. Duplicate observations
SELECT 'OBSERVATION_DUPLICATES' AS check_name,
       experiment_id, source_signal_id, COUNT(*) AS cnt
FROM research.prospective_observation
GROUP BY experiment_id, source_signal_id
HAVING COUNT(*) > 1;

-- 6b. Outcome without observation
SELECT 'OUTCOME_WITHOUT_OBSERVATION' AS check_name,
       po2.observation_id
FROM research.prospective_outcome po2
WHERE NOT EXISTS (
    SELECT 1 FROM research.prospective_observation po
    WHERE po.observation_id = po2.observation_id
);

-- 6c. Observation without outcome (orphans)
SELECT 'OBSERVATION_WITHOUT_OUTCOME' AS check_name,
       po.experiment_id, po.observation_id
FROM research.prospective_observation po
WHERE NOT EXISTS (
    SELECT 1 FROM research.prospective_outcome po2
    WHERE po2.observation_id = po.observation_id
);

-- 6d. NULL reference price
SELECT 'NULL_REFERENCE_PRICE' AS check_name,
       experiment_id, observation_id
FROM research.prospective_observation
WHERE reference_price IS NULL;

-- 6e. NULL invalidation
SELECT 'NULL_INVALIDATION' AS check_name,
       experiment_id, observation_id
FROM research.prospective_observation
WHERE invalidation_price IS NULL;

-- 6f. Invalid risk distance (reference = invalidation)
SELECT 'INVALID_RISK_DISTANCE' AS check_name,
       experiment_id, observation_id
FROM research.prospective_observation
WHERE reference_price = invalidation_price;

-- 6g. Direction mismatch
SELECT 'DIRECTION_MISMATCH' AS check_name,
       po.experiment_id, po.observation_id
FROM research.prospective_observation po
JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
WHERE po.direction != pe.direction;

-- 6h. Experiment_id mismatch
SELECT 'EXPERIMENT_ID_MISMATCH' AS check_name,
       po.observation_id, po.experiment_id AS obs_experiment_id
FROM research.prospective_observation po
WHERE NOT EXISTS (
    SELECT 1 FROM research.prospective_experiment pe
    WHERE pe.experiment_id = po.experiment_id
);

\echo ''

-- ============================================================
-- 7. ORPHAN EXPERIMENT_ID CHECK
-- ============================================================
\echo '=== 7. ORPHAN EXPERIMENT_ID CHECK ==='
SELECT DISTINCT
    po.experiment_id AS orphan_experiment_id
FROM research.prospective_observation po
WHERE NOT EXISTS (
    SELECT 1 FROM research.prospective_experiment pe
    WHERE pe.experiment_id = po.experiment_id
);

\echo ''

-- ============================================================
-- 8. CLUSTERING / DEPENDENCE CHECK
-- ============================================================
\echo '=== 8. CLUSTERING / DEPENDENCE CHECK ==='
SELECT
    po.experiment_id,
    COUNT(DISTINCT po.symbol) AS unique_symbols,
    COUNT(*) AS total_observations,
    ROUND(COUNT(*)::numeric / NULLIF(COUNT(DISTINCT po.symbol), 0), 2) AS avg_obs_per_symbol,
    MIN(po.signal_time) AS first_signal,
    MAX(po.signal_time) AS last_signal,
    EXTRACT(EPOCH FROM (MAX(po.signal_time) - MIN(po.signal_time))) / 3600 AS span_hours
FROM research.prospective_observation po
JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
WHERE pe.started_at IS NOT NULL
  AND po.signal_time >= pe.started_at
GROUP BY po.experiment_id
ORDER BY po.experiment_id;

\echo ''

-- ============================================================
-- 9. PER-SYMBOL CONCENTRATION
-- ============================================================
\echo '=== 9. PER-SYMBOL CONCENTRATION ==='
SELECT
    po.experiment_id,
    po.symbol,
    COUNT(*) AS observations,
    ROUND(COUNT(*)::numeric * 100.0 /
          SUM(COUNT(*)) OVER (PARTITION BY po.experiment_id), 1) AS pct_of_experiment
FROM research.prospective_observation po
JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
WHERE pe.started_at IS NOT NULL
  AND po.signal_time >= pe.started_at
GROUP BY po.experiment_id, po.symbol
ORDER BY po.experiment_id, observations DESC;

\echo ''

-- ============================================================
-- 10. TEMPORAL CONCENTRATION (signals per hour)
-- ============================================================
\echo '=== 10. TEMPORAL CONCENTRATION ==='
SELECT
    po.experiment_id,
    DATE_TRUNC('hour', po.signal_time) AS signal_hour,
    COUNT(*) AS signals_in_hour
FROM research.prospective_observation po
JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
WHERE pe.started_at IS NOT NULL
  AND po.signal_time >= pe.started_at
GROUP BY po.experiment_id, DATE_TRUNC('hour', po.signal_time)
HAVING COUNT(*) > 1
ORDER BY po.experiment_id, signals_in_hour DESC;

\echo ''

-- ============================================================
-- 11. PRE-ACTIVATION CONTAMINATION CHECK
-- ============================================================
\echo '=== 11. PRE-ACTIVATION CONTAMINATION CHECK ==='
SELECT
    po.experiment_id,
    COUNT(*) AS pre_t0_outcomes
FROM research.prospective_outcome po2
JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
WHERE pe.started_at IS NOT NULL
  AND po.signal_time < pe.started_at
GROUP BY po.experiment_id;

\echo ''

-- ============================================================
-- 12. PERFORMANCE ANALYSIS (N >= 10) — TP/SL STATS
-- ============================================================
\echo '=== 12. PERFORMANCE ANALYSIS (N >= 10) — TP/SL STATS ==='
WITH post_t0_finalized AS (
    SELECT
        po.experiment_id,
        po2.observation_id,
        po.symbol,
        po.signal_time,
        po2.tp_hit, po2.sl_hit,
        po2.tp_before_sl, po2.sl_before_tp,
        po2.ambiguous_intrabar,
        po2.time_to_tp, po2.time_to_sl,
        po2.mfe_15m, po2.mae_15m,
        po2.mfe_30m, po2.mae_30m,
        po2.mfe_60m, po2.mae_60m,
        po2.mfe_120m, po2.mae_120m,
        po2.mfe_240m, po2.mae_240m
    FROM research.prospective_outcome po2
    JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
    JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
    WHERE pe.started_at IS NOT NULL
      AND po.signal_time >= pe.started_at
      AND po2.is_final = TRUE
),
experiment_stats AS (
    SELECT
        experiment_id,
        COUNT(*) AS n_finalized,
        COUNT(DISTINCT symbol) AS unique_symbols,
        COUNT(*) FILTER (WHERE tp_hit) AS tp_hit,
        COUNT(*) FILTER (WHERE sl_hit) AS sl_hit,
        COUNT(*) FILTER (WHERE tp_before_sl) AS tp_before_sl,
        COUNT(*) FILTER (WHERE sl_before_tp) AS sl_before_tp,
        COUNT(*) FILTER (WHERE ambiguous_intrabar) AS ambiguous
    FROM post_t0_finalized
    GROUP BY experiment_id
    HAVING COUNT(*) >= 10
)
SELECT
    experiment_id,
    n_finalized,
    unique_symbols,
    tp_hit,
    ROUND(tp_hit::numeric / n_finalized * 100, 1) AS tp_hit_pct,
    sl_hit,
    ROUND(sl_hit::numeric / n_finalized * 100, 1) AS sl_hit_pct,
    tp_before_sl,
    ROUND(tp_before_sl::numeric / n_finalized * 100, 1) AS tp_before_sl_pct,
    sl_before_tp,
    ROUND(sl_before_tp::numeric / n_finalized * 100, 1) AS sl_before_tp_pct,
    ambiguous,
    ROUND(ambiguous::numeric / n_finalized * 100, 1) AS ambiguous_pct
FROM experiment_stats
ORDER BY experiment_id;

\echo ''

-- ============================================================
-- 13. MFE/MAE STATISTICS (N >= 10)
-- ============================================================
\echo '=== 13. MFE/MAE STATISTICS (N >= 10) ==='
WITH post_t0_finalized AS (
    SELECT
        po.experiment_id,
        po2.observation_id,
        po2.mfe_15m, po2.mae_15m,
        po2.mfe_30m, po2.mae_30m,
        po2.mfe_60m, po2.mae_60m,
        po2.mfe_120m, po2.mae_120m,
        po2.mfe_240m, po2.mae_240m
    FROM research.prospective_outcome po2
    JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
    JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
    WHERE pe.started_at IS NOT NULL
      AND po.signal_time >= pe.started_at
      AND po2.is_final = TRUE
),
experiment_counts AS (
    SELECT experiment_id, COUNT(*) AS n
    FROM post_t0_finalized
    GROUP BY experiment_id
    HAVING COUNT(*) >= 10
)
SELECT
    ptf.experiment_id,
    'MFE_15m' AS metric,
    ROUND(AVG(ptf.mfe_15m)::numeric, 4) AS mean,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ptf.mfe_15m) AS median,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY ptf.mfe_15m) AS p25,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY ptf.mfe_15m) AS p75,
    PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ptf.mfe_15m) AS p90,
    MIN(ptf.mfe_15m) AS min_val,
    MAX(ptf.mfe_15m) AS max_val,
    COUNT(ptf.mfe_15m) AS count
FROM post_t0_finalized ptf
JOIN experiment_counts ec ON ec.experiment_id = ptf.experiment_id
WHERE ptf.mfe_15m IS NOT NULL
GROUP BY ptf.experiment_id

UNION ALL

SELECT
    ptf.experiment_id,
    'MAE_15m' AS metric,
    ROUND(AVG(ptf.mae_15m)::numeric, 4) AS mean,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ptf.mae_15m) AS median,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY ptf.mae_15m) AS p25,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY ptf.mae_15m) AS p75,
    PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ptf.mae_15m) AS p90,
    MIN(ptf.mae_15m) AS min_val,
    MAX(ptf.mae_15m) AS max_val,
    COUNT(ptf.mae_15m) AS count
FROM post_t0_finalized ptf
JOIN experiment_counts ec ON ec.experiment_id = ptf.experiment_id
WHERE ptf.mae_15m IS NOT NULL
GROUP BY ptf.experiment_id

UNION ALL

SELECT
    ptf.experiment_id,
    'MFE_60m' AS metric,
    ROUND(AVG(ptf.mfe_60m)::numeric, 4) AS mean,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ptf.mfe_60m) AS median,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY ptf.mfe_60m) AS p25,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY ptf.mfe_60m) AS p75,
    PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ptf.mfe_60m) AS p90,
    MIN(ptf.mfe_60m) AS min_val,
    MAX(ptf.mfe_60m) AS max_val,
    COUNT(ptf.mfe_60m) AS count
FROM post_t0_finalized ptf
JOIN experiment_counts ec ON ec.experiment_id = ptf.experiment_id
WHERE ptf.mfe_60m IS NOT NULL
GROUP BY ptf.experiment_id

UNION ALL

SELECT
    ptf.experiment_id,
    'MAE_60m' AS metric,
    ROUND(AVG(ptf.mae_60m)::numeric, 4) AS mean,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ptf.mae_60m) AS median,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY ptf.mae_60m) AS p25,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY ptf.mae_60m) AS p75,
    PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ptf.mae_60m) AS p90,
    MIN(ptf.mae_60m) AS min_val,
    MAX(ptf.mae_60m) AS max_val,
    COUNT(ptf.mae_60m) AS count
FROM post_t0_finalized ptf
JOIN experiment_counts ec ON ec.experiment_id = ptf.experiment_id
WHERE ptf.mae_60m IS NOT NULL
GROUP BY ptf.experiment_id

UNION ALL

SELECT
    ptf.experiment_id,
    'MFE_240m' AS metric,
    ROUND(AVG(ptf.mfe_240m)::numeric, 4) AS mean,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ptf.mfe_240m) AS median,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY ptf.mfe_240m) AS p25,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY ptf.mfe_240m) AS p75,
    PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ptf.mfe_240m) AS p90,
    MIN(ptf.mfe_240m) AS min_val,
    MAX(ptf.mfe_240m) AS max_val,
    COUNT(ptf.mfe_240m) AS count
FROM post_t0_finalized ptf
JOIN experiment_counts ec ON ec.experiment_id = ptf.experiment_id
WHERE ptf.mfe_240m IS NOT NULL
GROUP BY ptf.experiment_id

UNION ALL

SELECT
    ptf.experiment_id,
    'MAE_240m' AS metric,
    ROUND(AVG(ptf.mae_240m)::numeric, 4) AS mean,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ptf.mae_240m) AS median,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY ptf.mae_240m) AS p25,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY ptf.mae_240m) AS p75,
    PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ptf.mae_240m) AS p90,
    MIN(ptf.mae_240m) AS min_val,
    MAX(ptf.mae_240m) AS max_val,
    COUNT(ptf.mae_240m) AS count
FROM post_t0_finalized ptf
JOIN experiment_counts ec ON ec.experiment_id = ptf.experiment_id
WHERE ptf.mae_240m IS NOT NULL
GROUP BY ptf.experiment_id

ORDER BY experiment_id, metric;

\echo ''

-- ============================================================
-- 14. FAVORABLE EXCURSION PROBABILITIES (N >= 10)
-- ============================================================
\echo '=== 14. FAVORABLE EXCURSION PROBABILITIES (N >= 10) ==='
WITH post_t0_finalized AS (
    SELECT
        po.experiment_id,
        po2.mfe_30m, po2.mfe_60m, po2.mfe_120m, po2.mfe_240m
    FROM research.prospective_outcome po2
    JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
    JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
    WHERE pe.started_at IS NOT NULL
      AND po.signal_time >= pe.started_at
      AND po2.is_final = TRUE
),
experiment_counts AS (
    SELECT experiment_id, COUNT(*) AS n
    FROM post_t0_finalized
    GROUP BY experiment_id
    HAVING COUNT(*) >= 10
)
SELECT
    ptf.experiment_id,
    COUNT(*) AS n,
    -- 30m horizon
    ROUND(COUNT(*) FILTER (WHERE mfe_30m >= 0.25)::numeric / COUNT(*) * 100, 1) AS mfe30_gte_025r,
    ROUND(COUNT(*) FILTER (WHERE mfe_30m >= 0.50)::numeric / COUNT(*) * 100, 1) AS mfe30_gte_050r,
    ROUND(COUNT(*) FILTER (WHERE mfe_30m >= 0.75)::numeric / COUNT(*) * 100, 1) AS mfe30_gte_075r,
    ROUND(COUNT(*) FILTER (WHERE mfe_30m >= 1.00)::numeric / COUNT(*) * 100, 1) AS mfe30_gte_100r,
    ROUND(COUNT(*) FILTER (WHERE mfe_30m >= 1.25)::numeric / COUNT(*) * 100, 1) AS mfe30_gte_125r,
    ROUND(COUNT(*) FILTER (WHERE mfe_30m >= 1.50)::numeric / COUNT(*) * 100, 1) AS mfe30_gte_150r,
    ROUND(COUNT(*) FILTER (WHERE mfe_30m >= 2.00)::numeric / COUNT(*) * 100, 1) AS mfe30_gte_200r,
    -- 60m horizon
    ROUND(COUNT(*) FILTER (WHERE mfe_60m >= 0.25)::numeric / COUNT(*) * 100, 1) AS mfe60_gte_025r,
    ROUND(COUNT(*) FILTER (WHERE mfe_60m >= 0.50)::numeric / COUNT(*) * 100, 1) AS mfe60_gte_050r,
    ROUND(COUNT(*) FILTER (WHERE mfe_60m >= 0.75)::numeric / COUNT(*) * 100, 1) AS mfe60_gte_075r,
    ROUND(COUNT(*) FILTER (WHERE mfe_60m >= 1.00)::numeric / COUNT(*) * 100, 1) AS mfe60_gte_100r,
    ROUND(COUNT(*) FILTER (WHERE mfe_60m >= 1.25)::numeric / COUNT(*) * 100, 1) AS mfe60_gte_125r,
    ROUND(COUNT(*) FILTER (WHERE mfe_60m >= 1.50)::numeric / COUNT(*) * 100, 1) AS mfe60_gte_150r,
    ROUND(COUNT(*) FILTER (WHERE mfe_60m >= 2.00)::numeric / COUNT(*) * 100, 1) AS mfe60_gte_200r,
    -- 120m horizon
    ROUND(COUNT(*) FILTER (WHERE mfe_120m >= 0.25)::numeric / COUNT(*) * 100, 1) AS mfe120_gte_025r,
    ROUND(COUNT(*) FILTER (WHERE mfe_120m >= 0.50)::numeric / COUNT(*) * 100, 1) AS mfe120_gte_050r,
    ROUND(COUNT(*) FILTER (WHERE mfe_120m >= 0.75)::numeric / COUNT(*) * 100, 1) AS mfe120_gte_075r,
    ROUND(COUNT(*) FILTER (WHERE mfe_120m >= 1.00)::numeric / COUNT(*) * 100, 1) AS mfe120_gte_100r,
    ROUND(COUNT(*) FILTER (WHERE mfe_120m >= 1.25)::numeric / COUNT(*) * 100, 1) AS mfe120_gte_125r,
    ROUND(COUNT(*) FILTER (WHERE mfe_120m >= 1.50)::numeric / COUNT(*) * 100, 1) AS mfe120_gte_150r,
    ROUND(COUNT(*) FILTER (WHERE mfe_120m >= 2.00)::numeric / COUNT(*) * 100, 1) AS mfe120_gte_200r,
    -- 240m horizon
    ROUND(COUNT(*) FILTER (WHERE mfe_240m >= 0.25)::numeric / COUNT(*) * 100, 1) AS mfe240_gte_025r,
    ROUND(COUNT(*) FILTER (WHERE mfe_240m >= 0.50)::numeric / COUNT(*) * 100, 1) AS mfe240_gte_050r,
    ROUND(COUNT(*) FILTER (WHERE mfe_240m >= 0.75)::numeric / COUNT(*) * 100, 1) AS mfe240_gte_075r,
    ROUND(COUNT(*) FILTER (WHERE mfe_240m >= 1.00)::numeric / COUNT(*) * 100, 1) AS mfe240_gte_100r,
    ROUND(COUNT(*) FILTER (WHERE mfe_240m >= 1.25)::numeric / COUNT(*) * 100, 1) AS mfe240_gte_125r,
    ROUND(COUNT(*) FILTER (WHERE mfe_240m >= 1.50)::numeric / COUNT(*) * 100, 1) AS mfe240_gte_150r,
    ROUND(COUNT(*) FILTER (WHERE mfe_240m >= 2.00)::numeric / COUNT(*) * 100, 1) AS mfe240_gte_200r
FROM post_t0_finalized ptf
JOIN experiment_counts ec ON ec.experiment_id = ptf.experiment_id
GROUP BY ptf.experiment_id
ORDER BY ptf.experiment_id;

\echo ''

-- ============================================================
-- 15. GIVE-BACK ANALYSIS (N >= 10)
-- ============================================================
\echo '=== 15. GIVE-BACK ANALYSIS (N >= 10) ==='
WITH post_t0_finalized AS (
    SELECT
        po.experiment_id,
        po2.observation_id,
        po2.sl_hit,
        po2.mfe_15m, po2.mfe_30m, po2.mfe_60m, po2.mfe_120m, po2.mfe_240m
    FROM research.prospective_outcome po2
    JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
    JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
    WHERE pe.started_at IS NOT NULL
      AND po.signal_time >= pe.started_at
      AND po2.is_final = TRUE
),
experiment_sl AS (
    SELECT
        experiment_id,
        COUNT(*) AS n_total,
        COUNT(*) FILTER (WHERE sl_hit) AS n_sl,
        COUNT(*) FILTER (WHERE sl_hit AND mfe_30m >= 0.5) AS sl_after_mfe30_05r,
        COUNT(*) FILTER (WHERE sl_hit AND mfe_30m >= 1.0) AS sl_after_mfe30_10r,
        COUNT(*) FILTER (WHERE sl_hit AND mfe_30m >= 1.5) AS sl_after_mfe30_15r,
        COUNT(*) FILTER (WHERE sl_hit AND mfe_60m >= 0.5) AS sl_after_mfe60_05r,
        COUNT(*) FILTER (WHERE sl_hit AND mfe_60m >= 1.0) AS sl_after_mfe60_10r,
        COUNT(*) FILTER (WHERE sl_hit AND mfe_60m >= 1.5) AS sl_after_mfe60_15r,
        COUNT(*) FILTER (WHERE sl_hit AND mfe_120m >= 0.5) AS sl_after_mfe120_05r,
        COUNT(*) FILTER (WHERE sl_hit AND mfe_120m >= 1.0) AS sl_after_mfe120_10r,
        COUNT(*) FILTER (WHERE sl_hit AND mfe_120m >= 1.5) AS sl_after_mfe120_15r
    FROM post_t0_finalized
    GROUP BY experiment_id
    HAVING COUNT(*) >= 10
)
SELECT
    experiment_id,
    n_total,
    n_sl,
    ROUND(n_sl::numeric / n_total * 100, 1) AS sl_pct,
    -- 30m giveback
    sl_after_mfe30_05r,
    ROUND(sl_after_mfe30_05r::numeric / NULLIF(n_sl, 0) * 100, 1) AS giveback30_05r_pct,
    sl_after_mfe30_10r,
    ROUND(sl_after_mfe30_10r::numeric / NULLIF(n_sl, 0) * 100, 1) AS giveback30_10r_pct,
    sl_after_mfe30_15r,
    ROUND(sl_after_mfe30_15r::numeric / NULLIF(n_sl, 0) * 100, 1) AS giveback30_15r_pct,
    -- 60m giveback
    sl_after_mfe60_05r,
    ROUND(sl_after_mfe60_05r::numeric / NULLIF(n_sl, 0) * 100, 1) AS giveback60_05r_pct,
    sl_after_mfe60_10r,
    ROUND(sl_after_mfe60_10r::numeric / NULLIF(n_sl, 0) * 100, 1) AS giveback60_10r_pct,
    sl_after_mfe60_15r,
    ROUND(sl_after_mfe60_15r::numeric / NULLIF(n_sl, 0) * 100, 1) AS giveback60_15r_pct,
    -- 120m giveback
    sl_after_mfe120_05r,
    ROUND(sl_after_mfe120_05r::numeric / NULLIF(n_sl, 0) * 100, 1) AS giveback120_05r_pct,
    sl_after_mfe120_10r,
    ROUND(sl_after_mfe120_10r::numeric / NULLIF(n_sl, 0) * 100, 1) AS giveback120_10r_pct,
    sl_after_mfe120_15r,
    ROUND(sl_after_mfe120_15r::numeric / NULLIF(n_sl, 0) * 100, 1) AS giveback120_15r_pct
FROM experiment_sl
ORDER BY experiment_id;

\echo ''

-- ============================================================
-- 16. COUNTERFACTUAL EXITS — TP THRESHOLDS (N >= 10)
-- ============================================================
\echo '=== 16. COUNTERFACTUAL EXITS — TP THRESHOLDS (N >= 10) ==='
\echo 'NOTE: True counterfactual requires candle-level reconstruction.'
\echo 'This section shows current TP/SL hit rates as baseline.'
\echo ''

WITH post_t0_finalized AS (
    SELECT
        po.experiment_id,
        po2.observation_id,
        po2.tp_hit, po2.sl_hit,
        po2.tp_before_sl, po2.sl_before_tp,
        po2.ambiguous_intrabar,
        po2.mfe_60m, po2.mae_60m
    FROM research.prospective_outcome po2
    JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
    JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
    WHERE pe.started_at IS NOT NULL
      AND po.signal_time >= pe.started_at
      AND po2.is_final = TRUE
)
SELECT
    experiment_id,
    COUNT(*) AS n,
    -- Current exit results
    COUNT(*) FILTER (WHERE tp_hit) AS tp_hit,
    COUNT(*) FILTER (WHERE sl_hit) AS sl_hit,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_before_sl,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_before_tp,
    COUNT(*) FILTER (WHERE ambiguous_intrabar) AS ambiguous,
    -- Win rate excluding ambiguous
    ROUND(COUNT(*) FILTER (WHERE tp_before_sl)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE tp_before_sl) + COUNT(*) FILTER (WHERE sl_before_tp), 0) * 100, 1) AS win_rate_excl_ambiguous,
    -- Conservative (ambiguous = SL)
    ROUND(COUNT(*) FILTER (WHERE tp_before_sl)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE tp_before_sl) + COUNT(*) FILTER (WHERE sl_before_tp) + COUNT(*) FILTER (WHERE ambiguous_intrabar), 0) * 100, 1) AS win_rate_conservative
FROM post_t0_finalized
GROUP BY experiment_id
HAVING COUNT(*) >= 10
ORDER BY experiment_id;

\echo ''

-- ============================================================
-- 17. MARKET REGIME SEGMENTATION (N >= 10)
-- ============================================================
\echo '=== 17. MARKET REGIME SEGMENTATION (N >= 10) ==='
WITH post_t0_finalized AS (
    SELECT
        po.experiment_id,
        po.market_regime,
        po2.mfe_60m
    FROM research.prospective_outcome po2
    JOIN research.prospective_observation po ON po.observation_id = po2.observation_id
    JOIN research.prospective_experiment pe ON pe.experiment_id = po.experiment_id
    WHERE pe.started_at IS NOT NULL
      AND po.signal_time >= pe.started_at
      AND po2.is_final = TRUE
),
experiment_regime AS (
    SELECT
        experiment_id,
        market_regime,
        COUNT(*) AS n,
        ROUND(AVG(mfe_60m)::numeric, 4) AS avg_mfe_60m
    FROM post_t0_finalized
    GROUP BY experiment_id, market_regime
    HAVING COUNT(*) >= 5
)
SELECT
    experiment_id,
    market_regime,
    n,
    avg_mfe_60m
FROM experiment_regime
ORDER BY experiment_id, n DESC;

\echo ''

-- ============================================================
-- 18. HYPOTHESIS / EXPERIMENT CONTEXT
-- ============================================================
\echo '=== 18. HYPOTHESIS / EXPERIMENT CONTEXT ==='
SELECT
    experiment_id,
    scanner_name,
    direction,
    experiment_type,
    hypothesis,
    filter_rule,
    entry_rule,
    stop_rule,
    target_rule,
    position_sizing,
    fee_assumption,
    minimum_n,
    minimum_symbols,
    discovery_source,
    paired_with
FROM research.prospective_experiment
ORDER BY experiment_id;

\echo ''

-- ============================================================
-- 19. ACCUMULATION VIEW
-- ============================================================
\echo '=== 19. ACCUMULATION VIEW ==='
SELECT * FROM research.v_prospective_accumulation ORDER BY experiment_id;

\echo ''

\echo '============================================================='
\echo '  ANALYSIS COMPLETE — ' || NOW()::text
\echo '============================================================='
