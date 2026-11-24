-- Issue #296
-- ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1
-- Review remediation (PR #303): append-only research-contour state ledger.
--
-- Records the fail-closed BLOCKED transitions of the NEW observer only.
-- Do NOT modify source V3 tables or activation. The freeze_ts stays
-- immutable (migration 092 trigger) and is never touched by this table.
--
-- The observer role gets SELECT + INSERT only. A RESUMED row is written
-- exclusively by the administrative recovery contour
-- (deploy/sql/296_observer_recovery.sql) executed by a PostgreSQL
-- administrator — never by the observer process itself.

BEGIN;

CREATE TABLE IF NOT EXISTS research_me_entry_quality.observer_state_event (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id TEXT NOT NULL
        REFERENCES research_me_entry_quality.activation(experiment_id),
    state TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT me_eq_observer_state_chk
    CHECK (state IN ('BLOCKED', 'RESUMED')),

    CONSTRAINT me_eq_observer_state_reason_chk
    CHECK (reason_code <> '')
);

CREATE INDEX IF NOT EXISTS idx_me_eq_observer_state_recorded
ON research_me_entry_quality.observer_state_event
    (experiment_id, recorded_at);


-- Append-only: state history can never be rewritten or deleted.

CREATE OR REPLACE FUNCTION
research_me_entry_quality.protect_observer_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'OBSERVER_STATE_IMMUTABLE';
END;
$$;

CREATE TRIGGER me_eq_protect_observer_state
BEFORE UPDATE OR DELETE
ON research_me_entry_quality.observer_state_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_entry_quality.protect_observer_state();

COMMIT;
