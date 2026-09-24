-- 05_multi_horizon_summary.sql
-- MFE/MAE across all horizons for SRR LONG signals

SELECT
    '15m' AS horizon,
    COUNT(*) AS signals,
    ROUND(AVG(mfe_15m) FILTER (WHERE mfe_15m IS NOT NULL), 4) AS avg_mfe_pct,
    ROUND(AVG(mae_15m) FILTER (WHERE mae_15m IS NOT NULL), 4) AS avg_mae_pct,
    ROUND(AVG(mfe_r_15m) FILTER (WHERE mfe_r_15m IS NOT NULL), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r_15m) FILTER (WHERE mae_r_15m IS NOT NULL), 4) AS avg_mae_r,
    COUNT(*) FILTER (WHERE mfe_15m IS NOT NULL) AS evaluated
FROM dds.srr_research_outcome
WHERE experiment_id = 'SRR_LONG_OUTCOME_V1'

UNION ALL

SELECT
    '30m', COUNT(*),
    ROUND(AVG(mfe_30m), 4), ROUND(AVG(mae_30m), 4),
    ROUND(AVG(mfe_r_30m), 4), ROUND(AVG(mae_r_30m), 4),
    COUNT(*) FILTER (WHERE mfe_30m IS NOT NULL)
FROM dds.srr_research_outcome WHERE experiment_id = 'SRR_LONG_OUTCOME_V1'

UNION ALL

SELECT
    '60m', COUNT(*),
    ROUND(AVG(mfe_60m), 4), ROUND(AVG(mae_60m), 4),
    ROUND(AVG(mfe_r_60m), 4), ROUND(AVG(mae_r_60m), 4),
    COUNT(*) FILTER (WHERE mfe_60m IS NOT NULL)
FROM dds.srr_research_outcome WHERE experiment_id = 'SRR_LONG_OUTCOME_V1'

UNION ALL

SELECT
    '120m', COUNT(*),
    ROUND(AVG(mfe_120m), 4), ROUND(AVG(mae_120m), 4),
    ROUND(AVG(mfe_r_120m), 4), ROUND(AVG(mae_r_120m), 4),
    COUNT(*) FILTER (WHERE mfe_120m IS NOT NULL)
FROM dds.srr_research_outcome WHERE experiment_id = 'SRR_LONG_OUTCOME_V1'

UNION ALL

SELECT
    '240m', COUNT(*),
    ROUND(AVG(mfe_240m), 4), ROUND(AVG(mae_240m), 4),
    ROUND(AVG(mfe_r_240m), 4), ROUND(AVG(mae_r_240m), 4),
    COUNT(*) FILTER (WHERE mfe_240m IS NOT NULL)
FROM dds.srr_research_outcome WHERE experiment_id = 'SRR_LONG_OUTCOME_V1'

ORDER BY
    CASE horizon
        WHEN '15m' THEN 1 WHEN '30m' THEN 2 WHEN '60m' THEN 3
        WHEN '120m' THEN 4 WHEN '240m' THEN 5
    END;
