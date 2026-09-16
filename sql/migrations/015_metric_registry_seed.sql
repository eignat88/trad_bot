-- Migration 015: Metric Registry Seed
-- Seeds the initial set of canonical metrics for Stage 2.
--
-- Each metric has:
--   - formula_sql_or_code_ref: human-readable formula
--   - units: USDT, R, ratio, count, etc.
--   - grain: which fact table it applies to
--   - direction_convention: LONG=+1 SHORT=-1
--   - fee_model_version: tracks fee model changes
--
-- Idempotent: uses INSERT ... ON CONFLICT DO NOTHING.

INSERT INTO analytics.metric_registry
    (metric_name, metric_version, formula_sql_or_code_ref, units, grain,
     direction_convention, fee_model_version, valid_from, deprecated_at)
VALUES
    -- ============================================================
    -- PnL Metrics
    -- ============================================================
    ('gross_pnl', '1.0.0',
     'CASE direction WHEN ''LONG'' THEN (exit_price - initial_fill_price) * final_quantity
                     WHEN ''SHORT'' THEN (initial_fill_price - exit_price) * final_quantity END',
     'USDT', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('net_pnl', '1.0.0',
     'gross_pnl - entry_fee - dca_fee - exit_fee - funding - slippage_cost',
     'USDT', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('pnl_r', '1.0.0',
     'direction_sign * (exit_price - reference_price) / initial_risk_distance',
     'R', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    -- ============================================================
    -- Risk Metrics
    -- ============================================================
    ('mfe_r', '1.0.0',
     'MAX(0, direction_sign * (max_favorable_price - reference_price) / initial_risk_distance)',
     'R', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('mae_r', '1.0.0',
     'MIN(0, direction_sign * (max_adverse_price - reference_price) / initial_risk_distance)',
     'R', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('initial_risk_distance', '1.0.0',
     '|reference_price - initial_stop|  (fixed before first fill, never changes after DCA)',
     'price', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('risk_usdt', '1.0.0',
     'position_size * initial_risk_distance',
     'USDT', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    -- ============================================================
    -- Fee / Cost Metrics
    -- ============================================================
    ('total_fees', '1.0.0',
     'entry_fee + dca_fee + exit_fee',
     'USDT', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('total_slippage', '1.0.0',
     'entry_slippage + dca_slippage',
     'USDT', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('cost_per_r', '1.0.0',
     '(total_fees + total_slippage + funding) / ABS(pnl_r)',
     'USDT/R', 'trade_fact', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    -- ============================================================
    -- Horizon Metrics
    -- ============================================================
    ('favorable_move_r', '1.0.0',
     'direction_sign * (max_price_in_horizon - anchor_price) / initial_risk_distance',
     'R', 'trade_horizon_metric', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('adverse_move_r', '1.0.0',
     'direction_sign * (min_price_in_horizon - anchor_price) / initial_risk_distance',
     'R', 'trade_horizon_metric', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('late_entry_loss_r', '1.0.0',
     'direction_sign * (actual_entry_price - signal_price) / initial_risk_distance',
     'R', 'trade_horizon_metric', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('post_exit_opportunity_r', '1.0.0',
     'direction_sign * (best_price_after_exit - exit_price) / initial_risk_distance',
     'R', 'trade_horizon_metric', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('continuation_r', '1.0.0',
     'direction_sign * (price_at_horizon - exit_price) / initial_risk_distance',
     'R', 'trade_horizon_metric', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('recovery_r', '1.0.0',
     'direction_sign * (price_at_horizon - reference_price) / initial_risk_distance',
     'R', 'trade_horizon_metric', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    -- ============================================================
    -- Aggregate Metrics
    -- ============================================================
    ('win_rate', '1.0.0',
     'COUNT(CASE WHEN pnl_r > 0 THEN 1 END) / COUNT(*)',
     'ratio', 'metric_snapshot', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('profit_factor', '1.0.0',
     'SUM(CASE WHEN pnl_r > 0 THEN pnl_r ELSE 0 END) / ABS(SUM(CASE WHEN pnl_r < 0 THEN pnl_r ELSE 0 END))',
     'ratio', 'metric_snapshot', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('avg_r', '1.0.0',
     'AVG(pnl_r)',
     'R', 'metric_snapshot', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('total_pnl_usdt', '1.0.0',
     'SUM(net_pnl)',
     'USDT', 'metric_snapshot', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('sharpe_approx', '1.0.0',
     'AVG(pnl_r) / NULLIF(STDDEV(pnl_r), 0)',
     'ratio', 'metric_snapshot', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL),

    ('expectancy_r', '1.0.0',
     'win_rate * avg_win_r + (1 - win_rate) * avg_loss_r',
     'R', 'metric_snapshot', 'LONG=+1 SHORT=-1', '1.0.0', NOW(), NULL)

ON CONFLICT (metric_name, metric_version) DO NOTHING;
