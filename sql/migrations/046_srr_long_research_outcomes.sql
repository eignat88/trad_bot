-- ============================================================
-- SRR LONG Research Outcomes — Parameter Analysis Infrastructure
-- ============================================================
-- Captures SUPPORT_RESISTANCE_REACTION LONG signals for research
-- independently of paper trading / direction gates.
--
-- NO production behaviour is changed.  SRR LONG remains blocked
-- for paper trading via blocked_scanner_directions.
--
-- Idempotent migration — safe to re-run.
-- ============================================================

-- ── signal table ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dds.srr_research_signal (
    signal_id           BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL DEFAULT 'SRR_LONG_OUTCOME_V1',
    setup_id            TEXT,
    scanner_name        TEXT NOT NULL DEFAULT 'SUPPORT_RESISTANCE_REACTION',
    scanner_version     TEXT NOT NULL DEFAULT '2.0.0',
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL DEFAULT 'LONG',
    timeframe           TEXT NOT NULL DEFAULT '5m',
    htf_timeframe       TEXT NOT NULL DEFAULT '1h',
    setup_timeframe     TEXT NOT NULL DEFAULT '15m',
    entry_timeframe     TEXT NOT NULL DEFAULT '5m',
    signal_time         TIMESTAMPTZ NOT NULL,
    signal_candle_open_time BIGINT NOT NULL DEFAULT 0,

    -- price levels
    reference_price     NUMERIC NOT NULL,
    entry_zone_low      NUMERIC NOT NULL,
    entry_zone_high     NUMERIC NOT NULL,
    invalidation_price  NUMERIC NOT NULL,
    target_1            NUMERIC,
    target_2            NUMERIC,

    -- normalised quality features
    level_touch_count   NUMERIC NOT NULL,
    rejection_strength  NUMERIC NOT NULL,
    rr_ratio            NUMERIC NOT NULL,
    stop_distance_atr   NUMERIC NOT NULL,
    volume_spike        BOOLEAN NOT NULL DEFAULT FALSE,
    regime_alignment    NUMERIC NOT NULL,

    -- raw numeric features for parameter analysis
    raw_touch_count         INTEGER NOT NULL DEFAULT 0,
    raw_level_distance_pct  NUMERIC NOT NULL DEFAULT 0,
    raw_atr                 NUMERIC NOT NULL DEFAULT 0,
    raw_atr_pct             NUMERIC NOT NULL DEFAULT 0,
    raw_candle_range        NUMERIC NOT NULL DEFAULT 0,
    raw_candle_body         NUMERIC NOT NULL DEFAULT 0,
    raw_upper_wick          NUMERIC NOT NULL DEFAULT 0,
    raw_lower_wick          NUMERIC NOT NULL DEFAULT 0,
    raw_wick_body_ratio     NUMERIC NOT NULL DEFAULT 0,
    raw_volume              NUMERIC NOT NULL DEFAULT 0,
    raw_volume_ratio        NUMERIC NOT NULL DEFAULT 0,
    raw_rr                  NUMERIC NOT NULL DEFAULT 0,
    raw_risk_distance       NUMERIC NOT NULL DEFAULT 0,
    raw_risk_distance_pct   NUMERIC NOT NULL DEFAULT 0,
    raw_stop_distance_atr   NUMERIC NOT NULL DEFAULT 0,

    -- context
    score               NUMERIC NOT NULL DEFAULT 0,
    market_regime       TEXT,
    level_type          TEXT NOT NULL DEFAULT 'support',
    reasons             JSONB DEFAULT '[]'::jsonb,

    -- metadata
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT srr_research_direction_chk CHECK (direction = 'LONG'),
    CONSTRAINT srr_research_experiment_chk CHECK (experiment_id = 'SRR_LONG_OUTCOME_V1')
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_srr_research_signal_experiment
    ON dds.srr_research_signal (experiment_id);
CREATE INDEX IF NOT EXISTS idx_srr_research_signal_symbol
    ON dds.srr_research_signal (symbol, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_srr_research_signal_time
    ON dds.srr_research_signal (signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_srr_research_signal_regime
    ON dds.srr_research_signal (market_regime);
CREATE UNIQUE INDEX IF NOT EXISTS uq_srr_research_signal_candle
    ON dds.srr_research_signal (experiment_id, symbol, signal_candle_open_time)
    WHERE signal_candle_open_time > 0;

-- ── outcome table ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dds.srr_research_outcome (
    signal_id           BIGINT PRIMARY KEY REFERENCES dds.srr_research_signal(signal_id) ON DELETE CASCADE,
    experiment_id       TEXT NOT NULL DEFAULT 'SRR_LONG_OUTCOME_V1',
    symbol              TEXT NOT NULL,

    -- MFE/MAE by horizon (in % from entry)
    mfe_15m  NUMERIC, mae_15m  NUMERIC,
    mfe_30m  NUMERIC, mae_30m  NUMERIC,
    mfe_60m  NUMERIC, mae_60m  NUMERIC,
    mfe_120m NUMERIC, mae_120m NUMERIC,
    mfe_240m NUMERIC, mae_240m NUMERIC,

    -- MFE/MAE normalised to R (1R = abs(entry - stop))
    mfe_r_15m  NUMERIC, mae_r_15m  NUMERIC,
    mfe_r_30m  NUMERIC, mae_r_30m  NUMERIC,
    mfe_r_60m  NUMERIC, mae_r_60m  NUMERIC,
    mfe_r_120m NUMERIC, mae_r_120m NUMERIC,
    mfe_r_240m NUMERIC, mae_r_240m NUMERIC,

    -- Target/stop hit flags
    tp_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    sl_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    tp_before_sl    BOOLEAN NOT NULL DEFAULT FALSE,
    sl_before_tp    BOOLEAN NOT NULL DEFAULT FALSE,

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

    CONSTRAINT srr_outcome_experiment_chk CHECK (experiment_id = 'SRR_LONG_OUTCOME_V1')
);

CREATE INDEX IF NOT EXISTS idx_srr_outcome_experiment
    ON dds.srr_research_outcome (experiment_id);
CREATE INDEX IF NOT EXISTS idx_srr_outcome_symbol
    ON dds.srr_research_outcome (symbol);
CREATE INDEX IF NOT EXISTS idx_srr_outcome_mfe
    ON dds.srr_research_outcome (mfe_60m DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_srr_outcome_is_final
    ON dds.srr_research_outcome (is_final) WHERE is_final = FALSE;

-- ── Observability view ──────────────────────────────────────

CREATE OR REPLACE VIEW dds.v_srr_research_accumulation AS
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
FROM dds.srr_research_signal s
LEFT JOIN dds.srr_research_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'SRR_LONG_OUTCOME_V1'
GROUP BY s.experiment_id;

-- ── Summary by symbol ───────────────────────────────────────

CREATE OR REPLACE VIEW dds.v_srr_research_by_symbol AS
SELECT
    s.symbol,
    COUNT(*) AS signals,
    COUNT(o.signal_id) AS evaluated,
    ROUND(AVG(s.raw_touch_count)::numeric, 2) AS avg_touches,
    ROUND(AVG(s.raw_atr_pct)::numeric, 4) AS avg_atr_pct,
    ROUND(AVG(s.raw_wick_body_ratio)::numeric, 4) AS avg_wick_body_ratio,
    ROUND(AVG(s.raw_volume_ratio)::numeric, 4) AS avg_volume_ratio,
    MIN(s.signal_time) AS first_signal,
    MAX(s.signal_time) AS last_signal
FROM dds.srr_research_signal s
LEFT JOIN dds.srr_research_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'SRR_LONG_OUTCOME_V1'
GROUP BY s.symbol
ORDER BY signals DESC;

-- ── Parameter bucketing view (single feature) ───────────────
-- Pre-built bucketing for common parameters.
-- Extend with additional features as needed.

CREATE OR REPLACE VIEW dds.v_srr_research_bucketed AS
WITH base AS (
    SELECT
        s.signal_id,
        s.symbol,
        s.raw_touch_count,
        s.raw_level_distance_pct,
        s.raw_atr_pct,
        s.raw_wick_body_ratio,
        s.raw_volume_ratio,
        s.raw_rr,
        s.raw_stop_distance_atr,
        s.market_regime,
        s.level_type,
        o.mfe_60m, o.mae_60m,
        o.mfe_r_60m, o.mae_r_60m,
        o.tp_hit, o.sl_hit,
        o.tp_before_sl, o.sl_before_tp,
        o.is_final,
        -- bucket touch count
        CASE
            WHEN s.raw_touch_count <= 2 THEN '1-2'
            WHEN s.raw_touch_count <= 3 THEN '3'
            WHEN s.raw_touch_count <= 4 THEN '4'
            ELSE '5+'
        END AS touch_bucket,
        -- bucket ATR
        CASE
            WHEN s.raw_atr_pct < 1.0 THEN 'low_<1'
            WHEN s.raw_atr_pct < 2.0 THEN 'mid_1-2'
            WHEN s.raw_atr_pct < 3.0 THEN 'high_2-3'
            ELSE 'vhigh_3+'
        END AS atr_bucket,
        -- bucket wick:body ratio
        CASE
            WHEN s.raw_wick_body_ratio < 1.0 THEN 'small_<1'
            WHEN s.raw_wick_body_ratio < 2.0 THEN 'medium_1-2'
            WHEN s.raw_wick_body_ratio < 3.0 THEN 'large_2-3'
            ELSE 'xlarge_3+'
        END AS wick_bucket,
        -- bucket volume ratio
        CASE
            WHEN s.raw_volume_ratio < 0.8 THEN 'below_avg'
            WHEN s.raw_volume_ratio < 1.2 THEN 'average'
            WHEN s.raw_volume_ratio < 2.0 THEN 'elevated_1.2-2'
            ELSE 'spike_2+'
        END AS volume_bucket,
        -- bucket level distance
        CASE
            WHEN s.raw_level_distance_pct < 0.1 THEN 'very_close_<0.1'
            WHEN s.raw_level_distance_pct < 0.3 THEN 'close_0.1-0.3'
            WHEN s.raw_level_distance_pct < 0.5 THEN 'mid_0.3-0.5'
            ELSE 'far_0.5+'
        END AS distance_bucket
    FROM dds.srr_research_signal s
    LEFT JOIN dds.srr_research_outcome o ON o.signal_id = s.signal_id
    WHERE s.experiment_id = 'SRR_LONG_OUTCOME_V1'
)
SELECT * FROM base;

-- ============================================================
-- NO paper trades are created by this migration.
-- SRR LONG remains BLOCKED in config.scanner_direction_gate.
-- ============================================================
