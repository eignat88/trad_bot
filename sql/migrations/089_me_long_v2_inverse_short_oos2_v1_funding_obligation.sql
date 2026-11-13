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
--   * additive ONLY — one NEW table in the OOS-2 namespace of schema dds;
--     no ALTER, no DROP, no trigger changes, no V1/V2 or production object;
--   * backward compatible with migration 088 (the position/close_leg/
--     close_outbox tables are untouched; this table may simply be absent
--     in an older environment, which the store reports as a documented
--     degraded mode exactly like a missing close_outbox);
--   * the production database never receives this migration in this task.
-- ============================================================================

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
