-- ============================================================
-- Migration 072: ME SHORT Geometry A Execution V1 Activation
-- ============================================================
-- Immutable prospective boundary:
--   freeze_ts / started_at = 2026-10-19T08:26:00Z
--
-- Eligible iff signal_time > freeze_ts.
-- Observer additionally requires DB status RUNNING and started_at equal
-- to this exact registry boundary. No historical backfill.
-- ============================================================

BEGIN;

DO $$
DECLARE
    row_count INTEGER;
    obs_count INTEGER;
    bad_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO row_count
    FROM research.prospective_experiment
    WHERE experiment_id = 'ME_SHORT_GEOM_A_EXECUTION_V1';

    IF row_count <> 1 THEN
        RAISE EXCEPTION
            'ME A execution activation precondition failed: expected 1 experiment row, found %',
            row_count;
    END IF;

    SELECT COUNT(*) INTO obs_count
    FROM research.prospective_observation
    WHERE experiment_id = 'ME_SHORT_GEOM_A_EXECUTION_V1';

    IF obs_count <> 0 THEN
        RAISE EXCEPTION
            'ME A execution activation blocked: pre-activation observations exist: %',
            obs_count;
    END IF;

    SELECT COUNT(*) INTO bad_count
    FROM research.prospective_experiment
    WHERE experiment_id = 'ME_SHORT_GEOM_A_EXECUTION_V1'
      AND NOT (
          (status = 'READY_TO_START' AND started_at IS NULL)
          OR
          (
              status = 'RUNNING'
              AND started_at = '2026-10-19T08:26:00Z'::timestamptz
          )
      );

    IF bad_count <> 0 THEN
        RAISE EXCEPTION
            'ME A execution activation conflict: incompatible lifecycle state/boundary';
    END IF;
END
$$;

UPDATE research.prospective_experiment
SET
    status = 'RUNNING',
    started_at = '2026-10-19T08:26:00Z'::timestamptz
WHERE experiment_id = 'ME_SHORT_GEOM_A_EXECUTION_V1'
  AND status = 'READY_TO_START'
  AND started_at IS NULL;

DO $$
DECLARE
    activated_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO activated_count
    FROM research.prospective_experiment
    WHERE experiment_id = 'ME_SHORT_GEOM_A_EXECUTION_V1'
      AND status = 'RUNNING'
      AND started_at = '2026-10-19T08:26:00Z'::timestamptz;

    IF activated_count <> 1 THEN
        RAISE EXCEPTION
            'ME A execution activation verification failed: expected exact RUNNING boundary';
    END IF;
END
$$;

COMMIT;
