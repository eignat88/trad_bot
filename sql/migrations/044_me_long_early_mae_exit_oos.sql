-- ============================================================
-- Shadow/OOS Experiment: ME_R_LONG Early MAE Exit
-- ME_R_LONG_EARLY_MAE_EXIT_OOS_V1
-- ============================================================
-- Hypothesis: If MAE >= 0.50R within 15 minutes after entry,
-- early exit at 15m close may substantially reduce the loss
-- compared to current paper trading logic.
--
-- This table records per-observation counterfactual evaluations
-- for MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG paper trades.
-- NO production behavior is changed.
--
-- PRIMARY rule (frozen):
--   evaluation_age = 15 minutes
--   MAE threshold  = 0.50R
--   IF trade_age >= 15 min AND MAE_15m >= 0.50R THEN shadow_exit = TRUE
--
-- Additional shadow variants computed in parallel:
--   MAE_10m >= 0.40R    MAE_10m >= 0.50R
--   MAE_15m >= 0.40R    MAE_15m >= 0.50R (PRIMARY)   MAE_15m >= 0.60R   MAE_15m >= 0.75R
--   MAE_30m >= 0.50R
-- ============================================================

CREATE TABLE IF NOT EXISTS dds.me_r_long_early_exit_observation (
    observation_id          BIGSERIAL PRIMARY KEY,

    -- Experiment identity
    experiment_id           TEXT NOT NULL DEFAULT 'ME_R_LONG_EARLY_MAE_EXIT_OOS_V1',
    variant_id              TEXT NOT NULL,

    -- Source paper trade
    trade_id                BIGINT NOT NULL REFERENCES dds.paper_trade(trade_id),
    symbol                  TEXT NOT NULL,
    scanner_name            TEXT NOT NULL,
    direction               TEXT NOT NULL DEFAULT 'LONG',

    -- Entry parameters
    entered_at              TIMESTAMPTZ NOT NULL,
    entry_price             NUMERIC NOT NULL,
    stop_price              NUMERIC NOT NULL,
    risk_price              NUMERIC NOT NULL,

    -- Evaluation point
    evaluation_at           TIMESTAMPTZ,
    evaluation_age_min      NUMERIC,

    -- MFE/MAE at evaluation point
    mfe_r_at_eval           NUMERIC,
    mae_r_at_eval           NUMERIC,

    -- Shadow exit rule
    threshold_mae_r         NUMERIC NOT NULL,
    rule_triggered          BOOLEAN NOT NULL DEFAULT FALSE,

    -- Counterfactual exit
    counterfactual_exit_at  TIMESTAMPTZ,
    counterfactual_exit_price NUMERIC,
    counterfactual_exit_r   NUMERIC,

    -- Actual trade outcome (filled when paper_trade closes)
    actual_closed_at        TIMESTAMPTZ,
    actual_exit_reason      TEXT,
    actual_pnl_r            NUMERIC,

    -- Delta
    delta_r                 NUMERIC,
    improved                BOOLEAN,

    -- Status lifecycle: PENDING -> EVALUATED -> CLOSED | ERROR
    status                  TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'EVALUATED', 'CLOSED', 'ERROR')),

    -- Metadata
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_me_early_exit_experiment
    ON dds.me_r_long_early_exit_observation (experiment_id);
CREATE INDEX IF NOT EXISTS idx_me_early_exit_trade
    ON dds.me_r_long_early_exit_observation (trade_id);
CREATE INDEX IF NOT EXISTS idx_me_early_exit_variant
    ON dds.me_r_long_early_exit_observation (variant_id);
CREATE INDEX IF NOT EXISTS idx_me_early_exit_status
    ON dds.me_r_long_early_exit_observation (status);
CREATE INDEX IF NOT EXISTS idx_me_early_exit_symbol
    ON dds.me_r_long_early_exit_observation (symbol, entered_at);
CREATE INDEX IF NOT EXISTS idx_me_early_exit_triggered
    ON dds.me_r_long_early_exit_observation (rule_triggered)
    WHERE rule_triggered = TRUE;

