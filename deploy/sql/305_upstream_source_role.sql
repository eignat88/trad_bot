-- Issue #305
-- Run only AFTER migration 094 has been applied to the research database.
-- Execute as a PostgreSQL administrator.
--
-- Role for the isolated upstream source capture
-- (ME_INVERSE_SHORT_V3_UPSTREAM_SOURCE_CAPTURE_V1).
--
-- Privilege scope:
--   * SELECT-only on the V3 source ledger (read-only reconciliation input);
--   * SELECT/INSERT only inside research_me_upstream_coverage;
--   * SELECT only on the activation row (activation is administrative);
--   * NO UPDATE/DELETE anywhere in the research schema (append-only);
--   * NO access to any trading table (dds.scanner_setup, dds.paper_trade,
--     dds.scanner_error, direction gates, expectancy ...).

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'tradbot_me_upstream_source'
    ) THEN
        CREATE ROLE tradbot_me_upstream_source
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION;
    END IF;
END;
$$;

GRANT CONNECT ON DATABASE trad_bot
    TO tradbot_me_upstream_source;

-- V3 producer ledger: SELECT only (reconciliation input, never a target).
GRANT USAGE ON SCHEMA dds
    TO tradbot_me_upstream_source;

GRANT SELECT
ON dds.me_long_v2_inverse_short_v3_event
TO tradbot_me_upstream_source;

-- Own research schema.
GRANT USAGE ON SCHEMA research_me_upstream_coverage
    TO tradbot_me_upstream_source;

-- Activation is controlled administratively (scripts/..._admin.py).
GRANT SELECT
ON research_me_upstream_coverage.activation
TO tradbot_me_upstream_source;

-- Event cohort, rejection ledger, health, audit results, state ledger:
-- append-only writes (SELECT, INSERT).  No UPDATE/DELETE by construction.
GRANT SELECT, INSERT
ON research_me_upstream_coverage.source_event,
   research_me_upstream_coverage.rejected_event,
   research_me_upstream_coverage.capture_health,
   research_me_upstream_coverage.coverage_audit,
   research_me_upstream_coverage.capture_state_event
TO tradbot_me_upstream_source;

GRANT USAGE ON ALL SEQUENCES
IN SCHEMA research_me_upstream_coverage
TO tradbot_me_upstream_source;

COMMIT;
