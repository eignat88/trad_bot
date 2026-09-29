-- ============================================================
-- SRR Fee Impact Model
-- ============================================================
-- Estimates net E[R] after fees for different fee scenarios.
-- Uses the recommended SL=0.75R / TP=1.5R exit model.
--
-- Fee scenarios:
--   1. Conservative: 0.21% round trip (taker 0.055% + slippage 0.05% per side)
--   2. Optimistic:   0.15% round trip (maker rebates + minimal slippage)
--   3. Aggressive:   0.30% round trip (high slippage, small accounts)
-- ============================================================

WITH base AS (
    SELECT
        s.direction,
        s.reference_price AS entry_price,
        s.invalidation_price,
        o.mfe_r_60m,
        o.mae_r_60m,
        -- 1R as percentage of entry
        CASE
            WHEN s.reference_price > 0
            THEN ABS(s.reference_price - s.invalidation_price) / s.reference_price * 100
            ELSE 0
        END AS risk_1r_pct
    FROM research.research_observation s
    JOIN research.research_outcome o ON o.observation_id = s.observation_id
    WHERE s.experiment_id = 'SRR_GENERIC_V1'
      AND o.is_final = TRUE
      AND o.mfe_r_60m IS NOT NULL
      AND o.mae_r_60m IS NOT NULL
),

-- Simulate SL=0.75R / TP=1.5R exit
exit_sim AS (
    SELECT
        *,
        CASE
            WHEN mfe_r_60m >= 1.5 AND mae_r_60m < 0.75 THEN 1.5  -- WIN: +1.5R
            WHEN mae_r_60m >= 0.75 AND mfe_r_60m < 1.5 THEN -0.75  -- LOSS: -0.75R
            WHEN mfe_r_60m >= 1.5 AND mae_r_60m >= 0.75 THEN 1.5  -- WIN (both hit, TP first)
            ELSE 0  -- NEITHER
        END AS pnl_r,
        -- Fee in R units: round_trip_pct / risk_1r_pct
        CASE
            WHEN risk_1r_pct > 0 THEN 0.21 / risk_1r_pct
            ELSE 0
        END AS fee_r_conservative,
        CASE
            WHEN risk_1r_pct > 0 THEN 0.15 / risk_1r_pct
            ELSE 0
        END AS fee_r_optimistic,
        CASE
            WHEN risk_1r_pct > 0 THEN 0.30 / risk_1r_pct
            ELSE 0
        END AS fee_r_aggressive
    FROM base
    WHERE risk_1r_pct > 0
),

-- Summary by direction and fee scenario
summary AS (
    SELECT
        direction,
        COUNT(*) AS n,
        COUNT(*) FILTER (WHERE pnl_r > 0) AS wins,
        COUNT(*) FILTER (WHERE pnl_r < 0) AS losses,
        COUNT(*) FILTER (WHERE pnl_r = 0) AS neither,

        -- Gross E[R]
        ROUND(AVG(pnl_r), 3) AS gross_er,

        -- Net E[R] by fee scenario
        ROUND(AVG(pnl_r - fee_r_conservative), 3) AS net_er_conservative,
        ROUND(AVG(pnl_r - fee_r_optimistic), 3) AS net_er_optimistic,
        ROUND(AVG(pnl_r - fee_r_aggressive), 3) AS net_er_aggressive,

        -- Average fee in R units
        ROUND(AVG(fee_r_conservative), 4) AS avg_fee_r_conservative,
        ROUND(AVG(fee_r_optimistic), 4) AS avg_fee_r_optimistic,
        ROUND(AVG(fee_r_aggressive), 4) AS avg_fee_r_aggressive,

        -- Average risk_1r_pct
        ROUND(AVG(risk_1r_pct), 4) AS avg_risk_1r_pct

    FROM exit_sim
    GROUP BY direction
)

SELECT * FROM summary ORDER BY direction;
