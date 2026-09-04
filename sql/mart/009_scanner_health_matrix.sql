-- 009_scanner_health_matrix.sql
-- Scanner Health Matrix: per-scanner status from latest run.
-- Panels: Scanner Health Matrix, SCANNERS HEALTHY card, Candidates pipeline.

-- =============================================
-- 1. scanner_health_matrix — Main Table
-- =============================================
CREATE OR REPLACE VIEW mart.scanner_health_matrix AS
WITH latest_run AS (
    SELECT run_id, symbols_total,
           EXTRACT(EPOCH FROM (NOW() - finished_at)) / 60 AS age_minutes
    FROM dds.scanner_run
    WHERE status = 'COMPLETED'
    ORDER BY finished_at DESC
    LIMIT 1
),
per_scanner AS (
    SELECT
        srs.scanner_name,
        srs.symbols_scanned,
        srs.candidates_found,
        srs.setups_saved,
        srs.errors_count,
        ROUND(srs.duration_ms, 1) AS duration_ms,
        lr.age_minutes,
        lr.symbols_total
    FROM dds.scanner_run_stat srs
    JOIN latest_run lr ON srs.run_id = (SELECT run_id FROM dds.scanner_run ORDER BY started_at DESC LIMIT 1)
)
SELECT
    scanner_name,
    CASE
        WHEN age_minutes > 12                          THEN 'STALE'
        WHEN errors_count > 0                           THEN 'ERROR'
        WHEN symbols_scanned < symbols_total            THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    ROUND(age_minutes, 0)::INT || 'm ago'              AS last_seen,
    symbols_scanned || '/' || symbols_total             AS coverage,
    errors_count                                        AS errors,
    duration_ms,
    candidates_found                                    AS candidates,
    setups_saved                                        AS setups
FROM per_scanner
ORDER BY scanner_name;

COMMENT ON VIEW mart.scanner_health_matrix IS 'Per-scanner health from latest run: status, coverage, errors, duration.';

-- =============================================
-- 2. scanner_direction_availability — Direction Gate Panel
-- =============================================
CREATE OR REPLACE VIEW mart.scanner_direction_availability AS
WITH current_regime AS (
    SELECT market_regime
    FROM dds.scanner_setup
    WHERE market_regime IS NOT NULL
    ORDER BY detected_at DESC
    LIMIT 1
)
SELECT
    gate.scanner_name,
    gate.direction,
    gate.status,
    gate.allowed_regimes,
    gate.reason,
    gate.source,
    gate.updated_at,
    current_regime.market_regime AS current_market_regime,
    CASE
        WHEN gate.status = 'REGIME'
             AND current_regime.market_regime = ANY(gate.allowed_regimes) THEN 'ENABLED'
        WHEN gate.status = 'REGIME' THEN 'BLOCKED_BY_REGIME'
        ELSE gate.status
    END AS effective_status
FROM config.scanner_direction_gate gate
LEFT JOIN current_regime ON TRUE
ORDER BY gate.scanner_name, gate.direction;

CREATE OR REPLACE VIEW mart.scanner_direction_status AS
SELECT
    COALESCE(avail.scanner_name, run_stats.scanner_name) AS scanner_name,
    CASE
        WHEN avail.scanner_name IS NULL                THEN 'CONFIG_MISSING'
        WHEN avail_long.status IS NULL                 THEN 'CONFIG_MISSING'
        WHEN avail_long.effective_status = 'ENABLED'   THEN 'ENABLED'
        WHEN avail_long.effective_status = 'BLOCKED_BY_REGIME' THEN 'REGIME'
        WHEN avail_long.status = 'BLOCKED'             THEN 'BLOCKED'
        ELSE avail_long.effective_status
    END AS long_status,
    CASE
        WHEN avail.scanner_name IS NULL                THEN 'CONFIG_MISSING'
        WHEN avail_short.status IS NULL                THEN 'CONFIG_MISSING'
        WHEN avail_short.effective_status = 'ENABLED'  THEN 'ENABLED'
        WHEN avail_short.effective_status = 'BLOCKED_BY_REGIME' THEN 'REGIME'
        WHEN avail_short.status = 'BLOCKED'            THEN 'BLOCKED'
        ELSE avail_short.effective_status
    END AS short_status
FROM (
    SELECT DISTINCT scanner_name
    FROM dds.scanner_run_stat
) run_stats
LEFT JOIN (
    SELECT DISTINCT scanner_name
    FROM mart.scanner_direction_availability
) avail ON avail.scanner_name = run_stats.scanner_name
LEFT JOIN mart.scanner_direction_availability avail_long
    ON avail_long.scanner_name = run_stats.scanner_name AND avail_long.direction = 'LONG'
LEFT JOIN mart.scanner_direction_availability avail_short
    ON avail_short.scanner_name = run_stats.scanner_name AND avail_short.direction = 'SHORT'
ORDER BY run_stats.scanner_name;

COMMENT ON VIEW mart.scanner_direction_availability IS
    'Runtime scanner/direction gate, source, reason, and effective status.';

-- =============================================
-- 3. scanner_candidates_pipeline — Pipeline
-- =============================================
CREATE OR REPLACE VIEW mart.scanner_candidates_pipeline AS
WITH latest_run AS (
    SELECT run_id
    FROM dds.scanner_run
    WHERE status = 'COMPLETED'
    ORDER BY finished_at DESC
    LIMIT 1
)
SELECT
    srs.scanner_name,
    srs.symbols_scanned                                    AS symbols,
    srs.candidates_found                                   AS candidates,
    srs.setups_saved                                       AS setups,
    CASE
        WHEN srs.candidates_found > 0
        THEN ROUND(100.0 * srs.setups_saved / srs.candidates_found, 1)
        ELSE 0
    END                                                    AS conversion_pct
FROM dds.scanner_run_stat srs
JOIN latest_run lr ON srs.run_id = lr.run_id
ORDER BY srs.scanner_name;

COMMENT ON VIEW mart.scanner_candidates_pipeline IS 'Candidates to Setups pipeline for latest run.';
