-- ============================================================
-- SRR Regime Stability Analysis
-- ============================================================
-- Checks edge stability across market regimes.
-- Target: edge should be positive in at least 2 of 3 regimes.
-- ============================================================

WITH regime_stats AS (
    SELECT
        s.direction,
        COALESCE(s.market_regime, 'UNKNOWN') AS market_regime,
        COUNT(*) AS n,
        ROUND(AVG(o.mfe_r_60m), 3) AS avg_mfe_r,
        ROUND(AVG(o.mae_r_60m), 3) AS avg_mae_r,
        CASE
            WHEN AVG(o.mae_r_60m) > 0
            THEN ROUND(AVG(o.mfe_r_60m) / AVG(o.mae_r_60m), 2)
            ELSE NULL
        END AS mfe_mae_ratio,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mfe_r_60m >= 1.0) / COUNT(*), 1) AS p_ge_1r,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mfe_r_60m >= 2.0) / COUNT(*), 1) AS p_ge_2r
    FROM research.research_observation s
    JOIN research.research_outcome o ON o.observation_id = s.observation_id
    WHERE s.experiment_id = 'SRR_GENERIC_V1'
      AND o.is_final = TRUE
      AND o.mfe_r_60m IS NOT NULL
      AND o.mae_r_60m IS NOT NULL
    GROUP BY s.direction, COALESCE(s.market_regime, 'UNKNOWN')
)

SELECT * FROM regime_stats ORDER BY direction, n DESC;
