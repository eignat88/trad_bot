-- Issue #324 / H1-E — PROSPECTIVE LEDGER DDL PROPOSAL V1
-- ============================================================================
-- *** PROPOSAL ONLY — DO NOT APPLY ***
-- RESEARCH_ONLY / NO_DDL_APPLY / NO_PRODUCTION_DB_WRITES
--
-- This file is a DESIGN ARTIFACT for review.  It is NEVER executed by any
-- code in this PR.  Applying it (Phase C, isolated research database, only
-- after a separate owner GO) would create a NEW, SEPARATE schema
-- `research_me_entry_quality_h1e` — it does NOT touch, alter or depend on:
--   * research_me_upstream_coverage.*  (the #305 source — READ-ONLY input)
--   * dds.* / market.* / paper / live production tables
--   * any existing OOS schema or activation row
--
-- SQL dialect: PostgreSQL (mirrors sql/migrations/094_me_upstream_source_
-- capture_v1.sql — TIMESTAMPTZ / BIGINT GENERATED ALWAYS AS IDENTITY /
-- JSONB / plpgsql immutability triggers), additive-only: CREATE SCHEMA /
-- CREATE TABLE / CREATE INDEX / CREATE FUNCTION / CREATE TRIGGER only.
--
-- Identity: one ledger row per (source_event_key, h1e_protocol_fingerprint).
-- Append-only everywhere except the single-row checkpoint; status is DERIVED
-- from the append-only journal — immutable evidence is never overwritten.
-- Retention: classification payloads + provenance snapshot hashes are kept
-- for the experiment lifetime (they ARE the evidence); reject/conflict
-- journals are kept forever (audit).  Deterministic recovery: restart from
-- the committed checkpoint and re-deliver (at-least-once); exactly-once
-- effect comes from identity idempotency (unique key + identical redelivery
-- is a no-op in the application transaction), never from rewriting rows.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research_me_entry_quality_h1e;

-- ---------------------------------------------------------------------------
-- H1-E activation (the SEPARATE, owner-approved H1-E freeze).
-- NEVER derived from, never equal to, the #305 freeze.  Phase A creates NO
-- row here: a freeze is minted only in Phase C with an explicit approval.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h1e.h1e_activation (
    experiment_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    freeze_ts TIMESTAMPTZ,
    protocol_version TEXT NOT NULL DEFAULT '1.0.0',
    protocol_fingerprint TEXT NOT NULL DEFAULT '',
    approval_ref TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT h1e_activation_status_chk
    CHECK (status IN ('DRAFT', 'APPROVED', 'RUNNING', 'PAUSED', 'BLOCKED')),

    CONSTRAINT h1e_activation_freeze_chk
    CHECK (status <> 'RUNNING' OR freeze_ts IS NOT NULL)
);

-- freeze_ts / protocol identity are immutable after fixation (mirrors #305)
CREATE OR REPLACE FUNCTION
research_me_entry_quality_h1e.protect_h1e_activation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.freeze_ts IS NOT NULL
       AND NEW.freeze_ts IS DISTINCT FROM OLD.freeze_ts
    THEN
        RAISE EXCEPTION 'H1E_IMMUTABLE_FREEZE_TS';
    END IF;
    IF NEW.protocol_fingerprint IS DISTINCT FROM OLD.protocol_fingerprint
       AND OLD.freeze_ts IS NOT NULL
    THEN
        RAISE EXCEPTION 'H1E_IMMUTABLE_PROTOCOL_FINGERPRINT';
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

CREATE TRIGGER h1e_protect_activation
BEFORE UPDATE ON research_me_entry_quality_h1e.h1e_activation
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h1e.protect_h1e_activation();

-- ---------------------------------------------------------------------------
-- Immutable first-capture event record.
-- Preserved #305 identity carried VERBATIM: source_event_key,
-- source_identity, signal_time (µs exact), lineage_hash, source_fingerprint.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h1e.h1e_event (
    ledger_identity TEXT PRIMARY KEY,
    source_event_key TEXT NOT NULL,
    h1e_protocol_fingerprint TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_time_us BIGINT NOT NULL,
    signal_time TIMESTAMPTZ NOT NULL,
    signal_time_ms BIGINT NOT NULL,
    captured_at_us BIGINT NOT NULL,
    source_freeze_ts_us BIGINT NOT NULL,
    lineage_hash TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL DEFAULT '',
    preserved_hash TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- exact microsecond floor policy: signal_time_ms == signal_time_us / 1000
    CONSTRAINT h1e_event_ms_floor_chk
    CHECK (signal_time_ms = signal_time_us / 1000),

    -- strict prospective boundary (the H1-E freeze — NOT the #305 freeze)
    CONSTRAINT h1e_event_prospective_chk
    CHECK (signal_time_us > source_freeze_ts_us)
);

-- Unique identity per (source_event_key, H1 protocol/fingerprint).
CREATE UNIQUE INDEX IF NOT EXISTS uq_h1e_event_identity
ON research_me_entry_quality_h1e.h1e_event (source_event_key, h1e_protocol_fingerprint);

CREATE INDEX IF NOT EXISTS idx_h1e_event_signal
ON research_me_entry_quality_h1e.h1e_event (signal_time_us, source_event_key);

CREATE INDEX IF NOT EXISTS idx_h1e_event_captured
ON research_me_entry_quality_h1e.h1e_event (captured_at_us);

-- append-only: no UPDATE, no DELETE (immutable evidence)
CREATE OR REPLACE FUNCTION
research_me_entry_quality_h1e.protect_h1e_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'H1E_LEDGER_IMMUTABLE';
END;
$$;

CREATE TRIGGER h1e_protect_event
BEFORE UPDATE OR DELETE
ON research_me_entry_quality_h1e.h1e_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h1e.protect_h1e_append_only();

-- ---------------------------------------------------------------------------
-- Immutable classification records (one row per classification attempt;
-- DEFERRED retries append NEW rows — prior rows are never rewritten).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h1e.h1e_classification (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ledger_identity TEXT NOT NULL
        REFERENCES research_me_entry_quality_h1e.h1e_event(ledger_identity),
    classification_version TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    asof_us BIGINT NOT NULL,
    eligible BOOLEAN NOT NULL DEFAULT FALSE,
    eligibility_reason TEXT NOT NULL DEFAULT '',
    non_eligible_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    regime_4h TEXT,
    bos_4h TEXT,
    regime_1d TEXT,
    bos_1d TEXT,
    combined_regime TEXT,
    coverage_4h_status TEXT NOT NULL DEFAULT '',
    coverage_1d_status TEXT NOT NULL DEFAULT '',
    coverage_4h_present INTEGER NOT NULL DEFAULT 0,
    coverage_1d_present INTEGER NOT NULL DEFAULT 0,
    coverage_4h_missing_open_ms JSONB NOT NULL DEFAULT '[]'::jsonb,
    coverage_1d_missing_open_ms JSONB NOT NULL DEFAULT '[]'::jsonb,
    snapshot_sha256 TEXT NOT NULL DEFAULT '',
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT h1e_classification_status_chk
    CHECK (status IN ('CLASSIFIED', 'DEFERRED')),

    -- ASOF = original signal_time (µs), never captured_at
    CONSTRAINT h1e_classification_asof_chk
    CHECK (asof_us > 0)
);

CREATE INDEX IF NOT EXISTS idx_h1e_classification_identity
ON research_me_entry_quality_h1e.h1e_classification (ledger_identity, id);

CREATE TRIGGER h1e_protect_classification
BEFORE UPDATE OR DELETE
ON research_me_entry_quality_h1e.h1e_classification
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h1e.protect_h1e_append_only();

-- ---------------------------------------------------------------------------
-- Append-only status journal — the ONLY place status transitions happen.
-- The current status of an identity is DERIVED from this journal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h1e.h1e_status_event (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ledger_identity TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT h1e_status_event_kind_chk
    CHECK (event_kind IN (
        'ACCEPTED', 'CLASSIFIED', 'DEFERRED', 'REJECTED', 'CONFLICT'
    ))
);

CREATE INDEX IF NOT EXISTS idx_h1e_status_event_identity
ON research_me_entry_quality_h1e.h1e_status_event (ledger_identity, id);

CREATE TRIGGER h1e_protect_status_event
BEFORE UPDATE OR DELETE
ON research_me_entry_quality_h1e.h1e_status_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h1e.protect_h1e_append_only();

-- ---------------------------------------------------------------------------
-- Durable rejection ledger (explicit reason codes — never silent drops).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h1e.h1e_reject_event (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_event_key TEXT,
    h1e_protocol_fingerprint TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    captured_at_us BIGINT NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_h1e_reject_event_recorded
ON research_me_entry_quality_h1e.h1e_reject_event (recorded_at, reason_code);

CREATE TRIGGER h1e_protect_reject_event
BEFORE UPDATE OR DELETE
ON research_me_entry_quality_h1e.h1e_reject_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h1e.protect_h1e_append_only();

-- ---------------------------------------------------------------------------
-- Conflicting-payload isolation (H1E_EVENT_KEY_LINEAGE_CONFLICT):
-- a redelivery with DIFFERENT preserved facts is recorded here and NEVER
-- overwrites the original h1e_event row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h1e.h1e_conflict_event (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_event_key TEXT NOT NULL,
    ledger_identity TEXT NOT NULL,
    existing_preserved_hash TEXT NOT NULL,
    delivered_preserved_hash TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER h1e_protect_conflict_event
BEFORE UPDATE OR DELETE
ON research_me_entry_quality_h1e.h1e_conflict_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h1e.protect_h1e_append_only();

-- ---------------------------------------------------------------------------
-- Provenance snapshot hashes (H1D_HTF_SNAPSHOT_2).  The full snapshot
-- document is retained verbatim in the classification diagnostics payload;
-- these columns are the indexed integrity anchors.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h1e.h1e_provenance_snapshot (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ledger_identity TEXT NOT NULL,
    snapshot_schema TEXT NOT NULL DEFAULT 'H1D_HTF_SNAPSHOT_2',
    snapshot_sha256 TEXT NOT NULL,
    payload_4h_sha256 TEXT NOT NULL,
    payload_1d_sha256 TEXT NOT NULL,
    fixture_label TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT h1e_snapshot_fixture_chk
    CHECK (fixture_label IN ('SYNTHETIC_TEST_FIXTURE', 'REAL_BYBIT_FETCH'))
);

CREATE TRIGGER h1e_protect_snapshot
BEFORE UPDATE OR DELETE
ON research_me_entry_quality_h1e.h1e_provenance_snapshot
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h1e.protect_h1e_append_only();

-- ---------------------------------------------------------------------------
-- Single-row delivery checkpoint.  Updated ONLY inside the same transaction
-- as the ledger batch inserts (atomic checkpoint + ledger semantics); a
-- monotonic trigger forbids cursor regression.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h1e.h1e_checkpoint (
    worker_id TEXT PRIMARY KEY,
    captured_us BIGINT NOT NULL DEFAULT 0,
    source_event_key TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION
research_me_entry_quality_h1e.protect_h1e_checkpoint()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.captured_us < OLD.captured_us
       OR (NEW.captured_us = OLD.captured_us
           AND NEW.source_event_key < OLD.source_event_key)
    THEN
        RAISE EXCEPTION 'H1E_CHECKPOINT_REGRESSION';
    END IF;
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

CREATE TRIGGER h1e_protect_checkpoint
BEFORE UPDATE ON research_me_entry_quality_h1e.h1e_checkpoint
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h1e.protect_h1e_checkpoint();

-- ---------------------------------------------------------------------------
-- Persisted reconciliation runs (H1E_COVERAGE_HEALTH_V1) — the five
-- dimensions are stored verbatim; no blanket PASS is ever derived here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h1e.h1e_coverage_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    as_of_us BIGINT NOT NULL,
    window_start_us BIGINT,
    window_end_us BIGINT,
    verdict TEXT NOT NULL,
    dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_h1e_coverage_audit_run
ON research_me_entry_quality_h1e.h1e_coverage_audit (run_at, verdict);

COMMIT;
