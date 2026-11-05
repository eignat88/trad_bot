-- =====================================================================
-- SRR research outcome candle coverage audit — READ-ONLY SQL
-- (ISSUE244 / OOS_SHADOW_CANDLE_COVERAGE_FAIL_CLOSED_V1)
--
-- Experiment restriction: EVERY statement below is hard-limited to
--     experiment_id = 'SRR_LONG_OUTCOME_V1'
--
-- READ-ONLY contract:
--   * SELECT statements only — no DML, no DDL, no migrations, no
--     modification of historical outcomes, no backfill, no recalculation;
--   * the two export commands are psql CLIENT meta-commands (\copy): they
--     write a local CSV file on the machine running psql and NEVER store
--     data inside PostgreSQL;
--   * this file is executed only when an operator runs it manually
--     (psql -f ...).  The Python audit module
--     (tools/research/srr_research_outcome_candle_coverage_audit_v1.py)
--     embeds byte-equivalent copies of these six statements, NEVER reads
--     or executes this file, and never connects to any database.  The
--     consistency between both copies is checked by
--     tests/test_srr_research_outcome_candle_coverage_audit_v1.py (A12).
--
-- Usage (from the repository root):
--   psql "$CONN" -f sql/srr_research_outcome_candle_coverage_audit_v1.sql
-- prints the four aggregate diagnostics and writes the two CSVs:
--   srr_coverage_outcomes.csv   (17 columns, order = OUTCOME_COLUMNS)
--   srr_coverage_signals.csv    (4 columns,  order = SIGNAL_COLUMNS)
-- then analyze offline:
--   python -m tools.research.srr_research_outcome_candle_coverage_audit_v1 \
--       --outcomes srr_coverage_outcomes.csv \
--       [--signals srr_coverage_signals.csv]
-- =====================================================================

-- name: SQL_TOTALS
SELECT
    (SELECT COUNT(*)
       FROM dds.srr_research_signal
      WHERE experiment_id = 'SRR_LONG_OUTCOME_V1')        AS signals,
    COUNT(*)                                              AS outcomes,
    COUNT(*) FILTER (WHERE o.is_final)                    AS finalized,
    COUNT(*) FILTER (WHERE NOT o.is_final)                AS non_final,
    COUNT(DISTINCT o.symbol)                              AS symbols
FROM dds.srr_research_outcome o
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
;

-- name: SQL_FINALIZED_MISSING_MARKS
SELECT COUNT(*) AS finalized_missing_marks
FROM dds.srr_research_outcome o
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
  AND o.is_final
  AND (o.evaluated_15m_at IS NULL
       OR o.evaluated_30m_at IS NULL
       OR o.evaluated_60m_at IS NULL
       OR o.evaluated_120m_at IS NULL
       OR o.evaluated_240m_at IS NULL)
;

-- name: SQL_EARLY_MARKS
SELECT COUNT(*) AS early_marks
FROM dds.srr_research_outcome o
JOIN dds.srr_research_signal s ON s.signal_id = o.signal_id
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
  AND ((o.evaluated_15m_at  IS NOT NULL AND o.evaluated_15m_at  < s.signal_time + interval '15 minutes')
    OR (o.evaluated_30m_at  IS NOT NULL AND o.evaluated_30m_at  < s.signal_time + interval '30 minutes')
    OR (o.evaluated_60m_at  IS NOT NULL AND o.evaluated_60m_at  < s.signal_time + interval '60 minutes')
    OR (o.evaluated_120m_at IS NOT NULL AND o.evaluated_120m_at < s.signal_time + interval '120 minutes')
    OR (o.evaluated_240m_at IS NOT NULL AND o.evaluated_240m_at < s.signal_time + interval '240 minutes'))
;

-- name: SQL_NULL_MARK_VALUES
SELECT COUNT(*) AS null_mark_values
FROM dds.srr_research_outcome o
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
  AND ((o.evaluated_15m_at  IS NOT NULL AND o.mfe_15m  IS NULL)
    OR (o.evaluated_30m_at  IS NOT NULL AND o.mfe_30m  IS NULL)
    OR (o.evaluated_60m_at  IS NOT NULL AND o.mfe_60m  IS NULL)
    OR (o.evaluated_120m_at IS NOT NULL AND o.mfe_120m IS NULL)
    OR (o.evaluated_240m_at IS NOT NULL AND o.mfe_240m IS NULL))
;

-- name: SQL_OUTCOMES_EXPORT
-- outcomes.csv export: client-side file write (17 columns, order fixed
-- by OUTCOME_COLUMNS in the Python module).
\copy (
SELECT
    o.signal_id,
    o.experiment_id,
    o.symbol,
    s.signal_time,
    o.is_final,
    o.evaluated_15m_at,
    o.evaluated_30m_at,
    o.evaluated_60m_at,
    o.evaluated_120m_at,
    o.evaluated_240m_at,
    o.mfe_15m,
    o.mfe_30m,
    o.mfe_60m,
    o.mfe_120m,
    o.mfe_240m,
    o.created_at,
    o.updated_at
FROM dds.srr_research_outcome o
JOIN dds.srr_research_signal s ON s.signal_id = o.signal_id
WHERE o.experiment_id = 'SRR_LONG_OUTCOME_V1'
ORDER BY s.signal_time, o.signal_id
) TO 'srr_coverage_outcomes.csv' WITH CSV HEADER

-- name: SQL_SIGNALS_EXPORT
-- signals.csv export: client-side file write (4 columns, order fixed by
-- SIGNAL_COLUMNS in the Python module).
\copy (
SELECT signal_id, experiment_id, symbol, signal_time
FROM dds.srr_research_signal
WHERE experiment_id = 'SRR_LONG_OUTCOME_V1'
ORDER BY signal_time, signal_id
) TO 'srr_coverage_signals.csv' WITH CSV HEADER
