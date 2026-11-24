-- Issue #296 — ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1
-- MANUAL ADMINISTRATIVE RECOVERY CONTOUR (PR #303 review remediation).
--
-- ============================================================================
-- EXECUTE ONLY AS A POSTGRESQL ADMINISTRATOR — NEVER AS tradbot_me_entry_quality.
-- The observer role has no UPDATE/DELETE rights and cannot resume itself.
-- ============================================================================
--
-- Purpose: resume a research contour that was placed into the fail-closed
-- BLOCKED state by the source observer after a deterministic source
-- integrity or validation violation.
--
-- Preconditions (ALL must hold; verify before running):
--   1. The blocking cause is investigated and remediated. Read the
--      diagnostics first — they are preserved across the block:
--
--        SELECT state, reason_code, details, recorded_at
--        FROM research_me_entry_quality.observer_state_event
--        ORDER BY id DESC LIMIT 5;
--
--        SELECT checked_at, status, source_rows_seen, events_inserted,
--               error_code, diagnostics
--        FROM research_me_entry_quality.source_health
--        ORDER BY id DESC LIMIT 10;
--
--        SELECT event_key, processing_status, reason_code, processed_at
--        FROM research_me_entry_quality.processed_event
--        WHERE processing_status = 'REJECTED'
--        ORDER BY processed_at DESC;
--
--   2. Any source-side discrepancy is resolved OUTSIDE this contour
--      (this script never modifies dds.* or any V3 data).
--   3. The operator decision is recorded in the change log.
--
-- Invariants — this script MUST NOT:
--   * change activation.freeze_ts (immutable since first fixation;
--     migration 092 trigger IMMUTABLE_FREEZE_TS enforces it);
--   * backfill historical events (the prospective boundary is kept;
--     pre-freeze rows are rejected again after a resume);
--   * modify, delete or rewrite source_event / processed_event rows
--     (both are append-only);
--   * touch the V3 source, the scanner or any trading contour.
--
-- After the resume: start the observer unit explicitly
--   sudo systemctl start trad-bot-me-entry-quality-observer.service
-- The unit never auto-restarts out of the BLOCKED state
-- (RestartPreventExitStatus=5).

BEGIN;

INSERT INTO research_me_entry_quality.observer_state_event (
    experiment_id,
    state,
    reason_code,
    details
)
VALUES (
    'ME_INVERSE_SHORT_V3_ENTRY_QUALITY_V1',
    'RESUMED',
    'MANUAL_RECOVERY',
    jsonb_build_object(
        'recovery_script', 'deploy/sql/296_observer_recovery.sql',
        'freeze_ts_reset', false,
        'backfill_performed', false,
        'note', 'Administrative recovery; freeze_ts and append-only ledgers untouched'
    )
);

COMMIT;

-- Verification after recovery:
--   SELECT state, reason_code, recorded_at
--   FROM research_me_entry_quality.observer_state_event
--   ORDER BY id DESC LIMIT 2;
-- Expected: latest row = RESUMED / MANUAL_RECOVERY.
