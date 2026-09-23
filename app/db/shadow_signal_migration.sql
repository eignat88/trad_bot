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