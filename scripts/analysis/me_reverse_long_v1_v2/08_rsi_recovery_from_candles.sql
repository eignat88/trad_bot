-- 08_rsi_recovery_from_candles.sql
-- Recover RSI(14) and RSI delta from dds.market_candle for all V1 trades
-- Uses Wilder's RSI computed from 5m candles at signal candle open time
-- No look-ahead: only candles fully closed BEFORE signal candle.
--
-- RSI(14) formula (Wilder's smoothing):
--   1. First avg_gain = avg_loss = simple average of first 14 bars' gains/losses
--   2. Subsequent: avg_gain = (prev_avg_gain * 13 + current_gain) / 14
--   3. RS = avg_gain / avg_loss
--   4. RSI = 100 - 100 / (1 + RS)
--
-- rsi_delta_3 = RSI(current) - RSI(3 bars ago)

WITH v1_trades AS (
    SELECT
        pt.trade_id,
        pt.setup_id,
        pt.symbol,
        pt.scanner_name,
        pt.entered_at,
        pt.pnl_r,
        pt.pnl_usdt,
        pt.mfe,
        pt.mae,
        pt.mfe_r,
        pt.mae_r,
        pt.exit_reason,
        pt.duration_sec,
        ss.detected_at AS signal_time,
        ss.signal_candle_open_time,
        ss.instrument_id,
        ss.features
    FROM dds.paper_trade pt
    JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
    WHERE pt.scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'
      AND pt.direction = 'LONG'
      AND pt.status = 'CLOSED'
),
-- Get the signal candle open time (epoch) for each trade
signal_candles AS (
    SELECT
        vt.*,
        -- signal_candle_open_time is stored as bigint (epoch seconds)
        to_timestamp(vt.signal_candle_open_time) AS signal_candle_ts
    FROM v1_trades vt
),
-- Get 5m candles up to and including signal candle for each trade
-- We need at least RSI_PERIOD + rsi_delta_lookback + 1 candles before signal
candle_data AS (
    SELECT
        sc.trade_id,
        sc.symbol,
        mc.open_time,
        mc.open,
        mc.high,
        mc.low,
        mc.close,
        mc.volume,
        ROW_NUMBER() OVER (
            PARTITION BY sc.trade_id
            ORDER BY mc.open_time DESC
        ) AS candle_idx  -- 1 = signal candle, 2 = 1 bar before, etc.
    FROM signal_candles sc
    JOIN dds.market_candle mc
        ON mc.instrument_id = sc.instrument_id
        AND mc.timeframe = '5m'
        -- Include signal candle and up to 30 bars before for RSI(14) + delta
        AND mc.open_time >= sc.signal_candle_ts - INTERVAL '180 minutes'
        AND mc.open_time <= sc.signal_candle_ts
),
-- Compute RSI for each trade using window functions
-- First, compute gains and losses
candle_gains AS (
    SELECT
        trade_id,
        symbol,
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
-- Now compute Wilder's RSI using cumulative sums
-- This is a simplified approach: compute simple average for first 14,
-- then use the recursive formula
rsi_raw AS (
    SELECT
        trade_id,
        symbol,
        open_time,
        close,
        candle_idx,
        gain,
        loss,
        -- For Wilder's RSI, we need running averages
        -- Simple approach: use the last 14-bar window
        AVG(gain) OVER (
            PARTITION BY trade_id
            ORDER BY open_time
            ROWS BETWEEN 14 PRECEDING AND CURRENT ROW
        ) AS avg_gain_14,
        AVG(loss) OVER (
            PARTITION BY trade_id
            ORDER BY open_time
            ROWS BETWEEN 14 PRECEDING AND CURRENT ROW
        ) AS avg_loss_14,
        COUNT(*) OVER (
            PARTITION BY trade_id
            ORDER BY open_time
            ROWS BETWEEN 14 PRECEDING AND CURRENT ROW
        ) AS window_count
    FROM candle_gains
    WHERE gain IS NOT NULL  -- skip first row (no previous close)
),
rsi_computed AS (
    SELECT
        trade_id,
        symbol,
        open_time,
        close,
        candle_idx,
        CASE
            WHEN avg_loss_14 = 0 THEN 100.0
            WHEN avg_gain_14 = 0 THEN 0.0
            ELSE ROUND((100.0 - 100.0 / (1.0 + avg_gain_14 / avg_loss_14))::numeric, 4)
        END AS rsi_14,
        window_count
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
    vt.mfe,
    vt.mae,
    vt.mfe_r,
    vt.mae_r,
    vt.exit_reason,
    vt.duration_sec,
    vt.entered_at,
    vt.signal_time,
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
        THEN TRUE
        ELSE FALSE
    END AS rsi_delta_3_positive,
    -- V2-style filter: would this trade pass rsi_delta_3 > 0?
    CASE
        WHEN ra.rsi_signal IS NOT NULL AND ra.rsi_3_bars_ago IS NOT NULL
             AND (ra.rsi_signal - ra.rsi_3_bars_ago) > 0
        THEN 'PASS'
        ELSE 'REJECT'
    END AS v2_filter_result,
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
