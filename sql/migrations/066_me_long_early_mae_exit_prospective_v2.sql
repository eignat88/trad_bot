-- ============================================================
-- Migration 066: ME_R_LONG_EARLY_MAE_EXIT_PROSPECTIVE_SHADOW_PIPELINE_V2
-- ============================================================
-- STRICT RESEARCH-ONLY prospective infrastructure.
--
-- This migration creates an independent V2 research schema. It does NOT:
--   - modify the V1 scanner or dds.scanner_setup;
--   - create paper/live trades;
--   - link to dds.paper_trade by foreign key;
--   - modify dds.me_r_long_early_exit_observation.
--
-- Frozen protocol version: 2.1.0
-- Experiment ID: ME_R_LONG_EARLY_MAE_EXIT_OOS_V2
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research_me_early_mae_v2;

CREATE TABLE IF NOT EXISTS research_me_early_mae_v2.protocol (
    protocol_version       TEXT PRIMARY KEY,
    experiment_id          TEXT NOT NULL,
    scanner_name           TEXT NOT NULL,
    scanner_version        TEXT NOT NULL,
    direction              TEXT NOT NULL,
    frozen_at              TIMESTAMPTZ NOT NULL,
    activation_at          TIMESTAMPTZ NOT NULL,
    entry_rule             TEXT NOT NULL,
    entry_expiry_minutes   INTEGER NOT NULL CHECK (entry_expiry_minutes > 0),
    baseline_policy        TEXT NOT NULL,
    baseline_max_hold_minutes INTEGER NOT NULL CHECK (baseline_max_hold_minutes > 0),
    taker_fee_per_side     NUMERIC NOT NULL CHECK (taker_fee_per_side >= 0),
    slippage_per_side      NUMERIC NOT NULL CHECK (slippage_per_side >= 0),
    intrabar_policy        TEXT NOT NULL CHECK (intrabar_policy IN ('STOP_FIRST', 'TP_FIRST', 'ORDER_FIRST')),
    protocol_json          JSONB NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (direction = 'LONG'),
    CHECK (scanner_name = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V1'),
    CHECK (scanner_version = '1.0.0'),
    CHECK (experiment_id = 'ME_R_LONG_EARLY_MAE_EXIT_OOS_V2')
);

COMMENT ON TABLE research_me_early_mae_v2.protocol
    IS 'Frozen V2 prospective protocol; no production paper/live linkage.';

CREATE TABLE IF NOT EXISTS research_me_early_mae_v2.variant (
    protocol_version  TEXT NOT NULL REFERENCES research_me_early_mae_v2.protocol(protocol_version),
    variant_id        TEXT NOT NULL,
    horizon_minutes   INTEGER NOT NULL CHECK (horizon_minutes > 0),
    threshold_mae_r   NUMERIC NOT NULL CHECK (threshold_mae_r > 0),
    is_primary        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (protocol_version, variant_id)
);

CREATE TABLE IF NOT EXISTS research_me_early_mae_v2.source_event (
    source_event_id        BIGSERIAL PRIMARY KEY,
    protocol_version       TEXT NOT NULL REFERENCES research_me_early_mae_v2.protocol(protocol_version),
    source_setup_id        UUID NOT NULL,
    instrument_id          BIGINT NOT NULL,
    symbol                 TEXT NOT NULL,
    scanner_name           TEXT NOT NULL,
    scanner_version        TEXT NOT NULL,
    direction              TEXT NOT NULL,
    detected_at            TIMESTAMPTZ NOT NULL,
    source_available_at   TIMESTAMPTZ,
    signal_candle_open_at  TIMESTAMPTZ NOT NULL,
    entry_zone_low         NUMERIC NOT NULL,
    entry_zone_high        NUMERIC NOT NULL,
    reference_price        NUMERIC NOT NULL,
    invalidation_price     NUMERIC NOT NULL,
    target_price           NUMERIC NOT NULL,
    source_features        JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_status          TEXT NOT NULL DEFAULT 'DETECTED'
        CHECK (source_status IN (
            'DETECTED', 'ENTRY_PENDING', 'ENTRY_FILLED',
            'ENTRY_NOT_FILLED', 'INVALID', 'FINALIZED'
        )),
    invalid_reason         TEXT,
    eligibility            TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (eligibility IN ('PENDING', 'ELIGIBLE', 'PRE_FREEZE', 'INVALID')),
    entry_pending_at       TIMESTAMPTZ,
    entry_filled_at        TIMESTAMPTZ,
    entry_price            NUMERIC,
    virtual_stop_price     NUMERIC,
    virtual_target_price   NUMERIC,
    risk_distance          NUMERIC,
    baseline_exit_at       TIMESTAMPTZ,
    baseline_exit_price    NUMERIC,
    baseline_exit_reason   TEXT,
    baseline_gross_r       NUMERIC,
    baseline_net_r         NUMERIC,
    source_coverage        TEXT NOT NULL DEFAULT 'PENDING',
    protocol_fingerprint   TEXT NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (protocol_version, source_setup_id),
    UNIQUE (protocol_version, instrument_id, scanner_name, direction, signal_candle_open_at),
    CHECK (
        (source_status = 'ENTRY_FILLED' AND entry_price IS NOT NULL
         AND virtual_stop_price IS NOT NULL AND virtual_target_price IS NOT NULL
         AND risk_distance IS NOT NULL AND risk_distance > 0)
        OR source_status <> 'ENTRY_FILLED'
    ),
    CHECK (
        (source_status = 'INVALID' AND invalid_reason IS NOT NULL)
        OR source_status <> 'INVALID'
    )
);

CREATE INDEX IF NOT EXISTS idx_me_mae_v2_source_pending
    ON research_me_early_mae_v2.source_event (protocol_version, source_status)
    WHERE source_status IN ('DETECTED', 'ENTRY_PENDING', 'ENTRY_FILLED');

CREATE INDEX IF NOT EXISTS idx_me_mae_v2_source_symbol_time
    ON research_me_early_mae_v2.source_event (symbol, detected_at DESC);

CREATE TABLE IF NOT EXISTS research_me_early_mae_v2.variant_outcome (
    protocol_version   TEXT NOT NULL,
    source_event_id    BIGINT NOT NULL REFERENCES research_me_early_mae_v2.source_event(source_event_id) ON DELETE CASCADE,
    variant_id         TEXT NOT NULL,
    variant_status     TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (variant_status IN (
            'PENDING', 'EVALUATED_TRIGGERED', 'EVALUATED_NO_TRIGGER',
            'BASELINE_EXITED_EARLY', 'INCOMPLETE_COVERAGE', 'AMBIGUOUS', 'FINALIZED'
        )),
    horizon_minutes    INTEGER NOT NULL CHECK (horizon_minutes > 0),
    threshold_mae_r    NUMERIC NOT NULL CHECK (threshold_mae_r > 0),
    mae_r              NUMERIC,
    mfe_r              NUMERIC,
    window_low         NUMERIC,
    window_high        NUMERIC,
    trigger_result     TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (trigger_result IN ('PENDING', 'TRIGGERED', 'NO_TRIGGER', 'BASELINE_EXITED_EARLY', 'INCOMPLETE_COVERAGE', 'AMBIGUOUS')),
    variant_exit_at    TIMESTAMPTZ,
    variant_exit_price NUMERIC,
    variant_exit_reason TEXT,
    variant_gross_r    NUMERIC,
    variant_net_r      NUMERIC,
    delta_net_r        NUMERIC,
    ambiguous_ordering BOOLEAN NOT NULL DEFAULT FALSE,
    coverage_reason    TEXT,
    candle_source_version TEXT NOT NULL DEFAULT 'MARKET_CANDLE_CLOSED_5M_V1',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (protocol_version, source_event_id, variant_id),
    UNIQUE (source_event_id, variant_id, protocol_version)
);

CREATE INDEX IF NOT EXISTS idx_me_mae_v2_variant_pending
    ON research_me_early_mae_v2.variant_outcome (protocol_version, variant_status)
    WHERE variant_status NOT IN ('FINALIZED', 'AMBIGUOUS');

CREATE INDEX IF NOT EXISTS idx_me_mae_v2_variant_delta
    ON research_me_early_mae_v2.variant_outcome (variant_id, delta_net_r DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS research_me_early_mae_v2.pipeline_run (
    run_id             BIGSERIAL PRIMARY KEY,
    protocol_version   TEXT NOT NULL REFERENCES research_me_early_mae_v2.protocol(protocol_version),
    run_started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_finished_at    TIMESTAMPTZ,
    sources_processed  INTEGER NOT NULL DEFAULT 0,
    fills_created      INTEGER NOT NULL DEFAULT 0,
    variants_evaluated INTEGER NOT NULL DEFAULT 0,
    errors             INTEGER NOT NULL DEFAULT 0,
    run_status         TEXT NOT NULL DEFAULT 'RUNNING'
        CHECK (run_status IN ('RUNNING', 'COMPLETED', 'FAILED'))
);

CREATE OR REPLACE FUNCTION research_me_early_mae_v2.fn_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_me_mae_v2_source_updated_at'
    ) THEN
        CREATE TRIGGER trg_me_mae_v2_source_updated_at
        BEFORE UPDATE ON research_me_early_mae_v2.source_event
        FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v2.fn_set_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_me_mae_v2_variant_updated_at'
    ) THEN
        CREATE TRIGGER trg_me_mae_v2_variant_updated_at
        BEFORE UPDATE ON research_me_early_mae_v2.variant_outcome
        FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v2.fn_set_updated_at();
    END IF;
