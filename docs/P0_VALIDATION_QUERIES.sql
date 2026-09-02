-- ============================================================
-- P0 Score Inversion: Validation Queries
-- Run these BEFORE implementing fixes to confirm root cause
-- ============================================================

-- ============================================================
-- 1. Feature-Outcome Correlation Audit
-- Shows which features actually predict positive R
-- ============================================================
SELECT
    f.key AS feature_name,
    COUNT(*) AS samples,
    ROUND(AVG(o.result_r), 4) AS avg_r,
    ROUND(AVG(o.fee_slippage_adjusted_result_r), 4) AS avg_r_adjusted,
    ROUND(
        COUNT(*) FILTER (WHERE o.first_event IN ('TP1', 'TP2'))::numeric /
        NULLIF(COUNT(*) FILTER (WHERE o.entry_touched), 0), 4
    ) AS win_rate,
    ROUND(
        SUM(GREATEST(o.result_r, 0))
        / NULLIF(ABS(SUM(LEAST(o.result_r, 0))), 0), 4
    ) AS profit_factor
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
CROSS JOIN LATERAL jsonb_each_text(s.features) AS f(key, value)
WHERE o.entry_touched = true
GROUP BY f.key
HAVING COUNT(*) >= 10
ORDER BY avg_r DESC;

-- ============================================================
-- 2. Per-Scanner Score Distribution
-- Shows if high scores cluster in specific scanners
-- ============================================================
SELECT
    scanner_name,
    direction,
    CASE
        WHEN score < 30 THEN '<30'
        WHEN score < 40 THEN '30-39'
        WHEN score < 50 THEN '40-49'
        ELSE '50+'
    END AS score_bucket,
    COUNT(*) AS samples,
    COUNT(*) FILTER (WHERE o.entry_touched) AS entries,
    ROUND(AVG(o.result_r), 4) AS avg_r,
    ROUND(AVG(o.fee_slippage_adjusted_result_r), 4) AS avg_r_adjusted,
    ROUND(
        COUNT(*) FILTER (WHERE o.first_event IN ('TP1', 'TP2'))::numeric /
        NULLIF(COUNT(*) FILTER (WHERE o.entry_touched), 0), 4
    ) AS win_rate
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
GROUP BY scanner_name, direction, score_bucket
HAVING COUNT(*) >= 5
ORDER BY scanner_name, direction, score_bucket;

-- ============================================================
-- 3. Score Inversion Confirmation
-- The problematic pattern: high score = negative R
-- ============================================================
SELECT
    CASE
        WHEN s.score < 30 THEN '<30'
        WHEN s.score < 40 THEN '30-39'
        WHEN s.score < 50 THEN '40-49'
        ELSE '50+'
    END AS score_bucket,
    s.direction,
    COUNT(*) AS samples,
    COUNT(*) FILTER (WHERE o.entry_touched) AS entries,
    ROUND(AVG(o.result_r), 4) AS avg_r,
    ROUND(AVG(o.fee_slippage_adjusted_result_r), 4) AS avg_r_adjusted,
    ROUND(
        COUNT(*) FILTER (WHERE o.first_event IN ('TP1', 'TP2'))::numeric /
        NULLIF(COUNT(*) FILTER (WHERE o.entry_touched), 0), 4
    ) AS win_rate
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
GROUP BY score_bucket, s.direction
ORDER BY score_bucket, s.direction;

-- ============================================================
-- 4. Volatility-Feature Correlation
-- Check if high-score setups cluster in volatile regimes
-- ============================================================
SELECT
    s.market_regime,
    CASE
        WHEN s.score < 30 THEN '<30'
        WHEN s.score < 50 THEN '30-49'
        ELSE '50+'
    END AS score_bucket,
    COUNT(*) AS samples,
    ROUND(AVG(o.result_r), 4) AS avg_r,
    ROUND(AVG(o.fee_slippage_adjusted_result_r), 4) AS avg_r_adjusted
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
WHERE o.entry_touched = true
GROUP BY s.market_regime, score_bucket
HAVING COUNT(*) >= 5
ORDER BY s.market_regime, score_bucket;

-- ============================================================
-- 5. LIQUIDITY_SWEEP_CHOCH_OB Dominance Check
-- Is this scanner dominating the 50+ bucket?
-- ============================================================
SELECT
    scanner_name,
    CASE
        WHEN s.score < 30 THEN '<30'
        WHEN s.score < 40 THEN '30-39'
        WHEN s.score < 50 THEN '40-49'
        ELSE '50+'
    END AS score_bucket,
    COUNT(*) AS total_setups,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY score_bucket), 1) AS pct_of_bucket,
    ROUND(AVG(o.result_r), 4) AS avg_r
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
WHERE o.entry_touched = true
GROUP BY scanner_name, score_bucket
ORDER BY score_bucket, total_setups DESC;

-- ============================================================
-- 6. Feature Independence Check
-- Are features truly independent or always co-occurring?
-- ============================================================
WITH feature_pairs AS (
    SELECT
        o.setup_id,
        f1.key AS feature_a,
        f2.key AS feature_b
    FROM dds.signal_outcome o
    JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
    CROSS JOIN LATERAL jsonb_each_text(s.features) AS f1(key, value)
    CROSS JOIN LATERAL jsonb_each_text(s.features) AS f2(key, value)
    WHERE f1.key < f2.key
      AND o.entry_touched = true
)
SELECT
    feature_a,
    feature_b,
    COUNT(*) AS co_occurrence,
    ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM dds.signal_outcome WHERE entry_touched), 1) AS pct_of_setups
