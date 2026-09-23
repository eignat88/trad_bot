-- 01_sample_size.sql
-- Sample size and date ranges for V1 and V2 scanners

-- V1 sample stats
SELECT
    'V1' AS version,
    COUNT(*) AS total_trades,
    COUNT(DISTINCT pt.symbol) AS unique_symbols,
    COUNT(DISTINCT pt.setup_id) AS unique_setups,
    MIN(pt.entered_at) AS period_start,
    MAX(pt.entered_at) AS period_end,
    MAX(pt.entered_at) - MIN(pt.entered_at) AS period_duration
FROM dds.paper_trade pt
WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
  AND pt.direction = 'LONG'
  AND pt.status = 'CLOSED'

UNION ALL

-- V2 sample stats
SELECT
    'V2' AS version,
    COUNT(*) AS total_trades,
    COUNT(DISTINCT pt.symbol) AS unique_symbols,
    COUNT(DISTINCT pt.setup_id) AS unique_setups,
    MIN(pt.entered_at) AS period_start,
    MAX(pt.entered_at) AS period_end,
    MAX(pt.entered_at) - MIN(pt.entered_at) AS period_duration
FROM dds.paper_trade pt
WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
  AND pt.direction = 'LONG'
  AND pt.status = 'CLOSED';
