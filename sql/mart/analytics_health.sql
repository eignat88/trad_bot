-- Analytics Health Mart for Grafana
-- Provides metrics for monitoring analytics pipeline health

-- 1. Analysis Run Summary
CREATE OR REPLACE VIEW mart.analytics_run_summary AS
SELECT 
    ar.run_id,
    ar.business_date,
    ar.maturity,
    ar.status,
    ar.pipeline_version,
    ar.analysis_from,
    ar.analysis_to,
    ar.started_at,
    ar.finished_at,
    EXTRACT(EPOCH FROM (ar.finished_at - ar.started_at)) AS duration_seconds,
    ar.error_code,
    ar.error_message,
    ar.created_at,
    ar.updated_at,
    -- Age of the run
    EXTRACT(EPOCH FROM (NOW() - ar.created_at)) / 3600 AS age_hours
FROM analytics.analysis_run ar
ORDER BY ar.business_date DESC, ar.created_at DESC;

-- 2. Stage Performance
CREATE OR REPLACE VIEW mart.analytics_stage_performance AS
SELECT 
    ar.business_date,
    ar.pipeline_version,
    asr.stage_name,
    asr.attempt,
    asr.status,
    asr.input_rows,
    asr.output_rows,
    EXTRACT(EPOCH FROM (asr.finished_at - asr.started_at)) AS duration_seconds,
    asr.error_code,
    asr.error_message,
    asr.started_at,
    asr.finished_at
FROM analytics.analysis_stage_run asr
JOIN analytics.analysis_run ar ON asr.run_id = ar.run_id
ORDER BY ar.business_date DESC, asr.started_at;

-- 3. Quality Gate Summary
CREATE OR REPLACE VIEW mart.analytics_quality_summary AS
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

-- 4. Candle Statistics
CREATE OR REPLACE VIEW mart.analytics_candle_stats AS
SELECT 
    exchange,
    market_type,
    instrument_id,
    timeframe,
    COUNT(*) AS total_candles,
    MIN(open_time) AS earliest_candle,
    MAX(open_time) AS latest_candle,
    EXTRACT(EPOCH FROM (MAX(open_time) - MIN(open_time))) / 3600 AS coverage_hours,
    COUNT(DISTINCT DATE_TRUNC('day', open_time)) AS coverage_days,
    -- Age of latest candle
    EXTRACT(EPOCH FROM (NOW() - MAX(open_time))) / 3600 AS candle_age_hours
FROM market.candle
WHERE is_closed = TRUE
GROUP BY exchange, market_type, instrument_id, timeframe;

-- 5. Pipeline Health Metrics
CREATE OR REPLACE VIEW mart.analytics_pipeline_health AS
WITH latest_runs AS (
    SELECT 
        business_date,
        maturity,
        status,
        started_at,
        finished_at,
        error_code,
        ROW_NUMBER() OVER (PARTITION BY business_date ORDER BY created_at DESC) as rn
    FROM analytics.analysis_run
),
daily_stats AS (
    SELECT 
        business_date,
        MAX(CASE WHEN rn = 1 THEN maturity END) AS latest_maturity,
        MAX(CASE WHEN rn = 1 THEN status END) AS latest_status,
        MAX(CASE WHEN rn = 1 THEN started_at END) AS latest_started,
        MAX(CASE WHEN rn = 1 THEN finished_at END) AS latest_finished,
        MAX(CASE WHEN rn = 1 THEN error_code END) AS latest_error,
        COUNT(*) AS total_runs
    FROM latest_runs
    GROUP BY business_date
)
SELECT 
    business_date,
    latest_maturity,
    latest_status,
    latest_started,
    latest_finished,
    EXTRACT(EPOCH FROM (latest_finished - latest_started)) AS duration_seconds,
    latest_error,
    total_runs,
    -- Age of report
    EXTRACT(EPOCH FROM (NOW() - business_date::timestamp)) / 86400 AS age_days,
    -- Is this the latest run?
    CASE WHEN business_date = MAX(business_date) OVER () THEN TRUE ELSE FALSE END AS is_latest
FROM daily_stats
ORDER BY business_date DESC;

