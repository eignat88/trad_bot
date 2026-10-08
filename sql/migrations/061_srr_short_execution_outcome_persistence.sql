-- ============================================================
-- Migration 061: SRR Short Execution-R Prospective Outcome Persistence
-- ============================================================
-- Adds the dedicated, non-generic persistence contract for:
--   SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1
--
-- This table is intentionally separate from
--   research.prospective_outcome
-- and from
--   research.research_outcome.
-- The legacy generic outcome tables remain unchanged.
--
-- Safety invariants:
--   1. observation_id is the primary key.
--   2. Exactly one outcome row may exist per prospective observation.
--   3. A finalized row can never be overwritten or re-finalized.
--   4. Non-final diagnostics may be idempotently refreshed.
--   5. Required source/status fields make non-finalizable evidence explicit.
--   6. Frozen economics are NULL whenever a path is not finalizable.
--
-- NOT APPLIED IN THIS TASK. This migration is source-controlled only and
-- must not be executed against production PostgreSQL until separately
-- approved deployment controls are satisfied.
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research;

-- ── 1. Observation snapshot ──────────────────────────────────
-- The frozen experiment and signal boundary are copied into the outcome row
-- so that lifecycle correctness does not depend on mutable observation data.

CREATE TABLE IF NOT EXISTS research.srr_short_execution_prospective_outcome (
    observation_id              BIGINT PRIMARY KEY
                                REFERENCES research.prospective_observation(observation_id)
                                ON DELETE CASCADE,
    experiment_id               TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    direction                   TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    signal_time                 TIMESTAMPTZ NOT NULL,
    freeze_ts                   TIMESTAMPTZ NOT NULL,

    -- ── Route status ────────────────────────────────────────
    status                      TEXT NOT NULL,
    reason_code                 TEXT NOT NULL,
    path_class                  TEXT,
    is_final                    BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── Frozen geometry snapshot ────────────────────────────
    entry_price                 NUMERIC(30, 14),
    invalidation_price          NUMERIC(30, 14),
    variant_entry               NUMERIC(30, 14),
    variant_stop                NUMERIC(30, 14),
    variant_target              NUMERIC(30, 14),
    structural_r                NUMERIC(30, 14),
    execution_r                 NUMERIC(30, 14),
    max_hold_minutes            INTEGER,
    intrabar_policy             TEXT,

    -- ── Frozen execution economics ──────────────────────────
    gross_r                     NUMERIC(30, 14),
    cost_r_normal               NUMERIC(30, 14),
    cost_r_elevated             NUMERIC(30, 14),
    net_r_normal                NUMERIC(30, 14),
    net_r_elevated              NUMERIC(30, 14),

    -- ── Candle-path diagnostics ─────────────────────────────
    eligible_candle_count       INTEGER NOT NULL DEFAULT 0,
    selected_timeout_open_ms    BIGINT,
    selected_timeout_close_ms   BIGINT,
    selected_timeout_close      NUMERIC(30, 14),
    return_at_120m              NUMERIC(30, 14),
    signal_boundary_uncertain   BOOLEAN NOT NULL DEFAULT FALSE,
    cutoff_boundary_uncertain   BOOLEAN NOT NULL DEFAULT FALSE,
    coverage_complete           BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── Source evidence and provenance ──────────────────────
    source_status               TEXT NOT NULL,
    source_confidence           TEXT,
    source_kind                 TEXT,
    source_id                   TEXT,
    retrieval_method            TEXT,
    retrieval_ts_ms             BIGINT,
    source_notes                TEXT,
    source_diagnostics          JSONB NOT NULL DEFAULT '{}'::jsonb,
    data_quality                JSONB NOT NULL DEFAULT '{}'::jsonb,
    route_diagnostics           JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- ── Execution identity ──────────────────────────────────
    evaluation_asof_ms          BIGINT NOT NULL,
    adapter_version             TEXT NOT NULL,
    frozen_policy_version       TEXT NOT NULL,

    -- ── Lifecycle timestamps ────────────────────────────────
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE research.srr_short_execution_prospective_outcome
    IS 'Dedicated SRR execution-R prospective outcomes; finalized rows are immutable';

COMMENT ON COLUMN research.srr_short_execution_prospective_outcome.path_class
    IS 'TP_FIRST, SL_FIRST, TIMEOUT, or an explicit uncertainty class';

COMMENT ON COLUMN research.srr_short_execution_prospective_outcome.gross_r
    IS 'Frozen execution-R normalized gross result; NULL unless finalization is eligible';

COMMENT ON COLUMN research.srr_short_execution_prospective_outcome.source_diagnostics
    IS 'Structured historical source evidence; never used to fabricate economics';

-- ── 2. Supporting indexes ────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_srr_short_execution_outcome_experiment
    ON research.srr_short_execution_prospective_outcome (experiment_id);

CREATE INDEX IF NOT EXISTS idx_srr_short_execution_outcome_signal_time
    ON research.srr_short_execution_prospective_outcome (signal_time);

CREATE INDEX IF NOT EXISTS idx_srr_short_execution_outcome_pending
    ON research.srr_short_execution_prospective_outcome (observation_id)
    WHERE is_final = FALSE;

-- Finalized rows are the immutable validation cohort and must be directly
-- inspectable without scanning non-final diagnostics.
CREATE INDEX IF NOT EXISTS idx_srr_short_execution_outcome_final
    ON research.srr_short_execution_prospective_outcome (experiment_id, signal_time)
    WHERE is_final = TRUE;

-- ── 3. Finalization immutability trigger ─────────────────────
-- SQL cannot express every writer-level invariant in a single CHECK
-- constraint when columns are dynamically updated. This trigger enforces the
-- protocol-critical rule at the database boundary:
--   once is_final=TRUE, no subsequent UPDATE may change that row.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_srr_short_execution_outcome_updated_at'
    ) THEN
        CREATE TRIGGER trg_srr_short_execution_outcome_updated_at
        BEFORE UPDATE ON research.srr_short_execution_prospective_outcome
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;
END $$;

