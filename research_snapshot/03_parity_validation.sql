-- ============================================================
-- PARITY VALIDATION — Research Snapshot
-- ============================================================
-- Run after restore to verify snapshot integrity:
--   psql -U postgres -d trad_bot_research_snapshot -f 03_parity_validation.sql
--
-- Compare results against export_metadata.txt from VPS export.
-- Minor count differences are expected if production was writing
-- during the export window.  The authoritative snapshot is the
-- full dump file; counts below are a sanity check, not a strict
-- gate.
-- ============================================================

-- ── 0. Snapshot header ────────────────────────────────────────
\echo '============================================='
\echo '  RESEARCH SNAPSHOT PARITY VALIDATION'
\echo '============================================='
\echo ''

-- ── 1. Table row counts ───────────────────────────────────────
\echo '=== 1. TABLE ROW COUNTS ==='
SELECT 'research_experiment'  AS table_name, COUNT(*) AS row_count
FROM research.research_experiment
UNION ALL
SELECT 'research_observation', COUNT(*) FROM research.research_observation
UNION ALL
SELECT 'research_signal',      COUNT(*) FROM research.research_signal
UNION ALL
SELECT 'research_outcome',     COUNT(*) FROM research.research_outcome;

-- ── 2. Time range ─────────────────────────────────────────────
\echo ''
\echo '=== 2. SIGNAL TIME RANGE ==='
SELECT
    MIN(signal_time)::text AS earliest_signal,
    MAX(signal_time)::text AS latest_signal,
    COUNT(DISTINCT signal_time::date) AS distinct_days
FROM research.research_signal;

-- ── 3. Unique symbols ─────────────────────────────────────────
\echo ''
\echo '=== 3. UNIQUE SYMBOLS ==='
SELECT
    COUNT(DISTINCT symbol) AS unique_symbols
FROM research.research_observation;

-- ── 4. Per-experiment breakdown ────────────────────────────────
\echo ''
\echo '=== 4. PER-EXPERIMENT COUNTS ==='
SELECT
    e.experiment_id,
    e.scanner_name,
    e.status,
    COUNT(DISTINCT o.observation_id) AS observations,
    COUNT(DISTINCT s.signal_id)      AS signals,
    COUNT(DISTINCT ro.signal_id)     AS outcomes
FROM research.research_experiment e
LEFT JOIN research.research_observation o ON o.experiment_id = e.experiment_id
LEFT JOIN research.research_signal s      ON s.experiment_id = e.experiment_id
LEFT JOIN research.research_outcome ro    ON ro.experiment_id = e.experiment_id
GROUP BY e.experiment_id, e.scanner_name, e.status
ORDER BY e.experiment_id;

-- ── 5. Orphan / integrity checks ──────────────────────────────
\echo ''
\echo '=== 5. ORPHAN / INTEGRITY CHECKS ==='

\echo '--- Signals without matching observation ---'
SELECT COUNT(*) AS orphan_signals
FROM research.research_signal s
WHERE NOT EXISTS (
    SELECT 1 FROM research.research_observation o
    WHERE o.observation_id = s.observation_id
);

\echo '--- Outcomes without matching signal ---'
SELECT COUNT(*) AS orphan_outcomes
FROM research.research_outcome ro
WHERE NOT EXISTS (
    SELECT 1 FROM research.research_signal s
    WHERE s.signal_id = ro.signal_id
);

\echo '--- NULL/empty experiment_id in observations ---'
SELECT COUNT(*) AS null_exp_in_obs
FROM research.research_observation
WHERE experiment_id IS NULL OR experiment_id = '';

\echo '--- NULL/empty experiment_id in outcomes ---'
SELECT COUNT(*) AS null_exp_in_outcomes
FROM research.research_outcome
WHERE experiment_id IS NULL OR experiment_id = '';

-- ── 6. Duplicate checks ───────────────────────────────────────
\echo ''
\echo '=== 6. DUPLICATE CHECKS ==='

