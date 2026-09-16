-- Migration 027: Quality Result Idempotency
-- Adds unique constraint on (run_id, check_name) to prevent duplicate quality results.
-- This ensures that one analysis_run + one check_name = one current quality result.
--
-- Idempotent: safe to run multiple times.

-- ============================================================
-- 1. Add unique constraint on (run_id, check_name)
-- ============================================================
DO $$
BEGIN
    -- Check if constraint already exists
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint 
        WHERE conname = 'uq_quality_result_run_check' 
        AND conrelid = 'analytics.data_quality_result'::regclass
    ) THEN
        ALTER TABLE analytics.data_quality_result
        ADD CONSTRAINT uq_quality_result_run_check UNIQUE (run_id, check_name);
        
        RAISE NOTICE 'Added unique constraint uq_quality_result_run_check';
    ELSE
        RAISE NOTICE 'Constraint uq_quality_result_run_check already exists';
    END IF;
END $$;

-- ============================================================
-- 2. Add index for efficient upsert queries
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_quality_result_run_check 
    ON analytics.data_quality_result (run_id, check_name);

-- ============================================================
-- 3. Comment on the constraint
-- ============================================================
COMMENT ON CONSTRAINT uq_quality_result_run_check 
    ON analytics.data_quality_result 
    IS 'Ensures one quality result per check per analysis run (idempotency)';