END $$;

CREATE OR REPLACE VIEW research_me_early_mae_v2.v_protocol AS
SELECT
    p.protocol_version,
    p.experiment_id,
    p.scanner_name,
    p.scanner_version,
    p.direction,
    p.frozen_at,
    p.activation_at,
    p.entry_expiry_minutes,
    p.baseline_policy,
    p.baseline_max_hold_minutes,
    p.taker_fee_per_side,
    p.slippage_per_side,
    p.protocol_json,
    jsonb_agg(
        jsonb_build_object(
            'variant_id', v.variant_id,
            'horizon_minutes', v.horizon_minutes,
            'threshold_mae_r', v.threshold_mae_r,
            'is_primary', v.is_primary
        ) ORDER BY v.variant_id
    ) AS variants
FROM research_me_early_mae_v2.protocol p
LEFT JOIN research_me_early_mae_v2.variant v
    ON v.protocol_version = p.protocol_version
GROUP BY p.protocol_version;

CREATE OR REPLACE VIEW research_me_early_mae_v2.v_source_summary AS
SELECT
    s.protocol_version,
    s.source_status,
    s.eligibility,
    count(*) AS source_events,
    count(*) FILTER (WHERE s.baseline_exit_at IS NOT NULL) AS baseline_finalized
FROM research_me_early_mae_v2.source_event s
GROUP BY s.protocol_version, s.source_status, s.eligibility;

CREATE OR REPLACE VIEW research_me_early_mae_v2.v_variant_summary AS
SELECT
    o.protocol_version,
    o.variant_id,
    o.variant_status,
    count(*) AS observations,
    count(*) FILTER (WHERE o.trigger_result = 'TRIGGERED') AS triggered,
    round(avg(o.delta_net_r) FILTER (WHERE o.delta_net_r IS NOT NULL), 8) AS mean_delta_net_r,
    round(sum(o.delta_net_r) FILTER (WHERE o.delta_net_r IS NOT NULL), 8) AS total_delta_net_r
FROM research_me_early_mae_v2.variant_outcome o
GROUP BY o.protocol_version, o.variant_id, o.variant_status;

COMMENT ON VIEW research_me_early_mae_v2.v_variant_summary
    IS 'Read-only summary of V2 virtual baseline and early-exit variants.';

COMMIT;