\echo '--- Duplicate signals ---'
SELECT experiment_id, symbol, direction, signal_candle_open_time, COUNT(*) AS cnt
FROM research.research_signal
WHERE signal_candle_open_time > 0
GROUP BY experiment_id, symbol, direction, signal_candle_open_time
HAVING COUNT(*) > 1;

\echo '--- Duplicate outcomes ---'
SELECT signal_id, COUNT(*) AS cnt
FROM research.research_outcome
GROUP BY signal_id
HAVING COUNT(*) > 1;

-- ── 7. Feature completeness ───────────────────────────────────
\echo ''
\echo '=== 7. FEATURE COMPLETENESS ==='

SELECT
    experiment_id,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE features = '{}'::jsonb OR features IS NULL) AS empty_features,
    ROUND(
        COUNT(*) FILTER (WHERE features = '{}'::jsonb OR features IS NULL)::numeric
        / NULLIF(COUNT(*), 0) * 100,
        1
    ) AS empty_pct
FROM research.research_observation
GROUP BY experiment_id
ORDER BY experiment_id;

-- ── 8. Feature keys inventory ─────────────────────────────────
\echo ''
\echo '=== 8. FEATURE KEYS PER EXPERIMENT ==='
SELECT
    experiment_id,
    jsonb_object_keys(features) AS feature_key,
    COUNT(*) AS cnt
FROM research.research_observation
WHERE features != '{}'::jsonb AND features IS NOT NULL
GROUP BY experiment_id, jsonb_object_keys(features)
ORDER BY experiment_id, cnt DESC;

-- ── 9. Outcome horizon coverage ───────────────────────────────
\echo ''
\echo '=== 9. OUTCOME HORIZON COVERAGE ==='
SELECT
    experiment_id,
    COUNT(*)                            AS total_outcomes,
    COUNT(mfe_15m)                      AS has_15m,
    COUNT(mfe_30m)                      AS has_30m,
    COUNT(mfe_60m)                      AS has_60m,
    COUNT(mfe_120m)                     AS has_120m,
    COUNT(mfe_240m)                     AS has_240m,
    COUNT(*) FILTER (WHERE is_final)    AS finalized,
    COUNT(*) FILTER (WHERE NOT is_final) AS pending
FROM research.research_outcome
GROUP BY experiment_id
ORDER BY experiment_id;

-- ── 10. Direction breakdown ───────────────────────────────────
\echo ''
\echo '=== 10. DIRECTION BREAKDOWN ==='
SELECT
    experiment_id,
    direction,
    COUNT(*) AS observations,
    COUNT(*) FILTER (WHERE status = 'SETUP_READY') AS setup_ready
FROM research.research_observation
GROUP BY experiment_id, direction
ORDER BY experiment_id, direction;

-- ── 11. Summary ───────────────────────────────────────────────
\echo ''
\echo '=== 11. SNAPSHOT SUMMARY ==='
SELECT
    (SELECT COUNT(*) FROM research.research_experiment)              AS experiments,
    (SELECT COUNT(*) FROM research.research_observation)             AS observations,
    (SELECT COUNT(*) FROM research.research_signal)                  AS signals,
    (SELECT COUNT(*) FROM research.research_outcome)                 AS outcomes,
    (SELECT MIN(signal_time) FROM research.research_signal)          AS earliest_signal,
    (SELECT MAX(signal_time) FROM research.research_signal)          AS latest_signal,
    (SELECT COUNT(DISTINCT symbol) FROM research.research_observation)  AS unique_symbols,
    (SELECT COUNT(DISTINCT signal_time::date) FROM research.research_signal) AS unique_days;

\echo ''
\echo '=== VALIDATION COMPLETE ==='
\echo 'Compare counts above with export_metadata.txt.'
\echo 'Minor differences are expected if production was writing during export.'
