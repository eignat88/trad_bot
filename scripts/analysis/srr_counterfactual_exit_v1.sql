-- ============================================================
-- SRR Counterfactual Exit Analysis V1
-- ============================================================
-- Evaluates different SL/TP combinations on SRR_GENERIC_V1 OOS data.
-- Uses the MFE/MAE horizon data to simulate fixed-R exit models.
--
-- Exit models tested:
--   1. SL=0.5R / TP=1.0R (tight stop, conservative target)
--   2. SL=0.75R / TP=1.5R (balanced — recommended)
--   3. SL=1.0R / TP=2.0R (wide stop, ambitious target)
--   4. SL=0.75R / TP=2.0R (asymmetric — wide target)
--   5. SL=0.5R / TP=1.5R (tight stop, moderate target)
--
-- All metrics computed at 60m horizon (primary evaluation window).
-- Fee model: 0.21% round trip (taker 0.055% + slippage 0.05% per side).
-- ============================================================

-- ── 1. Base signal data with 60m MFE/MAE ─────────────────────

WITH base AS (
    SELECT
        s.signal_id,
        s.symbol,
        s.direction,
        s.signal_time,
        s.reference_price AS entry_price,
        s.invalidation_price,
        s.raw_risk_distance,
        s.raw_risk_distance_pct,
        s.market_regime,
        s.score,
        s.level_type,
        o.mfe_60m,
        o.mae_60m,
        o.mfe_r_60m,
        o.mae_r_60m,
        o.tp_hit,
        o.sl_hit,
        o.tp_before_sl,
        o.sl_before_tp,
        -- 1R = abs(entry - invalidation) as percentage
        CASE
            WHEN s.reference_price > 0
            THEN ABS(s.reference_price - s.invalidation_price) / s.reference_price * 100
            ELSE 0
        END AS risk_1r_pct
    FROM research.research_observation s
    JOIN research.research_outcome o ON o.observation_id = s.observation_id
    WHERE s.experiment_id = 'SRR_GENERIC_V1'
      AND o.is_final = TRUE
      AND o.mfe_60m IS NOT NULL
      AND o.mae_60m IS NOT NULL
),

-- ── 2. Counterfactual exit simulation ─────────────────────────
-- For each exit model, determine win/loss based on MFE/MAE

counterfactual AS (
    SELECT
        *,
        -- Model 1: SL=0.5R / TP=1R
        CASE
            WHEN mfe_60m >= risk_1r_pct * 1.0 AND mae_60m < risk_1r_pct * 0.5 THEN 'WIN'
            WHEN mae_60m >= risk_1r_pct * 0.5 AND mfe_60m < risk_1r_pct * 1.0 THEN 'LOSS'
            WHEN mfe_60m >= risk_1r_pct * 1.0 AND mae_60m >= risk_1r_pct * 0.5 THEN 'BOTH_1R_05R'
            ELSE 'NEITHER'
        END AS exit_05r_1r,

        -- Model 2: SL=0.75R / TP=1.5R
        CASE
            WHEN mfe_60m >= risk_1r_pct * 1.5 AND mae_60m < risk_1r_pct * 0.75 THEN 'WIN'
            WHEN mae_60m >= risk_1r_pct * 0.75 AND mfe_60m < risk_1r_pct * 1.5 THEN 'LOSS'
            WHEN mfe_60m >= risk_1r_pct * 1.5 AND mae_60m >= risk_1r_pct * 0.75 THEN 'BOTH_15R_075R'
            ELSE 'NEITHER'
        END AS exit_075r_15r,

        -- Model 3: SL=1R / TP=2R
        CASE
            WHEN mfe_60m >= risk_1r_pct * 2.0 AND mae_60m < risk_1r_pct * 1.0 THEN 'WIN'
            WHEN mae_60m >= risk_1r_pct * 1.0 AND mfe_60m < risk_1r_pct * 2.0 THEN 'LOSS'
            WHEN mfe_60m >= risk_1r_pct * 2.0 AND mae_60m >= risk_1r_pct * 1.0 THEN 'BOTH_2R_1R'
            ELSE 'NEITHER'
        END AS exit_1r_2r,

        -- Model 4: SL=0.75R / TP=2R (asymmetric)
        CASE
            WHEN mfe_60m >= risk_1r_pct * 2.0 AND mae_60m < risk_1r_pct * 0.75 THEN 'WIN'
            WHEN mae_60m >= risk_1r_pct * 0.75 AND mfe_60m < risk_1r_pct * 2.0 THEN 'LOSS'
            WHEN mfe_60m >= risk_1r_pct * 2.0 AND mae_60m >= risk_1r_pct * 0.75 THEN 'BOTH_2R_075R'
            ELSE 'NEITHER'
        END AS exit_075r_2r,

        -- Model 5: SL=0.5R / TP=1.5R (tight stop)
        CASE
            WHEN mfe_60m >= risk_1r_pct * 1.5 AND mae_60m < risk_1r_pct * 0.5 THEN 'WIN'
            WHEN mae_60m >= risk_1r_pct * 0.5 AND mfe_60m < risk_1r_pct * 1.5 THEN 'LOSS'
            WHEN mfe_60m >= risk_1r_pct * 1.5 AND mae_60m >= risk_1r_pct * 0.5 THEN 'BOTH_15R_05R'
            ELSE 'NEITHER'
        END AS exit_05r_15r
    FROM base
    WHERE risk_1r_pct > 0  -- exclude zero-risk signals
),

