-- 045_scanner_candidates_pipeline_time_range.sql
-- Fix: scanner_candidates_pipeline now returns all completed runs,
-- enabling Grafana time-range filtering.
-- Also creates mart.scanner_candidates_latest for latest-run diagnostics.
--
-- Idempotent: safe to run multiple times.

CREATE OR REPLACE VIEW mart.scanner_candidates_pipeline AS
SELECT
    sr.run_id,
    sr.finished_at,
    srs.scanner_name,
    srs.symbols_scanned                                    AS symbols,
    srs.candidates_found                                   AS candidates,
    srs.setups_saved                                       AS setups
FROM dds.scanner_run_stat srs
JOIN dds.scanner_run sr
    ON sr.run_id = srs.run_id
WHERE sr.status = 'COMPLETED'
  AND config.is_scanner_visible(srs.scanner_name);

COMMENT ON VIEW mart.scanner_candidates_pipeline IS
    'Candidates to Setups pipeline: one row per completed run per scanner. Use $__timeFilter(finished_at) for time-range filtering in Grafana.';

CREATE OR REPLACE VIEW mart.scanner_candidates_latest AS
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
WHERE config.is_scanner_visible(srs.scanner_name)
ORDER BY srs.scanner_name;

COMMENT ON VIEW mart.scanner_candidates_latest IS
    'Candidates to Setups pipeline for latest COMPLETED run only (diagnostic).';
