-- 02_baseline_metrics.sql
-- Baseline trading metrics for V1 and V2
-- NOTE: All ROUND() calls use ::numeric cast to avoid
--       PostgreSQL "function round(double precision, integer) does not exist"

-- V1 baseline
SELECT
    'V1' AS version,
    COUNT(*) AS trades,
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
    ROUND((SUM(CASE WHEN pnl_r > 0 THEN 1.0 ELSE 0 END) / COUNT(*))::numeric, 4) AS win_rate,
    ROUND(SUM(pnl_usdt)::numeric, 2) AS net_pnl_usdt,
    ROUND(AVG(pnl_usdt)::numeric, 2) AS avg_pnl_usdt,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_usdt))::numeric, 2) AS median_pnl_usdt,
    ROUND(SUM(pnl_r)::numeric, 4) AS total_r,
    ROUND(AVG(pnl_r)::numeric, 4) AS expectancy_r,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_r))::numeric, 4) AS median_r,
    CASE
        WHEN SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END) > 0
        THEN ROUND((
            SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
            / SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END))::numeric, 4)
        ELSE NULL
    END AS profit_factor,
    ROUND(AVG(CASE WHEN pnl_r > 0 THEN pnl_r END)::numeric, 4) AS avg_win_r,
    ROUND(AVG(CASE WHEN pnl_r <= 0 THEN pnl_r END)::numeric, 4) AS avg_loss_r,
    MAX(pnl_r) AS max_win_r,
    MIN(pnl_r) AS max_loss_r
FROM dds.paper_trade pt
WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
  AND pt.direction = 'LONG'
  AND pt.status = 'CLOSED'

UNION ALL

-- V2 baseline
SELECT
    'V2' AS version,
    COUNT(*) AS trades,
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses,
    ROUND((SUM(CASE WHEN pnl_r > 0 THEN 1.0 ELSE 0 END) / COUNT(*))::numeric, 4) AS win_rate,
    ROUND(SUM(pnl_usdt)::numeric, 2) AS net_pnl_usdt,
    ROUND(AVG(pnl_usdt)::numeric, 2) AS avg_pnl_usdt,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_usdt))::numeric, 2) AS median_pnl_usdt,
    ROUND(SUM(pnl_r)::numeric, 4) AS total_r,
    ROUND(AVG(pnl_r)::numeric, 4) AS expectancy_r,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_r))::numeric, 4) AS median_r,
    CASE
        WHEN SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END) > 0
        THEN ROUND((
            SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END)
            / SUM(CASE WHEN pnl_r < 0 THEN ABS(pnl_r) ELSE 0 END))::numeric, 4)
        ELSE NULL
    END AS profit_factor,
    ROUND(AVG(CASE WHEN pnl_r > 0 THEN pnl_r END)::numeric, 4) AS avg_win_r,
    ROUND(AVG(CASE WHEN pnl_r <= 0 THEN pnl_r END)::numeric, 4) AS avg_loss_r,
    MAX(pnl_r) AS max_win_r,
    MIN(pnl_r) AS max_loss_r
FROM dds.paper_trade pt
WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
  AND pt.direction = 'LONG'
  AND pt.status = 'CLOSED';
