-- ============================================================
-- SRR Exit by Horizon Analysis
-- ============================================================
-- Determines the optimal max_hold by examining MFE/MAE growth
-- across horizons. If MFE plateaus between 120m and 240m,
-- the 120m max_hold is justified.
-- ============================================================

WITH horizon_data AS (
    SELECT
        s.direction,
        -- 15m
        AVG(o.mfe_r_15m) AS avg_mfe_r_15m,
        AVG(o.mae_r_15m) AS avg_mae_r_15m,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mfe_r_15m >= 1.0) / COUNT(*), 1) AS p_ge_1r_15m,

        -- 30m
        AVG(o.mfe_r_30m) AS avg_mfe_r_30m,
        AVG(o.mae_r_30m) AS avg_mae_r_30m,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mfe_r_30m >= 1.0) / COUNT(*), 1) AS p_ge_1r_30m,

        -- 60m
        AVG(o.mfe_r_60m) AS avg_mfe_r_60m,
        AVG(o.mae_r_60m) AS avg_mae_r_60m,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mfe_r_60m >= 1.0) / COUNT(*), 1) AS p_ge_1r_60m,

        -- 120m
        AVG(o.mfe_r_120m) AS avg_mfe_r_120m,
        AVG(o.mae_r_120m) AS avg_mae_r_120m,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mfe_r_120m >= 1.0) / COUNT(*), 1) AS p_ge_1r_120m,

        -- 240m
        AVG(o.mfe_r_240m) AS avg_mfe_r_240m,
        AVG(o.mae_r_240m) AS avg_mae_r_240m,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mfe_r_240m >= 1.0) / COUNT(*), 1) AS p_ge_1r_240m,

        COUNT(*) AS n

    FROM research.research_observation s
    JOIN research.research_outcome o ON o.observation_id = s.observation_id
    WHERE s.experiment_id = 'SRR_GENERIC_V1'
      AND o.is_final = TRUE
    GROUP BY s.direction
)

SELECT * FROM horizon_data ORDER BY direction;
