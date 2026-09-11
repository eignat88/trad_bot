-- Migration 013: Fix analytics_quality_failures to show latest state only
--
-- Problem:
-- analytics.data_quality_result keeps historical results for every retry.
-- The old mart.analytics_quality_failures returned ALL rows where
-- status='FAIL' AND severity='BLOCKING', including stale failures from
-- earlier retry attempts. This caused Grafana to show false incidents.
--
-- Solution:
-- Rewrite mart.analytics_quality_failures using a window function to
-- select only the MOST RECENT result per (run_id, stage_name, check_name),
-- then filter for current BLOCKING failures.
--
-- Historical quality data remains in analytics.data_quality_result.
--
-- Also re-creates mart.analytics_quality_summary with a comment clarifying
-- it is intentionally historical (for audit trail).
--
-- Idempotent: CREATE OR REPLACE VIEW.

-- 9. Quality Gate Failures — CURRENT / LATEST state only
CREATE OR REPLACE VIEW mart.analytics_quality_failures AS
WITH latest_results AS (
    SELECT
        dqr.run_id,
        dqr.stage_name,
        dqr.check_name,
        dqr.severity,
        dqr.status,
        dqr.affected_entity_count,
        dqr.checked_at,
        dqr.details,
        dqr.quality_result_id,
        ROW_NUMBER() OVER (
            PARTITION BY dqr.run_id, dqr.stage_name, dqr.check_name
            ORDER BY dqr.checked_at DESC, dqr.quality_result_id DESC
        ) AS rn
    FROM analytics.data_quality_result dqr
)
SELECT
    ar.business_date,
    ar.pipeline_version,
    lr.stage_name,
    lr.check_name,
    lr.severity,
    lr.status,
    lr.affected_entity_count,
    lr.checked_at,
    lr.details
FROM latest_results lr
JOIN analytics.analysis_run ar ON lr.run_id = ar.run_id
WHERE lr.rn = 1
  AND lr.severity = 'BLOCKING'
  AND lr.status = 'FAIL'
ORDER BY ar.business_date DESC, lr.checked_at;

-- 3. Quality Gate Summary — intentionally HISTORICAL (audit trail)
-- Re-create with explicit documentation comment.
CREATE OR REPLACE VIEW mart.analytics_quality_summary AS
-- This view is intentionally HISTORICAL: it shows all quality check results
-- across all retry attempts for audit and debugging purposes.
-- For current-state dashboards, use mart.analytics_quality_failures instead.
SELECT
    ar.business_date,
    ar.pipeline_version,
    dqr.stage_name,
    dqr.check_name,
    dqr.severity,
    dqr.status,
    dqr.affected_entity_count,
    dqr.checked_at,
    dqr.details
FROM analytics.data_quality_result dqr
JOIN analytics.analysis_run ar ON dqr.run_id = ar.run_id
ORDER BY ar.business_date DESC, dqr.checked_at;

-- 8. Failed Runs Summary — intentionally HISTORICAL
-- Shows ALL runs that ever failed, for audit trail.
-- Current-state dashboard should use analytics_pipeline_health.
-- Re-create with explicit documentation comment.
CREATE OR REPLACE VIEW mart.analytics_failed_runs AS
-- This view is intentionally HISTORICAL: it shows all runs that ever failed,
-- including retries that subsequently succeeded.
-- For current-state dashboard, use mart.analytics_pipeline_health instead.
SELECT
    business_date,
    maturity,
    status,
    error_code,
    error_message,
    started_at,
    finished_at,
    EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600 AS age_hours
FROM analytics.analysis_run
WHERE status = 'FAILED' OR maturity = 'FAILED'
ORDER BY business_date DESC;