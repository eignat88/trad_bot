-- ============================================================
-- Shadow Signal Collection: ATR Wick Rejection Short V1
-- ============================================================
-- This table stores shadow/counterfactual SHORT signals from the
-- ATR_WICK_REJECTION_SHORT_V1 scanner for experimental analysis.
-- Scanner does NOT open paper/live positions — only collects signals.
-- ============================================================

CREATE TABLE IF NOT EXISTS dds.shadow_signal (
    signal_id           BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL DEFAULT 'ATR_WICK_REJECTION_SHORT_V1',
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL DEFAULT '5m',
    signal_time         TIMESTAMPTZ NOT NULL,
    signal_price        NUMERIC NOT NULL,
    -- Candle data
    open                NUMERIC NOT NULL,
    high                NUMERIC NOT NULL,
    low                 NUMERIC NOT NULL,
    close               NUMERIC NOT NULL,
    volume              NUMERIC NOT NULL,
    -- ATR indicators
    atr                 NUMERIC NOT NULL,
    atr_pct             NUMERIC NOT NULL,
    -- Wick analysis
    wick_size           NUMERIC NOT NULL,
    wick_atr            NUMERIC NOT NULL,
    upper_wick_pct      NUMERIC NOT NULL,
    close_location      NUMERIC NOT NULL,
    -- RSI/StochRSI
    rsi                 NUMERIC NOT NULL,
    stoch_rsi           NUMERIC,
    -- Bollinger Bands
    bb_upper            NUMERIC NOT NULL,
    bb_mid              NUMERIC NOT NULL,
    bb_lower            NUMERIC NOT NULL,
    bb_width            NUMERIC NOT NULL,
    distance_to_upper_bb NUMERIC NOT NULL,
    -- EMA
    ema_fast            NUMERIC NOT NULL,
    ema_medium          NUMERIC NOT NULL,
    ema_slow            NUMERIC NOT NULL,
    ema_slope           NUMERIC NOT NULL,
    -- Volume
    volume_ratio        NUMERIC NOT NULL,
    -- Metadata
    signal_version      TEXT NOT NULL DEFAULT '1.0.0',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Constraints
    CONSTRAINT shadow_signal_timeframe_chk CHECK (timeframe = '5m'),
    CONSTRAINT shadow_signal_direction_chk CHECK (experiment_id = 'ATR_WICK_REJECTION_SHORT_V1')
);

-- Add strict_pass column (idempotent - safe to run multiple times)
ALTER TABLE dds.shadow_signal
ADD COLUMN IF NOT EXISTS strict_pass BOOLEAN NOT NULL DEFAULT FALSE;

-- Add OOS filter flags (idempotent)
ALTER TABLE dds.shadow_signal
ADD COLUMN IF NOT EXISTS oos_a_stoch_08 BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE dds.shadow_signal
ADD COLUMN IF NOT EXISTS oos_b_stoch_08_vol_10 BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE dds.shadow_signal
ADD COLUMN IF NOT EXISTS oos_c_stoch_06_vol_10 BOOLEAN NOT NULL DEFAULT FALSE;

-- Indexes for OOS analysis
CREATE INDEX IF NOT EXISTS idx_shadow_signal_oos_a
ON dds.shadow_signal (oos_a_stoch_08) WHERE oos_a_stoch_08 = TRUE;
CREATE INDEX IF NOT EXISTS idx_shadow_signal_oos_b
ON dds.shadow_signal (oos_b_stoch_08_vol_10) WHERE oos_b_stoch_08_vol_10 = TRUE;
CREATE INDEX IF NOT EXISTS idx_shadow_signal_oos_c
ON dds.shadow_signal (oos_c_stoch_06_vol_10) WHERE oos_c_stoch_06_vol_10 = TRUE;

