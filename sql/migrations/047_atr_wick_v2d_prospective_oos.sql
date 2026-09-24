-- ============================================================
-- ATR Wick Filter OOS V2_D — Prospective Experiment
-- ============================================================
-- Standalone prospective OOS experiment that applies a
-- StochRSI filter (0.20 <= StochRSI < 0.60) to SHORT signals
-- detected by the ATR Wick scanner.
--
-- Architecture:
--   - Separate tables: dds.v2d_signal, dds.v2d_outcome
--   - Prospective only: signals recorded from T0 (deploy time) forward
--   - No backfill of historical V1 observations
--   - Independent outcome evaluation pipeline
--
-- Filter: direction = SHORT, 0.20 <= StochRSI < 0.60
-- No volume filter applied (per experiment config)
--
-- Registry dependency:
--   dds.shadow_oos_experiment_registry is a shared canonical table
--   created by earlier migrations.  This migration does NOT create
--   or redefine it.  The V2_D row must already exist with
--   exploratory metadata before this migration is run.
--
-- Idempotent migration — safe to re-run.
-- ============================================================

-- ── signal table ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dds.v2d_signal (
    signal_id           BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL DEFAULT 'ATR_WICK_FILTER_OOS_V2_D',
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL DEFAULT '5m',
    direction           TEXT NOT NULL DEFAULT 'SHORT',

    -- price data
    signal_time         TIMESTAMPTZ NOT NULL,
    signal_price        NUMERIC NOT NULL,
    open                NUMERIC NOT NULL,
    high                NUMERIC NOT NULL,
    low                 NUMERIC NOT NULL,
    close               NUMERIC NOT NULL,
    volume              NUMERIC NOT NULL,

    -- ATR indicators
    atr                 NUMERIC NOT NULL,
    atr_pct             NUMERIC NOT NULL,

    -- wick analysis
    wick_size           NUMERIC NOT NULL,
    wick_atr            NUMERIC NOT NULL,
    upper_wick_pct      NUMERIC NOT NULL,
    close_location      NUMERIC NOT NULL,

    -- RSI / StochRSI
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

    -- V2_D filter result
    filter_pass         BOOLEAN NOT NULL,
    filter_reason       TEXT,

    -- metadata
    signal_version      TEXT NOT NULL DEFAULT '1.0.0',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- constraints
    CONSTRAINT v2d_signal_experiment_chk
        CHECK (experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'),
    CONSTRAINT v2d_signal_direction_chk
        CHECK (direction = 'SHORT'),
    CONSTRAINT v2d_signal_filter_pass_chk
        CHECK (filter_pass = TRUE)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_v2d_signal_experiment
    ON dds.v2d_signal (experiment_id);
CREATE INDEX IF NOT EXISTS idx_v2d_signal_symbol
    ON dds.v2d_signal (symbol, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_v2d_signal_time
    ON dds.v2d_signal (signal_time DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_v2d_signal_experiment_symbol_time
    ON dds.v2d_signal (experiment_id, symbol, signal_time);

-- ── outcome table ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dds.v2d_outcome (
    signal_id           BIGINT PRIMARY KEY REFERENCES dds.v2d_signal(signal_id) ON DELETE CASCADE,
    experiment_id       TEXT NOT NULL DEFAULT 'ATR_WICK_FILTER_OOS_V2_D',
    symbol              TEXT NOT NULL,

    -- MFE/MAE by horizon (in % from entry, SHORT direction)
    mfe_15m   NUMERIC, mae_15m   NUMERIC,
    mfe_30m   NUMERIC, mae_30m   NUMERIC,
    mfe_60m   NUMERIC, mae_60m   NUMERIC,
    mfe_120m  NUMERIC, mae_120m  NUMERIC,
    mfe_240m  NUMERIC, mae_240m  NUMERIC,

    -- Target hit flags
    reached_minus_0_5  BOOLEAN NOT NULL DEFAULT FALSE,
    reached_minus_1_0  BOOLEAN NOT NULL DEFAULT FALSE,
    reached_minus_1_5  BOOLEAN NOT NULL DEFAULT FALSE,
    reached_minus_2_0  BOOLEAN NOT NULL DEFAULT FALSE,
    hit_plus_0_5_before_target  BOOLEAN NOT NULL DEFAULT FALSE,
    hit_plus_1_0_before_target  BOOLEAN NOT NULL DEFAULT FALSE,
    hit_plus_1_5_before_target  BOOLEAN NOT NULL DEFAULT FALSE,

    -- Per-horizon evaluation timestamps
    evaluated_15m_at  TIMESTAMPTZ,
    evaluated_30m_at  TIMESTAMPTZ,
    evaluated_60m_at  TIMESTAMPTZ,
    evaluated_120m_at TIMESTAMPTZ,
    evaluated_240m_at TIMESTAMPTZ,

    is_final        BOOLEAN NOT NULL DEFAULT FALSE,

    -- metadata
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT v2d_outcome_experiment_chk
        CHECK (experiment_id = 'ATR_WICK_FILTER_OOS_V2_D')
);

CREATE INDEX IF NOT EXISTS idx_v2d_outcome_experiment
    ON dds.v2d_outcome (experiment_id);
CREATE INDEX IF NOT EXISTS idx_v2d_outcome_symbol
    ON dds.v2d_outcome (symbol);
CREATE INDEX IF NOT EXISTS idx_v2d_outcome_mfe
    ON dds.v2d_outcome (mfe_60m DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_v2d_outcome_is_final
    ON dds.v2d_outcome (is_final) WHERE is_final = FALSE;

-- ── Experiment registry ─────────────────────────────────────
--
-- Registry is canonical and created by earlier migrations.
-- The V2_D row MUST already exist from exploratory analysis
-- with full discovery metadata (hypothesis, filter_definition,
-- sample_n, good/bad counts, etc.).
--
-- This migration only flips prospective_started_at to signal
-- the start of prospective OOS collection.
-- COALESCE ensures the timestamp is set exactly once.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM dds.shadow_oos_experiment_registry
        WHERE experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
    ) THEN
        RAISE EXCEPTION
            'ATR_WICK_FILTER_OOS_V2_D is missing from '
            'shadow_oos_experiment_registry; '
            'exploratory hypothesis must be registered '
            'before prospective OOS starts';
    END IF;
END $$;

UPDATE dds.shadow_oos_experiment_registry
SET
    prospective_started_at =
        COALESCE(prospective_started_at, now())
WHERE experiment_id = 'ATR_WICK_FILTER_OOS_V2_D';

-- ── Observability view ──────────────────────────────────────

CREATE OR REPLACE VIEW dds.v_v2d_accumulation AS
SELECT
    s.experiment_id,
    COUNT(*)                                                        AS signals_collected,
    COUNT(o.signal_id)                                              AS outcomes_created,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_15m_at IS NOT NULL)  AS outcomes_15m,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_30m_at IS NOT NULL)  AS outcomes_30m,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_60m_at IS NOT NULL)  AS outcomes_60m,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_120m_at IS NOT NULL) AS outcomes_120m,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_240m_at IS NOT NULL) AS outcomes_240m,
    COUNT(o.signal_id) FILTER (WHERE o.is_final = TRUE)               AS outcomes_final,
    MIN(s.signal_time)                                            AS oldest_signal,
    MAX(s.signal_time)                                            AS newest_signal,
    COUNT(o.signal_id) FILTER (WHERE o.is_final = FALSE AND o.signal_id IS NOT NULL) AS pending_evaluation
