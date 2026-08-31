-- 004_scanner_daily_stats.sql
-- Daily aggregated stats per scanner / direction for time-series charts.

CREATE OR REPLACE VIEW mart.scanner_daily_stats AS
SELECT
    DATE(pt.closed_at)                          AS trade_date,
    pt.scanner_name,
    pt.direction,
    COUNT(*)                                    AS trades,
    SUM(CASE WHEN pt.pnl_r > 0 THEN 1 ELSE 0 END)  AS wins,
    SUM(CASE WHEN pt.pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
    ROUND(SUM(pt.pnl_usdt), 2)                 AS pnl_usdt,
    ROUND(SUM(pt.pnl_r), 4)                    AS pnl_r,
    CASE
        WHEN COUNT(*) > 0
        THEN ROUND(SUM(CASE WHEN pt.pnl_r > 0 THEN 1 ELSE 0 END)::numeric / COUNT(*), 4)
        ELSE NULL
    END                                         AS win_rate,
    CASE
        WHEN SUM(CASE WHEN pt.pnl_r < 0 THEN ABS(pt.pnl_r) ELSE 0 END) > 0
        THEN ROUND(
            SUM(CASE WHEN pt.pnl_r > 0 THEN pt.pnl_r ELSE 0 END)
            / SUM(CASE WHEN pt.pnl_r < 0 THEN ABS(pt.pnl_r) ELSE 0 END),
            4)
        ELSE NULL
    END                                         AS profit_factor
FROM dds.paper_trade pt
WHERE pt.status = 'CLOSED'
  AND pt.closed_at IS NOT NULL
GROUP BY DATE(pt.closed_at), pt.scanner_name, pt.direction;
