-- 006_paper_trade_stats.sql
-- Aggregate stats for all paper trades (closed & open combined).
-- Single-row summary for KPI panels.

CREATE OR REPLACE VIEW mart.paper_trade_stats AS
SELECT
    COUNT(*)                                                            AS total_trades,
    SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END)                 AS closed_trades,
    SUM(CASE WHEN status = 'OPEN'   THEN 1 ELSE 0 END)                 AS open_trades,
    SUM(CASE WHEN status = 'CLOSED' AND pnl_r > 0 THEN 1 ELSE 0 END)  AS wins,
    SUM(CASE WHEN status = 'CLOSED' AND pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
    CASE
        WHEN SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) > 0
        THEN ROUND(
            SUM(CASE WHEN status = 'CLOSED' AND pnl_r > 0 THEN 1 ELSE 0 END)::numeric
            / SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END),
            4)
        ELSE NULL
    END                                                                 AS win_rate,
    ROUND(SUM(CASE WHEN status = 'CLOSED' THEN pnl_usdt ELSE 0 END), 2) AS total_pnl_usdt,
    CASE
        WHEN SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) > 0
        THEN ROUND(
            SUM(CASE WHEN status = 'CLOSED' THEN pnl_usdt ELSE 0 END)
            / SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END),
            2)
        ELSE NULL
    END                                                                 AS avg_pnl_usdt,
    ROUND(SUM(CASE WHEN status = 'CLOSED' THEN pnl_r ELSE 0 END), 4)   AS total_r,
    CASE
        WHEN SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) > 0
        THEN ROUND(
            SUM(CASE WHEN status = 'CLOSED' THEN pnl_r ELSE 0 END)
            / SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END),
            4)
        ELSE NULL
    END                                                                 AS avg_r,
    -- Profit factor
    CASE
        WHEN SUM(CASE WHEN status = 'CLOSED' AND pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END) > 0
        THEN ROUND(
            SUM(CASE WHEN status = 'CLOSED' AND pnl_r > 0 THEN pnl_r ELSE 0 END)
            / SUM(CASE WHEN status = 'CLOSED' AND pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END),
            4)
        ELSE NULL
    END                                                                 AS profit_factor,
    -- Best / worst trade
    (SELECT MAX(pnl_r)  FROM dds.paper_trade WHERE status = 'CLOSED') AS best_trade_r,
    (SELECT MIN(pnl_r)  FROM dds.paper_trade WHERE status = 'CLOSED') AS worst_trade_r,
    (SELECT MAX(pnl_usdt) FROM dds.paper_trade WHERE status = 'CLOSED') AS best_trade_usdt,
    (SELECT MIN(pnl_usdt) FROM dds.paper_trade WHERE status = 'CLOSED') AS worst_trade_usdt,
    -- Max drawdown from paper_account (latest snapshot)
    (SELECT max_drawdown FROM dds.paper_account ORDER BY snapshot_id DESC LIMIT 1) AS max_drawdown
FROM dds.paper_trade;
