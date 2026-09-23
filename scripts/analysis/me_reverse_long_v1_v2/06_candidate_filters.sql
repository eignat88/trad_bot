-- 06_candidate_filters.sql
-- Candidate filter analysis: test various thresholds on features
-- This script tests multiple filters and shows their impact

-- Filter 1: rsi_delta_3 threshold (V2 only feature, but V1 trades have it too if we compute it)
-- For V1 trades, we need to check if features contain rsi_delta_3
-- V2 already filters on rsi_delta_3 > 0, so we test tighter thresholds

WITH base AS (
    SELECT
        pt.trade_id,
        pt.scanner_name,
        pt.pnl_r,
        pt.mfe,
        pt.mae,
        (pt.features->>'exhaustion_magnitude')::numeric AS exhaustion_magnitude,
        (pt.features->>'body_ratio')::numeric AS body_ratio,
        (pt.features->>'rsi_confirmation')::numeric AS rsi_confirmation,
        (pt.features->>'volume_ratio')::numeric AS volume_ratio,
        (pt.features->>'rr_ratio')::numeric AS rr_ratio,
        (pt.features->>'stop_distance_atr')::numeric AS stop_distance_atr,
        (pt.features->>'rsi_14')::numeric AS rsi_14,
        (pt.features->>'rsi_delta_3')::numeric AS rsi_delta_3
    FROM dds.paper_trade pt
    WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
      AND pt.direction = 'LONG'
      AND pt.status = 'CLOSED'
),
-- Compute percentiles for filter thresholds
percentiles AS (
    SELECT
        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY rsi_confirmation) AS p25_rsi_confirm,
        PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY rsi_confirmation) AS p50_rsi_confirm,
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY rsi_confirmation) AS p75_rsi_confirm,
        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY volume_ratio) AS p25_vol_ratio,
        PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY volume_ratio) AS p50_vol_ratio,
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY volume_ratio) AS p75_vol_ratio,
        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY body_ratio) AS p25_body_ratio,
        PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY body_ratio) AS p50_body_ratio,
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY body_ratio) AS p75_body_ratio,
        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY exhaustion_magnitude) AS p25_exhaust_mag,
        PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY exhaustion_magnitude) AS p50_exhaust_mag,
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY exhaustion_magnitude) AS p75_exhaust_mag
    FROM base
)
-- Filter candidates
SELECT
    'V1_rsi_confirmation_gte_0.5' AS filter_name,
    COUNT(*) AS total_before,
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses_before,
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins_before,
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_losses_before
FROM base
UNION ALL
SELECT
    'V1_rsi_confirmation_gte_0.5_filtered' AS filter_name,
    COUNT(*) AS total_after,
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END) AS losses_after,
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END) AS wins_after,
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END) AS hard_losses_after
FROM base
WHERE rsi_confirmation >= 0.5

UNION ALL

SELECT
    'V1_body_ratio_gte_0.6' AS filter_name,
    COUNT(*),
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END)
FROM base
UNION ALL
SELECT
    'V1_body_ratio_gte_0.6_filtered' AS filter_name,
    COUNT(*),
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END)
FROM base
WHERE body_ratio >= 0.6

UNION ALL

SELECT
    'V1_volume_ratio_gte_0.5' AS filter_name,
    COUNT(*),
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END)
FROM base
UNION ALL
SELECT
    'V1_volume_ratio_gte_0.5_filtered' AS filter_name,
    COUNT(*),
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END)
FROM base
WHERE volume_ratio >= 0.5

UNION ALL

SELECT
    'V1_exhaustion_mag_gte_0.5' AS filter_name,
    COUNT(*),
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END)
FROM base
UNION ALL
SELECT
    'V1_exhaustion_mag_gte_0.5_filtered' AS filter_name,
    COUNT(*),
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END)
FROM base
WHERE exhaustion_magnitude >= 0.5

UNION ALL

SELECT
    'V1_rr_ratio_gte_0.5' AS filter_name,
    COUNT(*),
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END)
FROM base
UNION ALL
SELECT
    'V1_rr_ratio_gte_0.5_filtered' AS filter_name,
    COUNT(*),
    SUM(CASE WHEN pnl_r <= 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r > 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN pnl_r <= -0.9 THEN 1 ELSE 0 END)
FROM base
WHERE rr_ratio >= 0.5;
