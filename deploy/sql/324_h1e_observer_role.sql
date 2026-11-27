-- Issue #324 / H1-E — least-privilege role for the isolated observer.
-- Run ONLY after the reviewed H1-E DDL proposal has been applied (owner GO).
-- Execute as a PostgreSQL administrator.  PROPOSAL ONLY — DO NOT APPLY on
-- any working database without an explicit owner GO.
--
-- The observer role is separated per contour:
--   * READ-ONLY on the #305 source table (never writes upstream state);
--   * write access ONLY inside research_me_entry_quality_h1e;
--   * no access to dds.*, market.*, scanner/paper/live or OOS tables.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'tradbot_h1e_observer'
    ) THEN
        CREATE ROLE tradbot_h1e_observer
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION;
    END IF;
END;
$$;

GRANT CONNECT ON DATABASE trad_bot
    TO tradbot_h1e_observer;

-- #305 upstream source: SELECT ONLY (never INSERT/UPDATE/DELETE).
GRANT USAGE ON SCHEMA research_me_upstream_coverage
    TO tradbot_h1e_observer;

GRANT SELECT
ON research_me_upstream_coverage.source_event
TO tradbot_h1e_observer;

-- Own isolated research schema (the ledger).
GRANT USAGE ON SCHEMA research_me_entry_quality_h1e
    TO tradbot_h1e_observer;

-- Activation is controlled by the administrative contour: SELECT ONLY.
GRANT SELECT
ON research_me_entry_quality_h1e.h1e_activation
TO tradbot_h1e_observer;

-- Ledger writes are restricted to the reviewed batch transaction.
GRANT SELECT, INSERT
ON research_me_entry_quality_h1e.h1e_event,
   research_me_entry_quality_h1e.h1e_classification,
   research_me_entry_quality_h1e.h1e_status_event,
   research_me_entry_quality_h1e.h1e_reject_event,
   research_me_entry_quality_h1e.h1e_conflict_event,
   research_me_entry_quality_h1e.h1e_provenance_snapshot,
   research_me_entry_quality_h1e.h1e_coverage_audit
TO tradbot_h1e_observer;

-- Mutable retry state + checkpoint (updated only inside the batch).
GRANT SELECT, INSERT, UPDATE, DELETE
ON research_me_entry_quality_h1e.h1e_pending_retry,
   research_me_entry_quality_h1e.h1e_checkpoint
TO tradbot_h1e_observer;

-- Sequences backing the identity columns.
GRANT USAGE ON ALL SEQUENCES IN SCHEMA research_me_entry_quality_h1e
    TO tradbot_h1e_observer;

COMMIT;
