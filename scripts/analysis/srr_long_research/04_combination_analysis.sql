-- 04_combination_analysis.sql
-- 2-3 feature combination analysis with min_samples filter
-- Adjust min_samples as data accumulates

-- ── Combination: touch_count × ATR bucket ────────────────────
SELECT
    touch_bucket,
    atr_bucket,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(
        COUNT(*) FILTER (WHERE tp_before_sl)::numeric /
        NULLIF(COUNT(*) FILTER (WHERE tp_before_sl) + COUNT(*) FILTER (WHERE sl_before_tp), 0) * 100, 1
    ) AS tp_first_pct,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r,
    ROUND(AVG(mfe_60m) FILTER (WHERE is_final), 4) AS avg_mfe_pct,
    ROUND(AVG(mae_60m) FILTER (WHERE is_final), 4) AS avg_mae_pct
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY touch_bucket, atr_bucket
HAVING COUNT(*) >= 10
ORDER BY signals DESC;

-- ── Combination: touch_count × wick_bucket ───────────────────
SELECT
    touch_bucket,
    wick_bucket,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY touch_bucket, wick_bucket
HAVING COUNT(*) >= 10
ORDER BY signals DESC;

-- ── Combination: wick_bucket × volume_bucket ─────────────────
SELECT
    wick_bucket,
    volume_bucket,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY wick_bucket, volume_bucket
HAVING COUNT(*) >= 10
ORDER BY signals DESC;

-- ── Triple: touch_count × atr_bucket × wick_bucket ───────────
SELECT
    touch_bucket,
    atr_bucket,
    wick_bucket,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY touch_bucket, atr_bucket, wick_bucket
HAVING COUNT(*) >= 10
ORDER BY signals DESC;

-- ── Combination: distance_bucket × atr_bucket ────────────────
SELECT
    distance_bucket,
    atr_bucket,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE is_final) AS finalized,
    COUNT(*) FILTER (WHERE tp_before_sl) AS tp_first,
    COUNT(*) FILTER (WHERE sl_before_tp) AS sl_first,
    ROUND(AVG(mfe_r_60m) FILTER (WHERE is_final), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_60m) FILTER (WHERE is_final), 4) AS avg_mae_r
FROM dds.v_srr_research_bucketed
WHERE is_final = TRUE
GROUP BY distance_bucket, atr_bucket
HAVING COUNT(*) >= 10
ORDER BY signals DESC;
