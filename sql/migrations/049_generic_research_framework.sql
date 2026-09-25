-- ============================================================
-- Migration 049: Generic OOS Research Framework V1
-- ============================================================
-- Creates the `research` schema and core tables for the unified
-- research observation pipeline.  Every scanner candidate is
-- captured as a research observation, regardless of its fate in
-- the production pipeline (gates, filters, dedup, etc.).
--
-- Idempotent: all CREATE TABLE IF NOT EXISTS.
-- Does NOT modify: dds.*, config.*, paper_* tables.
--
-- Tables created:
--   research.research_experiment      — experiment registry
--   research.research_observation     — every candidate (pass + reject)
--   research.research_signal          — quality-filtered subset for evaluation
--   research.research_outcome         — multi-horizon MFE/MAE/TP/SL
--
-- ============================================================

-- ── 0. Schema ──────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS research;

-- ── 1. research_experiment ─────────────────────────────────
-- One row per active research experiment (one scanner, one parameter set).

CREATE TABLE IF NOT EXISTS research.research_experiment (
    experiment_id       TEXT PRIMARY KEY,
    scanner_name        TEXT NOT NULL,
    scanner_version     TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','PAUSED','COMPLETED')),
    parameter_set_id    TEXT NOT NULL,
    parameters_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_re_experiment_scanner
    ON research.research_experiment (scanner_name);

CREATE INDEX IF NOT EXISTS idx_re_experiment_status
    ON research.research_experiment (status);

COMMENT ON TABLE research.research_experiment
    IS 'Registry of active research experiments — one per scanner+parameter_set (migration 049)';
COMMENT ON COLUMN research.research_experiment.parameter_set_id
    IS 'Unique versioned parameter snapshot ID, e.g. me_r_1.0.0_20260928';
COMMENT ON COLUMN research.research_experiment.parameters_snapshot
    IS 'Frozen JSONB snapshot of scanner parameters at experiment creation time';

-- ── 2. research_observation ────────────────────────────────
-- The core table: every scanner candidate is recorded here.
-- Both PASS and REJECT candidates are stored.
-- Append-only: no UPDATE/DELETE on this table in normal operation.

CREATE TABLE IF NOT EXISTS research.research_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL
                        REFERENCES research.research_experiment(experiment_id),

    -- Scanner identity
    scanner_name        TEXT NOT NULL,
    scanner_version     TEXT NOT NULL,
    parameter_set_id    TEXT NOT NULL,

    -- Signal identity
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
    signal_time         TIMESTAMPTZ NOT NULL,
    signal_candle_open_time BIGINT NOT NULL DEFAULT 0,

    -- Price levels (from scanner)
    reference_price     NUMERIC NOT NULL,
    entry_zone_low      NUMERIC,
    entry_zone_high     NUMERIC,
    invalidation_price  NUMERIC,
    target_1            NUMERIC,
    target_2            NUMERIC,
    score               NUMERIC NOT NULL DEFAULT 0,

    -- Rejection chain
    status              TEXT NOT NULL DEFAULT 'DETECTED'
                        CHECK (status IN (
                            'DETECTED',
                            'SCORE_REJECTED',
                            'GEOMETRY_REJECTED',
                            'DEDUP_REJECTED',
                            'GATE_REJECTED',
                            'EXPECTANCY_REJECTED',
                            'REGIME_REJECTED',
                            'SETUP_READY',
                            'OOS_REJECTED'
                        )),
    rejection_stage     TEXT,
    rejection_reason    TEXT,

    -- Immutable feature snapshot (JSONB — scanner-specific keys)
    features            JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Immutable parameter snapshot
    parameters          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Market context snapshot (at detection time)
    market_regime       TEXT,
    htf_timeframe       TEXT,
    setup_timeframe     TEXT,
    entry_timeframe     TEXT,

    -- Production link
    setup_id            TEXT,

    -- Metadata
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedup: one observation per experiment + symbol + direction + candle
-- Includes direction because some scanners (e.g. VOLATILITY_COMPRESSION)
-- can emit both LONG and SHORT candidates on the same candle.
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_observation_identity
    ON research.research_observation (experiment_id, symbol, direction, signal_candle_open_time)
    WHERE signal_candle_open_time > 0;

-- Evaluator query performance
CREATE INDEX IF NOT EXISTS idx_ro_experiment
    ON research.research_observation (experiment_id);
CREATE INDEX IF NOT EXISTS idx_ro_scanner
    ON research.research_observation (scanner_name, direction);
CREATE INDEX IF NOT EXISTS idx_ro_symbol_time
    ON research.research_observation (symbol, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_ro_status
    ON research.research_observation (status);
CREATE INDEX IF NOT EXISTS idx_ro_time
    ON research.research_observation (signal_time DESC);

-- GIN for JSONB feature queries
CREATE INDEX IF NOT EXISTS idx_ro_features
    ON research.research_observation USING GIN (features);

-- Lookup by production setup_id (for status resolution)
CREATE INDEX IF NOT EXISTS idx_ro_setup_id
    ON research.research_observation (setup_id)
    WHERE setup_id IS NOT NULL;

COMMENT ON TABLE research.research_observation
    IS 'Immutable record of every scanner candidate — pass + reject (migration 049)';
COMMENT ON COLUMN research.research_observation.status
    IS 'Final status in rejection chain: DETECTED -> *_REJECTED / SETUP_READY';
COMMENT ON COLUMN research.research_observation.features
    IS 'Frozen JSONB snapshot of scanner-specific features at detection time';
COMMENT ON COLUMN research.research_observation.parameters
    IS 'Frozen JSONB snapshot of scanner parameters (copied from experiment)';
COMMENT ON COLUMN research.research_observation.rejection_stage
    IS 'Pipeline stage where rejection occurred (dedup, geometry, score, gate, etc.)';
COMMENT ON COLUMN research.research_observation.rejection_reason
    IS 'Human-readable rejection reason';

-- ── 3. research_signal ─────────────────────────────────────
-- Quality-filtered subset: only observations with valid price levels
-- and SL/TP that can be evaluated.  This is the evaluator's working set.

CREATE TABLE IF NOT EXISTS research.research_signal (
    signal_id           BIGSERIAL PRIMARY KEY,
    observation_id      BIGINT NOT NULL
                        REFERENCES research.research_observation(observation_id),

    -- Denormalized for fast evaluator queries
    experiment_id       TEXT NOT NULL,
    scanner_name        TEXT NOT NULL,
    parameter_set_id    TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    signal_time         TIMESTAMPTZ NOT NULL,
    signal_candle_open_time BIGINT NOT NULL DEFAULT 0,
    reference_price     NUMERIC NOT NULL,
    invalidation_price  NUMERIC,
    target_1            NUMERIC,
    target_2            NUMERIC,
    score               NUMERIC NOT NULL DEFAULT 0,
    features            JSONB NOT NULL DEFAULT '{}'::jsonb,
    parameters          JSONB NOT NULL DEFAULT '{}'::jsonb,
    market_regime       TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedup: one signal per experiment + symbol + direction + candle
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_signal_identity
    ON research.research_signal (experiment_id, symbol, direction, signal_candle_open_time)
    WHERE signal_candle_open_time > 0;

CREATE INDEX IF NOT EXISTS idx_rs_experiment
    ON research.research_signal (experiment_id);
CREATE INDEX IF NOT EXISTS idx_rs_scanner
    ON research.research_signal (scanner_name, direction);
CREATE INDEX IF NOT EXISTS idx_rs_symbol
    ON research.research_signal (symbol, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_rs_features
    ON research.research_signal USING GIN (features);

COMMENT ON TABLE research.research_signal
    IS 'Quality-filtered research signals linked to observations (migration 049)';

-- ── 4. research_outcome ────────────────────────────────────
-- Multi-horizon MFE/MAE/TP/SL evaluation.
-- One row per signal, updated incrementally as horizons mature.

CREATE TABLE IF NOT EXISTS research.research_outcome (
    signal_id           BIGINT PRIMARY KEY
                        REFERENCES research.research_signal(signal_id) ON DELETE CASCADE,
    experiment_id       TEXT NOT NULL,
    symbol              TEXT NOT NULL,

    -- MFE/MAE by horizon (in % from entry)
    mfe_15m  NUMERIC, mae_15m  NUMERIC,
    mfe_30m  NUMERIC, mae_30m  NUMERIC,
    mfe_60m  NUMERIC, mae_60m  NUMERIC,
    mfe_120m NUMERIC, mae_120m NUMERIC,
    mfe_240m NUMERIC, mae_240m NUMERIC,

    -- MFE/MAE normalised to R (1R = abs(entry - invalidation))
    mfe_r_15m  NUMERIC, mae_r_15m  NUMERIC,
    mfe_r_30m  NUMERIC, mae_r_30m  NUMERIC,
    mfe_r_60m  NUMERIC, mae_r_60m  NUMERIC,
    mfe_r_120m NUMERIC, mae_r_120m NUMERIC,
    mfe_r_240m NUMERIC, mae_r_240m NUMERIC,

    -- Return at horizon (total return % if exit at horizon close)
    return_at_15m  NUMERIC,
    return_at_30m  NUMERIC,
    return_at_60m  NUMERIC,
    return_at_120m NUMERIC,
    return_at_240m NUMERIC,

    -- Target/stop hit flags
    tp_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    sl_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    tp_before_sl    BOOLEAN NOT NULL DEFAULT FALSE,
    sl_before_tp    BOOLEAN NOT NULL DEFAULT FALSE,

    -- Time to TP / SL (in minutes, NULL if not hit within 240m)
    time_to_tp      NUMERIC,
    time_to_sl      NUMERIC,

    -- Per-horizon evaluation timestamps
    evaluated_15m_at  TIMESTAMPTZ,
    evaluated_30m_at  TIMESTAMPTZ,
    evaluated_60m_at  TIMESTAMPTZ,
    evaluated_120m_at TIMESTAMPTZ,
    evaluated_240m_at TIMESTAMPTZ,

    is_final        BOOLEAN NOT NULL DEFAULT FALSE,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rou_experiment
    ON research.research_outcome (experiment_id);
CREATE INDEX IF NOT EXISTS idx_rou_symbol
    ON research.research_outcome (symbol);
CREATE INDEX IF NOT EXISTS idx_rou_mfe60
    ON research.research_outcome (mfe_60m DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_rou_pending
    ON research.research_outcome (is_final) WHERE is_final = FALSE;

COMMENT ON TABLE research.research_outcome
    IS 'Multi-horizon MFE/MAE/TP/SL evaluation for research signals (migration 049)';

-- ── 5. Observability views ─────────────────────────────────

CREATE OR REPLACE VIEW research.v_experiment_accumulation AS
SELECT
    o.experiment_id,
    o.scanner_name,
    COUNT(*)                                                        AS observations_total,
    COUNT(*) FILTER (WHERE o.status = 'SETUP_READY')                AS setup_ready,
    COUNT(*) FILTER (WHERE o.status = 'DETECTED')                   AS still_detected,
    COUNT(*) FILTER (WHERE o.status LIKE '%REJECTED')               AS total_rejected,
    COUNT(*) FILTER (WHERE o.status = 'GATE_REJECTED')              AS gate_rejected,
    COUNT(*) FILTER (WHERE o.status = 'SCORE_REJECTED')             AS score_rejected,
    COUNT(*) FILTER (WHERE o.status = 'GEOMETRY_REJECTED')          AS geometry_rejected,
    COUNT(*) FILTER (WHERE o.status = 'DEDUP_REJECTED')             AS dedup_rejected,
    COUNT(*) FILTER (WHERE o.status = 'EXPECTANCY_REJECTED')        AS expectancy_rejected,
    COUNT(*) FILTER (WHERE o.status = 'REGIME_REJECTED')            AS regime_rejected,
    COUNT(s.signal_id)                                              AS signals_created,
    COUNT(r.signal_id)                                              AS outcomes_created,
    COUNT(r.signal_id) FILTER (WHERE r.is_final = TRUE)             AS outcomes_finalized,
    MIN(o.signal_time)                                              AS oldest_observation,
    MAX(o.signal_time)                                              AS newest_observation
FROM research.research_observation o
LEFT JOIN research.research_signal s ON s.observation_id = o.observation_id
LEFT JOIN research.research_outcome r ON r.signal_id = s.signal_id
GROUP BY o.experiment_id, o.scanner_name;

COMMENT ON VIEW research.v_experiment_accumulation
    IS 'Accumulation stats per experiment — observations, signals, outcomes';

CREATE OR REPLACE VIEW research.v_rejection_breakdown AS
SELECT
    experiment_id,
    rejection_stage,
    rejection_reason,
    COUNT(*) AS count,
    MIN(signal_time) AS first_seen,
    MAX(signal_time) AS last_seen
FROM research.research_observation
WHERE status LIKE '%REJECTED'
GROUP BY experiment_id, rejection_stage, rejection_reason
ORDER BY experiment_id, count DESC;

COMMENT ON VIEW research.v_rejection_breakdown
    IS 'Rejection breakdown per experiment and reason';

-- ── 6. updated_at trigger for research_experiment ──────────

CREATE OR REPLACE FUNCTION research.fn_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_research_experiment_updated_at'
    ) THEN
        CREATE TRIGGER trg_research_experiment_updated_at
            BEFORE UPDATE ON research.research_experiment
            FOR EACH ROW
            EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_research_outcome_updated_at'
    ) THEN
        CREATE TRIGGER trg_research_outcome_updated_at
            BEFORE UPDATE ON research.research_outcome
            FOR EACH ROW
            EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;
END
$$;

-- ── 7. Seed: MOMENTUM_EXHAUSTION_R pilot experiment ────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'MER_GENERIC_V1',
    'MOMENTUM_EXHAUSTION_R',
    '1.0.0',
    'Generic research observation for MOMENTUM_EXHAUSTION_R. '
    'Captures all LONG and SHORT candidates for outcome evaluation '
    'and offline parameter optimization.',
    'ACTIVE',
    'mer_1.0.0_20260928',
    '{"swing_lookback": 5, "exhaustion_threshold": 0.003}'::jsonb
)
ON CONFLICT (experiment_id) DO NOTHING;

-- ── 8. Seed: TREND_PULLBACK_V2 experiment ──────────────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'TPV2_GENERIC_V1',
    'TREND_PULLBACK_V2',
    '1.0.0',
    'Generic research observation for TREND_PULLBACK_V2. '
    'Captures all LONG candidates for outcome evaluation '
    'and offline parameter optimization.',
    'ACTIVE',
    'tpv2_1.0.0_20260928',
    '{"pullback_tolerance": 0.012, "rsi_cool_threshold": 55, "target_r": 0.50, "stop_buffer": 0.002}'::jsonb
)
ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9. Seed: BREAKOUT_RETEST experiment ────────────────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'BR_GENERIC_V1',
    'BREAKOUT_RETEST',
    '2.0.0',
    'Generic research observation for BREAKOUT_RETEST. '
    'Captures all LONG and SHORT candidates for outcome evaluation '
    'and offline parameter optimization.',
    'ACTIVE',
    'br_2.0.0_20260928',
    '{"swing_lookback": 5, "breakout_margin": 0.001, "retest_margin": 0.003}'::jsonb
)
ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9b. Seed: VOLATILITY_COMPRESSION ───────────────────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'VC_GENERIC_V1', 'VOLATILITY_COMPRESSION', '2.0.0',
    'Generic research for VOLATILITY_COMPRESSION. LONG + SHORT.',
    'ACTIVE', 'vc_2.0.0_20260928',
    '{"atr_period": 14, "squeeze_lookback": 20, "bb_squeeze_threshold": 0.02}'::jsonb
) ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9c. Seed: MOMENTUM_EXHAUSTION ──────────────────────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'ME_GENERIC_V1', 'MOMENTUM_EXHAUSTION', '1.0.0',
    'Generic research for MOMENTUM_EXHAUSTION. LONG + SHORT.',
    'ACTIVE', 'me_1.0.0_20260928',
    '{"swing_lookback": 5, "exhaustion_threshold": 0.003}'::jsonb
) ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9d. Seed: SUPPORT_RESISTANCE_REACTION ──────────────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'SRR_GENERIC_V1', 'SUPPORT_RESISTANCE_REACTION', '2.0.0',
    'Generic research for SUPPORT_RESISTANCE_REACTION. LONG + SHORT. Parallel to SRR_LONG_OUTCOME_V1.',
    'ACTIVE', 'srr_2.0.0_20260928',
    '{"swing_lookback": 5, "level_proximity_pct": 0.003, "min_touches": 3, "min_rejection_body_ratio": 0.5}'::jsonb
) ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9e. Seed: LIQUIDITY_REVERSAL ───────────────────────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'LR_GENERIC_V1', 'LIQUIDITY_REVERSAL', '2.0.0',
    'Generic research for LIQUIDITY_REVERSAL. LONG + SHORT.',
    'ACTIVE', 'lr_2.0.0_20260928',
    '{"swing_lookback": 5, "sweep_margin": 0.001}'::jsonb
) ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9f. Seed: LIQUIDITY_SWEEP_CHOCH_OB ─────────────────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'LSCO_GENERIC_V1', 'LIQUIDITY_SWEEP_CHOCH_OB', '2.0.0',
    'Generic research for LIQUIDITY_SWEEP_CHOCH_OB. LONG + SHORT.',
    'ACTIVE', 'lsco_2.0.0_20260928',
    '{"ob_lookback": 5, "swing_lookback": 5, "sweep_margin": 0.001}'::jsonb
) ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9g. Seed: TREND_PULLBACK_V3 ────────────────────────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'TPV3_GENERIC_V1', 'TREND_PULLBACK_V3', '1.0.0',
    'Generic research for TREND_PULLBACK_V3. Edge-optimized, LONG only.',
    'ACTIVE', 'tpv3_1.0.0_20260928',
    '{"pullback_tolerance": 0.012, "rsi_threshold": 60.0, "adx_threshold": 35.0, "target_r": 0.50, "stop_buffer": 0.002}'::jsonb
) ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9h. Seed: MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 ──────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'ME_RL_V2_GENERIC_V1', 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2', '1.0.0',
    'Generic research for ME_R_LONG_V2. Reversal LONG with RSI delta.',
    'ACTIVE', 'me_rlv2_1.0.0_20260928',
    '{"swing_lookback": 5, "exhaustion_threshold": 0.003, "rsi_period": 14, "rsi_delta_lookback": 3}'::jsonb
) ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9i. Seed: MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 ──────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'ME_RL_V1_GENERIC_V1', 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', '1.0.0',
    'Generic research for ME_R_LONG_V1. BLOCKED in production — captures blocked candidates.',
    'ACTIVE', 'me_rlv1_1.0.0_20260928',
    '{"swing_lookback": 5, "exhaustion_threshold": 0.003}'::jsonb
) ON CONFLICT (experiment_id) DO NOTHING;