-- Indexes for analysis and backfill
CREATE INDEX IF NOT EXISTS idx_shadow_signal_experiment ON dds.shadow_signal (experiment_id);
CREATE INDEX IF NOT EXISTS idx_shadow_signal_symbol ON dds.shadow_signal (symbol, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_signal_time ON dds.shadow_signal (signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_signal_symbol_time ON dds.shadow_signal (symbol, signal_time DESC);

-- Unique: one signal per symbol per candle per experiment
CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_signal_experiment_symbol_time
ON dds.shadow_signal (experiment_id, symbol, signal_time);

-- ============================================================
-- MFE/MAE Evaluation Results
-- ============================================================
-- Stores post-signal price movement analysis for each shadow signal
-- ============================================================

CREATE TABLE IF NOT EXISTS dds.shadow_signal_outcome (
    signal_id           BIGINT PRIMARY KEY REFERENCES dds.shadow_signal(signal_id) ON DELETE CASCADE,
    experiment_id       TEXT NOT NULL DEFAULT 'ATR_WICK_REJECTION_SHORT_V1',
    symbol              TEXT NOT NULL,
    -- Evaluation horizons
    mfe_15m             NUMERIC,  -- Max favorable excursion at 15m
    mae_15m             NUMERIC,  -- Max adverse excursion at 15m
    mfe_30m             NUMERIC,
    mae_30m             NUMERIC,
    mfe_60m             NUMERIC,
    mae_60m             NUMERIC,
    mfe_120m            NUMERIC,
    mae_120m            NUMERIC,
    mfe_240m            NUMERIC,
    mae_240m            NUMERIC,
    mfe_eod             NUMERIC,  -- Max favorable excursion to end of day
    mae_eod             NUMERIC,  -- Max adverse excursion to end of day
    -- Target achievement (SHORT signals)
    reached_minus_0_5   BOOLEAN NOT NULL DEFAULT FALSE,  -- Price dropped 0.5%
    reached_minus_1_0   BOOLEAN NOT NULL DEFAULT FALSE,  -- Price dropped 1.0%
    reached_minus_1_5   BOOLEAN NOT NULL DEFAULT FALSE,  -- Price dropped 1.5%
    reached_minus_2_0   BOOLEAN NOT NULL DEFAULT FALSE,  -- Price dropped 2.0%
    -- Adverse excursion before target
    hit_plus_0_5_before_target BOOLEAN NOT NULL DEFAULT FALSE,
    hit_plus_1_0_before_target BOOLEAN NOT NULL DEFAULT FALSE,
    hit_plus_1_5_before_target BOOLEAN NOT NULL DEFAULT FALSE,
    -- Evaluation metadata
    evaluated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Constraints
    CONSTRAINT shadow_outcome_experiment_chk CHECK (experiment_id = 'ATR_WICK_REJECTION_SHORT_V1')
);

-- Indexes for outcome analysis
CREATE INDEX IF NOT EXISTS idx_shadow_outcome_experiment ON dds.shadow_signal_outcome (experiment_id);
CREATE INDEX IF NOT EXISTS idx_shadow_outcome_symbol ON dds.shadow_signal_outcome (symbol);
CREATE INDEX IF NOT EXISTS idx_shadow_outcome_mfe ON dds.shadow_signal_outcome (mfe_60m DESC NULLS LAST);

-- Add per-horizon evaluation timestamps and finalization flag (idempotent)
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_15m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_30m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_60m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_120m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_240m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_eod_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS is_final BOOLEAN NOT NULL DEFAULT FALSE;

-- Reset stale data for existing outcomes so evaluator can re-evaluate mature horizons
UPDATE dds.shadow_signal_outcome SET
    evaluated_120m_at = NULL,
    evaluated_240m_at = NULL,
    evaluated_eod_at = NULL,
    mfe_120m = NULL, mae_120m = NULL,
    mfe_240m = NULL, mae_240m = NULL,
    mfe_eod = NULL, mae_eod = NULL,
    is_final = FALSE
WHERE mfe_120m IS NOT NULL OR mfe_240m IS NOT NULL OR mfe_eod IS NOT NULL;

-- Index for eligible signal query
CREATE INDEX IF NOT EXISTS idx_shadow_outcome_is_final
ON dds.shadow_signal_outcome (is_final) WHERE is_final = FALSE;

-- ============================================================
-- Reporting Views
-- ============================================================

-- View: Signal summary by symbol
CREATE OR REPLACE VIEW dds.v_shadow_signal_summary AS
SELECT
    s.symbol,
    COUNT(*) AS signals,
    COUNT(o.signal_id) AS evaluated,
    ROUND(AVG(s.atr_pct), 4) AS avg_atr_pct,
    ROUND(AVG(s.rsi), 2) AS avg_rsi,
    ROUND(AVG(s.wick_atr), 4) AS avg_wick_atr,
    ROUND(AVG(s.close_location), 4) AS avg_close_location,
    ROUND(AVG(s.bb_width), 4) AS avg_bb_width,
    ROUND(AVG(s.volume_ratio), 4) AS avg_volume_ratio
FROM dds.shadow_signal s
LEFT JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
GROUP BY s.symbol
ORDER BY signals DESC;

-- View: MFE/MAE distribution by symbol
CREATE OR REPLACE VIEW dds.v_shadow_mfe_mae_distribution AS
SELECT
    o.symbol,
    COUNT(*) AS samples,
    ROUND(AVG(o.mfe_60m), 4) AS avg_mfe_60m,
    ROUND(AVG(o.mae_60m), 4) AS avg_mae_60m,
    ROUND(AVG(o.mfe_120m), 4) AS avg_mfe_120m,
    ROUND(AVG(o.mae_120m), 4) AS avg_mae_120m,
    ROUND(
        COUNT(*) FILTER (WHERE o.reached_minus_0_5)::numeric / COUNT(*), 4
    ) AS pct_reached_0_5,
    ROUND(
        COUNT(*) FILTER (WHERE o.reached_minus_1_0)::numeric / COUNT(*), 4
    ) AS pct_reached_1_0,
    ROUND(
        COUNT(*) FILTER (WHERE o.reached_minus_1_5)::numeric / COUNT(*), 4
    ) AS pct_reached_1_5,
    ROUND(
        COUNT(*) FILTER (WHERE o.reached_minus_2_0)::numeric / COUNT(*), 4
    ) AS pct_reached_2_0,
    ROUND(
        COUNT(*) FILTER (WHERE o.hit_plus_0_5_before_target)::numeric / COUNT(*), 4
    ) AS pct_hit_0_5_adverse
FROM dds.shadow_signal_outcome o
WHERE o.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
GROUP BY o.symbol
HAVING COUNT(*) >= 3
ORDER BY avg_mfe_60m DESC NULLS LAST;

-- View: Feature comparison (profitable vs losing signals)
CREATE OR REPLACE VIEW dds.v_shadow_feature_comparison AS
SELECT
    o.symbol,
    CASE WHEN o.mfe_60m > 0 THEN 'profitable' ELSE 'losing' END AS outcome,
    COUNT(*) AS samples,
    ROUND(AVG(s.atr_pct), 4) AS avg_atr_pct,
    ROUND(AVG(s.rsi), 2) AS avg_rsi,
    ROUND(AVG(s.wick_atr), 4) AS avg_wick_atr,
    ROUND(AVG(s.close_location), 4) AS avg_close_location,
    ROUND(AVG(s.bb_width), 4) AS avg_bb_width,
    ROUND(AVG(s.distance_to_upper_bb), 4) AS avg_distance_to_upper_bb,
    ROUND(AVG(s.volume_ratio), 4) AS avg_volume_ratio,
    ROUND(AVG(s.ema_slope), 6) AS avg_ema_slope
FROM dds.shadow_signal s
JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
WHERE o.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
  AND o.mfe_60m IS NOT NULL
GROUP BY o.symbol, CASE WHEN o.mfe_60m > 0 THEN 'profitable' ELSE 'losing' END
HAVING COUNT(*) >= 2
ORDER BY o.symbol, outcome;

-- ============================================================
-- OOS Filter Analysis View
-- ============================================================
-- For each OOS filter variant, compute N, coverage, avg/median MFE/MAE,
-- target hit rates, and per-symbol breakdown.
-- ============================================================

-- OOS filter summary by variant
CREATE OR REPLACE VIEW dds.v_shadow_oos_filter_summary AS
WITH oos_variants AS (
    SELECT
        s.signal_id, s.symbol, s.stoch_rsi, s.volume_ratio, s.signal_time,
        o.mfe_60m, o.mae_60m, o.reached_minus_0_5, o.reached_minus_1_0,
        o.reached_minus_1_5, o.reached_minus_2_0,
        'A: stoch>=0.8' AS variant,
        s.oos_a_stoch_08 AS passes
    FROM dds.shadow_signal s
    JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
    WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
      AND o.mfe_60m IS NOT NULL

    UNION ALL

    SELECT
        s.signal_id, s.symbol, s.stoch_rsi, s.volume_ratio, s.signal_time,
        o.mfe_60m, o.mae_60m, o.reached_minus_0_5, o.reached_minus_1_0,
        o.reached_minus_1_5, o.reached_minus_2_0,
        'B: stoch>=0.8 & vol<=1.0' AS variant,
        s.oos_b_stoch_08_vol_10 AS passes
    FROM dds.shadow_signal s
    JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
    WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
      AND o.mfe_60m IS NOT NULL

    UNION ALL

    SELECT
        s.signal_id, s.symbol, s.stoch_rsi, s.volume_ratio, s.signal_time,
        o.mfe_60m, o.mae_60m, o.reached_minus_0_5, o.reached_minus_1_0,
        o.reached_minus_1_5, o.reached_minus_2_0,
        'C: stoch>=0.6 & vol<=1.0' AS variant,
        s.oos_c_stoch_06_vol_10 AS passes
    FROM dds.shadow_signal s
    JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
    WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
      AND o.mfe_60m IS NOT NULL
)
SELECT
    variant,
    COUNT(*) AS n,
    COUNT(*) FILTER (WHERE passes) AS passing,
    ROUND(COUNT(*) FILTER (WHERE passes)::numeric / COUNT(*), 4) AS coverage,
    -- Among passing signals
    ROUND(AVG(mfe_60m) FILTER (WHERE passes AND mfe_60m > 0), 4) AS avg_mfe_good,
    ROUND(AVG(mae_60m) FILTER (WHERE passes AND mfe_60m > 0), 4) AS avg_mae_good,
    ROUND(AVG(mfe_60m) FILTER (WHERE passes AND mfe_60m <= 0), 4) AS avg_mfe_bad,
    ROUND(AVG(mae_60m) FILTER (WHERE passes AND mfe_60m <= 0), 4) AS avg_mae_bad,
    ROUND(
        COUNT(*) FILTER (WHERE passes AND mfe_60m > 0)::numeric /
        NULLIF(COUNT(*) FILTER (WHERE passes AND mfe_60m <= 0), 0),
        4
    ) AS good_bad_ratio,
    -- Target hit rates among passing signals
    ROUND(COUNT(*) FILTER (WHERE passes AND reached_minus_0_5)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes), 0), 4) AS hit_0_5_pct,
    ROUND(COUNT(*) FILTER (WHERE passes AND reached_minus_1_0)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes), 0), 4) AS hit_1_0_pct,
    ROUND(COUNT(*) FILTER (WHERE passes AND reached_minus_1_5)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes), 0), 4) AS hit_1_5_pct,
    ROUND(COUNT(*) FILTER (WHERE passes AND reached_minus_2_0)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes), 0), 4) AS hit_2_0_pct
