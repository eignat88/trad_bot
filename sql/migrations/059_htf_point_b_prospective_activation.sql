-- ============================================================
-- Migration 059: HTF Point-B Prospective Phase B Activation
-- ============================================================
-- One immutable activation boundary for Point-B and baseline.
--
-- freeze_ts / started_at = 2026-10-06T12:10:00Z
--
-- Runtime eligibility remains strictly:
--     signal_time > freeze_ts
--
-- No historical backfill.
-- Research / observe-only.
-- ============================================================

BEGIN;

DO $$
DECLARE
    bad_count INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO bad_count
    FROM research.prospective_experiment
    WHERE experiment_id IN (
        'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE',
        'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE'
    )
      AND (
          status IS DISTINCT FROM 'READY_TO_START'
          OR started_at IS NOT NULL
      );

    IF bad_count <> 0 THEN
        RAISE EXCEPTION
            'HTF Phase B activation precondition failed: % experiment(s) not READY_TO_START/NULL started_at',
            bad_count;
    END IF;

    SELECT 2 - COUNT(*)
    INTO bad_count
    FROM research.prospective_experiment
    WHERE experiment_id IN (
        'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE',
        'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE'
    );

    IF bad_count <> 0 THEN
        RAISE EXCEPTION
            'HTF Phase B activation precondition failed: missing % experiment(s)',
            bad_count;
    END IF;
END
$$;

UPDATE research.prospective_experiment
SET
    status = 'RUNNING',
    started_at = '2026-10-06T12:10:00Z'::timestamptz
WHERE experiment_id IN (
    'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE',
    'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE'
)
  AND status = 'READY_TO_START'
  AND started_at IS NULL;

COMMIT;
