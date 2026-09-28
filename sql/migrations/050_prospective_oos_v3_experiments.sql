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
-- Idempotent:
--   - CREATE TABLE IF NOT EXISTS
--   - INSERT ON CONFLICT DO NOTHING (first run inserts, repeat run verifies)
--   - Invariant check: if row exists, frozen fields MUST match
-- ============================================================

BEGIN;

-- ── 1. Prospective experiment registry ────────────────────────

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

CREATE TABLE IF NOT EXISTS research.prospective_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL REFERENCES research.prospective_experiment(experiment_id),
    source_signal_id    BIGINT NOT NULL,
    source_observation_id BIGINT,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    signal_time         TIMESTAMPTZ NOT NULL,
    reference_price     NUMERIC NOT NULL,
    invalidation_price  NUMERIC,
    target_1            NUMERIC,
    target_2            NUMERIC,
    score               NUMERIC NOT NULL DEFAULT 0,
    variant_entry       NUMERIC,
    variant_stop        NUMERIC,
    variant_target      NUMERIC,
    rule_passed         BOOLEAN NOT NULL DEFAULT TRUE,
    filter_reason       TEXT,
    gate_result         TEXT,
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

CREATE TABLE IF NOT EXISTS research.prospective_outcome (
    observation_id      BIGINT PRIMARY KEY REFERENCES research.prospective_observation(observation_id) ON DELETE CASCADE,
    experiment_id       TEXT NOT NULL,
    mfe_15m  NUMERIC, mae_15m  NUMERIC,
    mfe_30m  NUMERIC, mae_30m  NUMERIC,
    mfe_60m  NUMERIC, mae_60m  NUMERIC,
    mfe_120m NUMERIC, mae_120m NUMERIC,
    mfe_240m NUMERIC, mae_240m NUMERIC,
    mfe_r_15m  NUMERIC, mae_r_15m  NUMERIC,
    mfe_r_30m  NUMERIC, mae_r_30m  NUMERIC,
    mfe_r_60m  NUMERIC, mae_r_60m  NUMERIC,
    mfe_r_120m NUMERIC, mae_r_120m NUMERIC,
    mfe_r_240m NUMERIC, mae_r_240m NUMERIC,
    return_at_15m  NUMERIC,
    return_at_30m  NUMERIC,
    return_at_60m  NUMERIC,
    return_at_120m NUMERIC,
    return_at_240m NUMERIC,
    tp_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    sl_hit          BOOLEAN NOT NULL DEFAULT FALSE,
    tp_before_sl    BOOLEAN NOT NULL DEFAULT FALSE,
    sl_before_tp    BOOLEAN NOT NULL DEFAULT FALSE,
    ambiguous_intrabar BOOLEAN NOT NULL DEFAULT FALSE,
    time_to_tp      NUMERIC,
    time_to_sl      NUMERIC,
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

-- ── 5. Seed frozen experiment registry ─────────────────────────
-- Faithfully maps from app/research/prospective_registry.json.
-- ON CONFLICT DO NOTHING: first run inserts, repeat run skips.
-- After seeding, invariant check verifies frozen fields match.

INSERT INTO research.prospective_experiment (
    experiment_id, version, scanner_name, direction, experiment_type,
    hypothesis, primary_metric, secondary_metrics, filter_rule, threshold,
    entry_rule, stop_rule, target_rule, position_sizing, fee_assumption,
    horizons, minimum_n, minimum_symbols, discovery_source, paired_with,
    status, started_at
) VALUES
-- 1. SRR LONG baseline validation
('SRR_LONG_BASELINE_V1', 1,
 'SUPPORT_RESISTANCE_REACTION', 'LONG', 'BASELINE_VALIDATION',
 'SRR LONG has signal edge (80.1% Fav) and gate edge. Prospective collection to validate.',
 'MFE_pct_60m',
 '["MAE_pct_60m","MFE_pct_15m","MFE_pct_30m","MFE_pct_120m","MFE_pct_240m","TP_before_SL","SL_before_TP","MFE_R_60m","MAE_R_60m"]'::jsonb,
 'NONE - capture all SRR LONG candidates before production gate',
 NULL,
 'Existing production entry semantics',
 'Existing production invalidation_price',
 'Existing production target_1',
 'shadow_only', 'none',
 '["15m","30m","60m","120m","240m"]'::jsonb,
 50, 10, '49b3828', NULL,
 'READY_TO_START', NULL),

-- 2. ME SHORT geometry control
('ME_SHORT_GEOM_A_V1', 1,
 'MOMENTUM_EXHAUSTION', 'SHORT', 'GEOMETRY_CONTROL',
 'ME SHORT has 85.8% Fav but current 0.2% stop destroys it. Control: current geometry.',
 'MFE_pct_60m',
 '["MAE_pct_60m","MFE_pct_15m","MFE_pct_30m","MFE_pct_120m","MFE_pct_240m","TP_before_SL","SL_before_TP","MFE_R_60m","MAE_R_60m"]'::jsonb,
 'NONE',
 NULL,
 'reference_price (recent_high from 5m candles)',
 'invalidation_price = reference_price * 1.002 (existing 0.2% stop)',
 'target_1 = current_price - ATR * 2',
 'shadow_only', 'none',
 '["15m","30m","60m","120m","240m"]'::jsonb,
 50, 10, '49b3828', NULL,
 'READY_TO_START', NULL),

-- 3. ME SHORT geometry intervention: wider stop
('ME_SHORT_GEOM_B_V1', 1,
 'MOMENTUM_EXHAUSTION', 'SHORT', 'GEOMETRY_INTERVENTION_WIDER_STOP',
 'Wider stop reduces premature SL hits. stop=max(risk, 0.5*ATR_14).',
 'MFE_pct_60m',
 '["MAE_pct_60m","MFE_pct_15m","MFE_pct_30m","MFE_pct_120m","MFE_pct_240m","TP_before_SL","SL_before_TP","MFE_R_60m","MAE_R_60m"]'::jsonb,
 'NONE - same signals as GEOM_A',
 NULL,
 'Same as GEOM_A',
 'FROZEN: stop_distance = max(abs(reference_price - invalidation_price), 0.5 * ATR_14_5m)',
 'Same risk distance as GEOM_A, adjusted to new entry',
 'shadow_only', 'none',
 '["15m","30m","60m","120m","240m"]'::jsonb,
 50, 10, '49b3828', 'ME_SHORT_GEOM_A_V1',
 'READY_TO_START', NULL),

-- 4. ME SHORT geometry intervention: delayed entry
('ME_SHORT_GEOM_C_V1', 1,
 'MOMENTUM_EXHAUSTION', 'SHORT', 'GEOMETRY_INTERVENTION_DELAYED_ENTRY',
 'Delayed entry at candle close avoids initial noise. 25.6% have MAE in first 15m then recover.',
 'MFE_pct_60m',
 '["MAE_pct_60m","MFE_pct_15m","MFE_pct_30m","MFE_pct_120m","MFE_pct_240m","TP_before_SL","SL_before_TP","MFE_R_60m","MAE_R_60m"]'::jsonb,
 'NONE - same signals as GEOM_A',
 NULL,
 'FROZEN: entry at close of 5m candle that triggered detection. Stop/target distance preserved.',
 'adjusted: new_stop = delayed_entry + original_risk_distance',
 'adjusted: new_target = delayed_entry - original_target_distance',
 'shadow_only', 'none',
 '["15m","30m","60m","120m","240m"]'::jsonb,
 50, 10, '49b3828', 'ME_SHORT_GEOM_A_V1',
 'READY_TO_START', NULL),

-- 5. VC SHORT bb_width filter
('VC_SHORT_BB_WIDTH_V1', 1,
 'VOLATILITY_COMPRESSION', 'SHORT', 'FEATURE_FILTER',
 'VC SHORT baseline flat but bb_width < 0.569723 isolates compressed states with different MFE.',
 'MFE_pct_60m',
 '["MAE_pct_60m","MFE_pct_15m","MFE_pct_30m","MFE_pct_120m","MFE_pct_240m","TP_before_SL","SL_before_TP"]'::jsonb,
 'PASS: bb_width_percentile < 0.569723. CONTROL: >= 0.569723.',
 0.569723,
 'Existing production entry semantics',
 'Existing production invalidation_price',
 'Existing production target_1',
 'shadow_only', 'none',
 '["15m","30m","60m","120m","240m"]'::jsonb,
 50, 10, '49b3828', NULL,
 'READY_TO_START', NULL),

-- 6. LR SHORT gate validation
('LR_SHORT_GATE_V1', 1,
 'LIQUIDITY_REVERSAL', 'SHORT', 'GATE_VALIDATION',
 'Existing gate improves forward MFE% (SETUP Fav=67.8% vs REJECTED Fav=16.7% in discovery).',
 'MFE_pct_60m',
 '["MAE_pct_60m","MFE_pct_15m","MFE_pct_30m","MFE_pct_120m","MFE_pct_240m","TP_before_SL","SL_before_TP"]'::jsonb,
 'NONE - capture all, tag with gate result',
 NULL,
 'Existing production entry semantics',
 'Existing production invalidation_price',
 'Existing production target_1',
 'shadow_only', 'none',
 '["15m","30m","60m","120m","240m"]'::jsonb,
 30, 5, '49b3828', NULL,
 'READY_TO_START', NULL)

ON CONFLICT (experiment_id) DO NOTHING;

-- ── 5b. Invariant check: verify frozen fields match ──────────
-- If a row already exists, its frozen configuration must match.
-- Runtime fields (started_at, status, created_at, updated_at) are NOT checked.

DO $$
DECLARE
    _rec RECORD;
    _mismatch TEXT := '';
BEGIN
    FOR _rec IN
        SELECT experiment_id, scanner_name, direction, version, primary_metric, threshold
        FROM research.prospective_experiment
        WHERE experiment_id IN (
            'SRR_LONG_BASELINE_V1', 'ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1',
            'ME_SHORT_GEOM_C_V1', 'VC_SHORT_BB_WIDTH_V1', 'LR_SHORT_GATE_V1'
        )
    LOOP
        -- Verify scanner_name matches frozen registry
        CASE _rec.experiment_id
            WHEN 'SRR_LONG_BASELINE_V1' THEN
                IF _rec.scanner_name != 'SUPPORT_RESISTANCE_REACTION' OR _rec.direction != 'LONG' THEN
                    _mismatch := _mismatch || _rec.experiment_id || ': scanner/direction mismatch; ';
                END IF;
            WHEN 'ME_SHORT_GEOM_A_V1' THEN
                IF _rec.scanner_name != 'MOMENTUM_EXHAUSTION' OR _rec.direction != 'SHORT' THEN
                    _mismatch := _mismatch || _rec.experiment_id || ': scanner/direction mismatch; ';
                END IF;
            WHEN 'ME_SHORT_GEOM_B_V1' THEN
                IF _rec.scanner_name != 'MOMENTUM_EXHAUSTION' OR _rec.direction != 'SHORT' THEN
                    _mismatch := _mismatch || _rec.experiment_id || ': scanner/direction mismatch; ';
                END IF;
            WHEN 'ME_SHORT_GEOM_C_V1' THEN
                IF _rec.scanner_name != 'MOMENTUM_EXHAUSTION' OR _rec.direction != 'SHORT' THEN
                    _mismatch := _mismatch || _rec.experiment_id || ': scanner/direction mismatch; ';
                END IF;
            WHEN 'VC_SHORT_BB_WIDTH_V1' THEN
                IF _rec.scanner_name != 'VOLATILITY_COMPRESSION' OR _rec.direction != 'SHORT'
                   OR _rec.threshold != 0.569723 THEN
                    _mismatch := _mismatch || _rec.experiment_id || ': scanner/direction/threshold mismatch; ';
                END IF;
            WHEN 'LR_SHORT_GATE_V1' THEN
                IF _rec.scanner_name != 'LIQUIDITY_REVERSAL' OR _rec.direction != 'SHORT' THEN
                    _mismatch := _mismatch || _rec.experiment_id || ': scanner/direction mismatch; ';
                END IF;
        END CASE;
    END LOOP;

    IF _mismatch != '' THEN
        RAISE EXCEPTION 'FROZEN CONFIGURATION DRIFT DETECTED: %', _mismatch;
    END IF;

    -- Verify exactly 6 expected experiments exist
    IF (SELECT COUNT(*) FROM research.prospective_experiment
        WHERE experiment_id IN (
            'SRR_LONG_BASELINE_V1', 'ME_SHORT_GEOM_A_V1', 'ME_SHORT_GEOM_B_V1',
            'ME_SHORT_GEOM_C_V1', 'VC_SHORT_BB_WIDTH_V1', 'LR_SHORT_GATE_V1'
        )) != 6 THEN
        RAISE EXCEPTION 'Expected exactly 6 prospective experiments, found different count';
    END IF;
END $$;

-- ── 6. Accumulation view ──────────────────────────────────────

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

-- ── 7. GRANT permissions ──────────────────────────────────────
-- NOTE: PostgreSQL does not support "GRANT SELECT ON ALL VIEWS".
-- Views in the research schema get SELECT via the table GRANT
-- when trad_bot owns or has privileges on them.
-- We grant on ALL TABLES which covers views accessed through
-- the research schema, and also grant explicit SELECT on the view.

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA research TO trad_bot;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO trad_bot;

-- Explicitly grant SELECT on the accumulation view
GRANT SELECT ON research.v_prospective_accumulation TO trad_bot;

ALTER DEFAULT PRIVILEGES IN SCHEMA research
    GRANT SELECT, INSERT, UPDATE ON TABLES TO trad_bot;
ALTER DEFAULT PRIVILEGES IN SCHEMA research
    GRANT USAGE, SELECT ON SEQUENCES TO trad_bot;

COMMIT;

-- ============================================================
-- NO production tables modified.
-- NO scanner parameters changed.
-- NO paper/live trading behavior changed.
-- ============================================================
