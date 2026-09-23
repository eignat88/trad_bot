-- ============================================================
-- ME_R_LONG Early MAE Exit OOS: PRIMARY Rule Report
-- experiment_id = ME_R_LONG_EARLY_MAE_EXIT_OOS_V1
-- variant_id    = MAE_15m_050  (PRIMARY)
-- ============================================================
-- Displays the primary experiment result:
-- 15m / MAE >= 0.50R
-- ============================================================

SELECT
    'PRIMARY' AS report_type,
    '15m / MAE >= 0.50R' AS rule_description,
    COUNT(*) AS observations,
    COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
    COUNT(*) FILTER (WHERE rule_triggered) AS triggered,

    ROUND(
        SUM(actual_pnl_r) FILTER (WHERE status = 'CLOSED'),
        3
    ) AS actual_total_r,

    ROUND(
        AVG(actual_pnl_r) FILTER (WHERE status = 'CLOSED'),
        4
    ) AS actual_expectancy_r,

    ROUND(
        SUM(
            CASE
                WHEN rule_triggered AND status = 'CLOSED'
                    THEN counterfactual_exit_r
                ELSE actual_pnl_r
            END
        ) FILTER (WHERE status = 'CLOSED'),
        3
    ) AS counterfactual_total_r,

    ROUND(
        AVG(
            CASE
                WHEN rule_triggered AND status = 'CLOSED'
                    THEN counterfactual_exit_r
                ELSE actual_pnl_r
            END
        ) FILTER (WHERE status = 'CLOSED'),
        4
    ) AS counterfactual_expectancy_r,

    ROUND(
        SUM(delta_r) FILTER (
            WHERE status = 'CLOSED' AND rule_triggered
        ),
        3
    ) AS saved_r,

    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND rule_triggered AND improved = TRUE
    ) AS improved,
    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND rule_triggered AND improved = FALSE AND delta_r < 0
    ) AS worsened,

    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND rule_triggered
          AND actual_pnl_r < 0 AND counterfactual_exit_r > actual_pnl_r
    ) AS rescued_losses,
    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND rule_triggered
          AND actual_pnl_r > 0 AND counterfactual_exit_r < actual_pnl_r
    ) AS spoiled_winners

FROM dds.me_r_long_early_exit_observation
WHERE experiment_id = 'ME_R_LONG_EARLY_MAE_EXIT_OOS_V1'
  AND variant_id = 'MAE_15m_050';
