-- ============================================================
-- Migration 055: VC_SHORT_EXECUTION_V1 — VC SHORT execution validation
-- ============================================================
-- Seeds the already-frozen VC_SHORT_EXECUTION_V1 experiment for
-- prospective research capture/evaluation.
--
-- Frozen protocol:
--   FREEZE_TS = 2026-10-02T07:30:21Z
--   filter = bb_width_percentile < 0.569723
--   entry = reference_price
--   SL = invalidation_price
--   TP = target_1
--   MAX_HOLD = 120 minutes
--   intrabar = STOP_FIRST
--   shadow_only / research-only
--
-- Idempotent: INSERT ON CONFLICT DO NOTHING.
-- Does NOT modify: existing experiments, scanner, paper, live, or config.
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
    'VC_SHORT_EXECUTION_V1', 1,
    'VOLATILITY_COMPRESSION', 'SHORT', 'EXECUTION_PROSPECTIVE_VALIDATION',
    'VC SHORT signals satisfying the frozen BB-width filter can become executable under pre-frozen structural geometry and project costs.',
    'Net E[R] after normal project costs',
    '["Gross E[R]","Net E[R]","Gross PF","Net PF","win rate","TP count","SL count","timeout count","ambiguous count","total Net R","max losing streak"]'::jsonb,
    'PASS: bb_width_percentile < 0.569723. Strictly post-freeze only.',
    0.569723,
    'FROZEN: immediate SHORT entry at reference_price; signal_time > 2026-10-02T07:30:21Z.',
    'FROZEN: SL = invalidation_price; structural_R = abs(reference_price - invalidation_price); invalidation_price > reference_price.',
    'FROZEN: TP = target_1; target_1 < reference_price; MAX_HOLD = 120 minutes; timeout at deterministic 5m candle close at/after timeout; PRIMARY_INTRABAR_POLICY = STOP_FIRST.',
    'shadow_only',
    'taker 0.055% per side + normal slippage 0.05% per side; elevated sensitivity 0.10% per side.',
    '["15m","30m","60m","120m","240m"]'::jsonb,
    50, 10,
    'VC_SHORT_EXECUTION_PROSPECTIVE_PROTOCOL_V1', NULL,
    'READY_TO_START', NULL
)
ON CONFLICT (experiment_id) DO NOTHING;

COMMIT;
