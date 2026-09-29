-- ============================================================
-- Migration 052: SRR OOS Prospective Experiment
-- ============================================================
-- Seeds the SRR_OOS_SCANNER_V1_PROSPECTIVE experiment for
-- prospective validation of the frozen OOS exit geometry:
--   SL = 0.75R, TP = 1.50R, MAX_HOLD = 120m
--
-- This experiment captures BOTH LONG and SHORT from the
-- existing SUPPORT_RESISTANCE_REACTION scanner.
--
-- Historical context:
--   SRR_GENERIC_V1 (N=543) showed:
--     SHORT net E[R] = +0.48R, LONG net E[R] = +0.37R
--     after costs with this exit geometry.
--
-- Idempotent: INSERT ON CONFLICT DO NOTHING.
-- Does NOT modify: SRR_GENERIC_V1, paper tables, scanner logic.
-- ============================================================

BEGIN;

-- ── Seed: SRR_OOS_SCANNER_V1_PROSPECTIVE ───────────────────
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
    'SRR_OOS_SCANNER_V1_PROSPECTIVE', 1,
    'SUPPORT_RESISTANCE_REACTION', 'LONG', 'OOS_EXIT_VALIDATION',
    'SRR_GENERIC_V1 showed positive expectancy with SL=0.75R/TP=1.5R/MAX_HOLD=120m. '
    'Prospective validation with frozen OOS exit geometry on BOTH LONG and SHORT.',
    'MFE_R_60m',
    '["MAE_R_60m","MFE_R_15m","MFE_R_30m","MFE_R_120m","MFE_R_240m","TP_before_SL","SL_before_TP","MFE_pct_60m","MAE_pct_60m"]'::jsonb,
    'NONE - capture all SRR candidates, apply frozen OOS exit geometry',
    NULL,
    'FROZEN: entry at reference_price (rejection candle close)',
    'FROZEN: for LONG, stop = entry - 0.75R; for SHORT, stop = entry + 0.75R; where R = abs(reference_price - invalidation_price)',
    'FROZEN: for LONG, target = entry + 1.50R; for SHORT, target = entry - 1.50R; where R = abs(reference_price - invalidation_price)',
    'shadow_only',
    'taker 0.055% per side + 0.05% slippage per side = 0.21% round-trip',
    '["15m","30m","60m","120m","240m"]'::jsonb,
    50, 10,
    'srr_generic_v1_oos_analysis', NULL,
    'READY_TO_START', NULL
)
ON CONFLICT (experiment_id) DO NOTHING;

-- ── Verify ─────────────────────────────────────────────────
-- Should return exactly 1 row with frozen parameters.
-- SELECT experiment_id, scanner_name, direction, experiment_type, status
-- FROM research.prospective_experiment
-- WHERE experiment_id = 'SRR_OOS_SCANNER_V1_PROSPECTIVE';

COMMIT;
