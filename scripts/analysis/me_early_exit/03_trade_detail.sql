-- ============================================================
-- ME_R_LONG Early MAE Exit OOS: Trade-Level Detail
-- experiment_id = ME_R_LONG_EARLY_MAE_EXIT_OOS_V1
-- ============================================================
-- Shows per-trade detail for the PRIMARY variant (MAE_15m_050)
-- to inspect individual outcomes.
-- ============================================================

SELECT
    trade_id,
    symbol,
    entered_at,
    ROUND(entry_price::numeric, 4) AS entry_price,
    ROUND(stop_price::numeric, 4) AS stop_price,
    ROUND(risk_price::numeric, 4) AS risk_price,
    ROUND(threshold_mae_r::numeric, 4) AS threshold_mae_r,
    ROUND(mae_r_at_eval::numeric, 4) AS mae_r_at_eval,
    ROUND(mfe_r_at_eval::numeric, 4) AS mfe_r_at_eval,
    rule_triggered,
    counterfactual_exit_at,
    ROUND(counterfactual_exit_price::numeric, 4) AS counterfactual_exit_price,
    ROUND(counterfactual_exit_r::numeric, 4) AS counterfactual_exit_r,
    actual_exit_reason,
    ROUND(actual_pnl_r::numeric, 4) AS actual_pnl_r,
    ROUND(delta_r::numeric, 4) AS delta_r,
    improved,
    status
FROM dds.me_r_long_early_exit_observation
WHERE experiment_id = 'ME_R_LONG_EARLY_MAE_EXIT_OOS_V1'
  AND variant_id = 'MAE_15m_050'
ORDER BY entered_at DESC;