FROM dds.v2d_signal s
LEFT JOIN dds.v2d_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'ATR_WICK_FILTER_OOS_V2_D'
GROUP BY s.experiment_id;

-- ── Filter compliance view ──────────────────────────────────

CREATE OR REPLACE VIEW dds.v_v2d_filter_compliance AS
SELECT
    COUNT(*) AS total_signals,
    COUNT(*) FILTER (WHERE direction = 'SHORT') AS direction_short,
    COUNT(*) FILTER (WHERE stoch_rsi >= 0.20 AND stoch_rsi < 0.60) AS stoch_rsi_in_range,
    COUNT(*) FILTER (WHERE stoch_rsi < 0.20 OR stoch_rsi >= 0.60) AS stoch_rsi_out_of_range,
    COUNT(*) FILTER (WHERE filter_pass = TRUE) AS filter_pass_true,
    COUNT(*) FILTER (WHERE filter_pass = FALSE) AS filter_pass_false
FROM dds.v2d_signal
WHERE experiment_id = 'ATR_WICK_FILTER_OOS_V2_D';

-- ============================================================
-- V2_D is a SHADOW/RESEARCH experiment only.
-- It does NOT create paper trades or live positions.
-- V1 continues operating independently — its data is untouched.
--
-- Discovery metadata in shadow_oos_experiment_registry is
-- preserved: status, hypothesis, filter_definition, sample_n,
-- good_count, bad_count, and all metrics remain unchanged.
-- ============================================================