FROM feature_pairs
GROUP BY feature_a, feature_b
HAVING COUNT(*) >= 10
ORDER BY co_occurrence DESC
LIMIT 20;

-- ============================================================
-- 7. R:R Ratio vs Actual R Correlation
-- Does higher R:R actually predict better outcomes?
-- ============================================================
SELECT
    CASE
        WHEN (s.target_1 - s.entry_zone_high) / NULLIF(ABS(s.entry_zone_high - s.invalidation_price), 0) < 1.0 THEN '<1.0'
        WHEN (s.target_1 - s.entry_zone_high) / NULLIF(ABS(s.entry_zone_high - s.invalidation_price), 0) < 1.5 THEN '1.0-1.5'
        WHEN (s.target_1 - s.entry_zone_high) / NULLIF(ABS(s.entry_zone_high - s.invalidation_price), 0) < 2.0 THEN '1.5-2.0'
        WHEN (s.target_1 - s.entry_zone_high) / NULLIF(ABS(s.entry_zone_high - s.invalidation_price), 0) < 3.0 THEN '2.0-3.0'
        ELSE '3.0+'
    END AS rr_bucket,
    s.direction,
    COUNT(*) AS samples,
    ROUND(AVG(o.result_r), 4) AS avg_r,
    ROUND(
        COUNT(*) FILTER (WHERE o.first_event IN ('TP1', 'TP2'))::numeric /
        NULLIF(COUNT(*) FILTER (WHERE o.entry_touched), 0), 4
    ) AS win_rate
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
WHERE o.entry_touched = true
  AND s.direction = 'LONG'
GROUP BY rr_bucket, s.direction
ORDER BY rr_bucket;

-- ============================================================
-- 8. Score Feature Contribution Analysis
-- How much does each feature contribute to final score?
-- ============================================================
WITH score_components AS (
    SELECT
        s.scanner_name,
        s.direction,
        s.score,
        f.key AS feature_name,
        CASE WHEN f.value = 'true' THEN 1.0
             WHEN f.value ~ '^[0-9.]+$' THEN f.value::numeric
             ELSE 0.0
        END AS feature_value
    FROM dds.scanner_setup s
    CROSS JOIN LATERAL jsonb_each_text(s.features) AS f(key, value)
    WHERE s.score >= 50
)
SELECT
    scanner_name,
    feature_name,
    COUNT(*) AS occurrences,
    ROUND(AVG(feature_value), 3) AS avg_value,
    ROUND(AVG(score), 1) AS avg_score_when_present
FROM score_components
WHERE feature_value > 0
GROUP BY scanner_name, feature_name
ORDER BY scanner_name, avg_value DESC;

-- ============================================================
-- 9. Scanner Score Overlap Analysis
-- Do different scanners produce similar scores for same symbol/direction?
-- ============================================================
WITH scanner_scores AS (
    SELECT
        i.symbol,
        s.direction,
        s.scanner_name,
        s.score,
        s.detected_at
    FROM dds.scanner_setup s
    JOIN dds.instrument i ON i.instrument_id = s.instrument_id
    WHERE s.score >= 40
      AND s.detected_at > now() - interval '7 days'
)
SELECT
    a.symbol,
    a.direction,
    a.scanner_name AS scanner_1,
    b.scanner_name AS scanner_2,
    ABS(a.score - b.score) AS score_diff,
    a.score AS score_1,
    b.score AS score_2
FROM scanner_scores a
JOIN scanner_scores b ON (
    a.symbol = b.symbol
    AND a.direction = b.direction
    AND a.scanner_name < b.scanner_name
    AND ABS(EXTRACT(EPOCH FROM (a.detected_at - b.detected_at))) < 600
)
WHERE ABS(a.score - b.score) < 10
ORDER BY score_diff
LIMIT 20;

-- ============================================================
-- 10. Expected vs Actual Score-R Relationship
-- What the relationship SHOULD look like (monotonic increasing)
-- ============================================================
WITH bucket_stats AS (
    SELECT
        CASE
            WHEN s.score < 20 THEN 1
            WHEN s.score < 40 THEN 2
            WHEN s.score < 60 THEN 3
            WHEN s.score < 80 THEN 4
            ELSE 5
        END AS bucket_num,
        CASE
            WHEN s.score < 20 THEN '00-19'
            WHEN s.score < 40 THEN '20-39'
            WHEN s.score < 60 THEN '40-59'
            WHEN s.score < 80 THEN '60-79'
            ELSE '80-100'
        END AS score_range,
        o.result_r,
        o.fee_slippage_adjusted_result_r,
        o.first_event,
        o.entry_touched
    FROM dds.signal_outcome o
    JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
)
SELECT
    score_range,
    bucket_num,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE entry_touched) AS entries,
    ROUND(AVG(result_r), 4) AS avg_r,
    ROUND(AVG(fee_slippage_adjusted_result_r), 4) AS avg_r_adjusted,
    ROUND(
        COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2'))::numeric /
        NULLIF(COUNT(*) FILTER (WHERE entry_touched), 0), 4
    ) AS win_rate,
    -- Monotonicity check: is this bucket better than the previous?
    CASE
        WHEN AVG(result_r) > LAG(AVG(result_r)) OVER (ORDER BY bucket_num) THEN '✓'
        WHEN LAG(AVG(result_r)) OVER (ORDER BY bucket_num) IS NULL THEN '—'
        ELSE '✗ INVERTED'
    END AS monotonicity
FROM bucket_stats
GROUP BY score_range, bucket_num
ORDER BY bucket_num;
