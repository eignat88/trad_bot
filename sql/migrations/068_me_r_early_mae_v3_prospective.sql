-- ============================================================
-- Migration 068: ME_R_LONG_EARLY_MAE_EXIT_PROSPECTIVE_V3
-- Research-only; DO NOT APPLY until separately approved.
-- ============================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS research_me_early_mae_v3;

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.protocol (
    protocol_version TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    scanner_name TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    source_candle_version TEXT NOT NULL,
    direction TEXT NOT NULL,
    frozen_at TIMESTAMPTZ NOT NULL,
    activation_at TIMESTAMPTZ NOT NULL,
    protocol_json JSONB NOT NULL,
    protocol_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (direction = 'LONG'),
    CHECK (scanner_name = 'ME_R_LONG_CLOSED_V1'),
    CHECK (experiment_id = 'ME_R_LONG_EARLY_MAE_EXIT_OOS_V3'),
    CHECK (frozen_at < activation_at)
);

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.variant (
    protocol_version TEXT NOT NULL REFERENCES research_me_early_mae_v3.protocol(protocol_version),
    variant_id TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes > 0),
    threshold_mae_r NUMERIC NOT NULL CHECK (threshold_mae_r > 0),
    role TEXT NOT NULL CHECK (role IN ('PRIMARY','SECONDARY')),
    is_primary BOOLEAN NOT NULL,
    PRIMARY KEY (protocol_version, variant_id),
    CHECK ((role = 'PRIMARY') = is_primary)
);

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.source_event (
    source_event_id BIGSERIAL PRIMARY KEY,
    protocol_version TEXT NOT NULL REFERENCES research_me_early_mae_v3.protocol(protocol_version),
    source_setup_id TEXT NOT NULL,
    instrument_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    scanner_name TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    source_candle_version TEXT NOT NULL,
    direction TEXT NOT NULL,
    source_candle_open_at TIMESTAMPTZ NOT NULL,
    source_candle_close_at TIMESTAMPTZ NOT NULL,
    source_fetch_started_at TIMESTAMPTZ NOT NULL,
    source_available_at TIMESTAMPTZ NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    entry_eligible_at TIMESTAMPTZ NOT NULL,
    entry_filled_at TIMESTAMPTZ,
    entry_price NUMERIC,
    stop_price NUMERIC,
    target_price NUMERIC,
    risk_distance NUMERIC,
    entry_zone_low NUMERIC,
    entry_zone_high NUMERIC,
    reference_price NUMERIC,
    baseline_deadline_at TIMESTAMPTZ,
    source_features JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_status TEXT NOT NULL DEFAULT 'DETECTED'
        CHECK (source_status IN ('DETECTED','ENTRY_PENDING','ENTRY_FILLED','ENTRY_NOT_FILLED','INVALID','FINALIZED')),
    coverage_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (coverage_status IN ('PENDING','COMPLETE','INCOMPLETE')),
    invalid_reason TEXT,
    protocol_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (protocol_version, source_setup_id),
    UNIQUE (protocol_version, instrument_id, scanner_name, direction, source_candle_open_at),
    CHECK (source_candle_close_at <= detected_at),
    CHECK (source_available_at <= detected_at),
    CHECK (source_available_at <= source_candle_close_at + INTERVAL '300 seconds'),
    CHECK (entry_eligible_at >= source_available_at),
    CHECK (entry_filled_at IS NULL OR entry_filled_at >= entry_eligible_at),
    CHECK (coverage_status <> 'INCOMPLETE' OR source_status <> 'FINALIZED')
);

CREATE INDEX IF NOT EXISTS idx_me_mae_v3_pending_sources
    ON research_me_early_mae_v3.source_event (protocol_version, source_status)
    WHERE source_status IN ('DETECTED','ENTRY_PENDING','ENTRY_FILLED');

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.variant_outcome (
    protocol_version TEXT NOT NULL,
    source_event_id BIGINT NOT NULL REFERENCES research_me_early_mae_v3.source_event(source_event_id) ON DELETE CASCADE,
    variant_id TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes > 0),
    threshold_mae_r NUMERIC NOT NULL CHECK (threshold_mae_r > 0),
    role TEXT NOT NULL CHECK (role IN ('PRIMARY','SECONDARY')),
    is_primary BOOLEAN NOT NULL,
    candle_source_version TEXT NOT NULL,
    variant_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (variant_status IN ('PENDING','EVALUATED','BASELINE_EXITED_EARLY','AMBIGUOUS','INCOMPLETE_COVERAGE','FINALIZED')),
    trigger_result TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (trigger_result IN ('PENDING','TRIGGERED','NO_TRIGGER','BASELINE_EXITED_EARLY','BASELINE_SL_TP_PRECEDENCE','INCOMPLETE_COVERAGE')),
    mae_r NUMERIC,
    mfe_r NUMERIC,
    window_low NUMERIC,
    window_high NUMERIC,
    horizon_end_at TIMESTAMPTZ,
    variant_exit_at TIMESTAMPTZ,
    variant_exit_price NUMERIC,
    variant_exit_reason TEXT,
    variant_gross_r NUMERIC,
    net_r_normal NUMERIC,
    net_r_elevated NUMERIC,
    delta_net_r_normal NUMERIC,
    delta_net_r_elevated NUMERIC,
    ambiguous_ordering BOOLEAN NOT NULL DEFAULT FALSE,
    coverage_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (coverage_status IN ('PENDING','COMPLETE','INCOMPLETE')),
    coverage_reason TEXT,
    execution_price_semantics TEXT,
    evaluated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (protocol_version, source_event_id, variant_id),
    CHECK ((role = 'PRIMARY') = is_primary),
    CHECK (
        variant_status <> 'FINALIZED'
        OR (variant_exit_at IS NOT NULL AND variant_exit_price IS NOT NULL
            AND net_r_normal IS NOT NULL AND net_r_elevated IS NOT NULL
            AND delta_net_r_normal IS NOT NULL AND delta_net_r_elevated IS NOT NULL
            AND coverage_status = 'COMPLETE')
    ),
    CHECK (
        variant_status <> 'INCOMPLETE_COVERAGE'
        OR (variant_exit_at IS NULL AND net_r_normal IS NULL
            AND delta_net_r_normal IS NULL AND delta_net_r_elevated IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_me_mae_v3_variant_primary
    ON research_me_early_mae_v3.variant_outcome (variant_id, delta_net_r_normal NULLS LAST);

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.pipeline_run (
    pipeline_run_id BIGSERIAL PRIMARY KEY,
    protocol_version TEXT NOT NULL REFERENCES research_me_early_mae_v3.protocol(protocol_version),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'RUNNING'
        CHECK (status IN ('RUNNING','COMPLETED','FAILED')),
    sources_seen INTEGER NOT NULL DEFAULT 0,
    sources_inserted INTEGER NOT NULL DEFAULT 0,
    fills_created INTEGER NOT NULL DEFAULT 0,
    variants_written INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DO $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_source_updated_at') THEN
  CREATE TRIGGER trg_me_mae_v3_source_updated_at
  BEFORE UPDATE ON research_me_early_mae_v3.source_event
  FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v3.fn_set_updated_at();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_variant_updated_at') THEN
  CREATE TRIGGER trg_me_mae_v3_variant_updated_at
  BEFORE UPDATE ON research_me_early_mae_v3.variant_outcome
  FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v3.fn_set_updated_at();
 END IF;
END $$;

COMMIT;
