-- Issue #296
-- Run only after migrations 092 and 093 have been applied.
-- Execute as a PostgreSQL administrator.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'tradbot_me_entry_quality'
    ) THEN
        CREATE ROLE tradbot_me_entry_quality
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION;
    END IF;
END;
$$;

GRANT CONNECT ON DATABASE trad_bot
    TO tradbot_me_entry_quality;

-- Existing V3 source: SELECT only.
GRANT USAGE ON SCHEMA dds
    TO tradbot_me_entry_quality;

GRANT SELECT
ON dds.me_long_v2_inverse_short_v3_event
TO tradbot_me_entry_quality;

-- Own research schema.
GRANT USAGE ON SCHEMA research_me_entry_quality
    TO tradbot_me_entry_quality;

-- Activation is controlled separately.
GRANT SELECT
ON research_me_entry_quality.activation
TO tradbot_me_entry_quality;

-- Research events and processing ledger.
GRANT SELECT, INSERT
ON research_me_entry_quality.source_event,
   research_me_entry_quality.processed_event
TO tradbot_me_entry_quality;

-- Fail-closed contour state ledger (append-only; BLOCKED rows only —
-- the RESUMED path belongs to the administrative recovery contour).
-- No UPDATE/DELETE anywhere in this schema for the observer role.
GRANT SELECT, INSERT
ON research_me_entry_quality.observer_state_event
TO tradbot_me_entry_quality;

-- Health telemetry.
GRANT SELECT, INSERT
ON research_me_entry_quality.source_health
TO tradbot_me_entry_quality;

GRANT USAGE ON ALL SEQUENCES
IN SCHEMA research_me_entry_quality
TO tradbot_me_entry_quality;

COMMIT;
