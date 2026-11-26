-- ===========================================================================
-- Issue #312 / PR #313 review remediation — migration 096
-- H2_RSI_ONLY prospective runtime: durable overlap reconciliation cursor.
--
-- Review P1-overlap (Source Overlap Starvation): reconciliation of the
-- #305 capture-order stream needs its OWN durable progress marker so a
-- bounded per-cycle batch budget can never starve the overlap sweep.
--
-- Research schema ONLY (research_me_entry_quality_h2_prospective).  Never
-- touches #305 capture, production schemas, trading or scanner surfaces.
--
-- Idempotent by construction (CREATE TABLE IF NOT EXISTS + guarded ALTERs):
-- re-application is a safe no-op and cannot damage existing ingest events,
-- variant states, results or the conflict ledger.  A transactional wrapper:
-- any error rolls the whole migration back.
--
-- NOT applied to production anywhere in this PR (ACTIVATION_HOLD).
-- ===========================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research_me_entry_quality_h2_prospective;

-- ---------------------------------------------------------------------------
-- Durable overlap reconciliation state (single row).
--
-- The reconciliation pass scans the capture-order stream in bounded,
-- contiguous slices.  Per pass:
--   * pass_floor_us .. pass_ceiling_us is the FIXED scan range of the pass
--     (the ceiling is pinned at pass start and never moves while the pass
--     runs, so forward-cursor advancement cannot exclude unchecked ranges);
--   * (cursor_captured_at_us, cursor_key) is the durable progress INSIDE the
--     pass — advanced only after the covered rows are durably ingested and
--     their variant states exist (checkpoint-last, crash-safe);
--   * pass_active marks a pass in progress (a restart resumes it);
--   * passes_completed counts finished passes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h2_prospective.overlap_cursor (
    id INTEGER PRIMARY KEY DEFAULT 1,
    cursor_captured_at_us BIGINT NOT NULL DEFAULT 0,
    cursor_key TEXT NOT NULL DEFAULT '',
    pass_floor_us BIGINT NOT NULL DEFAULT 0,
    pass_ceiling_us BIGINT NOT NULL DEFAULT 0,
    pass_active BOOLEAN NOT NULL DEFAULT FALSE,
    passes_completed BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT h2p_overlap_singleton_chk CHECK (id = 1),
    CONSTRAINT h2p_overlap_bounds_chk CHECK (
        pass_floor_us >= 0 AND pass_ceiling_us >= 0
        AND cursor_captured_at_us >= 0
        AND passes_completed >= 0
    )
);

-- ---------------------------------------------------------------------------
-- Diagnostic status for a violated overlap coverage contract (review FR-05):
-- when the documented overlap window cannot be honoured (the sweep runs
-- contiguously instead of re-checking the full window), the runtime reports
-- OVERLAP_COVERAGE_DEGRADED instead of a false coverage confirmation.
-- ---------------------------------------------------------------------------
ALTER TABLE research_me_entry_quality_h2_prospective.runtime_health
    DROP CONSTRAINT IF EXISTS h2p_health_status_chk;
ALTER TABLE research_me_entry_quality_h2_prospective.runtime_health
    ADD CONSTRAINT h2p_health_status_chk
    CHECK (
        status IN (
            'RUNNING',
            'SOURCE_EMPTY_WAITING',
            'BLOCKED_CANDLE_SOURCE',
            'DISABLED',
            'OVERLAP_COVERAGE_DEGRADED'
        )
    );

-- ---------------------------------------------------------------------------
-- Never silently accept an incompatible pre-existing schema (review P2):
-- a table that exists but lacks a required column is a predictable REFUSAL —
-- the whole transaction rolls back.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    missing TEXT;
BEGIN
    SELECT string_agg(expected.col, ', ') INTO missing
    FROM (
        SELECT 'overlap_cursor.pass_active' AS col
        WHERE NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'research_me_entry_quality_h2_prospective'
              AND table_name = 'overlap_cursor'
              AND column_name = 'pass_active'
        )
        UNION ALL
        SELECT 'overlap_cursor.pass_ceiling_us'
        WHERE NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'research_me_entry_quality_h2_prospective'
              AND table_name = 'overlap_cursor'
              AND column_name = 'pass_ceiling_us'
        )
    ) AS expected;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION
            'H2P_SCHEMA_INCOMPATIBLE: missing columns: %', missing;
    END IF;
END;
$$;

COMMIT;
