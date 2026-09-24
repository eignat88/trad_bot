-- 02_feature_snapshot_examples.sql
-- Example: 10 most recent SRR LONG signals with full feature snapshot

SELECT
    s.signal_id,
    s.symbol,
    s.signal_time,
    s.score,
    s.market_regime,
    s.level_type,
    -- price levels
    ROUND(s.reference_price::numeric, 4) AS level_price,
    ROUND(s.entry_zone_low::numeric, 4) AS entry_low,
    ROUND(s.entry_zone_high::numeric, 4) AS entry_high,
    ROUND(s.invalidation_price::numeric, 4) AS stop_price,
    ROUND(s.target_1::numeric, 4) AS tp1,
    -- raw features
    s.raw_touch_count,
    ROUND(s.raw_level_distance_pct::numeric, 4) AS distance_pct,
    ROUND(s.raw_atr_pct::numeric, 4) AS atr_pct,
    ROUND(s.raw_wick_body_ratio::numeric, 2) AS wick_body,
    ROUND(s.raw_volume_ratio::numeric, 2) AS vol_ratio,
    ROUND(s.raw_rr::numeric, 2) AS rr_raw,
    ROUND(s.raw_risk_distance_pct::numeric, 4) AS risk_pct,
    ROUND(s.raw_stop_distance_atr::numeric, 2) AS stop_atr,
    -- normalised features
    ROUND(s.level_touch_count::numeric, 3) AS touches_norm,
    ROUND(s.rejection_strength::numeric, 3) AS rejection_norm,
    ROUND(s.rr_ratio::numeric, 3) AS rr_norm,
    -- outcome
    o.mfe_60m, o.mae_60m,
    o.mfe_r_60m, o.mae_r_60m,
    o.tp_before_sl, o.sl_before_tp,
    o.is_final
FROM dds.srr_research_signal s
LEFT JOIN dds.srr_research_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'SRR_LONG_OUTCOME_V1'
ORDER BY s.signal_time DESC
LIMIT 10;
