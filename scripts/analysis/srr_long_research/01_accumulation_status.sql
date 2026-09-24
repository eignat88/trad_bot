-- 01_accumulation_status.sql
-- Quick status: how many SRR LONG signals collected, outcomes ready

-- Accumulation overview
SELECT * FROM dds.v_srr_research_accumulation;

-- By symbol
SELECT * FROM dds.v_srr_research_by_symbol;

-- Signals without any outcome yet
SELECT COUNT(*) AS signals_without_outcome
FROM dds.srr_research_signal s
WHERE s.experiment_id = 'SRR_LONG_OUTCOME_V1'
  AND NOT EXISTS (
    SELECT 1 FROM dds.srr_research_outcome o WHERE o.signal_id = s.signal_id
  );

-- Evaluator errors (if any outcome rows have NULL MFE at mature horizons)
SELECT
    s.symbol,
    s.signal_time,
    s.reference_price,
    o.evaluated_60m_at,
    o.mfe_60m,
    o.mae_60m
FROM dds.srr_research_signal s
LEFT JOIN dds.srr_research_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'SRR_LONG_OUTCOME_V1'
  AND s.signal_time < now() - interval '2 hours'
  AND (o.evaluated_60m_at IS NULL OR o.mfe_60m IS NULL)
ORDER BY s.signal_time DESC
LIMIT 20;
