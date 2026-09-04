-- 008_scanner_health.sql
-- Operational health: last run, last signal, error counts, run durations.
-- Single-row view for System Health dashboard.
-- Updated: added healthy_count, total_count, unhealthy_count for SCANNERS HEALTHY card.

CREATE OR REPLACE VIEW mart.scanner_health AS
WITH latest_run AS (
    SELECT
        created_at              AS last_scanner_run,
        finished_at             AS last_successful_run,
        status                  AS last_run_status,
        duration_sec            AS last_run_duration_sec,
        error_count             AS last_run_error_count
    FROM dds.scanner_run
    ORDER BY created_at DESC
    LIMIT 1
),
latest_signal AS (
    SELECT MAX(created_at) AS last_signal
    FROM dds.market_signal
),
latest_error AS (
    SELECT MAX(created_at) AS last_error
    FROM dds.scanner_error
),
errors_1h AS (
    SELECT COUNT(*) AS errors_last_1h
    FROM dds.scanner_error
    WHERE created_at >= NOW() - INTERVAL '1 hour'
),
errors_24h AS (
    SELECT COUNT(*) AS errors_last_24h
    FROM dds.scanner_error
    WHERE created_at >= NOW() - INTERVAL '24 hours'
),
runs_1h AS (
    SELECT COUNT(*) AS runs_last_1h
    FROM dds.scanner_run
    WHERE created_at >= NOW() - INTERVAL '1 hour'
),
signals_1h AS (
    SELECT COUNT(*) AS signals_last_1h
    FROM dds.market_signal
    WHERE created_at >= NOW() - INTERVAL '1 hour'
),
avg_duration AS (
    SELECT
        ROUND(AVG(duration_sec), 2) AS avg_run_duration_sec,
        SUM(CASE WHEN status IN ('ABORTED', 'PARTIAL') THEN 1 ELSE 0 END) AS failed_runs
    FROM dds.scanner_run
),
scanner_summary AS (
    SELECT
        COUNT(*) FILTER (
            WHERE CASE
                WHEN EXTRACT(EPOCH FROM (NOW() - sr.finished_at)) / 60 > 12 THEN 'STALE'
                WHEN srs.errors_count > 0 THEN 'ERROR'
                WHEN srs.symbols_scanned < sr.symbols_total THEN 'WARNING'
                ELSE 'OK'
            END = 'OK'
        ) AS healthy_count,
        COUNT(*) AS total_count,
        COUNT(*) FILTER (
            WHERE CASE
                WHEN EXTRACT(EPOCH FROM (NOW() - sr.finished_at)) / 60 > 12 THEN 'STALE'
                WHEN srs.errors_count > 0 THEN 'ERROR'
                WHEN srs.symbols_scanned < sr.symbols_total THEN 'WARNING'
                ELSE 'OK'
            END != 'OK'
        ) AS unhealthy_count
    FROM dds.scanner_run_stat srs
    JOIN dds.scanner_run sr ON sr.run_id = srs.run_id
    WHERE sr.run_id = (SELECT run_id FROM dds.scanner_run ORDER BY started_at DESC LIMIT 1)
      AND config.is_scanner_visible(srs.scanner_name)
)
SELECT
    lr.last_scanner_run,
    lr.last_successful_run,
    lr.last_run_status,
    lr.last_run_duration_sec,
    ls.last_signal,
    le.last_error,
    e1h.errors_last_1h,
    e24h.errors_last_24h,
    r1h.runs_last_1h,
    sg.signals_last_1h,
    ad.avg_run_duration_sec,
    ad.failed_runs,
    ss.healthy_count,
    ss.total_count,
    ss.unhealthy_count
FROM latest_run lr
CROSS JOIN latest_signal ls
CROSS JOIN latest_error le
CROSS JOIN errors_1h e1h
CROSS JOIN errors_24h e24h
CROSS JOIN runs_1h r1h
CROSS JOIN signals_1h sg
CROSS JOIN avg_duration ad
CROSS JOIN scanner_summary ss;
