-- 09_v2_exhaustion_shadow_analytics.sql
-- Shadow experiment: V2_ALL vs EXHAUST_PASS vs EXHAUST_REJECT
-- Threshold: exhaustion_magnitude <= 0.4

-- ============================================================
-- A. Group comparison: V2_ALL vs PASS vs REJECT
-- ============================================================
SELECT
    'V2_ALL' AS group_name,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed_outcomes,
    COUNT(*) FILTER (WHERE pnl_r > 0) AS wins,
    COUNT(*) FILTER (WHERE pnl_r <= 0) AS losses,
    COUNT(*) FILTER (WHERE pnl_r <= -0.9) AS hard_losses,
    ROUND(
        (COUNT(*) FILTER (WHERE pnl_r > 0))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE status = 'CLOSED'), 0), 4
    ) AS win_rate,
    ROUND(SUM(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS total_r,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS expectancy_r,
    CASE
        WHEN SUM(ABS(pnl_r)) FILTER (WHERE status = 'CLOSED' AND pnl_r < 0) > 0
        THEN ROUND(
            SUM(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r > 0)
            / SUM(ABS(pnl_r)) FILTER (WHERE status = 'CLOSED' AND pnl_r < 0), 4)
        ELSE NULL
    END AS profit_factor,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r > 0), 4) AS avg_win_r,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r <= 0), 4) AS avg_loss_r,
    ROUND(AVG(mfe_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_mae_r
FROM dds.shadow_exhaustion_observation
WHERE experiment_id = 'V2_EXHAUSTION_SHADOW_V1'

UNION ALL

SELECT
    'EXHAUST_PASS' AS group_name,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed_outcomes,
    COUNT(*) FILTER (WHERE pnl_r > 0) AS wins,
    COUNT(*) FILTER (WHERE pnl_r <= 0) AS losses,
    COUNT(*) FILTER (WHERE pnl_r <= -0.9) AS hard_losses,
    ROUND(
        (COUNT(*) FILTER (WHERE pnl_r > 0))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE status = 'CLOSED'), 0), 4
    ) AS win_rate,
    ROUND(SUM(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS total_r,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS expectancy_r,
    CASE
        WHEN SUM(ABS(pnl_r)) FILTER (WHERE status = 'CLOSED' AND pnl_r < 0) > 0
        THEN ROUND(
            SUM(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r > 0)
            / SUM(ABS(pnl_r)) FILTER (WHERE status = 'CLOSED' AND pnl_r < 0), 4)
        ELSE NULL
    END AS profit_factor,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r > 0), 4) AS avg_win_r,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r <= 0), 4) AS avg_loss_r,
    ROUND(AVG(mfe_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_mae_r
FROM dds.shadow_exhaustion_observation
WHERE experiment_id = 'V2_EXHAUSTION_SHADOW_V1'
  AND filter_result = 'PASS'

UNION ALL

SELECT
    'EXHAUST_REJECT' AS group_name,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed_outcomes,
    COUNT(*) FILTER (WHERE pnl_r > 0) AS wins,
    COUNT(*) FILTER (WHERE pnl_r <= 0) AS losses,
    COUNT(*) FILTER (WHERE pnl_r <= -0.9) AS hard_losses,
    ROUND(
        (COUNT(*) FILTER (WHERE pnl_r > 0))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE status = 'CLOSED'), 0), 4
    ) AS win_rate,
    ROUND(SUM(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS total_r,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS expectancy_r,
    CASE
        WHEN SUM(ABS(pnl_r)) FILTER (WHERE status = 'CLOSED' AND pnl_r < 0) > 0
        THEN ROUND(
            SUM(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r > 0)
            / SUM(ABS(pnl_r)) FILTER (WHERE status = 'CLOSED' AND pnl_r < 0), 4)
        ELSE NULL
    END AS profit_factor,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r > 0), 4) AS avg_win_r,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r <= 0), 4) AS avg_loss_r,
    ROUND(AVG(mfe_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_mae_r
FROM dds.shadow_exhaustion_observation
WHERE experiment_id = 'V2_EXHAUSTION_SHADOW_V1'
  AND filter_result = 'REJECT';

-- ============================================================
-- B. Hard-loss analysis: how many hard losses are in REJECT?
-- ============================================================
SELECT
    filter_result,
    COUNT(*) FILTER (WHERE pnl_r <= -0.9) AS hard_losses,
    COUNT(*) FILTER (WHERE pnl_r > 0) AS wins,
    CASE
        WHEN COUNT(*) FILTER (WHERE pnl_r > 0) > 0
        THEN ROUND(
            COUNT(*) FILTER (WHERE pnl_r <= -0.9)::numeric
            / COUNT(*) FILTER (WHERE pnl_r > 0), 2)
        ELSE NULL
    END AS hard_losses_per_winner
FROM dds.shadow_exhaustion_observation
WHERE experiment_id = 'V2_EXHAUSTION_SHADOW_V1'
  AND status = 'CLOSED'
GROUP BY filter_result;

-- ============================================================
-- C. Sample size checkpoint
-- ============================================================
SELECT
    COUNT(*) AS total_observations,
    COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed_outcomes,
    COUNT(*) FILTER (WHERE status = 'OPEN') AS open_trades,
    COUNT(*) FILTER (WHERE status = 'PENDING') AS pending,
    COUNT(*) FILTER (WHERE status = 'NO_TRADE') AS no_trade,
    COUNT(*) FILTER (WHERE filter_result = 'PASS') AS pass_signals,
    COUNT(*) FILTER (WHERE filter_result = 'REJECT') AS reject_signals,
    COUNT(*) FILTER (WHERE filter_result = 'MISSING_FEATURE') AS missing
FROM dds.shadow_exhaustion_observation
WHERE experiment_id = 'V2_EXHAUSTION_SHADOW_V1';

-- ============================================================
-- D. Detailed signal list (most recent 20)
-- ============================================================
SELECT
    setup_id,
    symbol,
    exhaustion_magnitude,
    filter_result,
    status,
    pnl_r,
    exit_reason,
    detected_at,
    outcome_source
FROM dds.shadow_exhaustion_observation
WHERE experiment_id = 'V2_EXHAUSTION_SHADOW_V1'
ORDER BY detected_at DESC
LIMIT 20;
