-- Issue #305
-- ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1
-- Isolated research-only storage for the independent upstream source
-- capture.  Do NOT modify V3 / OOS-2 / production tables or activations.
--
-- Apply ONLY to the research database after explicit approval; nothing in
-- this migration touches dds.* or any trading table.

BEGIN;

CREATE SCHEMA IF NOT EXISTS research_me_upstream_coverage;

-- ---------------------------------------------------------------------------
-- Administrative activation (immutable freeze boundary).
-- Written ONLY by scripts/me_upstream_source_v1_admin.py (administrative
-- contour) — the capture role gets SELECT only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_upstream_coverage.activation (
    experiment_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'DISABLED',
    freeze_ts TIMESTAMPTZ,
    protocol_version TEXT NOT NULL DEFAULT '1.0.0',
    protocol_fingerprint TEXT NOT NULL DEFAULT '',
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT me_ups_activation_status_chk
    CHECK (
        status IN (
            'DISABLED',
            'READY',
            'RUNNING',
            'PAUSED',
            'BLOCKED'
        )
    ),

    CONSTRAINT me_ups_running_freeze_chk
    CHECK (status <> 'RUNNING' OR freeze_ts IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- Eligible upstream source cohort.  Append-only, idempotent on
-- source_event_key (the deterministic upstream identity key).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_upstream_coverage.source_event (
    source_event_key TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL
        REFERENCES research_me_upstream_coverage.activation(experiment_id),
    source_identity TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_time TIMESTAMPTZ NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activation_freeze_ts TIMESTAMPTZ NOT NULL,
    source_setup_id TEXT,
    source_scanner TEXT NOT NULL,
    source_scanner_version TEXT,
    source_direction TEXT NOT NULL,
    entry_timeframe TEXT,
    signal_candle_open_time BIGINT,
    source_reference_price NUMERIC,
    source_invalidation_price NUMERIC,
    source_target_price NUMERIC,
    source_target_2 NUMERIC,
    entry_zone_low NUMERIC,
    entry_zone_high NUMERIC,
    source_score NUMERIC,
    market_regime TEXT,
    source_fingerprint TEXT NOT NULL DEFAULT '',
    capture_route TEXT NOT NULL,
    features JSONB NOT NULL DEFAULT '{}'::jsonb,
    lineage JSONB NOT NULL,
    lineage_hash TEXT NOT NULL,
    comparable_lineage_hash TEXT NOT NULL DEFAULT '',

    CONSTRAINT me_ups_event_scanner_chk
    CHECK (source_scanner = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'),

    CONSTRAINT me_ups_event_direction_chk
    CHECK (source_direction = 'LONG'),

    -- Strict prospective boundary: historical backfill is impossible by
    -- construction (mirrors the capture-side is_post_freeze guard).
    CONSTRAINT me_ups_event_freeze_chk
    CHECK (
        signal_time > activation_freeze_ts
        AND captured_at >= activation_freeze_ts
    )
);

-- One row per normalized original signal identity: a re-detection of the
-- same signal can never create a second row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_me_ups_event_identity
ON research_me_upstream_coverage.source_event (source_identity);

CREATE INDEX IF NOT EXISTS idx_me_ups_event_time
ON research_me_upstream_coverage.source_event (signal_time, source_event_key);

CREATE INDEX IF NOT EXISTS idx_me_ups_event_captured
ON research_me_upstream_coverage.source_event (captured_at);

-- ---------------------------------------------------------------------------
-- Durable rejection ledger.  An event that cannot enter the eligible cohort
-- (incomplete lineage / invalid features / pre-freeze) is recorded here with
-- a deterministic reason_code — never silently dropped.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_upstream_coverage.rejected_event (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_event_key TEXT,
    source_identity TEXT,
    reason_code TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_me_ups_rejected_recorded
ON research_me_upstream_coverage.rejected_event (recorded_at, reason_code);

-- ---------------------------------------------------------------------------
-- Capture health: delivery statistics, errors and delivery latency.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_upstream_coverage.capture_health (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL,
    events_seen INTEGER NOT NULL DEFAULT 1,
    events_inserted INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    delivery_latency_seconds NUMERIC,
    error_code TEXT,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_me_ups_health_recorded
ON research_me_upstream_coverage.capture_health (recorded_at, status);

-- ---------------------------------------------------------------------------
-- Read-only reconciliation results (explicitly persisted audit runs).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_upstream_coverage.coverage_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    as_of TIMESTAMPTZ NOT NULL,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    upstream_events INTEGER NOT NULL DEFAULT 0,
    v3_events INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    upstream_only_lag INTEGER NOT NULL DEFAULT 0,
    upstream_only_gap INTEGER NOT NULL DEFAULT 0,
    v3_only_lag INTEGER NOT NULL DEFAULT 0,
    v3_only_gap INTEGER NOT NULL DEFAULT 0,
    duplicate_deliveries INTEGER NOT NULL DEFAULT 0,
    v3_duplicate_identities INTEGER NOT NULL DEFAULT 0,
    lineage_mismatches INTEGER NOT NULL DEFAULT 0,
    capture_errors INTEGER NOT NULL DEFAULT 0,
    rejected_events INTEGER NOT NULL DEFAULT 0,
    max_delivery_latency_seconds NUMERIC,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_me_ups_coverage_audit_run
ON research_me_upstream_coverage.coverage_audit (run_at, status);

-- ---------------------------------------------------------------------------
-- Append-only capture state ledger (HEALTHY / BLOCKED / RESUMED).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_upstream_coverage.capture_state_event (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    state TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT me_ups_state_chk
    CHECK (state IN ('HEALTHY', 'BLOCKED', 'RESUMED'))
);

CREATE INDEX IF NOT EXISTS idx_me_ups_state_recorded
ON research_me_upstream_coverage.capture_state_event (recorded_at, state);

-- ---------------------------------------------------------------------------
-- Immutable activation protection: freeze_ts and protocol identity can
-- never be rewritten after fixation (the freeze is never auto-reset).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION
research_me_upstream_coverage.protect_activation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.freeze_ts IS NOT NULL
       AND NEW.freeze_ts IS DISTINCT FROM OLD.freeze_ts
    THEN
        RAISE EXCEPTION 'IMMUTABLE_FREEZE_TS';
    END IF;

    IF NEW.protocol_version IS DISTINCT FROM OLD.protocol_version
       AND OLD.freeze_ts IS NOT NULL
    THEN
        RAISE EXCEPTION 'IMMUTABLE_PROTOCOL_VERSION';
    END IF;

    IF NEW.protocol_fingerprint IS DISTINCT FROM OLD.protocol_fingerprint
       AND OLD.freeze_ts IS NOT NULL
       AND OLD.status = 'RUNNING'
    THEN
        RAISE EXCEPTION 'IMMUTABLE_PROTOCOL_FINGERPRINT';
    END IF;

    NEW.updated_at := NOW();

    RETURN NEW;
END;
$$;

CREATE TRIGGER me_ups_protect_activation
BEFORE UPDATE ON research_me_upstream_coverage.activation
FOR EACH ROW
EXECUTE FUNCTION
research_me_upstream_coverage.protect_activation();

-- Source events are append-only: no UPDATE, no DELETE.
CREATE OR REPLACE FUNCTION
research_me_upstream_coverage.protect_source_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'SOURCE_EVENT_IMMUTABLE';
END;
$$;

CREATE TRIGGER me_ups_protect_source_event
BEFORE UPDATE OR DELETE
ON research_me_upstream_coverage.source_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_upstream_coverage.protect_source_event();

-- Capture state ledger is append-only.
CREATE OR REPLACE FUNCTION
research_me_upstream_coverage.protect_state_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'STATE_EVENT_IMMUTABLE';
END;
$$;

CREATE TRIGGER me_ups_protect_state_event
BEFORE UPDATE OR DELETE
ON research_me_upstream_coverage.capture_state_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_upstream_coverage.protect_state_event();

COMMIT;
