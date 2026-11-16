-- ============================================================================
-- 089: durable FUNDING OBLIGATION for OOS-2 terminal closes (Issue #276,
-- round 6 / Codex P2-funding thread 4555591322).
--
-- Problem: when a protective STOP/TIMEOUT close fires while the close-leg
-- history is unreadable (HISTORY_UNAVAILABLE), the rate WAS observed but
-- folding it into the terminal snapshot is impossible — and a plain
-- `return 0.0` loses the known rate forever: CLOSED positions are never
-- accrued again, so the observed funding could never be settled later.
--
-- Fix: persist the obligation DURATLY before the protective close commits:
--
--     observed rate + observation time + periods due at observation +
--     deterministic obligation identity + partial-leg references
--
-- A later recovery pass (engine ``_retry_funding_obligations``) settles
-- the gap EXACTLY ONCE from the durable leg ledger once history is
-- readable again — guarded by the obligation status AND a positional
-- ``funding_periods`` guard, so a replayed or racing settlement can never
-- double-accrue.
--
-- Isolation / compatibility:
--   * additive — one NEW table in the OOS-2 namespace of schema dds, plus a
--     CREATE OR REPLACE of the OOS-2 guard FUNCTION below (the trigger
--     object and every 088 table stay untouched; no ALTER TABLE, no DROP,
--     no V1/V2 or production object);
--   * backward compatible with migration 088 (the position/close_leg/
--     close_outbox tables are untouched; this table may simply be absent
--     in an older environment, which the store reports as a documented
--     degraded mode exactly like a missing close_outbox);
--   * the production database never receives this migration in this task.
--
-- Guard refinement (why CREATE OR REPLACE is part of this migration):
-- 088 froze net_pnl on CLOSED rows unconditionally, but the recovery
-- settlement implemented here MUST book the settled funding onto the
-- already-CLOSED row (SETTLE_POSITION_FUNDING_SQL updates funding_paid,
-- funding_periods and net_pnl together).  Under the 088 guard every
-- settlement raised 'closed position is terminal' — a durable obligation
-- that could never be settled.  The replacement keeps every 088 rule
-- (entry facts immutable, ladder monotonicity, TP flags permanent, no
-- un-close) and relaxes ONLY the closed-row financial rule:
--   * funding_periods may only ADVANCE on a CLOSED row, and
--   * net_pnl / funding_paid may change only when funding_periods
--     advanced in the SAME statement (i.e. an exactly-once funding
--     settlement).
-- A net_pnl rewrite without an advancing funding counter, or any attempt
-- to rewind the counter (a stale terminal-snapshot replay), stays a hard
-- database error — double-counting and post-hoc PnL edits remain
-- impossible.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_oos2_v1_funding_obligation (
  position_key TEXT PRIMARY KEY
    CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_OOS2_V1:%'),
  -- deterministic identity of the observation window (position + entered_at
  -- + periods due/charged): a replayed close attempt upserts the SAME
  -- obligation, so a second row for the same observation can never exist.
  obligation_key TEXT NOT NULL UNIQUE,
  event_key TEXT NOT NULL DEFAULT '',
  symbol TEXT NOT NULL CHECK (symbol <> ''),
  -- KNOWN observed rate at the terminal close (the whole point: a rate is
  -- never dropped because history happened to be unreadable).
  observed_rate_percent numeric NOT NULL,
  observed_at timestamptz NOT NULL,
  -- frozen observation window: periods due and already charged at the
  -- moment of observation (periods_due may only ADVANCE on a later retry).
  entered_at timestamptz NOT NULL,
  periods_due INTEGER NOT NULL CHECK (periods_due > 0),
  periods_charged_at_observation INTEGER NOT NULL DEFAULT 0
    CHECK (periods_charged_at_observation >= 0),
  -- partial-leg references: the greatest close-leg seq known at observation
  -- (legs themselves stay in the close_leg ledger, referenced by
  -- position_key + strict executed_at < funding boundary).
  legs_ref_max_seq INTEGER NOT NULL DEFAULT -1,
  status TEXT NOT NULL DEFAULT 'OPEN'
    CHECK (status IN ('OPEN', 'SETTLED')),
  settled_at timestamptz,
  settled_cost numeric,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  CHECK (periods_charged_at_observation < periods_due)
);

COMMENT ON TABLE dds.me_long_v2_inverse_short_oos2_v1_funding_obligation
  IS 'OOS-2 durable funding obligation (Issue #276, round 6): observed funding rate + observation window persisted when close-leg history was unreadable at a terminal close; settled exactly once by the recovery pass when history is readable again. OOS-2 namespace only; additive to migration 088.';