-- Unique: one observation per experiment per trade per variant
CREATE UNIQUE INDEX IF NOT EXISTS uq_me_early_exit_trade_variant
    ON dds.me_r_long_early_exit_observation (experiment_id, trade_id, variant_id);

-- ============================================================
-- Variant definitions (for reference):
--   MAE_10m_040  : 10m / MAE >= 0.40R
--   MAE_10m_050  : 10m / MAE >= 0.50R
--   MAE_15m_040  : 15m / MAE >= 0.40R
--   MAE_15m_050  : 15m / MAE >= 0.50R  <-- PRIMARY
--   MAE_15m_060  : 15m / MAE >= 0.60R
--   MAE_15m_075  : 15m / MAE >= 0.75R
--   MAE_30m_050  : 30m / MAE >= 0.50R
-- ============================================================

-- ============================================================
-- Analytics view: variant summary
-- ============================================================
CREATE OR REPLACE VIEW dds.v_me_early_exit_variant_summary AS
SELECT
    variant_id,
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
    ) AS improved_count,
    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND rule_triggered AND improved = FALSE AND delta_r < 0
    ) AS worsened_count,
    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND rule_triggered AND delta_r = 0
    ) AS unchanged_count,

    ROUND(
        COUNT(*) FILTER (
            WHERE status = 'CLOSED' AND rule_triggered AND improved = TRUE
        )::numeric
        / NULLIF(COUNT(*) FILTER (
            WHERE status = 'CLOSED' AND rule_triggered
        ), 0),
        4
    ) AS improved_pct,

    ROUND(
        COUNT(*) FILTER (
            WHERE status = 'CLOSED' AND rule_triggered AND improved = FALSE AND delta_r < 0
        )::numeric
        / NULLIF(COUNT(*) FILTER (
            WHERE status = 'CLOSED' AND rule_triggered
        ), 0),
        4
    ) AS worsened_pct,

    -- Rescued losses: actual was a loss, counterfactual would have been less negative
    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND rule_triggered
          AND actual_pnl_r < 0 AND counterfactual_exit_r > actual_pnl_r
    ) AS rescued_losses,

    -- Spoiled winners: actual was a win, counterfactual would have been worse
    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND rule_triggered
          AND actual_pnl_r > 0 AND counterfactual_exit_r < actual_pnl_r
    ) AS spoiled_winners,

    -- Actual wins/losses
    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND actual_pnl_r > 0
    ) AS actual_wins,
    COUNT(*) FILTER (
        WHERE status = 'CLOSED' AND actual_pnl_r <= 0
    ) AS actual_losses,

    -- Profit factors
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

    CASE
        WHEN SUM(ABS(
            CASE
                WHEN rule_triggered AND status = 'CLOSED' THEN counterfactual_exit_r
                ELSE actual_pnl_r
            END
        )) FILTER (
            WHERE status = 'CLOSED'
              AND (
                CASE
                    WHEN rule_triggered AND status = 'CLOSED' THEN counterfactual_exit_r
                    ELSE actual_pnl_r
                END
              ) < 0
        ) > 0
        THEN ROUND(
            SUM(
                CASE
                    WHEN rule_triggered AND status = 'CLOSED' THEN counterfactual_exit_r
                    ELSE actual_pnl_r
                END
            ) FILTER (
                WHERE status = 'CLOSED'
                  AND (
                    CASE
                        WHEN rule_triggered AND status = 'CLOSED' THEN counterfactual_exit_r
                        ELSE actual_pnl_r
                    END
                  ) > 0
            )
            / ABS(SUM(
                CASE
                    WHEN rule_triggered AND status = 'CLOSED' THEN counterfactual_exit_r
                    ELSE actual_pnl_r
                END
            ) FILTER (
                WHERE status = 'CLOSED'
                  AND (
                    CASE
                        WHEN rule_triggered AND status = 'CLOSED' THEN counterfactual_exit_r
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

-- ============================================================
-- Analytics view: primary rule (MAE_15m_050) detailed report
-- ============================================================
CREATE OR REPLACE VIEW dds.v_me_early_exit_primary_report AS
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
