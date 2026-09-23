-- ============================================================
-- ME_R_LONG Early MAE Exit OOS: Variant Summary Report
-- experiment_id = ME_R_LONG_EARLY_MAE_EXIT_OOS_V1
-- ============================================================
-- Use this query to check the overall experiment results
-- grouped by variant_id.
-- ============================================================

SELECT
    variant_id,
    COUNT(*) AS observations,
    COUNT(*) FILTER (
        WHERE status = 'CLOSED'
    ) AS closed,

    COUNT(*) FILTER (
        WHERE rule_triggered
    ) AS triggered,

    ROUND(
        SUM(actual_pnl_r)
        FILTER (WHERE status = 'CLOSED'),
        3
    ) AS actual_total_r,

    ROUND(
        AVG(actual_pnl_r)
        FILTER (WHERE status = 'CLOSED'),
        4
    ) AS actual_expectancy_r,

    ROUND(
        SUM(
            CASE
                WHEN rule_triggered AND status = 'CLOSED'
                    THEN counterfactual_exit_r
                ELSE actual_pnl_r
            END
        )
        FILTER (WHERE status = 'CLOSED'),
        3
    ) AS counterfactual_total_r,

    ROUND(
        AVG(
            CASE
                WHEN rule_triggered AND status = 'CLOSED'
                    THEN counterfactual_exit_r
                ELSE actual_pnl_r
            END
        )
        FILTER (WHERE status = 'CLOSED'),
        4
    ) AS counterfactual_expectancy_r,

    ROUND(
        SUM(delta_r)
        FILTER (
            WHERE status = 'CLOSED'
              AND rule_triggered
        ),
        3
    ) AS saved_r,

    COUNT(*) FILTER (
        WHERE status = 'CLOSED'
          AND rule_triggered
          AND improved = TRUE
    ) AS improved_count,

    COUNT(*) FILTER (
        WHERE status = 'CLOSED'
          AND rule_triggered
          AND improved = FALSE
          AND delta_r < 0
    ) AS worsened_count,

    -- Rescued losses: actual loss, counterfactual less negative
    COUNT(*) FILTER (
        WHERE status = 'CLOSED'
          AND rule_triggered
          AND actual_pnl_r < 0
          AND counterfactual_exit_r > actual_pnl_r
    ) AS rescued_losses,

    -- Spoiled winners: actual win, counterfactual worse
    COUNT(*) FILTER (
        WHERE status = 'CLOSED'
          AND rule_triggered
          AND actual_pnl_r > 0
          AND counterfactual_exit_r < actual_pnl_r
    ) AS spoiled_winners,

    -- Profit factor actual
    CASE
        WHEN SUM(ABS(actual_pnl_r)) FILTER (
            WHERE status = 'CLOSED' AND actual_pnl_r < 0
        ) > 0
        THEN ROUND(
            SUM(actual_pnl_r) FILTER (
                WHERE status = 'CLOSED' AND actual_pnl_r > 0
            )
            / ABS(SUM(actual_pnl_r) FILTER (
                WHERE status = 'CLOSED' AND actual_pnl_r < 0
            )),
            4
        )
        ELSE NULL
    END AS profit_factor_actual,

    -- Profit factor counterfactual
    CASE
        WHEN SUM(ABS(
            CASE
                WHEN rule_triggered AND status = 'CLOSED'
                    THEN counterfactual_exit_r
                ELSE actual_pnl_r
            END
        )) FILTER (
            WHERE status = 'CLOSED'
              AND (
                CASE
                    WHEN rule_triggered AND status = 'CLOSED'
                        THEN counterfactual_exit_r
                    ELSE actual_pnl_r
                END
              ) < 0
        ) > 0
        THEN ROUND(
            SUM(
                CASE
                    WHEN rule_triggered AND status = 'CLOSED'
                        THEN counterfactual_exit_r
                    ELSE actual_pnl_r
                END
            ) FILTER (
                WHERE status = 'CLOSED'
                  AND (
                    CASE
                        WHEN rule_triggered AND status = 'CLOSED'
                            THEN counterfactual_exit_r
                        ELSE actual_pnl_r
                    END
                  ) > 0
            )
            / ABS(SUM(
                CASE
                    WHEN rule_triggered AND status = 'CLOSED'
                        THEN counterfactual_exit_r
                    ELSE actual_pnl_r
                END
            ) FILTER (
                WHERE status = 'CLOSED'
                  AND (
                    CASE
                        WHEN rule_triggered AND status = 'CLOSED'
                            THEN counterfactual_exit_r
                        ELSE actual_pnl_r
                    END
                  ) < 0
            )),
            4
        )
        ELSE NULL
    END AS profit_factor_counterfactual

FROM dds.me_r_long_early_exit_observation
WHERE experiment_id = 'ME_R_LONG_EARLY_MAE_EXIT_OOS_V1'
GROUP BY variant_id
ORDER BY variant_id;
