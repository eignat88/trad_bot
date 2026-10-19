-- ME SHORT A/B/C historical paired research lifecycle closure.
-- Only the three legacy geometry experiments are terminal.
-- The separate ME_SHORT_GEOM_A_EXECUTION_V1 remains RUNNING.
-- No observations/outcomes are deleted or rewritten.
BEGIN;

DO $$
DECLARE invalid_count integer;
BEGIN
 SELECT count(*) INTO invalid_count
 FROM research.prospective_experiment
 WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1','ME_SHORT_GEOM_C_V1')
   AND status NOT IN ('RUNNING','READY_TO_START','CANCELLED','COMPLETED','CLOSED_NEGATIVE');
 IF invalid_count <> 0 THEN
   RAISE EXCEPTION 'ME SHORT ABC closure blocked: unexpected existing statuses (%)',invalid_count;
 END IF;

 SELECT 3-count(*) INTO invalid_count
 FROM research.prospective_experiment
 WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1','ME_SHORT_GEOM_C_V1');
 IF invalid_count <> 0 THEN
   RAISE EXCEPTION 'ME SHORT ABC closure blocked: expected exactly 3 registry rows';
 END IF;

 IF NOT EXISTS (
     SELECT 1 FROM research.prospective_experiment
     WHERE experiment_id='ME_SHORT_GEOM_A_EXECUTION_V1'
       AND status='RUNNING'
       AND started_at=TIMESTAMPTZ '2026-10-19 08:26:00+00'
 ) THEN
     RAISE EXCEPTION 'ME SHORT ABC closure blocked: execution validation not RUNNING on frozen boundary';
 END IF;
END $$;

UPDATE research.prospective_experiment
SET status='CANCELLED'
WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1','ME_SHORT_GEOM_C_V1')
  AND status IN ('RUNNING','READY_TO_START');

-- A database-level fuse: later migrations, bootstrap code, and manual
-- status changes cannot silently restart retired A/B/C IDs.
CREATE OR REPLACE FUNCTION research.guard_retired_me_short_abc_v1()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
 IF NEW.experiment_id IN (
     'ME_SHORT_GEOM_A_V1',
     'ME_SHORT_GEOM_B_V1',
     'ME_SHORT_GEOM_C_V1'
 ) AND NEW.status NOT IN ('CANCELLED','COMPLETED','CLOSED_NEGATIVE') THEN
   RAISE EXCEPTION 'Retired ME SHORT geometry experiment % cannot be reactivated (status=%). Create a new versioned experiment instead.',
       NEW.experiment_id,NEW.status;
 END IF;
 RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_retired_me_short_abc_v1
ON research.prospective_experiment;
DROP TRIGGER IF EXISTS trg_guard_retired_me_short_abc_v1_insert
ON research.prospective_experiment;

-- UPDATE must be prevented before changing the stored lifecycle state.
CREATE TRIGGER trg_guard_retired_me_short_abc_v1
BEFORE UPDATE ON research.prospective_experiment
FOR EACH ROW EXECUTE FUNCTION research.guard_retired_me_short_abc_v1();

-- AFTER INSERT fires only for actual inserted rows; old seed migrations
-- use ON CONFLICT DO NOTHING and must remain safely replayable.
CREATE TRIGGER trg_guard_retired_me_short_abc_v1_insert
AFTER INSERT ON research.prospective_experiment
FOR EACH ROW EXECUTE FUNCTION research.guard_retired_me_short_abc_v1();

DO $$
BEGIN
 IF (SELECT count(*) FROM research.prospective_experiment
     WHERE experiment_id IN ('ME_SHORT_GEOM_A_V1','ME_SHORT_GEOM_B_V1','ME_SHORT_GEOM_C_V1')
       AND status IN ('CANCELLED','COMPLETED','CLOSED_NEGATIVE')) <> 3 THEN
   RAISE EXCEPTION 'ME SHORT ABC closure postcondition failed';
 END IF;
END $$;
COMMIT;