FROM oos_variants
GROUP BY variant
ORDER BY variant;

-- OOS per-symbol breakdown for variant A (as example)
CREATE OR REPLACE VIEW dds.v_shadow_oos_by_symbol AS
SELECT
    s.symbol,
    COUNT(*) AS n,
    COUNT(*) FILTER (WHERE s.oos_a_stoch_08) AS a_pass,
    COUNT(*) FILTER (WHERE s.oos_b_stoch_08_vol_10) AS b_pass,
    COUNT(*) FILTER (WHERE s.oos_c_stoch_06_vol_10) AS c_pass,
    ROUND(AVG(o.mfe_60m) FILTER (WHERE s.oos_a_stoch_08), 4) AS avg_mfe_a,
    ROUND(AVG(o.mfe_60m) FILTER (WHERE s.oos_b_stoch_08_vol_10), 4) AS avg_mfe_b,
    ROUND(AVG(o.mfe_60m) FILTER (WHERE s.oos_c_stoch_06_vol_10), 4) AS avg_mfe_c,
    ROUND(AVG(o.mae_60m) FILTER (WHERE s.oos_a_stoch_08), 4) AS avg_mae_a,
    ROUND(AVG(o.mae_60m) FILTER (WHERE s.oos_b_stoch_08_vol_10), 4) AS avg_mae_b,
    ROUND(AVG(o.mae_60m) FILTER (WHERE s.oos_c_stoch_06_vol_10), 4) AS avg_mae_c
FROM dds.shadow_signal s
JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
  AND o.mfe_60m IS NOT NULL
GROUP BY s.symbol
HAVING COUNT(*) >= 3
ORDER BY COUNT(*) DESC;