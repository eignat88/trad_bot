-- 003_scanner_performance.sql
-- Per-scanner / direction performance from paper trades.
-- NOTE: `risk_usdt` is used as the reference risk per trade (denominator for R).
--       `pnl_r` is already computed in dds.paper_trade.

CREATE OR REPLACE VIEW mart.scanner_performance AS
SELECT
    pt.scanner_name,
    pt.direction,
    COUNT(*)                                        AS total_trades,
    SUM(CASE WHEN pt.pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN pt.pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
    CASE
        WHEN COUNT(*) > 0
        THEN ROUND(SUM(CASE WHEN pt.pnl_r > 0 THEN 1 ELSE 0 END)::numeric / COUNT(*), 4)
        ELSE NULL
    END                                             AS win_rate,
    ROUND(SUM(pt.pnl_usdt), 2)                     AS total_pnl_usdt,
    CASE
        WHEN COUNT(*) > 0
        THEN ROUND(AVG(pt.pnl_usdt), 2)
        ELSE NULL
    END                                             AS avg_pnl_usdt,
    ROUND(SUM(pt.pnl_r), 4)                        AS total_r,
    CASE
        WHEN COUNT(*) > 0
        THEN ROUND(AVG(pt.pnl_r), 4)
        ELSE NULL
    END                                             AS avg_r,
    -- Profit factor: sum of positive R / abs(sum of negative R)
    CASE
        WHEN SUM(CASE WHEN pt.pnl_r < 0 THEN ABS(pt.pnl_r) ELSE 0 END) > 0
        THEN ROUND(
            SUM(CASE WHEN pt.pnl_r > 0 THEN pt.pnl_r ELSE 0 END)
            / SUM(CASE WHEN pt.pnl_r < 0 THEN ABS(pt.pnl_r) ELSE 0 END),
            4)
        ELSE NULL
    END                                             AS profit_factor,
    -- Avg win R / avg loss R
    CASE
        WHEN SUM(CASE WHEN pt.pnl_r > 0 THEN 1 ELSE 0 END) > 0
        THEN ROUND(
            SUM(CASE WHEN pt.pnl_r > 0 THEN pt.pnl_r ELSE 0 END)
            / SUM(CASE WHEN pt.pnl_r > 0 THEN 1 ELSE 0 END),
            4)
        ELSE NULL
    END                                             AS avg_win_r,
    CASE
        WHEN SUM(CASE WHEN pt.pnl_r < 0 THEN 1 ELSE 0 END) > 0
        THEN ROUND(
            SUM(CASE WHEN pt.pnl_r < 0 THEN pt.pnl_r ELSE 0 END)
            / SUM(CASE WHEN pt.pnl_r < 0 THEN 1 ELSE 0 END),
            4)
        ELSE NULL
    END                                             AS avg_loss_r
FROM dds.paper_trade pt
WHERE pt.status = 'CLOSED'
  AND config.is_scanner_visible(pt.scanner_name)
GROUP BY pt.scanner_name, pt.direction;
