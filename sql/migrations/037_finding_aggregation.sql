-- Migration 037: Finding Aggregation (Stage 4 PR2)
--
-- Adds missing columns to research.finding_occurrence for PR2 ingestion.
-- Adds UNIQUE index on research.finding.fingerprint for dedup lookup.
--
-- Idempotent: all ALTERs use IF NOT EXISTS / conditional blocks.

-- ============================================================
-- 1. finding_occurrence: add PR2 columns
-- ============================================================

-- agent_run_id: links occurrence to the runner-level execution context
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'research'
          AND table_name = 'finding_occurrence'
          AND column_name = 'agent_run_id'
    ) THEN
        ALTER TABLE research.finding_occurrence
            ADD COLUMN agent_run_id UUID;
    END IF;
END $$;

-- business_date: the trading date this occurrence relates to (for repeat policy)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'research'
          AND table_name = 'finding_occurrence'
          AND column_name = 'business_date'
    ) THEN
        ALTER TABLE research.finding_occurrence
            ADD COLUMN business_date DATE;
    END IF;
END $$;

-- maturity: PROVISIONAL | CONFIRMED — controls escalation path
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'research'
          AND table_name = 'finding_occurrence'
          AND column_name = 'maturity'
    ) THEN
        ALTER TABLE research.finding_occurrence
            ADD COLUMN maturity TEXT NOT NULL DEFAULT 'PROVISIONAL'
            CHECK (maturity IN ('PROVISIONAL', 'CONFIRMED'));
    END IF;
END $$;

-- source_agent_name: which Stage 3 agent produced this occurrence
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'research'
          AND table_name = 'finding_occurrence'
          AND column_name = 'source_agent_name'
    ) THEN
        ALTER TABLE research.finding_occurrence
            ADD COLUMN source_agent_name TEXT NOT NULL DEFAULT '';
    END IF;
END $$;

-- ============================================================
-- 2. Unique index on research.finding.fingerprint
-- ============================================================
-- Enable dedup lookups by fingerprint hash.  Partial index (WHERE fingerprint IS NOT NULL)
-- because PR1 allows nullable fingerprints.

CREATE UNIQUE INDEX IF NOT EXISTS idx_finding_fingerprint_unique
    ON research.finding (fingerprint)
    WHERE fingerprint IS NOT NULL;

-- ============================================================
-- 3. Indexes for new occurrence columns
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_fo_business_date ON research.finding_occurrence (business_date);
CREATE INDEX IF NOT EXISTS idx_fo_agent_run_id  ON research.finding_occurrence (agent_run_id);
CREATE INDEX IF NOT EXISTS idx_fo_maturity      ON research.finding_occurrence (maturity);

-- ============================================================
-- 4. Comments
-- ============================================================
COMMENT ON COLUMN research.finding_occurrence.agent_run_id
    IS 'Links to analytics runner execution context (PR2)';
COMMENT ON COLUMN research.finding_occurrence.business_date
    IS 'Trading date this occurrence relates to — used for repeat policy distinct-date counting (PR2)';
COMMENT ON COLUMN research.finding_occurrence.maturity
    IS 'PROVISIONAL or CONFIRMED — controls escalation and repeat policy thresholds (PR2)';
COMMENT ON COLUMN research.finding_occurrence.source_agent_name
    IS 'Stage 3 agent that produced this occurrence (PR2)';
