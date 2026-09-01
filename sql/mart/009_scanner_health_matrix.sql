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
-- 2. scanner_direction_status — Direction Panel
-- =============================================
CREATE OR REPLACE VIEW mart.scanner_direction_status AS
SELECT
    scanners.scanner_name,
    CASE
        WHEN sdc_long.scanner_name IS NULL     THEN 'ENABLED'   -- нет записи = не заблокирован
        WHEN sdc_long.enabled                   THEN 'ENABLED'
        WHEN sdc_long.block_reason = 'regime_filter' THEN 'REGIME'
        ELSE 'BLOCKED'
    END AS long_status,
    CASE
        WHEN sdc_short.scanner_name IS NULL    THEN 'ENABLED'   -- нет записи = не заблокирован
        WHEN sdc_short.enabled                  THEN 'ENABLED'
        WHEN sdc_short.block_reason = 'regime_filter' THEN 'REGIME'
        ELSE 'BLOCKED'
    END AS short_status
FROM (
    SELECT DISTINCT scanner_name
    FROM dds.scanner_run_stat
) scanners
LEFT JOIN dds.scanner_direction_config sdc_long
    ON sdc_long.scanner_name = scanners.scanner_name AND sdc_long.direction = 'LONG'
LEFT JOIN dds.scanner_direction_config sdc_short
    ON sdc_short.scanner_name = scanners.scanner_name AND sdc_short.direction = 'SHORT'
ORDER BY scanners.scanner_name;

COMMENT ON VIEW mart.scanner_direction_status IS 'Per-scanner direction availability: ENABLED/BLOCKED/REGIME.';

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
