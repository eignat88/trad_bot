-- ============================================================
-- Migration 086: ME_LONG_V2_INVERSE_SHORT_V1 durable close outbox
-- ============================================================
-- Review fix P1-3 (durable terminal position persistence).
--
-- The contour stages the EXACT terminal snapshot of a position in this
-- table BEFORE the position row flips to CLOSED.  If the status UPDATE
-- fails (DB hiccup) or the process restarts mid-close, the snapshot is
-- replayed byte-for-byte on recovery — same position_key, closed_at,
-- exit_reason, exit_price, PnL and funding — so an exit can never lose
-- its facts, be recomputed by STOP/TP/TIMEOUT, or be booked twice.
--
-- Migration-number safety (checked against origin/main at review time):
--   origin/main highest : 083_srr_short_fill_coverage_v1.sql
--   this PR             : 084_paper_execution_policy_recovery_v1.sql
--                         085_me_long_v2_inverse_short_v1.sql
--   this file           : 086 (next free) — verified NOT taken on
--                         origin/main before adding.
--
-- Scope: ADDITIVE ONLY.  Creates one brand-new table in the existing
-- dds schema.  Does NOT touch dds.paper_trade, dds.scanner_setup,
-- dds.paper_shadow_trade or any other contour/production table, does
-- NOT alter existing migrations, and does NOT change the frozen
-- protocol (TP/SL/hold/fingerprint) in any way.
--
-- Rollout safety: PgInverseShortStore degrades gracefully when this
-- migration is not applied (42P01 -> close durability falls back to the
-- in-process snapshot + retry loop), so the PR is safe to deploy before
-- the migration runs.  Source-controlled only: NOT executed by this PR.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v1_close_outbox (
    position_key TEXT PRIMARY KEY
        CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_V1:%'),
    symbol TEXT NOT NULL CHECK (symbol <> ''),
    exit_reason TEXT NOT NULL
        CHECK (exit_reason IN
               ('STOP_LOSS', 'STOP_LOSS_GAP', 'TAKE_PROFIT_1', 'TIMEOUT')),
    closed_at TIMESTAMPTZ NOT NULL,
    -- Full InverseShortPosition.to_dict() payload: every immutable entry
    -- fact (entered_at, deadline_at, entry geometry) plus the terminal
    -- exit facts needed to re-run the position UPDATE unchanged.
    snapshot JSONB NOT NULL,
    staged_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- NOTE: the position row keeps its OPEN status while a close is staged
-- (the terminal-consistency CHECK of migration 085 requires status=CLOSED
-- only together with exit facts on the POSITION row).  This outbox is the
-- single durable home of the terminal facts until the flip is confirmed.

COMMIT;
