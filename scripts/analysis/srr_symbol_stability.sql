-- ============================================================
-- SRR Symbol Stability Analysis
-- ============================================================
-- Checks edge distribution across symbols to detect concentration risk.
-- Target: no single symbol should contribute >20% of total N.
-- ============================================================

WITH symbol_stats AS (
    SELECT
        s.symbol,
        s.direction,
        COUNT(*) AS n,
        ROUND(AVG(o.mfe_r_60m), 3) AS avg_mfe_r,
        ROUND(AVG(o.mae_r_60m), 3) AS avg_mae_r,
        CASE
            WHEN AVG(o.mae_r_60m) > 0
            THEN ROUND(AVG(o.mfe_r_60m) / AVG(o.mae_r_60m), 2)
            ELSE NULL
        END AS mfe_mae_ratio,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mfe_r_60m >= 1.0) / COUNT(*), 1) AS p_ge_1r,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mfe_r_60m >= 2.0) / COUNT(*), 1) AS p_ge_2r,
        ROUND(100.0 * COUNT(*) FILTER (WHERE o.mae_r_60m <= 1.0) / COUNT(*), 1) AS p_mae_le_1r
    FROM research.research_observation s
    JOIN research.research_outcome o ON o.observation_id = s.observation_id
    WHERE s.experiment_id = 'SRR_GENERIC_V1'
      AND o.is_final = TRUE
      AND o.mfe_r_60m IS NOT NULL
      AND o.mae_r_60m IS NOT NULL
    GROUP BY s.symbol, s.direction
    HAVING COUNT(*) >= 5  -- minimum 5 signals per symbol for stability
),

-- Add concentration percentage
with_concentration AS (
    SELECT
        *,
        ROUND(100.0 * n / SUM(n) OVER (PARTITION BY direction), 1) AS pct_of_total
    FROM symbol_stats
)

-- Top 15 symbols by N, per direction
SELECT
    symbol,
    direction,
    n,
    pct_of_total,
    avg_mfe_r,
    avg_mae_r,
    mfe_mae_ratio,
    p_ge_1r,
    p_ge_2r,
    p_mae_le_1r
FROM with_concentration
ORDER BY direction, n DESC
LIMIT 30;
