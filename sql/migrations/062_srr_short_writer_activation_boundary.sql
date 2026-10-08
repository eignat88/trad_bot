-- ============================================================
-- Migration 062: SRR SHORT Writer Activation Boundary Record
-- ============================================================
-- Adds the immutable activation metadata record required by
--   SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1
--
-- The record is the authoritative source of ACTIVATION_TS once the SRR
-- writer is enabled. A mismatch between this record and the runtime
-- configuration must block persistence with an explicit diagnostic.
--
-- This migration is source-controlled only. It is NOT applied to
-- production PostgreSQL as part of this task.
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research;

-- ── 1. Activation metadata record ─────────────────────────────

CREATE TABLE IF NOT EXISTS research.srr_short_writer_activation (
    experiment_id           TEXT PRIMARY KEY
                            CHECK (
                                experiment_id =
                                'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
                            ),
    boundary_version        TEXT NOT NULL
                            CHECK (
                                boundary_version =
                                'SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1'
                            ),
    activation_ts           TIMESTAMPTZ NOT NULL,
    direction               TEXT NOT NULL DEFAULT 'SHORT'
                            CHECK (direction = 'SHORT'),
    status                  TEXT NOT NULL DEFAULT 'PENDING'
                            CHECK (status IN ('PENDING', 'ACTIVE', 'REVOKED')),
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE research.srr_short_writer_activation
    IS 'Immutable SRR SHORT writer activation metadata; used to block boundary drift';

COMMENT ON COLUMN research.srr_short_writer_activation.activation_ts
    IS 'Explicit UTC activation boundary; observations with signal_time > activation_ts are eligible';

-- ── 2. updated_at maintenance ─────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_srr_short_writer_activation_updated_at'
    ) THEN
        CREATE TRIGGER trg_srr_short_writer_activation_updated_at
        BEFORE UPDATE ON research.srr_short_writer_activation
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;
END $$;

-- ── 3. State machine and immutability guard ───────────────────
-- Immutable fields: experiment_id, direction, boundary_version,
-- activation_ts. They may never change after the initial INSERT,
-- regardless of status.
-- Permitted status transitions: PENDING → ACTIVE → REVOKED,
-- and PENDING → REVOKED (cancel-before-activation).
-- A REVOKED record is terminal; no reactivation is permitted.
-- DELETE of an activation record is always prohibited.

CREATE OR REPLACE FUNCTION research.fn_block_srr_short_writer_activation_change()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    -- Immutable fields may never change after INSERT.
    IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id THEN
        RAISE EXCEPTION
            'SRR writer activation experiment_id is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.direction IS DISTINCT FROM OLD.direction THEN
        RAISE EXCEPTION
            'SRR writer activation direction is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.boundary_version IS DISTINCT FROM OLD.boundary_version THEN
        RAISE EXCEPTION
            'SRR writer activation boundary_version is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.activation_ts IS DISTINCT FROM OLD.activation_ts THEN
        RAISE EXCEPTION
            'SRR writer activation activation_ts is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Status transitions: only PENDING → ACTIVE, PENDING → REVOKED,
    -- and ACTIVE → REVOKED are permitted. Self-transitions are a no-op.
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'PENDING' AND NEW.status IN ('ACTIVE', 'REVOKED'))
            OR (OLD.status = 'ACTIVE' AND NEW.status = 'REVOKED')
        ) THEN
            RAISE EXCEPTION
                'SRR writer activation status transition % → % is not permitted: experiment_id=%',
                OLD.status, NEW.status, OLD.experiment_id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgrelid =
            'research.srr_short_writer_activation'::regclass
          AND tgname = 'trg_srr_short_writer_activation_block_change'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_srr_short_writer_activation_block_change
        BEFORE UPDATE ON research.srr_short_writer_activation
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_block_srr_short_writer_activation_change();
    END IF;
END $$;

-- DELETE of an activation record is always prohibited.

CREATE OR REPLACE FUNCTION research.fn_block_srr_short_writer_activation_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    RAISE EXCEPTION
        'SRR writer activation record cannot be deleted: experiment_id=%',
        OLD.experiment_id
        USING ERRCODE = 'restrict_violation';
END;
$trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgrelid =
            'research.srr_short_writer_activation'::regclass
          AND tgname = 'trg_srr_short_writer_activation_block_delete'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_srr_short_writer_activation_block_delete
        BEFORE DELETE ON research.srr_short_writer_activation
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_block_srr_short_writer_activation_delete();
    END IF;
END $$;

-- ── 4. Verify (read-only) ─────────────────────────────────────
--
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_schema='research'
--   AND table_name='srr_short_writer_activation'
-- ORDER BY ordinal_position;
--
-- SELECT conname, pg_get_constraintdef(oid)
-- FROM pg_constraint
-- WHERE conrelid='research.srr_short_writer_activation'::regclass
-- ORDER BY conname;

COMMIT;

-- ============================================================
-- PRODUCTION STATUS:
--   NOT APPLIED
--   NOT ACTIVATED
--   DEPLOY BLOCKED
-- Existing prospective_observation, prospective_outcome,
-- research_outcome, srr_short_execution_prospective_outcome,
-- scanner, paper, live, and frozen protocol data are not modified.
-- ============================================================
