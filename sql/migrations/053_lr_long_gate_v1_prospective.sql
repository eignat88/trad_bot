-- ============================================================
-- Migration 053: LR_LONG_GATE_V1 — LIQUIDITY_REVERSAL LONG prospective OOS
-- ============================================================
-- Seeds the LR_LONG_GATE_V1 experiment for prospective validation
-- of LIQUIDITY_REVERSAL LONG signals.
--
-- Symmetric counterpart to LR_SHORT_GATE_V1 (which was closed 2026-09-30).
--
-- Design:
--   - scanner = LIQUIDITY_REVERSAL
--   - direction = LONG
--   - experiment_type = GATE_VALIDATION
--   - shadow_only (no paper trading)
--   - horizons = 15m/30m/60m/120m/240m
--   - minimum_n = 30, minimum_symbols = 5
--
-- Idempotent: INSERT ON CONFLICT DO NOTHING.
-- Does NOT modify: LR_SHORT_GATE_V1, paper tables, scanner logic.
-- ============================================================

BEGIN;

-- ── Seed: LR_LONG_GATE_V1 ─────────────────────────────────
-- Uses existing prospective_experiment table from migration 050.

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
    'LR_LONG_GATE_V1', 1,
    'LIQUIDITY_REVERSAL', 'LONG', 'GATE_VALIDATION',
    'LIQUIDITY_REVERSAL LONG prospective OOS capture. Symmetric counterpart to LR_SHORT_GATE_V1.',
    'MFE_pct_60m',
    '["MAE_pct_60m","MFE_pct_15m","MFE_pct_30m","MFE_pct_120m","MFE_pct_240m","TP_before_SL","SL_before_TP"]'::jsonb,
    'NONE - capture all',
    NULL,
    'Existing production entry semantics',
    'Existing production invalidation_price',
    'Existing production target_1',
    'shadow_only',
    'none',
    '["15m","30m","60m","120m","240m"]'::jsonb,
    30, 5,
    'discovery_2026-09-30', NULL,
    'READY_TO_START', NULL
)
ON CONFLICT (experiment_id) DO NOTHING;

-- ── Verify ─────────────────────────────────────────────────
-- Should return exactly 1 row with frozen parameters.
-- SELECT experiment_id, scanner_name, direction, experiment_type, status
-- FROM research.prospective_experiment
-- WHERE experiment_id = 'LR_LONG_GATE_V1';

COMMIT;