-- 6. Unresolved Gaps Summary
-- This view shows gaps for all instruments and timeframes
-- It uses a lateral join to call the function for each instrument/timeframe
CREATE OR REPLACE VIEW mart.analytics_unresolved_gaps AS
WITH instrument_timeframes AS (
    -- Get distinct instrument_id and timeframe combinations from recent candles
    SELECT DISTINCT 
        instrument_id, 
        timeframe
    FROM market.candle
    WHERE open_time >= NOW() - INTERVAL '7 days'
      AND is_closed = TRUE
)
SELECT 
    it.instrument_id,
    it.timeframe,
    COUNT(*) AS gap_count,
    SUM(EXTRACT(EPOCH FROM (g.gap_end - g.gap_start)) / 60) AS total_gap_minutes,
    MIN(g.gap_start) AS earliest_gap,
    MAX(g.gap_end) AS latest_gap
FROM instrument_timeframes it
CROSS JOIN LATERAL market.check_candle_coverage(
    it.instrument_id,
    it.timeframe,
    NOW() - INTERVAL '7 days',
    NOW()
) g
GROUP BY it.instrument_id, it.timeframe
HAVING COUNT(*) > 0;

-- 7. Key Performance Indicators
CREATE OR REPLACE VIEW mart.analytics_kpis AS
WITH current_run AS (
    SELECT 
        business_date,
        maturity,
        status,
        started_at,
        finished_at
    FROM analytics.analysis_run
    WHERE business_date = CURRENT_DATE
    ORDER BY created_at DESC
    LIMIT 1
),
previous_run AS (
    SELECT 
        business_date,
        maturity,
        status,
        started_at,
        finished_at
    FROM analytics.analysis_run
    WHERE business_date = CURRENT_DATE - INTERVAL '1 day'
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT 
    CURRENT_DATE AS report_date,
    -- Current run status
    cr.maturity AS current_maturity,
    cr.status AS current_status,
    EXTRACT(EPOCH FROM (cr.finished_at - cr.started_at)) AS current_duration_seconds,
    -- Previous run for comparison
    pr.maturity AS previous_maturity,
    pr.status AS previous_status,
    EXTRACT(EPOCH FROM (pr.finished_at - pr.started_at)) AS previous_duration_seconds,
    -- Trend
    CASE 
        WHEN cr.finished_at IS NULL THEN 'PENDING'
        WHEN pr.finished_at IS NULL THEN 'FIRST_RUN'
        WHEN EXTRACT(EPOCH FROM (cr.finished_at - cr.started_at)) < 
             EXTRACT(EPOCH FROM (pr.finished_at - pr.started_at)) THEN 'IMPROVED'
        ELSE 'DEGRADED'
    END AS performance_trend
FROM current_run cr
CROSS JOIN previous_run pr;

-- 8. Failed Runs Summary
CREATE OR REPLACE VIEW mart.analytics_failed_runs AS
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

-- 9. Quality Gate Failures
CREATE OR REPLACE VIEW mart.analytics_quality_failures AS
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
WHERE dqr.status = 'FAIL' AND dqr.severity = 'BLOCKING'
ORDER BY ar.business_date DESC, dqr.checked_at;

-- 10. Candle Coverage by Instrument
CREATE OR REPLACE VIEW mart.analytics_candle_coverage AS
SELECT 
    instrument_id,
    timeframe,
    COUNT(*) AS total_candles,
    MIN(open_time) AS earliest_candle,
    MAX(open_time) AS latest_candle,
    EXTRACT(EPOCH FROM (MAX(open_time) - MIN(open_time))) / 3600 AS coverage_hours,
    COUNT(DISTINCT DATE_TRUNC('day', open_time)) AS coverage_days,
    -- Calculate coverage percentage for last 7 days
    CASE 
        WHEN COUNT(*) = 0 THEN 0
        ELSE ROUND(
            (COUNT(*) * 100.0 / 
             (EXTRACT(EPOCH FROM (MAX(open_time) - MIN(open_time))) / 300 + 1)),
            2
        )
    END AS coverage_percentage
FROM market.candle
WHERE is_closed = TRUE
AND open_time >= NOW() - INTERVAL '7 days'
GROUP BY instrument_id, timeframe
ORDER BY instrument_id, timeframe;