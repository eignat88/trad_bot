-- 08_rsi_recovery_from_candles.sql
-- Recover RSI(14) and RSI delta from market.candle for all V1 trades
-- Uses Wilder's RSI computed from 5m candles at signal candle open time
-- No look-ahead: only candles fully closed AT OR BEFORE signal candle.
--
-- Schema:
--   market.candle: instrument_id, timeframe('5'/'15'), open_time, close, is_closed
--   dds.scanner_setup: signal_candle_open_time (bigint, EPOCH MILLISECONDS)
--
-- Canonical conversion:
--   signal_time_ts = to_timestamp(ss.signal_candle_open_time / 1000.0)
--
-- RSI(14) formula (Wilder's smoothing):
--   1. First avg_gain = avg_loss = simple average of first 14 bars' gains/losses
--   2. Subsequent: avg_gain = (prev_avg_gain * 13 + current_gain) / 14
--   3. RS = avg_gain / avg_loss
--   4. RSI = 100 - 100 / (1 + RS)
--
-- rsi_delta_3 = RSI(current) - RSI(3 bars ago)
-- Minimum candles needed: 14 (RSI warmup) + 3 (delta lookback) = 17

-- ============================================================
-- DIAGNOSTIC: verify timestamp conversion
-- Uncomment to verify: to_timestamp(X / 1000.0) produces correct dates
-- ============================================================
/*
SELECT
    setup_id,
    signal_candle_open_time,
    detected_at,
    to_timestamp(signal_candle_open_time / 1000.0) AS correct_ts
FROM dds.scanner_setup
WHERE scanner_name IN (
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1',
    'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'
)
ORDER BY detected_at DESC
LIMIT 10;
*/

-- ============================================================
-- MAIN QUERY: RSI recovery for all V1 trades
-- ============================================================
WITH v1_trades AS (
    SELECT
        pt.trade_id,
        pt.setup_id,
        pt.symbol,
        pt.scanner_name,
        pt.entered_at,
        pt.pnl_r,
        pt.pnl_usdt,
        pt.mfe_r,
        pt.mae_r,
        pt.exit_reason,
        pt.duration_sec,
        ss.detected_at AS signal_time,
        ss.signal_candle_open_time,
        ss.instrument_id,
        ss.features,
        -- CANONICAL: signal_candle_open_time is epoch milliseconds
        to_timestamp(ss.signal_candle_open_time / 1000.0) AS signal_candle_ts
    FROM dds.paper_trade pt
    JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
    WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
      AND pt.direction = 'LONG'
      AND pt.status = 'CLOSED'
),
-- Get up to 200 closed 5m candles before signal candle (sufficient for RSI(14) + delta 3)
candle_data AS (
    SELECT
        vt.trade_id,
        mc.open_time,
        mc.close,
        ROW_NUMBER() OVER (
            PARTITION BY vt.trade_id
            ORDER BY mc.open_time DESC
        ) AS candle_idx  -- 1 = signal candle, 2 = 1 bar before, etc.
    FROM v1_trades vt
    JOIN market.candle mc
        ON mc.instrument_id = vt.instrument_id
        AND mc.timeframe = '5'
        AND mc.is_closed = true
        AND mc.open_time <= vt.signal_candle_ts
    WHERE mc.open_time >= vt.signal_candle_ts - INTERVAL '1000 minutes'
    -- 200 bars × 5min = 1000 minutes lookback (generous for Wilder RSI)
),
-- Compute gains and losses for RSI
candle_gains AS (
    SELECT
        trade_id,
        open_time,
        close,
        candle_idx,
        GREATEST(close - LAG(close) OVER (
            PARTITION BY trade_id ORDER BY open_time
        ), 0) AS gain,
        GREATEST(LAG(close) OVER (
            PARTITION BY trade_id ORDER BY open_time
        ) - close, 0) AS loss
    FROM candle_data
),
-- Wilder's RSI using 14-bar rolling window
rsi_raw AS (
    SELECT
        trade_id,
        open_time,
        candle_idx,
        AVG(gain) OVER w AS avg_gain,
        AVG(loss) OVER w AS avg_loss,
        COUNT(*) OVER w AS window_count
    FROM candle_gains
    WHERE gain IS NOT NULL  -- skip first row (no previous close)
    WINDOW w AS (
        PARTITION BY trade_id
        ORDER BY open_time
        ROWS BETWEEN 14 PRECEDING AND CURRENT ROW
    )
),
rsi_computed AS (
    SELECT
        trade_id,
        candle_idx,
        CASE
            WHEN avg_loss = 0 THEN 100.0
            WHEN avg_gain = 0 THEN 0.0
            ELSE ROUND((100.0 - 100.0 / (1.0 + avg_gain / avg_loss))::numeric, 4)
        END AS rsi_14
    FROM rsi_raw
    WHERE window_count >= 14  -- need at least 14 bars for RSI
),
-- Get RSI for signal candle (candle_idx = 1) and 3 bars before (candle_idx = 4)
rsi_at_signal AS (
    SELECT
        trade_id,
        MAX(CASE WHEN candle_idx = 1 THEN rsi_14 END) AS rsi_signal,
        MAX(CASE WHEN candle_idx = 4 THEN rsi_14 END) AS rsi_3_bars_ago
    FROM rsi_computed
    GROUP BY trade_id
)
SELECT
    vt.trade_id,
    vt.symbol,
    vt.scanner_name,
    vt.pnl_r,
    vt.pnl_usdt,
    vt.mfe_r,
    vt.mae_r,
    vt.exit_reason,
    vt.duration_sec,
    vt.entered_at,
    vt.signal_candle_open_time,
    ra.rsi_signal AS rsi_14_at_signal,
    ra.rsi_3_bars_ago,
    CASE
        WHEN ra.rsi_signal IS NOT NULL AND ra.rsi_3_bars_ago IS NOT NULL
        THEN ROUND((ra.rsi_signal - ra.rsi_3_bars_ago)::numeric, 4)
        ELSE NULL
    END AS rsi_delta_3,
    CASE
        WHEN ra.rsi_signal IS NOT NULL AND ra.rsi_3_bars_ago IS NOT NULL
             AND (ra.rsi_signal - ra.rsi_3_bars_ago) > 0
        THEN 'PASS'
        ELSE 'REJECT'
    END AS v2_filter_result,
    -- Recovery status with reason
    CASE
        WHEN ra.rsi_signal IS NOT NULL AND ra.rsi_3_bars_ago IS NOT NULL
        THEN 'RSI_RECOVERED'
        WHEN ra.rsi_signal IS NOT NULL
        THEN 'INSUFFICIENT_CANDLES'
        ELSE 'NO_CANDLES'
    END AS recovery_status,
    -- Features from scanner_setup
    NULLIF(vt.features->>'exhaustion_magnitude', '')::numeric AS exhaustion_magnitude,
    NULLIF(vt.features->>'body_ratio', '')::numeric AS body_ratio,
    NULLIF(vt.features->>'rsi_confirmation', '')::numeric AS rsi_confirmation,
    NULLIF(vt.features->>'volume_ratio', '')::numeric AS volume_ratio,
    NULLIF(vt.features->>'rr_ratio', '')::numeric AS rr_ratio,
    NULLIF(vt.features->>'stop_distance_atr', '')::numeric AS stop_distance_atr
FROM v1_trades vt
LEFT JOIN rsi_at_signal ra ON ra.trade_id = vt.trade_id
ORDER BY vt.entered_at;
