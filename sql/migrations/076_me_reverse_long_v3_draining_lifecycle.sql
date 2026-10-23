-- Issue #189: install the V3-only lifecycle guard. Does NOT transition runtime state.
-- Deployment order: code + this migration -> controlled operator RUNNING->DRAINING
-- -> 100% FINALIZED -> controlled operator DRAINING->CANCELLED.
BEGIN;
-- Migration 050's existing status CHECK rejects DRAINING. Replace it with
-- the identical original statuses plus ONE scoped V3-only drain exception.
-- This stays idempotent when migration 076 is reapplied.
ALTER TABLE research.prospective_experiment
DROP CONSTRAINT IF EXISTS prospective_experiment_status_check;
ALTER TABLE research.prospective_experiment
ADD CONSTRAINT prospective_experiment_status_check
CHECK (
    status IN ('READY_TO_START','RUNNING','PAUSED','COMPLETED','CANCELLED')
    OR (
        experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS'
        AND status = 'DRAINING'
    )
);

CREATE OR REPLACE FUNCTION research.guard_me_reverse_long_v3_drain_lifecycle()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE unfinished bigint;
BEGIN
  IF NEW.experiment_id <> 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS' THEN
    RETURN NEW;
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.status IN ('DRAINING','CANCELLED') THEN
      RAISE EXCEPTION 'ME V3 cannot bootstrap directly in terminal/drain state';
    END IF;
    RETURN NEW;
  END IF;
  IF OLD.status IN ('DRAINING','CANCELLED','COMPLETED','CLOSED_NEGATIVE')
     AND NEW.status = 'RUNNING' THEN
    RAISE EXCEPTION 'ME V3 cannot reactivate from %', OLD.status;
  END IF;
  IF OLD.status = 'RUNNING' AND NEW.status = 'CANCELLED' THEN
    RAISE EXCEPTION 'ME V3 must DRAIN before CANCELLED';
  END IF;
  IF OLD.status = 'DRAINING' AND NEW.status NOT IN ('DRAINING','CANCELLED') THEN
    RAISE EXCEPTION 'ME V3 DRAINING can only transition to CANCELLED';
  END IF;
  IF OLD.status = 'DRAINING' AND NEW.status = 'CANCELLED' THEN
    SELECT COUNT(*) INTO unfinished
    FROM research.prospective_observation o
    LEFT JOIN research.me_reverse_long_v3_execution_outcome r
      ON r.observation_id = o.observation_id
     AND r.experiment_id = o.experiment_id
    WHERE o.experiment_id = NEW.experiment_id
      AND (r.observation_id IS NULL OR r.status IS DISTINCT FROM 'FINALIZED');
    IF unfinished <> 0 THEN
      RAISE EXCEPTION 'ME V3 has % unfinished observations; drain first',unfinished;
    END IF;
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_guard_me_reverse_long_v3_drain ON research.prospective_experiment;
CREATE TRIGGER trg_guard_me_reverse_long_v3_drain
BEFORE INSERT OR UPDATE ON research.prospective_experiment
FOR EACH ROW EXECUTE FUNCTION research.guard_me_reverse_long_v3_drain_lifecycle();
COMMIT;
