-- 002_scanner_overview.sql
-- KPI overview: single-row summary of Scanner activity.
-- Uses dds tables directly — no business-logic duplication.

CREATE OR REPLACE VIEW mart.scanner_overview AS
WITH runs AS (
    SELECT
        COUNT(*)                                          AS total_runs,
        COALESCE(MAX(created_at), NULL)                   AS last_run_at,
        SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed_runs,
        SUM(symbols_scanned)                              AS total_symbols_scanned,
        SUM(setups_found)                                 AS total_setups_found
    FROM dds.scanner_run
),
errors AS (
    SELECT
        COUNT(*)                    AS total_errors,
        MAX(created_at)             AS last_error_at
    FROM dds.scanner_error
),
setups AS (
    SELECT
        COUNT(*)                                            AS total_setups,
        SUM(CASE WHEN status = 'EXECUTED' THEN 1 ELSE 0 END) AS executed_setups,
        SUM(CASE WHEN status = 'EXPIRED'  THEN 1 ELSE 0 END) AS expired_setups
    FROM dds.scanner_setup
),
signals AS (
    SELECT
        COUNT(*)                    AS total_signals,
        MAX(created_at)             AS last_signal_at
    FROM dds.market_signal
),
trades AS (
    SELECT
        COUNT(*)                    AS total_paper_trades,
        SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END)    AS open_trades,
        SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END)  AS closed_trades
    FROM dds.paper_trade
)
SELECT
    r.total_runs,
    r.completed_runs,
    r.total_symbols_scanned,
    r.total_setups_found,
    r.last_run_at,
    s.total_setups,
    s.executed_setups,
    s.expired_setups,
    sig.total_signals,
    sig.last_signal_at,
    t.total_paper_trades,
    t.open_trades,
    t.closed_trades,
    e.total_errors,
    e.last_error_at
FROM runs r
CROSS JOIN errors e
CROSS JOIN setups s
CROSS JOIN signals sig
CROSS JOIN trades t;
