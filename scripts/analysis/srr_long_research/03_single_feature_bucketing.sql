-- 03_single_feature_bucketing.sql
-- Aggregate outcomes by single features with min_samples filter

-- Set minimum sample size
\set min_samples 20

-- ── Bucket by touch count ────────────────────────────────────
SELECT
    touch_bucket AS feature_value,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(
        COUNT(*) FILTER (WHERE tp_before_sl)::numeric /
        NULLIF(COUNT(*) FILTER (WHERE tp_before_sl) + COUNT(*) FILTER (WHERE sl_before_tp), 0) * 100, 1
    ) AS tp_before_sl_pct,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r,
    ROUND(AVG(mfe_60m) FILTER (WHERE is_final), 4) AS avg_mfe_pct,
    ROUND(AVG(mae_60m) FILTER (WHERE is_final), 4) AS avg_mae_pct
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY touch_bucket
HAVING COUNT(*) >= :min_samples
ORDER BY
    CASE touch_bucket
        WHEN '1-2' THEN 1
        WHEN '3' THEN 2
        WHEN '4' THEN 3
        WHEN '5+' THEN 4
    END;

-- ── Bucket by ATR volatility ─────────────────────────────────
SELECT
    atr_bucket AS feature_value,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r,
    ROUND(AVG(mfe_60m) FILTER (WHERE is_final), 4) AS avg_mfe_pct,
    ROUND(AVG(mae_60m) FILTER (WHERE is_final), 4) AS avg_mae_pct
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY atr_bucket
HAVING COUNT(*) >= :min_samples
ORDER BY atr_bucket;

-- ── Bucket by wick:body ratio ────────────────────────────────
SELECT
    wick_bucket AS feature_value,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r,
    ROUND(AVG(mfe_60m) FILTER (WHERE is_final), 4) AS avg_mfe_pct,
    ROUND(AVG(mae_60m) FILTER (WHERE is_final), 4) AS avg_mae_pct
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY wick_bucket
HAVING COUNT(*) >= :min_samples
ORDER BY wick_bucket;

-- ── Bucket by volume ratio ───────────────────────────────────
SELECT
    volume_bucket AS feature_value,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r,
    ROUND(AVG(mfe_60m) FILTER (WHERE is_final), 4) AS avg_mfe_pct,
    ROUND(AVG(mae_60m) FILTER (WHERE is_final), 4) AS avg_mae_pct
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY volume_bucket
HAVING COUNT(*) >= :min_samples
ORDER BY volume_bucket;

-- ── Bucket by market regime ──────────────────────────────────
SELECT
    COALESCE(market_regime, 'UNKNOWN') AS feature_value,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r,
    ROUND(AVG(mfe_60m) FILTER (WHERE is_final), 4) AS avg_mfe_pct,
    ROUND(AVG(mae_60m) FILTER (WHERE is_final), 4) AS avg_mae_pct
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY market_regime
HAVING COUNT(*) >= :min_samples
ORDER BY signals DESC;
