-- 05_repeat_signal_analysis.sql
-- Repeat signal analysis: same symbol, multiple signals
-- NOTE: All ROUND() calls use ::numeric cast to avoid
--       PostgreSQL "function round(double precision, integer) does not exist"

WITH symbol_trades AS (
    SELECT
        pt.trade_id,
        pt.scanner_name,
        pt.symbol,
        pt.direction,
        pt.pnl_r,
        pt.entered_at,
        pt.closed_at,
        pt.status,
        LAG(pt.entered_at) OVER (
            PARTITION BY pt.symbol, pt.scanner_name
            ORDER BY pt.entered_at
        ) AS prev_entered_at,
        LAG(pt.pnl_r) OVER (
            PARTITION BY pt.symbol, pt.scanner_name
            ORDER BY pt.entered_at
        ) AS prev_pnl_r,
        LAG(pt.trade_id) OVER (
            PARTITION BY pt.symbol, pt.scanner_name
            ORDER BY pt.entered_at
        ) AS prev_trade_id
    FROM dds.paper_trade pt
    WHERE pt.scanner_name IN (
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
    )
    AND pt.direction = 'LONG'
    AND pt.status = 'CLOSED'
)
SELECT
    scanner_name,
    COUNT(*) AS total_trades,
    SUM(CASE WHEN prev_entered_at IS NOT NULL THEN 1 ELSE 0 END) AS repeat_trades,
    ROUND((
        SUM(CASE WHEN prev_entered_at IS NOT NULL THEN 1 ELSE 0 END)::numeric
        / COUNT(*) * 100)::numeric, 1
    ) AS repeat_pct,
    SUM(CASE WHEN prev_entered_at IS NOT NULL
             AND EXTRACT(EPOCH FROM (entered_at - prev_entered_at)) / 60 <= 60
        THEN 1 ELSE 0 END) AS repeats_within_1h,
    SUM(CASE WHEN prev_entered_at IS NOT NULL
             AND EXTRACT(EPOCH FROM (entered_at - prev_entered_at)) / 60 <= 180
        THEN 1 ELSE 0 END) AS repeats_within_3h,
    -- Repeat after loss
    SUM(CASE WHEN prev_pnl_r IS NOT NULL AND prev_pnl_r <= 0 THEN 1 ELSE 0 END) AS repeats_after_loss,
    -- Repeat after win
    SUM(CASE WHEN prev_pnl_r IS NOT NULL AND prev_pnl_r > 0 THEN 1 ELSE 0 END) AS repeats_after_win,
    -- Win rate on repeats
    ROUND((
        SUM(CASE WHEN prev_entered_at IS NOT NULL AND pnl_r > 0 THEN 1 ELSE 0 END)::numeric
        / NULLIF(SUM(CASE WHEN prev_entered_at IS NOT NULL THEN 1 ELSE 0 END), 0))::numeric,
        4
    ) AS repeat_win_rate,
    -- Win rate on first trades
    ROUND((
        SUM(CASE WHEN prev_entered_at IS NULL AND pnl_r > 0 THEN 1 ELSE 0 END)::numeric
        / NULLIF(SUM(CASE WHEN prev_entered_at IS NULL THEN 1 ELSE 0 END), 0))::numeric,
        4
    ) AS first_trade_win_rate
FROM symbol_trades
GROUP BY scanner_name
ORDER BY scanner_name;