-- ── 3. Summary by exit model ──────────────────────────────────

summary AS (
    SELECT
        direction,
        COUNT(*) AS n,

        -- Model 1: SL=0.5R / TP=1R
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_05r_1r = 'WIN') / COUNT(*), 1) AS win_pct_05r_1r,
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_05r_1r = 'LOSS') / COUNT(*), 1) AS loss_pct_05r_1r,
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_05r_1r = 'NEITHER') / COUNT(*), 1) AS neither_pct_05r_1r,
        ROUND(
            (COUNT(*) FILTER (WHERE exit_05r_1r = 'WIN') * 1.0 -
             COUNT(*) FILTER (WHERE exit_05r_1r = 'LOSS') * 0.5) / COUNT(*), 3
        ) AS gross_er_05r_1r,

        -- Model 2: SL=0.75R / TP=1.5R (RECOMMENDED)
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_075r_15r = 'WIN') / COUNT(*), 1) AS win_pct_075r_15r,
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_075r_15r = 'LOSS') / COUNT(*), 1) AS loss_pct_075r_15r,
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_075r_15r = 'NEITHER') / COUNT(*), 1) AS neither_pct_075r_15r,
        ROUND(
            (COUNT(*) FILTER (WHERE exit_075r_15r = 'WIN') * 1.5 -
             COUNT(*) FILTER (WHERE exit_075r_15r = 'LOSS') * 0.75) / COUNT(*), 3
        ) AS gross_er_075r_15r,

        -- Model 3: SL=1R / TP=2R
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_1r_2r = 'WIN') / COUNT(*), 1) AS win_pct_1r_2r,
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_1r_2r = 'LOSS') / COUNT(*), 1) AS loss_pct_1r_2r,
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_1r_2r = 'NEITHER') / COUNT(*), 1) AS neither_pct_1r_2r,
        ROUND(
            (COUNT(*) FILTER (WHERE exit_1r_2r = 'WIN') * 2.0 -
             COUNT(*) FILTER (WHERE exit_1r_2r = 'LOSS') * 1.0) / COUNT(*), 3
        ) AS gross_er_1r_2r,

        -- Model 4: SL=0.75R / TP=2R (asymmetric)
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_075r_2r = 'WIN') / COUNT(*), 1) AS win_pct_075r_2r,
        ROUND(
            (COUNT(*) FILTER (WHERE exit_075r_2r = 'WIN') * 2.0 -
             COUNT(*) FILTER (WHERE exit_075r_2r = 'LOSS') * 0.75) / COUNT(*), 3
        ) AS gross_er_075r_2r,

        -- Model 5: SL=0.5R / TP=1.5R (tight stop)
        ROUND(100.0 * COUNT(*) FILTER (WHERE exit_05r_15r = 'WIN') / COUNT(*), 1) AS win_pct_05r_15r,
        ROUND(
            (COUNT(*) FILTER (WHERE exit_05r_15r = 'WIN') * 1.5 -
             COUNT(*) FILTER (WHERE exit_05r_15r = 'LOSS') * 0.5) / COUNT(*), 3
        ) AS gross_er_05r_15r

    FROM counterfactual
    GROUP BY direction
)

SELECT * FROM summary ORDER BY direction;
