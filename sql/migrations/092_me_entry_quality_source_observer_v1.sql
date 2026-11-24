-- Issue #296
-- ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1
-- Isolated research-only storage.
-- Do NOT modify source V3 tables or activation.

BEGIN;

CREATE SCHEMA IF NOT EXISTS research_me_entry_quality;

CREATE TABLE IF NOT EXISTS research_me_entry_quality.activation (
    experiment_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'DISABLED',
    freeze_ts TIMESTAMPTZ,
    protocol_version TEXT NOT NULL DEFAULT '1.0.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT me_eq_activation_status_chk
    CHECK (
        status IN (
            'DISABLED',
            'READY',
            'RUNNING',
            'PAUSED',
            'BLOCKED'
        )
    ),

    CONSTRAINT me_eq_running_freeze_chk
    CHECK (status <> 'RUNNING' OR freeze_ts IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS research_me_entry_quality.source_event (
    event_key TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_time TIMESTAMPTZ NOT NULL,
    source_captured_at TIMESTAMPTZ NOT NULL,
    source_created_at TIMESTAMPTZ NOT NULL,
    activation_freeze_ts TIMESTAMPTZ NOT NULL,
    source_setup_id TEXT,
    source_scanner TEXT NOT NULL,
    source_scanner_version TEXT,
    source_direction TEXT NOT NULL,
    entry_timeframe TEXT,
    signal_candle_open_time BIGINT,
    source_reference_price NUMERIC,
    lineage JSONB NOT NULL,
    lineage_hash TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT me_eq_event_direction_chk
    CHECK (source_direction = 'LONG'),

    CONSTRAINT me_eq_event_freeze_chk
    CHECK (
        signal_time > activation_freeze_ts
        AND source_created_at >= activation_freeze_ts
        AND source_captured_at >= activation_freeze_ts
    ),

    CONSTRAINT me_eq_event_activation_fk
    FOREIGN KEY (experiment_id)
    REFERENCES research_me_entry_quality.activation(experiment_id)
);

CREATE INDEX IF NOT EXISTS idx_me_eq_event_time
ON research_me_entry_quality.source_event
    (signal_time, event_key);

CREATE INDEX IF NOT EXISTS idx_me_eq_event_observed
ON research_me_entry_quality.source_event
    (observed_at);

CREATE TABLE IF NOT EXISTS research_me_entry_quality.source_health (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL,
    source_rows_seen INTEGER NOT NULL DEFAULT 0,
    events_inserted INTEGER NOT NULL DEFAULT 0,
    source_lag_seconds NUMERIC,
    error_code TEXT,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb
);


-- Frozen activation protection.
-- The freeze boundary cannot be changed after first fixation.

CREATE OR REPLACE FUNCTION
research_me_entry_quality.protect_activation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.freeze_ts IS NOT NULL
       AND NEW.freeze_ts IS DISTINCT FROM OLD.freeze_ts
    THEN
        RAISE EXCEPTION
            'IMMUTABLE_FREEZE_TS';
    END IF;

    IF NEW.protocol_version IS DISTINCT FROM OLD.protocol_version
       AND OLD.freeze_ts IS NOT NULL
    THEN
        RAISE EXCEPTION
            'IMMUTABLE_PROTOCOL_VERSION';
    END IF;

    NEW.updated_at := NOW();

    RETURN NEW;
END;
$$;

CREATE TRIGGER me_eq_protect_activation
BEFORE UPDATE ON research_me_entry_quality.activation
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality.protect_activation();


-- Source events are append-only.
-- Existing rows cannot be modified or deleted.

CREATE OR REPLACE FUNCTION
research_me_entry_quality.protect_source_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'SOURCE_EVENT_IMMUTABLE';
END;
$$;

CREATE TRIGGER me_eq_protect_source_event
BEFORE UPDATE OR DELETE
ON research_me_entry_quality.source_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality.protect_source_event();



-- Durable processing ledger.
-- Rejected events are recorded but never eligible for OOS.

CREATE TABLE research_me_entry_quality.processed_event (
    event_key TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL
        REFERENCES research_me_entry_quality.activation(experiment_id),
    processing_status TEXT NOT NULL
        CHECK (processing_status IN ('ACCEPTED', 'REJECTED')),
    reason_code TEXT,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT me_eq_processed_reason_chk
    CHECK (
        (processing_status = 'ACCEPTED' AND reason_code IS NULL)
        OR
        (processing_status = 'REJECTED' AND reason_code IS NOT NULL)
    )
);

CREATE INDEX idx_me_eq_processed_experiment
ON research_me_entry_quality.processed_event
    (experiment_id, processed_at);


COMMIT;
