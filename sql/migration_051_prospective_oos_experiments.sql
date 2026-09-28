-- Migration 051: Add Prospective OOS Experiments for Expectancy/Regime Rejected Candidates
-- This migration adds three new experiments to the research.prospective_experiment table
-- and updates the prospective_registry.json to include these experiments.

-- 1. Add BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1 experiment
INSERT INTO research.prospective_experiment (
    experiment_id,
    version,
    scanner_name,
    direction,
    experiment_type,
    hypothesis,
    primary_metric,
    secondary_metrics,
    filter_rule,
    threshold,
    entry_rule,
    stop_rule,
    target_rule,
    position_sizing,
    fee_assumption,
    horizons,
    minimum_n,
    minimum_symbols,
    discovery_source,
    started_at,
    status
) VALUES (
    'BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1',
    1,
    'BREAKOUT_RETEST',
    'LONG',
    'EXPECTANCY_REJECTION_OOS',
    'BREAKOUT_RETEST LONG has PF=1.0976 (below 1.20 threshold). Prospective capture to check if new signals have positive edge despite historical performance.',
    'MFE_R_60m',
    '["MAE_R_60m", "MFE_R_15m", "MFE_R_30m", "MFE_R_120m", "MFE_R_240m", "TP_before_SL", "SL_before_TP", "MFE_pct_60m", "MAE_pct_60m"]'::jsonb,
    'CAPTURE: candidates that pass risk_geometry, score_gate, direction_gate, but REJECTED by expectancy_filter (profit_factor < 1.20)',
    1.20,
    'Existing production entry semantics',
    'Existing production invalidation_price',
    'Existing production target_1',
    'shadow_only',
    'none',
    '["15m", "30m", "60m", "120m", "240m"]'::jsonb,
    50,
    10,
    'scanner_audit_phase2_report.md',
    NULL,
    'READY_TO_START'
)
ON CONFLICT (experiment_id) DO NOTHING;

-- 2. Add FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1 experiment
INSERT INTO research.prospective_experiment (
    experiment_id,
    version,
    scanner_name,
    direction,
    experiment_type,
    hypothesis,
    primary_metric,
    secondary_metrics,
    filter_rule,
    threshold,
    entry_rule,
    stop_rule,
    target_rule,
    position_sizing,
    fee_assumption,
    horizons,
    minimum_n,
    minimum_symbols,
    discovery_source,
    started_at,
    status
) VALUES (
    'FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1',
    1,
    'FVG_REACTION_LONG_LOCAL_STRUCT_V1',
    'LONG',
    'EXPECTANCY_REJECTION_OOS',
    'FVG_REACTION_LONG has negative historical expectancy (avg_r = -0.42). Prospective capture to check if there''s a subset with positive edge within the bad overall sample.',
    'MFE_R_60m',
    '["MAE_R_60m", "MFE_R_15m", "MFE_R_30m", "MFE_R_120m", "MFE_R_240m", "TP_before_SL", "SL_before_TP", "MFE_pct_60m", "MAE_pct_60m"]'::jsonb,
    'CAPTURE: candidates that pass risk_geometry, score_gate, direction_gate, but REJECTED by expectancy_filter (negative performance)',
    0.0,
    'Existing production entry semantics',
    'Existing production invalidation_price',
    'Existing production target_1',
    'shadow_only',
    'none',
    '["15m", "30m", "60m", "120m", "240m"]'::jsonb,
    50,
    10,
    'scanner_audit_phase2_report.md',
    NULL,
    'READY_TO_START'
)
ON CONFLICT (experiment_id) DO NOTHING;

-- 3. Add TREND_PULLBACK_V3_HIGH_VOL_OOS_V1 experiment
INSERT INTO research.prospective_experiment (
    experiment_id,
    version,
    scanner_name,
    direction,
    experiment_type,
    hypothesis,
    primary_metric,
    secondary_metrics,
    filter_rule,
    threshold,
    entry_rule,
    stop_rule,
    target_rule,
    position_sizing,
    fee_assumption,
    horizons,
    minimum_n,
    minimum_symbols,
    discovery_source,
    started_at,
    status
) VALUES (
    'TREND_PULLBACK_V3_HIGH_VOL_OOS_V1',
    1,
    'TREND_PULLBACK_V3',
    'LONG',
    'REGIME_COUNTERFACTUAL_OOS',
    'TREND_PULLBACK_V3 requires TREND_UP regime but current market is HIGH_VOLATILITY. Counterfactual OOS to check if signals would have been profitable in current regime.',
    'MFE_R_60m',
    '["MAE_R_60m", "MFE_R_15m", "MFE_R_30m", "MFE_R_120m", "MFE_R_240m", "TP_before_SL", "SL_before_TP", "MFE_pct_60m", "MAE_pct_60m"]'::jsonb,
    'CAPTURE: candidates that pass all filters EXCEPT regime_filter (actual=HIGH_VOLATILITY, required=TREND_UP)',
    NULL,
    'Existing production entry semantics',
    'Existing production invalidation_price',
    'Existing production target_1',
    'shadow_only',
    'none',
    '["15m", "30m", "60m", "120m", "240m"]'::jsonb,
    50,
    10,
    'scanner_audit_phase2_report.md',
    NULL,
    'READY_TO_START'
)
ON CONFLICT (experiment_id) DO NOTHING;

-- 4. Update the prospective_registry.json to include these experiments
-- This is done in code, not in SQL, but we document the expected state

-- 5. Verify the experiments were created
SELECT 
    experiment_id,
    scanner_name,
    direction,
    experiment_type,
    status,
    started_at
FROM research.prospective_experiment 
WHERE experiment_id IN (
    'BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1',
    'FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1',
    'TREND_PULLBACK_V3_HIGH_VOL_OOS_V1'
)
ORDER BY experiment_id;
