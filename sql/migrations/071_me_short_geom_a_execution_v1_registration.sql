-- ============================================================
-- Migration 071: ME SHORT Geometry A Execution V1 Registration
-- ============================================================
-- Registers a clean prospective observe-only validation of the retained
-- ME_SHORT_GEOM_A_V1 control geometry.
--
-- Frozen protocol:
--   FREEZE_TS      = 2026-10-19T08:26:00Z
--   direction      = SHORT
--   entry          = reference_price
--   stop           = invalidation_price
--   target         = target_1
--   max hold       = 240 minutes
--   intrabar       = STOP_FIRST
--   normal costs   = taker 0.055% + slippage 0.05% per side
--   elevated costs = taker 0.055% + slippage 0.10% per side
--
-- Registration only. Capture remains fail-closed until migration 072
-- activates the DB lifecycle row at the exact same boundary.
-- No historical backfill. No paper/live order routing.
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
    'ME_SHORT_GEOM_A_EXECUTION_V1', 1,
    'MOMENTUM_EXHAUSTION', 'SHORT', 'EXECUTION_PROSPECTIVE_VALIDATION',
    'The retained ME SHORT geometry A control remains profitable under a clean strictly post-freeze execution-validation cohort with frozen costs and exit semantics.',
    'Net E[R] after frozen normal execution costs',
    '["Gross E[R]","Net E[R]","Gross PF","Net PF","win rate","TP count","SL count","timeout count","ambiguous count","total Net R","max drawdown","max consecutive losses","day concentration","symbol concentration","regime concentration"]'::jsonb,
    'NONE - capture every valid new MOMENTUM_EXHAUSTION SHORT candidate strictly after freeze_ts; no post-hoc signal filter.',
    NULL,
    'FROZEN: immediate SHORT entry = reference_price; signal_time > 2026-10-19T08:26:00Z.',
    'FROZEN: stop = invalidation_price; require invalidation_price > reference_price and structural_R > 0.',
    'FROZEN: target = target_1; require target_1 < reference_price; MAX_HOLD = 240 minutes; PRIMARY_INTRABAR_POLICY = STOP_FIRST.',
    'shadow_only',
    'taker 0.055% per side + normal slippage 0.05% per side; elevated slippage sensitivity 0.10% per side.',
    '["15m","30m","60m","120m","240m"]'::jsonb,
    200, 20,
    'ME_SHORT_GEOMETRY_ABC_PAIRED_VALIDATION_V1', NULL,
    'READY_TO_START', NULL
)
ON CONFLICT (experiment_id) DO NOTHING;

COMMIT;
