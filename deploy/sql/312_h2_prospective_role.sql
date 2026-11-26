-- Issue #312
-- Run only AFTER migration 095 has been applied to the research database.
-- Execute as a PostgreSQL administrator.  TEMPLATE ONLY — do NOT apply
-- without a separate explicit GO.
--
-- Role for the isolated H2 prospective research runtime
-- (H2_RSI_ONLY_PROSPECTIVE_RUNTIME_V1).
--
-- Privilege scope:
--   * SELECT-only on the #305 upstream source cohort
--     (research_me_upstream_coverage.source_event) — the runtime's source
--     reader is structurally SELECT-only and never writes #305;
--   * full DML ONLY inside research_me_entry_quality_h2_prospective;
--   * NO access to any trading table (dds.scanner_setup, dds.paper_trade,
--     dds.scanner_error, direction gates, expectancy ...);
--   * NO access to paper/live state of any kind.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'tradbot_h2_prospective'
    ) THEN
        CREATE ROLE tradbot_h2_prospective
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION;
    END IF;
END;
$$;

GRANT CONNECT ON DATABASE trad_bot
    TO tradbot_h2_prospective;

-- #305 upstream source cohort: SELECT only (read-only event source).
GRANT USAGE ON SCHEMA research_me_upstream_coverage
    TO tradbot_h2_prospective;

GRANT SELECT
ON research_me_upstream_coverage.source_event
TO tradbot_h2_prospective;

-- Own research schema: writes allowed (idempotent ingest, lifecycle state,
-- immutable terminal results, evaluation journal, telemetry).
GRANT USAGE ON SCHEMA research_me_entry_quality_h2_prospective
    TO tradbot_h2_prospective;

GRANT SELECT, INSERT, UPDATE
ON research_me_entry_quality_h2_prospective.ingest_event,
   research_me_entry_quality_h2_prospective.reader_watermark,
   research_me_entry_quality_h2_prospective.variant_state,
   research_me_entry_quality_h2_prospective.variant_result,
   research_me_entry_quality_h2_prospective.result_conflict,
   research_me_entry_quality_h2_prospective.evaluation_log,
   research_me_entry_quality_h2_prospective.runtime_health
TO tradbot_h2_prospective;

-- Immutability of terminal results / journal / conflicts is enforced by
-- database triggers (H2P_ROW_IMMUTABLE), not by privileges alone.

GRANT USAGE ON ALL SEQUENCES
IN SCHEMA research_me_entry_quality_h2_prospective
TO tradbot_h2_prospective;

COMMIT;
