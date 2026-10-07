-- ============================================================
-- Migration 060: SRR Short Execution-R Expansion Prospective Registration
-- ============================================================
-- Registers SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1 as a
-- strictly prospective, observe-only execution-geometry validation experiment.
--
-- Frozen protocol:
--   direction       = SHORT
--   entry           = reference_price
--   structural_R    = abs(reference_price - invalidation_price)
--   SL              = entry + 2.00 * structural_R
--   TP              = entry - 1.50 * structural_R
--   MAX_HOLD        = 120 minutes
--   intrabar        = STOP_FIRST
--   execution_R     = 2.00 * structural_R
--   normal costs    = 0.21% round-trip
--   elevated costs  = 0.31% round-trip sensitivity
--
-- Parent discovery:
--   SRR_SHORT_EXECUTION_R_EXPANSION_COUNTERFACTUAL_V1
--   DISCOVERY ONLY; the historical 688 observations are FORBIDDEN for
--   prospective validation and are never inserted or backfilled here.
--
-- Lifecycle before controlled activation:
--   status     = READY_TO_START
--   started_at = NULL
--   freeze_ts  is registry-only and remains NULL until controlled activation.
--
-- Idempotent: INSERT ON CONFLICT (experiment_id) DO NOTHING.
-- Does NOT modify: existing experiments, observations, outcomes, scanner,
-- paper, live, or any closed SRR direction lifecycle row.
-- ============================================================

BEGIN;

INSERT INTO research.prospective_experiment (
    experiment_id, version, scanner_name, direction, experiment_type,
    hypothesis, primary_metric, secondary_metrics,
    filter_rule, threshold,
    entry_rule, stop_rule, target_rule,
    position_sizing, fee_assumption,
    horizons, minimum_n, minimum_symbols,
    discovery_source, paired_with,
    status, started_at
) VALUES (
    'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1', 1,
    'SUPPORT_RESISTANCE_REACTION', 'SHORT', 'OOS_EXECUTION_VALIDATION',
    'A frozen SRR SHORT execution protocol using entry at reference_price, SL at 2.00 structural_R, TP at 1.50 structural_R, 120-minute max hold, and STOP_FIRST same-candle handling has positive net expectancy after normal project transaction costs on strictly new prospective OOS data.',
    'Net E[R] after normal project costs, normalized by execution_R = 2.00 * structural_R',
    '["Gross E[R]","Net PF normal","Gross PF","Total Net R","win rate","TP count","SL count","AMBIGUOUS count","TIMEOUT count","normal cost_R distribution","elevated Net E[R]","elevated PF","day concentration","symbol concentration"]'::jsonb,
    'NONE - capture every valid new SUPPORT_RESISTANCE_REACTION SHORT candidate after strict freeze; no signal filter, no cost_R eligibility threshold, no symbol/regime/score filter.',
    NULL,
    'FROZEN: entry = reference_price; structural_R = abs(reference_price - invalidation_price); entry > 0, invalidation_price > 0, structural_R > 0; capture only signal_time > freeze_ts.',
    'FROZEN SHORT stop = entry + 2.00 * structural_R; execution_R = 2.00 * structural_R.',
    'FROZEN SHORT target = entry - 1.50 * structural_R; MAX_HOLD = 120 minutes; timeout at last eligible 5m candle close before cutoff; PRIMARY_INTRABAR_POLICY = STOP_FIRST.',
    'shadow_only',
    'normal round-trip 0.21%; elevated sensitivity round-trip 0.31%; observe-only and no paper/live execution.',
    '["15m","30m","60m","120m","240m"]'::jsonb,
    300, 30,
    'SRR_SHORT_EXECUTION_R_EXPANSION_COUNTERFACTUAL_V1', NULL,
    'READY_TO_START', NULL
)
ON CONFLICT (experiment_id) DO NOTHING;

-- ── Verify ─────────────────────────────────────────────────
-- Expected: exactly one row, status READY_TO_START, started_at NULL.
-- No observation or outcome rows may exist before activation.
-- SELECT experiment_id, scanner_name, direction, experiment_type, status, started_at
-- FROM research.prospective_experiment
-- WHERE experiment_id = 'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1';
--
-- SELECT COUNT(*) FROM research.prospective_observation
-- WHERE experiment_id = 'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1';
-- SELECT COUNT(*) FROM research.prospective_outcome
-- WHERE experiment_id = 'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1';

COMMIT;

-- ============================================================
-- NO production tables modified.
-- NO historical observations/outcomes inserted.
-- NO scanner, paper, live, or lifecycle behavior changed.
-- ============================================================
