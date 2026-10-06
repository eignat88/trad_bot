-- ============================================================
-- Migration 059: HTF Point-B Prospective Phase B Activation
-- ============================================================
-- One immutable activation boundary for Point-B and baseline.
--
-- freeze_ts / started_at = 2026-10-06T12:54:00Z
--
-- Runtime eligibility remains strictly:
--     signal_time > freeze_ts
--
-- Registry activation alone is not sufficient: observer capture
-- additionally requires DB status RUNNING and DB started_at equal
-- to this exact frozen timestamp.
--
-- Idempotent:
--   READY_TO_START + NULL -> activate
--   RUNNING + exact timestamp -> no-op / accepted
--   any conflicting state -> abort
--
-- No historical backfill.
-- Research / observe-only.
-- ============================================================

BEGIN;

DO $$
DECLARE
    experiment_count INTEGER;
    bad_count INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO experiment_count
    FROM research.prospective_experiment
    WHERE experiment_id IN (
        'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE',
        'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE'
    );

    IF experiment_count <> 2 THEN
        RAISE EXCEPTION
            'HTF Phase B activation precondition failed: expected 2 experiments, found %',
            experiment_count;
    END IF;

    SELECT COUNT(*)
    INTO bad_count
    FROM research.prospective_experiment
    WHERE experiment_id IN (
        'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE',
        'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE'
    )
      AND NOT (
          (status = 'READY_TO_START' AND started_at IS NULL)
          OR
          (
              status = 'RUNNING'
              AND started_at = '2026-10-06T12:54:00Z'::timestamptz
          )
      );

    IF bad_count <> 0 THEN
        RAISE EXCEPTION
            'HTF Phase B activation conflict: % experiment(s) have incompatible lifecycle state/boundary',
            bad_count;
    END IF;
END
$$;

UPDATE research.prospective_experiment
SET
    status = 'RUNNING',
    started_at = '2026-10-06T12:54:00Z'::timestamptz
WHERE experiment_id IN (
    'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE',
    'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE'
)
  AND status = 'READY_TO_START'
  AND started_at IS NULL;

DO $$
DECLARE
    activated_count INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO activated_count
    FROM research.prospective_experiment
    WHERE experiment_id IN (
        'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE',
        'HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE'
    )
      AND status = 'RUNNING'
      AND started_at = '2026-10-06T12:54:00Z'::timestamptz;

    IF activated_count <> 2 THEN
        RAISE EXCEPTION
            'HTF Phase B activation verification failed: expected 2 activated experiments, found %',
            activated_count;
    END IF;
END
$$;

COMMIT;
