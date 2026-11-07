-- ============================================================
-- Migration 084: Paper recovery — execution policy round-trip
-- ============================================================
-- Requirement (ME_LONG_V2_INVERSE_SHORT_V1 hardening, item 6):
--   the EXISTING Paper restart recovery must read
--   execution_policy, execution_policy_version and planned_exit_at
--   back from dds.paper_trade, so a fixed-horizon paper trade keeps
--   its policy and its original planned exit after a restart.
--
-- ADDITIVE ONLY:
--   * adds execution_policy_version (new column, default '');
--   * re-states the migration-038 columns with IF NOT EXISTS so a
--     schema.sql-bootstrapped database converges to the same shape;
--   * NO existing row is updated, deleted or backfilled — old paper
--     trades keep their data byte-for-byte (execution_policy /
--     planned_exit_at already stored since migration 038; the version
--     column simply reads '' for them, matching the in-memory default).
--
-- Application is guarded: app/db/repository.py probes
-- information_schema before using the new column, so code deployed
-- BEFORE this migration behaves exactly as before (version simply not
-- persisted), and code deployed AFTER gains the full round-trip.
--
-- This migration is source-controlled only. It is NOT applied to
-- production PostgreSQL as part of this task.
-- ============================================================

BEGIN;

ALTER TABLE dds.paper_trade
    ADD COLUMN IF NOT EXISTS execution_policy TEXT NOT NULL DEFAULT 'DEFAULT';

ALTER TABLE dds.paper_trade
    ADD COLUMN IF NOT EXISTS planned_exit_at TIMESTAMPTZ;

ALTER TABLE dds.paper_trade
    ADD COLUMN IF NOT EXISTS execution_policy_version TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN dds.paper_trade.execution_policy_version
    IS 'Version of execution_policy recorded at entry; restored by restart recovery (read back together with execution_policy and planned_exit_at).';

COMMIT;
