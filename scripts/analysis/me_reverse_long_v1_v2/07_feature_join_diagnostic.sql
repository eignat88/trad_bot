-- 07_feature_join_diagnostic.sql
-- Diagnostic: verify JOIN cardinality and actual JSON keys in features
-- This ensures the pt → ss JOIN doesn't change trade count
-- and confirms which feature keys actually exist in production data.

-- ============================================================
-- A. JOIN cardinality check
-- For each scanner, verify:
--   trade_count        = COUNT(pt.trade_id) from paper_trade
--   joined_count       = COUNT after JOIN with scanner_setup
--   missing_setup      = trades with no matching setup
--   duplicate_join     = trades matching multiple setups
-- ============================================================

-- A1. Trade count vs joined count
SELECT
    pt.scanner_name,
    COUNT(DISTINCT pt.trade_id) AS trade_count,
    COUNT(DISTINCT ss.setup_id) AS joined_setup_count,
    COUNT(DISTINCT pt.trade_id) FILTER (
        WHERE ss.setup_id IS NULL
    ) AS missing_setup_count
FROM dds.paper_trade pt
LEFT JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
WHERE pt.scanner_name IN (
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
)
AND pt.direction = 'LONG'
AND pt.status = 'CLOSED'
GROUP BY pt.scanner_name;

-- A2. Duplicate join detection (should be 0 if setup_id is unique per trade)
SELECT
    pt.scanner_name,
    pt.trade_id,
    COUNT(*) AS join_count
FROM dds.paper_trade pt
JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
WHERE pt.scanner_name IN (
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
)
AND pt.direction = 'LONG'
AND pt.status = 'CLOSED'
GROUP BY pt.scanner_name, pt.trade_id
HAVING COUNT(*) > 1;

-- ============================================================
-- B. Actual JSON keys in features for V1 and V2
-- Do NOT assume rsi_14 / rsi_delta_3 exist — verify from data.
-- ============================================================

SELECT
    pt.scanner_name,
    jsonb_object_keys(ss.features) AS feature_key,
    COUNT(*) AS count
FROM dds.paper_trade pt
JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
WHERE pt.scanner_name IN (
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
)
AND pt.direction = 'LONG'
AND pt.status = 'CLOSED'
GROUP BY 1, 2
ORDER BY 1, 2;

-- ============================================================
-- C. Sample features JSON for each scanner (first 3 trades)
-- ============================================================

SELECT
    pt.scanner_name,
    pt.trade_id,
    pt.symbol,
    pt.pnl_r,
    ss.features
FROM dds.paper_trade pt
JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
WHERE pt.scanner_name IN (
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
)
AND pt.direction = 'LONG'
AND pt.status = 'CLOSED'
ORDER BY pt.scanner_name, pt.entered_at
LIMIT 6;
