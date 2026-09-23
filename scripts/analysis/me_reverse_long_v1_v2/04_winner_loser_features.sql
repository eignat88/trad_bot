-- 04_winner_loser_features.sql
-- Feature comparison: winners vs losers for V1 and V2
-- Uses features stored in dds.paper_trade.features JSONB

WITH trades AS (
    SELECT
        pt.trade_id,
        pt.scanner_name,
        pt.symbol,
        pt.direction,
        pt.pnl_r,
        pt.pnl_usdt,
        pt.mfe,
        pt.mae,
        pt.exit_reason,
        pt.entered_at,
        pt.closed_at,
        pt.duration_sec,
        pt.entry_price,
        pt.exit_price,
        pt.stop_price,
        -- JSONB features extraction
        (pt.features->>'exhaustion_magnitude')::numeric AS exhaustion_magnitude,
        (pt.features->>'body_ratio')::numeric AS body_ratio,
        (pt.features->>'rsi_confirmation')::numeric AS rsi_confirmation,
        (pt.features->>'volume_ratio')::numeric AS volume_ratio,
        (pt.features->>'rr_ratio')::numeric AS rr_ratio,
        (pt.features->>'stop_distance_atr')::numeric AS stop_distance_atr,
        (pt.features->>'rsi_14')::numeric AS rsi_14,
        (pt.features->>'rsi_delta_3')::numeric AS rsi_delta_3,
        -- Compute derived features
        CASE
            WHEN pt.pnl_r > 0 THEN 'WIN'
            WHEN pt.pnl_r <= 0 AND pt.pnl_r > -0.9 THEN 'LOSS'
            ELSE 'HARD_LOSS'
        END AS outcome_group,
        CASE
            WHEN pt.pnl_r >= 0.8 THEN 'GOOD_WIN'
            ELSE 'OTHER'
        END AS win_quality
    FROM dds.paper_trade pt
    WHERE pt.scanner_name IN (
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
        'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
    )
    AND pt.direction = 'LONG'
    AND pt.status = 'CLOSED'
)
SELECT
    scanner_name,
    outcome_group,
    COUNT(*) AS count,
    ROUND(AVG(exhaustion_magnitude), 4) AS avg_exhaustion_mag,
    ROUND(AVG(body_ratio), 4) AS avg_body_ratio,
    ROUND(AVG(rsi_confirmation), 4) AS avg_rsi_confirm,
    ROUND(AVG(volume_ratio), 4) AS avg_volume_ratio,
    ROUND(AVG(rr_ratio), 4) AS avg_rr_ratio,
    ROUND(AVG(stop_distance_atr), 4) AS avg_stop_dist_atr,
    ROUND(AVG(rsi_14), 2) AS avg_rsi_14,
    ROUND(AVG(rsi_delta_3), 4) AS avg_rsi_delta_3,
    ROUND(AVG(pnl_r), 4) AS avg_pnl_r,
    ROUND(AVG(duration_sec)/60, 1) AS avg_hold_minutes
FROM trades
GROUP BY scanner_name, outcome_group
ORDER BY scanner_name, outcome_group;