-- ── 9j. Seed: FVG_REACTION_LONG_LOCAL_STRUCT_V1 ────────────

INSERT INTO research.research_experiment (
    experiment_id, scanner_name, scanner_version, description,
    status, parameter_set_id, parameters_snapshot
) VALUES (
    'FVG_GENERIC_V1', 'FVG_REACTION_LONG_LOCAL_STRUCT_V1', '1.0.1',
    'Generic research for FVG_REACTION_LONG. Stateful, LONG only. Parallel to FVG shadow.',
    'ACTIVE', 'fvg_1.0.1_20260928',
    '{"MIN_FVG_ATR": 0.05, "MIN_C2_BODY_RATIO": 0.50, "MAX_BARS_TO_TOUCH": 48, "SL_BUFFER_ATR": 0.05, "TARGET_R": 3.0, "LOCAL_STRUCT_LOOKBACK": 20}'::jsonb
) ON CONFLICT (experiment_id) DO NOTHING;

-- ── 10. GRANT permissions for trad_bot role ────────────────
-- Required: trad_bot process connects as role 'trad_bot'.
-- Idempotent: GRANT is safe to re-run.

GRANT USAGE ON SCHEMA research TO trad_bot;

GRANT SELECT, INSERT, UPDATE
    ON ALL TABLES IN SCHEMA research
    TO trad_bot;

GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA research
    TO trad_bot;

-- Default privileges: future tables/sequences created in research schema
-- automatically get granted to trad_bot.
ALTER DEFAULT PRIVILEGES IN SCHEMA research
    GRANT SELECT, INSERT, UPDATE ON TABLES TO trad_bot;

ALTER DEFAULT PRIVILEGES IN SCHEMA research
    GRANT USAGE, SELECT ON SEQUENCES TO trad_bot;

-- ── 11. Backfill experiment_id in existing outcomes ────────
-- Idempotent: only touches rows where experiment_id is empty.

UPDATE research.research_outcome o
SET experiment_id = s.experiment_id
FROM research.research_signal s
WHERE s.signal_id = o.signal_id
  AND (o.experiment_id IS NULL OR o.experiment_id = '');

-- ============================================================
-- NO production tables modified.
-- NO paper trading tables modified.
-- ============================================================