-- The trigger above normally maintains updated_at. The finalize guard is
-- defined as a separate trigger so the immutability rule remains explicit.

CREATE OR REPLACE FUNCTION research.fn_block_srr_short_execution_final_update()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    IF OLD.is_final = TRUE THEN
        RAISE EXCEPTION
            'finalized SRR short execution outcome is immutable: observation_id=%',
            OLD.observation_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid =
            'research.srr_short_execution_prospective_outcome'::regclass
          AND tgname = 'trg_srr_short_execution_outcome_block_final_update'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_srr_short_execution_outcome_block_final_update
        BEFORE UPDATE ON research.srr_short_execution_prospective_outcome
        FOR EACH ROW
        WHEN (OLD.is_final = TRUE)
        EXECUTE FUNCTION research.fn_block_srr_short_execution_final_update();
    END IF;
END $$;

-- ── 4. Local consistency checks ──────────────────────────────
-- These checks prevent contradictory rows at the storage boundary. They do
-- not replace writer validation; they protect against partial or external
-- writes.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_srr_short_execution_outcome_final_economics'
    ) THEN
        ALTER TABLE research.srr_short_execution_prospective_outcome
        ADD CONSTRAINT chk_srr_short_execution_outcome_final_economics
        CHECK (
            is_final = FALSE
            OR (
                path_class IS NOT NULL
                AND gross_r IS NOT NULL
                AND cost_r_normal IS NOT NULL
                AND cost_r_elevated IS NOT NULL
                AND net_r_normal IS NOT NULL
                AND net_r_elevated IS NOT NULL
                AND structural_r IS NOT NULL
                AND execution_r IS NOT NULL
                AND max_hold_minutes IS NOT NULL
                AND intrabar_policy IS NOT NULL
                AND coverage_complete = TRUE
            )
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_srr_short_execution_outcome_nonfinal_economics'
    ) THEN
        ALTER TABLE research.srr_short_execution_prospective_outcome
        ADD CONSTRAINT chk_srr_short_execution_outcome_nonfinal_economics
        CHECK (
            is_final = TRUE
            OR (
                gross_r IS NULL
                AND cost_r_normal IS NULL
                AND cost_r_elevated IS NULL
                AND net_r_normal IS NULL
                AND net_r_elevated IS NULL
            )
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_srr_short_execution_outcome_freeze_boundary'
    ) THEN
        ALTER TABLE research.srr_short_execution_prospective_outcome
        ADD CONSTRAINT chk_srr_short_execution_outcome_freeze_boundary
        CHECK (signal_time > freeze_ts);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_srr_short_execution_outcome_short_only'
    ) THEN
        ALTER TABLE research.srr_short_execution_prospective_outcome
        ADD CONSTRAINT chk_srr_short_execution_outcome_short_only
        CHECK (direction = 'SHORT');
    END IF;
END $$;

-- ── 5. Verify ────────────────────────────────────────────────
-- These read-only checks are intentionally commented until an approved
-- local migration run is requested:
--
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_schema='research'
--   AND table_name='srr_short_execution_prospective_outcome'
-- ORDER BY ordinal_position;
--
-- SELECT conname, pg_get_constraintdef(oid)
-- FROM pg_constraint
-- WHERE conrelid='research.srr_short_execution_prospective_outcome'::regclass
-- ORDER BY conname;
--
-- SELECT tgname
-- FROM pg_trigger
-- WHERE tgrelid='research.srr_short_execution_prospective_outcome'::regclass
--   AND NOT tgisinternal
-- ORDER BY tgname;


-- Enforce the frozen experiment identity at the storage boundary.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_srr_short_execution_outcome_experiment_id'
          AND conrelid =
              'research.srr_short_execution_prospective_outcome'::regclass
    ) THEN
        ALTER TABLE research.srr_short_execution_prospective_outcome
            ADD CONSTRAINT chk_srr_short_execution_outcome_experiment_id
            CHECK (
                experiment_id =
                'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
            );
    END IF;
END $$;

COMMIT;

-- ============================================================
-- PRODUCTION STATUS:
--   NOT APPLIED
--   NOT ACTIVATED
--   DEPLOY BLOCKED
-- Existing prospective_observation, prospective_outcome,
-- research_outcome, scanner, paper, live, and frozen protocol data
-- are not modified by this migration source.
-- ============================================================
