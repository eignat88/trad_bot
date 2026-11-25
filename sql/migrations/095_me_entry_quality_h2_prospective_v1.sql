-- Issue #312
-- H2_RSI_ONLY_PROSPECTIVE_RUNTIME_V1
-- Isolated research-only storage for the H2 prospective runtime.
-- Do NOT modify the #305 upstream-source schema, V3 / OOS-2, production
-- tables or activations — this migration touches ONLY its own research
-- schema.
--
-- Apply ONLY to the research database after explicit approval.  The runtime
-- itself is disabled by default; nothing here activates H2.

BEGIN;

CREATE SCHEMA IF NOT EXISTS research_me_entry_quality_h2_prospective;

-- ---------------------------------------------------------------------------
-- Ingest ledger: one row per #305 source_event_key seen by the runtime.
-- The first-seen snapshot is preserved VERBATIM (source_payload) and is
-- never overwritten; redeliveries are idempotent (redelivery_count only).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h2_prospective.ingest_event (
    source_event_key TEXT PRIMARY KEY,
    source_identity TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    signal_time TIMESTAMPTZ,
    signal_time_us BIGINT NOT NULL DEFAULT 0,
    captured_at TIMESTAMPTZ,
    activation_freeze_ts TIMESTAMPTZ,
    lineage_hash TEXT NOT NULL DEFAULT '',
    immutable_source_hash TEXT NOT NULL DEFAULT '',
    ingest_status TEXT NOT NULL,
    reject_reason TEXT NOT NULL DEFAULT '',
    reject_detail TEXT NOT NULL DEFAULT '',
    source_fingerprint TEXT NOT NULL DEFAULT '',
    signal_candle_open_time BIGINT,
    entry_timeframe TEXT,
    source_reference_price NUMERIC,
    source_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    redelivery_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT h2p_ingest_status_chk
    CHECK (
        ingest_status IN (
            'ACCEPTED',
            'REJECTED_PRE_FREEZE',
            'REJECTED_LINEAGE_INTEGRITY',
            'REJECTED_INVALID_SOURCE'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_h2p_ingest_signal
ON research_me_entry_quality_h2_prospective.ingest_event (signal_time_us, source_event_key);

-- Reader watermark: exclusive checkpoint (signal_time_us, source_event_key)
-- so restarts never skip unfinished events and never re-ingest old ones.
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h2_prospective.reader_watermark (
    id INTEGER PRIMARY KEY DEFAULT 1,
    signal_time_us BIGINT NOT NULL DEFAULT 0,
    source_event_key TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT h2p_watermark_singleton_chk CHECK (id = 1)
);

-- ---------------------------------------------------------------------------
-- Lifecycle state per (source_event_key, variant_id, policy_fingerprint).
-- States (FR-04): PENDING_CONFIRMATION / CONFIRMED_DELAYED / EXPIRED /
-- INCOMPLETE_COVERAGE / INVALID_SOURCE / NOT_READY.  One-way transitions;
-- terminal states are never re-decided (enforced by the store + this CHECK).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h2_prospective.variant_state (
    source_event_key TEXT NOT NULL,
    variant_id TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'PENDING_CONFIRMATION',
    symbol TEXT NOT NULL DEFAULT '',
    signal_time_us BIGINT NOT NULL DEFAULT 0,
    evaluation_attempts INTEGER NOT NULL DEFAULT 0,
    coverage_attempts INTEGER NOT NULL DEFAULT 0,
    last_evaluation_asof_ms BIGINT,
    last_reason TEXT NOT NULL DEFAULT '',
    last_detail TEXT NOT NULL DEFAULT '',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_evaluated_at TIMESTAMPTZ,
    terminal_at TIMESTAMPTZ,

    PRIMARY KEY (source_event_key, variant_id, policy_fingerprint),

    CONSTRAINT h2p_state_chk
    CHECK (
        state IN (
            'PENDING_CONFIRMATION',
            'CONFIRMED_DELAYED',
            'EXPIRED',
            'INCOMPLETE_COVERAGE',
            'INVALID_SOURCE',
            'NOT_READY'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_h2p_state_pending
ON research_me_entry_quality_h2_prospective.variant_state (state, signal_time_us);

-- ---------------------------------------------------------------------------
-- Immutable terminal outcome per (event, variant, policy) — FR-05.
-- RSI confirmation is NOT a trade: entry_candidate_price stays NULL and
-- entry_price_basis stays UNDETERMINED_NO_EXECUTION_MODEL.  No Net E[R] or
-- return metric exists anywhere in this schema (no approved execution model).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h2_prospective.variant_result (
    source_event_key TEXT NOT NULL,
    variant_id TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    expiry_reason TEXT,
    source_identity TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    lineage_hash TEXT NOT NULL DEFAULT '',
    signal_time_us BIGINT NOT NULL DEFAULT 0,
    activation_freeze_ts_ms BIGINT NOT NULL DEFAULT 0,
    policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    policy_fingerprint_check TEXT NOT NULL DEFAULT '',
    rsi_calc_version TEXT NOT NULL DEFAULT '',
    algorithm_version TEXT NOT NULL DEFAULT '',
    pipeline_version TEXT NOT NULL DEFAULT '',
    runtime_version TEXT NOT NULL DEFAULT '',
    confirmation_candle_open_ms BIGINT,
    confirmation_time_ms BIGINT,
    confirmation_time_us BIGINT,
    h2_rsi_at_confirmation NUMERIC,
    source_rsi_14_snapshot NUMERIC,
    source_rsi_delta_3_snapshot NUMERIC,
    source_entry_confirmation_rsi_rising BOOLEAN,
    entry_candidate_time_ms BIGINT,
    entry_candidate_time_us BIGINT,
    -- ALWAYS NULL: no execution model is approved (FR-06).
    entry_candidate_price NUMERIC,
    entry_price_basis TEXT NOT NULL DEFAULT '',
    entry_reference_close_price NUMERIC,
    entry_reference_label TEXT NOT NULL DEFAULT '',
    entry_delay_ms BIGINT,
    entry_delay_us BIGINT,
    observed_window_candles INTEGER NOT NULL DEFAULT 0,
    coverage_status TEXT NOT NULL DEFAULT '',
    coverage_detail TEXT NOT NULL DEFAULT '',
    original_price NUMERIC,
    original_price_exact TEXT,
    original_price_basis TEXT NOT NULL DEFAULT '',
    candle_source JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluation_asof_ms BIGINT NOT NULL DEFAULT 0,
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_payload_hash TEXT NOT NULL DEFAULT '',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (source_event_key, variant_id, policy_fingerprint),

    CONSTRAINT h2p_result_status_chk
    CHECK (
        status IN (
            'CONFIRMED_DELAYED',
            'EXPIRED',
            'INCOMPLETE_COVERAGE',
            'INVALID_SOURCE',
            'NOT_READY'
        )
    ),

    -- No fictional execution, ever.
    CONSTRAINT h2p_result_no_execution_price_chk
    CHECK (entry_candidate_price IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_h2p_result_status
ON research_me_entry_quality_h2_prospective.variant_result (status, recorded_at);

-- ---------------------------------------------------------------------------
-- Integrity-conflict ledger: immutable-data conflicts are recorded EXPLICITLY
-- (EVENT_KEY_LINEAGE_CONFLICT on redelivery drift; IMMUTABLE_RESULT_CONFLICT
-- on a differing re-write of a terminal outcome) — never swallowed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h2_prospective.result_conflict (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conflict_kind TEXT NOT NULL,
    source_event_key TEXT NOT NULL,
    variant_id TEXT NOT NULL DEFAULT '',
    policy_fingerprint TEXT NOT NULL DEFAULT '',
    stored_hash TEXT NOT NULL DEFAULT '',
    delivered_hash TEXT NOT NULL DEFAULT '',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT h2p_conflict_kind_chk
    CHECK (
        conflict_kind IN (
            'EVENT_KEY_LINEAGE_CONFLICT',
            'IMMUTABLE_RESULT_CONFLICT'
        )
    )
);

-- ---------------------------------------------------------------------------
-- Append-only evaluation journal: every evaluation attempt for every
-- (event, variant, policy) — restart recovery and coverage auditing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h2_prospective.evaluation_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_event_key TEXT NOT NULL,
    variant_id TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    evaluation_asof_ms BIGINT NOT NULL,
    pipeline_status TEXT NOT NULL DEFAULT '',
    state_before TEXT NOT NULL DEFAULT '',
    state_after TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    window_elapsed BOOLEAN NOT NULL DEFAULT FALSE,
    candle_source JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_h2p_evaluation_key
ON research_me_entry_quality_h2_prospective.evaluation_log
(source_event_key, variant_id, policy_fingerprint, evaluated_at);

-- ---------------------------------------------------------------------------
-- Runtime health / telemetry snapshots (FR-08).  SOURCE_EMPTY_WAITING is an
-- expected state here — never an RSI-algorithm error, never proof of source
-- coverage.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h2_prospective.runtime_health (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL,
    counters JSONB NOT NULL DEFAULT '{}'::jsonb,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT h2p_health_status_chk
    CHECK (
        status IN (
            'RUNNING',
            'SOURCE_EMPTY_WAITING',
            'BLOCKED_CANDLE_SOURCE',
            'DISABLED'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_h2p_health_recorded
ON research_me_entry_quality_h2_prospective.runtime_health (recorded_at, status);

-- ---------------------------------------------------------------------------
-- Immutability protection: terminal results, evaluation journal, conflict
-- ledger and the ingest snapshot are append-only / never rewritten.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION
research_me_entry_quality_h2_prospective.protect_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'H2P_ROW_IMMUTABLE';
END;
$$;

CREATE TRIGGER h2p_protect_variant_result
BEFORE UPDATE OR DELETE
ON research_me_entry_quality_h2_prospective.variant_result
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h2_prospective.protect_immutable();

CREATE TRIGGER h2p_protect_evaluation_log
BEFORE UPDATE OR DELETE
ON research_me_entry_quality_h2_prospective.evaluation_log
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h2_prospective.protect_immutable();

CREATE TRIGGER h2p_protect_result_conflict
BEFORE UPDATE OR DELETE
ON research_me_entry_quality_h2_prospective.result_conflict
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h2_prospective.protect_immutable();

-- The first-seen source snapshot may only accumulate redeliveries
-- (redelivery_count); its preserved payload/identity never changes.
CREATE OR REPLACE FUNCTION
research_me_entry_quality_h2_prospective.protect_ingest_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.source_payload IS DISTINCT FROM OLD.source_payload
       OR NEW.source_identity IS DISTINCT FROM OLD.source_identity
       OR NEW.lineage_hash IS DISTINCT FROM OLD.lineage_hash
       OR NEW.signal_time_us IS DISTINCT FROM OLD.signal_time_us
       OR NEW.immutable_source_hash IS DISTINCT FROM OLD.immutable_source_hash
       OR NEW.ingest_status IS DISTINCT FROM OLD.ingest_status
    THEN
        RAISE EXCEPTION 'H2P_INGEST_SNAPSHOT_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER h2p_protect_ingest_snapshot
BEFORE UPDATE ON research_me_entry_quality_h2_prospective.ingest_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h2_prospective.protect_ingest_snapshot();

CREATE TRIGGER h2p_protect_ingest_delete
BEFORE DELETE
ON research_me_entry_quality_h2_prospective.ingest_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality_h2_prospective.protect_immutable();

COMMIT;
