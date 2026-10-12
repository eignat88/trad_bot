-- ME close-location clean v1.2.0 analytics.
-- Legacy views are intentionally preserved.
-- Research only; no execution policy changes.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE OR REPLACE VIEW dds.v_me_r_cl_clean_cohort_v120 AS
WITH classified AS (
    SELECT
        s.signal_id,
        s.signal_time,
        s.symbol,
        CASE
            WHEN s.decision_time IS NULL
              OR s.signal_candle_open_time IS NULL
              OR s.decision_time < (
                  s.signal_candle_open_time + INTERVAL '5 minutes'
              )
              OR s.signal_time IS DISTINCT FROM s.signal_candle_open_time
              OR s.close_location IS NULL
              OR s.close_location NOT BETWEEN 0 AND 1
              OR s.close_location_threshold <> 0.70
              OR s.high <= s.low
              OR s.open NOT BETWEEN s.low AND s.high
              OR s.close NOT BETWEEN s.low AND s.high
              OR abs(
                  s.close_location -
                  (s.close - s.low) / nullif(s.high - s.low, 0)
              ) > 0.0000005001
              OR s.filter_passed IS DISTINCT FROM (
                  (s.close - s.low) / nullif(s.high - s.low, 0)
                  >= 0.70
              )
            THEN 'INVALID'
            WHEN s.filter_passed THEN 'PASS'
            ELSE 'REJECT'
        END AS cohort_class
    FROM dds.me_r_long_close_location_oos_signal s
    WHERE s.experiment_id =
        'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
      AND s.signal_version = '1.2.0'
)
SELECT
    COUNT(*) AS clean_base_n,
    COUNT(*) FILTER (WHERE cohort_class = 'PASS') AS pass_n,
    COUNT(*) FILTER (WHERE cohort_class = 'REJECT') AS reject_n,
    COUNT(*) FILTER (WHERE cohort_class = 'INVALID') AS invalid_n,
    COUNT(DISTINCT symbol) AS symbols,
    MIN(signal_time) AS first_signal,
    MAX(signal_time) AS last_signal,
    COUNT(*) = (
        COUNT(*) FILTER (WHERE cohort_class = 'PASS')
        + COUNT(*) FILTER (WHERE cohort_class = 'REJECT')
        + COUNT(*) FILTER (WHERE cohort_class = 'INVALID')
    ) AS accounting_integrity
FROM classified;

CREATE OR REPLACE VIEW dds.v_me_r_cl_clean_outcomes_v120 AS
WITH classified AS (
    SELECT
        s.signal_id,
        CASE
            WHEN s.decision_time IS NULL
              OR s.signal_candle_open_time IS NULL
              OR s.decision_time < (
                  s.signal_candle_open_time + INTERVAL '5 minutes'
              )
              OR s.signal_time IS DISTINCT FROM s.signal_candle_open_time
              OR s.close_location IS NULL
              OR s.close_location NOT BETWEEN 0 AND 1
              OR s.close_location_threshold <> 0.70
              OR s.high <= s.low
              OR s.open NOT BETWEEN s.low AND s.high
              OR s.close NOT BETWEEN s.low AND s.high
              OR abs(
                  s.close_location -
                  (s.close - s.low) / nullif(s.high - s.low, 0)
              ) > 0.0000005001
              OR s.filter_passed IS DISTINCT FROM (
                  (s.close - s.low) / nullif(s.high - s.low, 0)
                  >= 0.70
              )
            THEN 'INVALID'
            WHEN s.filter_passed THEN 'PASS'
            ELSE 'REJECT'
        END AS cohort_class
    FROM dds.me_r_long_close_location_oos_signal s
    WHERE s.experiment_id =
        'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
      AND s.signal_version = '1.2.0'
)
SELECT
    c.cohort_class,
    COUNT(*) AS signals,
    COUNT(o.signal_id) AS outcomes,
    COUNT(*) FILTER (WHERE o.is_final = TRUE) AS finalized,
    AVG(o.mfe_240m_r) FILTER (
        WHERE o.is_final = TRUE
    ) AS avg_final_mfe_240m_r,
    AVG(o.mae_240m_r) FILTER (
        WHERE o.is_final = TRUE
    ) AS avg_final_mae_240m_r
FROM classified c
LEFT JOIN dds.me_r_long_close_location_oos_outcome o
    ON o.signal_id = c.signal_id
GROUP BY c.cohort_class;

COMMIT;
