-- ============================================================
-- Migration 058: HTF Point-B Prospective Experiment Registration
-- ============================================================
-- Registers the HTF key-level Point-B prospective validation and
-- its observational key-level baseline cohort.
--
-- IMPORTANT:
--   This migration DOES NOT activate collection.
--   status     = READY_TO_START
--   started_at = NULL
--
-- Runtime HTF capture additionally requires a frozen registry
-- freeze_ts and enforces signal_time > freeze_ts.
--
-- Research-only / observe-only.  No paper/live/order behaviour.
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
) VALUES
(
    'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE', 1,
    'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE',
    'LONG',
    'PROSPECTIVE_EDGE_VALIDATION',
    'After causal HTF support/resistance interaction, reaction and local structure break, the first valid Point-B retracement may identify a reproducible directional edge. Validate prospectively without historical backfill.',
    'MFE_R_60m',
    '["MAE_R_60m","MFE_R_30m","MFE_R_120m","MFE_R_240m","TP_before_SL","SL_before_TP","MFE_pct_60m","MAE_pct_60m"]'::jsonb,
    'FROZEN DETECTOR: first valid Point-B wins; strictly post-freeze only; LONG and SHORT observed independently.',
    NULL,
    'FROZEN: entry at close/reference_price of the first qualifying Point-B candle.',
    'FROZEN: structural stop at reaction extreme; risk_abs must be strictly positive.',
    'FROZEN: detector target geometry captured at detection time; prospective path evaluated through 30m/60m/120m/240m.',
    'shadow_only',
    'none for path-health metrics; execution-cost validation requires a separately frozen execution protocol',
    '["30m","60m","120m","240m"]'::jsonb,
    300,
    30,
    'HTF_KEYLEVEL_POINT_B_EDGE_V1',
    'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE',
    'READY_TO_START',
    NULL
),
(
    'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE', 1,
    'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE',
    'LONG',
    'BASELINE_CONTROL',
    'Observational control cohort for the same HTF key-level setup family, retained to compare Point-B timing/geometry against the detector baseline without suppressing a valid Point-B candidate.',
    'MFE_R_60m',
    '["MAE_R_60m","MFE_R_30m","MFE_R_120m","MFE_R_240m","TP_before_SL","SL_before_TP","MFE_pct_60m","MAE_pct_60m"]'::jsonb,
    'OBSERVATIONAL CONTROL ONLY; baseline unavailability must never suppress a valid Point-B completion; strictly post-freeze only.',
    NULL,
    'FROZEN: baseline reference_price emitted by the detector when valid.',
    'FROZEN: baseline invalidation geometry emitted by the detector when valid and strictly positive-risk.',
    'FROZEN: baseline target geometry emitted by the detector.',
    'shadow_only',
    'none; observational control only',
    '["30m","60m","120m","240m"]'::jsonb,
    300,
    30,
    'HTF_KEYLEVEL_POINT_B_EDGE_V1',
    'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE',
    'READY_TO_START',
    NULL
)
ON CONFLICT (experiment_id) DO NOTHING;

COMMIT;