CREATE INDEX IF NOT EXISTS
    idx_me_inverse_short_oos2_v1_funding_obligation_status
    ON dds.me_long_v2_inverse_short_oos2_v1_funding_obligation (status)
    WHERE status = 'OPEN';

-- ---------------------------------------------------------------------------
-- Guard refinement (same transaction): CREATE OR REPLACE of the 088 guard.
-- The trigger trg_me_long_v2_inverse_short_oos2_v1_guard keeps executing
-- this function — no trigger is dropped or recreated here.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dds.fn_me_long_v2_inverse_short_oos2_v1_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.position_key IS DISTINCT FROM OLD.position_key
     OR NEW.event_key IS DISTINCT FROM OLD.event_key
     OR NEW.symbol IS DISTINCT FROM OLD.symbol
     OR NEW.direction IS DISTINCT FROM OLD.direction
     OR NEW.entered_at IS DISTINCT FROM OLD.entered_at
     OR NEW.deadline_at IS DISTINCT FROM OLD.deadline_at
     OR NEW.entry_market_price IS DISTINCT FROM OLD.entry_market_price
     OR NEW.entry_price IS DISTINCT FROM OLD.entry_price
     OR NEW.entry_fee IS DISTINCT FROM OLD.entry_fee
     OR NEW.entry_slippage IS DISTINCT FROM OLD.entry_slippage
     OR NEW.stop_price IS DISTINCT FROM OLD.stop_price
     OR NEW.target_price IS DISTINCT FROM OLD.target_price
     OR NEW.tp2_price IS DISTINCT FROM OLD.tp2_price
     OR NEW.tp3_price IS DISTINCT FROM OLD.tp3_price
     OR NEW.position_size IS DISTINCT FROM OLD.position_size THEN
    RAISE EXCEPTION
      'me_long_v2_inverse_short_oos2_v1: entry facts and deadline are immutable '
      '(position_key=%, entered_at=%, deadline_at=%)',
      NEW.position_key, NEW.entered_at, NEW.deadline_at;
  END IF;

  -- Partial-ladder monotonicity (Issue #276): the remainder never grows
  -- (no re-opening, no double close), stage flags never regress.
  IF NEW.remaining_size > OLD.remaining_size THEN
    RAISE EXCEPTION
      'me_long_v2_inverse_short_oos2_v1: remaining_size may only shrink '
      '(position_key=%, % -> %)',
      NEW.position_key, OLD.remaining_size, NEW.remaining_size;
  END IF;
  IF OLD.tp1_executed AND NOT NEW.tp1_executed
     OR OLD.tp2_executed AND NOT NEW.tp2_executed
     OR OLD.tp3_executed AND NOT NEW.tp3_executed THEN
    RAISE EXCEPTION
      'me_long_v2_inverse_short_oos2_v1: executed TP stages are permanent '
      '(position_key=%)',
      NEW.position_key;
  END IF;

  IF OLD.status = 'CLOSED' THEN
    IF NEW.status <> 'CLOSED'
       OR NEW.exit_reason IS DISTINCT FROM OLD.exit_reason
       OR NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
      RAISE EXCEPTION
        'me_long_v2_inverse_short_oos2_v1: closed position is terminal (position_key=%)',
        NEW.position_key;
    END IF;
    -- Closed-row financial rule (round 6 / P2-funding): a CLOSED row's
    -- funding_paid and net_pnl may move ONLY as part of a funding
    -- settlement, i.e. when the funding_periods counter ADVANCES in the
    -- same statement; the counter itself may never run backwards.
    -- Everything else — a net_pnl rewrite with the counter unchanged, or
    -- a stale terminal-snapshot replay rewinding the counter — is still a
    -- hard error.
    IF NEW.funding_periods < OLD.funding_periods THEN
      RAISE EXCEPTION
        'me_long_v2_inverse_short_oos2_v1: funding_periods may only advance on a closed position (position_key=%, % -> %)',
        NEW.position_key, OLD.funding_periods, NEW.funding_periods;
    END IF;
    IF (NEW.net_pnl IS DISTINCT FROM OLD.net_pnl
        OR NEW.funding_paid IS DISTINCT FROM OLD.funding_paid)
       AND NEW.funding_periods <= OLD.funding_periods THEN
      RAISE EXCEPTION
        'me_long_v2_inverse_short_oos2_v1: closed position is terminal (position_key=%)',
        NEW.position_key;
    END IF;
  END IF;

  RETURN NEW;
END;
$$;

COMMIT;
