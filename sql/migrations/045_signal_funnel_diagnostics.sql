-- ============================================================
-- Migration 045: Signal Funnel Diagnostics V1
-- MOMENTUM_EXHAUSTION_REVERSE_LONG_V2
-- ============================================================
-- Shadow observability: records per-hour and per-day funnel
-- counters for every V2 _scan_long() call.
--
-- NO production behavior is changed.  This migration adds
-- tables and views only — no triggers, no constraints on
-- existing tables, no schema modifications.
-- ============================================================

-- ── Hourly funnel observation ──────────────────────────────
CREATE TABLE IF NOT EXISTS dds.signal_funnel_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    scanner_name        TEXT NOT NULL,
    period_start        TIMESTAMPTZ NOT NULL,
    period_type         TEXT NOT NULL CHECK (period_type IN ('hourly', 'daily')),

    -- Pass counters (accumulative: each stage includes all previous)
    total_scans         BIGINT NOT NULL DEFAULT 0,
    pass_data_length    BIGINT NOT NULL DEFAULT 0,
    pass_swing_highs    BIGINT NOT NULL DEFAULT 0,
    pass_break_prev_high BIGINT NOT NULL DEFAULT 0,
    pass_return_near_high BIGINT NOT NULL DEFAULT 0,
    pass_bearish_candle BIGINT NOT NULL DEFAULT 0,
    pass_body_ratio     BIGINT NOT NULL DEFAULT 0,
    pass_rsi_65         BIGINT NOT NULL DEFAULT 0,
    pass_rsi_delta_available BIGINT NOT NULL DEFAULT 0,
    pass_rsi_delta_positive BIGINT NOT NULL DEFAULT 0,
    final_setup         BIGINT NOT NULL DEFAULT 0,

    -- Reject counters (first-match, mutually exclusive)
    no_data             BIGINT NOT NULL DEFAULT 0,
    no_swings           BIGINT NOT NULL DEFAULT 0,
    no_breakout         BIGINT NOT NULL DEFAULT 0,
    too_far_above_prev_high BIGINT NOT NULL DEFAULT 0,
    not_bearish         BIGINT NOT NULL DEFAULT 0,
    body_too_large      BIGINT NOT NULL DEFAULT 0,
    rsi_below_65        BIGINT NOT NULL DEFAULT 0,
    rsi_delta_missing   BIGINT NOT NULL DEFAULT 0,
    rsi_delta_not_positive BIGINT NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for analytics queries
CREATE INDEX IF NOT EXISTS idx_funnel_obs_scanner_period
    ON dds.signal_funnel_observation (scanner_name, period_start);
CREATE INDEX IF NOT EXISTS idx_funnel_obs_period_type
    ON dds.signal_funnel_observation (period_type);
CREATE INDEX IF NOT EXISTS idx_funnel_obs_created
    ON dds.signal_funnel_observation (created_at);

-- Unique: one observation per scanner per period
CREATE UNIQUE INDEX IF NOT EXISTS uq_funnel_obs_scanner_period
    ON dds.signal_funnel_observation (scanner_name, period_type, period_start);


-- ── Daily aggregated view ─────────────────────────────────
CREATE OR REPLACE VIEW dds.v_signal_funnel_daily AS
SELECT
    scanner_name,
    date_trunc('day', period_start) AS day,
    SUM(total_scans)           AS total_scans,
    SUM(pass_data_length)      AS pass_data_length,
    SUM(pass_swing_highs)      AS pass_swing_highs,
    SUM(pass_break_prev_high)  AS pass_break_prev_high,
    SUM(pass_return_near_high) AS pass_return_near_high,
    SUM(pass_bearish_candle)   AS pass_bearish_candle,
    SUM(pass_body_ratio)       AS pass_body_ratio,
    SUM(pass_rsi_65)           AS pass_rsi_65,
    SUM(pass_rsi_delta_available) AS pass_rsi_delta_available,
    SUM(pass_rsi_delta_positive)  AS pass_rsi_delta_positive,
    SUM(final_setup)           AS final_setup,
    SUM(no_data)               AS no_data,
    SUM(no_swings)             AS no_swings,
    SUM(no_breakout)           AS no_breakout,
    SUM(too_far_above_prev_high) AS too_far_above_prev_high,
    SUM(not_bearish)           AS not_bearish,
    SUM(body_too_large)        AS body_too_large,
    SUM(rsi_below_65)          AS rsi_below_65,
    SUM(rsi_delta_missing)     AS rsi_delta_missing,
    SUM(rsi_delta_not_positive) AS rsi_delta_not_positive
