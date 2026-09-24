-- 06_no_new_paper_trades.sql
-- Verify: NO paper trades were created from SRR research signals

-- SRR research signals are NOT linked to paper_trade
SELECT
    'srr_research_signals' AS source,
    COUNT(*) AS count
FROM dds.srr_research_signal
WHERE experiment_id = 'SRR_LONG_OUTCOME_V1'

UNION ALL

SELECT
    'srr_linked_paper_trades' AS source,
    COUNT(*) AS count
FROM dds.paper_trade pt
WHERE pt.scanner_name = 'SUPPORT_RESISTANCE_REACTION'
  AND pt.direction = 'LONG'
  AND pt.created_at >= (
    SELECT MIN(created_at) FROM dds.srr_research_signal
    WHERE experiment_id = 'SRR_LONG_OUTCOME_V1'
  );

-- Verify: scanner_direction_gate still shows SRR LONG as BLOCKED
SELECT
    scanner_name, direction, status, reason
FROM config.scanner_direction_gate
WHERE scanner_name = 'SUPPORT_RESISTANCE_REACTION';
