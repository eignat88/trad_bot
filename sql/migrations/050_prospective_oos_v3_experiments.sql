-- ============================================================
-- Migration 050: Prospective OOS Experiments V3
-- ============================================================
-- Creates infrastructure for 6 prospective experiments:
--   1. SRR_LONG_BASELINE_V1        (baseline validation)
--   2. ME_SHORT_GEOM_A_V1          (geometry control)
--   3. ME_SHORT_GEOM_B_V1          (wider stop intervention)
--   4. ME_SHORT_GEOM_C_V1          (delayed entry intervention)
--   5. VC_SHORT_BB_WIDTH_V1        (feature filter)
--   6. LR_SHORT_GATE_V1            (gate validation)
--
-- Architecture:
--   - Uses existing research schema (extends generic framework)
--   - Prospective registry for frozen experiment specs
--   - Prospective observations with experiment tags
--   - Prospective outcomes (reuses evaluator multi-horizon pattern)
--   - ME A/B/C share source_signal_id for paired comparison
--
-- Idempotent: all CREATE TABLE IF NOT EXISTS.
-- ============================================================

-- ── 1. Prospective experiment registry ────────────────────────
-- Stores frozen specifications for each prospective experiment.
-- started_at is NULL until deployment is complete.

CREATE TABLE IF NOT EXISTS research.prospective_experiment (
    experiment_id       TEXT PRIMARY KEY,
    version             INT NOT NULL DEFAULT 1,
    scanner_name        TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    experiment_type     TEXT NOT NULL,
    hypothesis          TEXT NOT NULL DEFAULT '',
    primary_metric      TEXT NOT NULL DEFAULT 'MFE_pct_60m',
    secondary_metrics   JSONB NOT NULL DEFAULT '[]'::jsonb,
    filter_rule         TEXT NOT NULL DEFAULT 'NONE',
    threshold           NUMERIC,
    entry_rule          TEXT NOT NULL DEFAULT '',
    stop_rule           TEXT NOT NULL DEFAULT '',
    target_rule         TEXT NOT NULL DEFAULT '',
    position_sizing     TEXT NOT NULL DEFAULT 'shadow_only',
    fee_assumption      TEXT NOT NULL DEFAULT 'none',
    horizons            JSONB NOT NULL DEFAULT '["15m","30m","60m","120m","240m"]'::jsonb,
    minimum_n           INT NOT NULL DEFAULT 50,
    minimum_symbols     INT NOT NULL DEFAULT 10,
    discovery_source    TEXT NOT NULL DEFAULT '',
    paired_with         TEXT,
    started_at          TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'READY_TO_START'
                        CHECK (status IN ('READY_TO_START', 'RUNNING', 'PAUSED', 'COMPLETED', 'CANCELLED')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE research.prospective_experiment
    IS 'Frozen prospective OOS experiment specifications (migration 050)';

-- ── 2. Prospective observations ───────────────────────────────
-- One row per candidate captured for a prospective experiment.
-- ME A/B/C variants share source_signal_id for paired comparison.

CREATE TABLE IF NOT EXISTS research.prospective_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL REFERENCES research.prospective_experiment(experiment_id),
    source_signal_id    BIGINT NOT NULL,
    source_observation_id BIGINT,

    -- Signal identity
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    signal_time         TIMESTAMPTZ NOT NULL,

    -- Original geometry (from scanner)
    reference_price     NUMERIC NOT NULL,
    invalidation_price  NUMERIC,
    target_1            NUMERIC,
    target_2            NUMERIC,
    score               NUMERIC NOT NULL DEFAULT 0,

    -- Variant-specific geometry (for ME A/B/C)
    variant_entry       NUMERIC,
    variant_stop        NUMERIC,
    variant_target      NUMERIC,

    -- Filter result
    rule_passed         BOOLEAN NOT NULL DEFAULT TRUE,
    filter_reason       TEXT,

    -- Gate result (for gate validation experiments)
    gate_result         TEXT,

    -- Feature values at detection time
    features            JSONB NOT NULL DEFAULT '{}'::jsonb,
    parameters          JSONB NOT NULL DEFAULT '{}'::jsonb,
    market_regime       TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (experiment_id, source_signal_id)
);

CREATE INDEX IF NOT EXISTS idx_pobs_experiment
    ON research.prospective_observation (experiment_id);
CREATE INDEX IF NOT EXISTS idx_pobs_source_signal
    ON research.prospective_observation (source_signal_id);
CREATE INDEX IF NOT EXISTS idx_pobs_symbol_time
    ON research.prospective_observation (symbol, signal_time DESC);

COMMENT ON TABLE research.prospective_observation
    IS 'Prospective OOS observations with frozen experiment rules (migration 050)';

-- ── 3. Prospective outcomes ───────────────────────────────────
-- Multi-horizon evaluation, same structure as research_outcome
-- but for prospective experiments only.

CREATE TABLE IF NOT EXISTS research.prospective_outcome (
    observation_id      BIGINT PRIMARY KEY REFERENCES research.prospective_observation(observation_id) ON DELETE CASCADE,
    experiment_id       TEXT NOT NULL,

    -- MFE/MAE by horizon (in % from entry)
    mfe_15m  NUMERIC, mae_15m  NUMERIC,
    mfe_30m  NUMERIC, mae_30m  NUMERIC,
    mfe_60m  NUMERIC, mae_60m  NUMERIC,
    mfe_120m NUMERIC, mae_120m NUMERIC,
    mfe_240m NUMERIC, mae_240m NUMERIC,

    -- R-normalised
    mfe_r_15m  NUMERIC, mae_r_15m  NUMERIC,
    mfe_r_30m  NUMERIC, mae_r_30m  NUMERIC,
    mfe_r_60m  NUMERIC, mae_r_60m  NUMERIC,
    mfe_r_120m NUMERIC, mae_r_120m NUMERIC,
    mfe_r_240m NUMERIC, mae_r_240m NUMERIC,

    -- Return at horizon
    return_at_15m  NUMERIC,
    return_at_30m  NUMERIC,
    return_at_60m  NUMERIC,
    return_at_120m NUMERIC,
    return_at_240m NUMERIC,

    -- TP/SL hit flags
    tp_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    sl_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    tp_before_sl    BOOLEAN NOT NULL DEFAULT FALSE,
    sl_before_tp    BOOLEAN NOT NULL DEFAULT FALSE,
    ambiguous_intrabar BOOLEAN NOT NULL DEFAULT FALSE,

    -- Time to TP / SL (minutes)
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

CREATE INDEX IF NOT EXISTS idx_pout_experiment
    ON research.prospective_outcome (experiment_id);
CREATE INDEX IF NOT EXISTS idx_pout_mfe60
    ON research.prospective_outcome (mfe_60m DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_pout_pending
    ON research.prospective_outcome (is_final) WHERE is_final = FALSE;

COMMENT ON TABLE research.prospective_outcome
    IS 'Multi-horizon outcomes for prospective OOS experiments (migration 050)';

-- ── 4. Updated_at triggers ────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_prospective_experiment_updated_at'
    ) THEN
        CREATE TRIGGER trg_prospective_experiment_updated_at
            BEFORE UPDATE ON research.prospective_experiment
            FOR EACH ROW
            EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_prospective_outcome_updated_at'
    ) THEN
        CREATE TRIGGER trg_prospective_outcome_updated_at
            BEFORE UPDATE ON research.prospective_outcome
            FOR EACH ROW
            EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;
END $$;

-- ── 5. Accumulation view ──────────────────────────────────────

CREATE OR REPLACE VIEW research.v_prospective_accumulation AS
SELECT
    o.experiment_id,
    pe.scanner_name,
    pe.direction,
    pe.experiment_type,
    pe.status,
    pe.started_at,
    COUNT(o.observation_id)                                          AS total_observations,
    COUNT(o.observation_id) FILTER (WHERE o.rule_passed)            AS pass_count,
    COUNT(o.observation_id) FILTER (WHERE NOT o.rule_passed)        AS control_count,
    COUNT(r.observation_id)                                          AS outcomes_created,
    COUNT(r.observation_id) FILTER (WHERE r.mfe_60m IS NOT NULL)    AS mature_60m,
    COUNT(r.observation_id) FILTER (WHERE r.mfe_240m IS NOT NULL)   AS mature_240m,
    COUNT(r.observation_id) FILTER (WHERE r.is_final)               AS finalized,
    COUNT(DISTINCT o.symbol)                                         AS unique_symbols,
    MIN(o.signal_time)                                               AS oldest_signal,
    MAX(o.signal_time)                                               AS newest_signal
FROM research.prospective_observation o
JOIN research.prospective_experiment pe ON pe.experiment_id = o.experiment_id
LEFT JOIN research.prospective_outcome r ON r.observation_id = o.observation_id
GROUP BY o.experiment_id, pe.scanner_name, pe.direction, pe.experiment_type,
         pe.status, pe.started_at;

COMMENT ON VIEW research.v_prospective_accumulation
    IS 'Accumulation stats for prospective OOS experiments (migration 050)';

-- ── 6. GRANT permissions ─────────────────────────────────────

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA research TO trad_bot;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO trad_bot;
GRANT SELECT ON ALL VIEWS IN SCHEMA research TO trad_bot;

ALTER DEFAULT PRIVILEGES IN SCHEMA research
    GRANT SELECT, INSERT, UPDATE ON TABLES TO trad_bot;
ALTER DEFAULT PRIVILEGES IN SCHEMA research
    GRANT USAGE, SELECT ON SEQUENCES TO trad_bot;

-- ============================================================
-- NO production tables modified.
-- NO scanner parameters changed.
-- NO paper/live trading behavior changed.
-- ============================================================
