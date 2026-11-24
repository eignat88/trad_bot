-- Issue #305
-- Administrative MANUAL recovery of the upstream source capture contour.
--
-- Use ONLY after an INTEGRITY_CONFLICT / BLOCKED state has been analysed
-- and a recovery has been explicitly approved.  This script:
--
--   * appends a RESUMED row to the append-only state ledger;
--   * NEVER resets freeze_ts (immutable — enforced by trigger);
--   * NEVER backfills, modifies or deletes source_event rows;
--   * NEVER touches the V3 / OOS-2 ledgers or any trading table.
--
-- The BLOCKED -> RESUMED transition is the ONLY state recovery path; it is
-- never automatic.

BEGIN;

INSERT INTO research_me_upstream_coverage.capture_state_event (
    state,
    reason_code,
    details
)
VALUES (
    'RESUMED',
    'MANUAL_ADMIN_RECOVERY',
    jsonb_build_object(
        'by', current_user,
        'note', 'Issue #305 administrative recovery; freeze_ts untouched',
        'recovered_at', NOW()
    )
);

COMMIT;