FROM dds.signal_funnel_observation
WHERE period_type = 'hourly'
GROUP BY scanner_name, date_trunc('day', period_start)
ORDER BY scanner_name, day DESC;


-- ── Funnel stage drop-off view (hourly) ───────────────────
-- Shows the percentage passing each stage relative to TOTAL_SCANS.
CREATE OR REPLACE VIEW dds.v_signal_funnel_hourly_dropoff AS
SELECT
    scanner_name,
    period_start,
    total_scans,
    pass_data_length,
    pass_swing_highs,
    pass_break_prev_high,
    pass_return_near_high,
    pass_bearish_candle,
    pass_body_ratio,
    pass_rsi_65,
    pass_rsi_delta_available,
    pass_rsi_delta_positive,
    final_setup,
    -- Drop-off percentages relative to total_scans
    CASE WHEN total_scans > 0
         THEN ROUND((pass_data_length::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_pass_data_length,
    CASE WHEN total_scans > 0
         THEN ROUND((pass_swing_highs::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_pass_swing_highs,
    CASE WHEN total_scans > 0
         THEN ROUND((pass_break_prev_high::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_pass_break_prev_high,
    CASE WHEN total_scans > 0
         THEN ROUND((pass_return_near_high::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_pass_return_near_high,
    CASE WHEN total_scans > 0
         THEN ROUND((pass_bearish_candle::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_pass_bearish_candle,
    CASE WHEN total_scans > 0
         THEN ROUND((pass_body_ratio::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_pass_body_ratio,
    CASE WHEN total_scans > 0
         THEN ROUND((pass_rsi_65::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_pass_rsi_65,
    CASE WHEN total_scans > 0
         THEN ROUND((pass_rsi_delta_available::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_pass_rsi_delta_available,
    CASE WHEN total_scans > 0
         THEN ROUND((pass_rsi_delta_positive::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_pass_rsi_delta_positive,
    CASE WHEN total_scans > 0
         THEN ROUND((final_setup::numeric / total_scans * 100), 2)
         ELSE 0 END AS pct_final_setup
FROM dds.signal_funnel_observation
WHERE period_type = 'hourly'
ORDER BY scanner_name, period_start DESC;


-- ── Top reject reasons (hourly aggregate) ─────────────────
-- Helps quickly identify which gate is killing the most signals.
CREATE OR REPLACE VIEW dds.v_signal_funnel_top_rejects AS
SELECT
    scanner_name,
    SUM(no_data)              AS no_data,
    SUM(no_swings)            AS no_swings,
    SUM(no_breakout)          AS no_breakout,
    SUM(too_far_above_prev_high) AS too_far_above_prev_high,
    SUM(not_bearish)          AS not_bearish,
    SUM(body_too_large)       AS body_too_large,
    SUM(rsi_below_65)         AS rsi_below_65,
    SUM(rsi_delta_missing)    AS rsi_delta_missing,
    SUM(rsi_delta_not_positive) AS rsi_delta_not_positive,
    SUM(total_scans)          AS total_scans,
    SUM(final_setup)          AS final_setup
FROM dds.signal_funnel_observation
WHERE period_type = 'hourly'
GROUP BY scanner_name
ORDER BY total_scans DESC;
