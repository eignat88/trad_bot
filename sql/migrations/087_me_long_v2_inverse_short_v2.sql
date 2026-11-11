-- ============================================================
-- Migration 087: ME_LONG_V2_INVERSE_SHORT_V2 — new experiment,
--                new fingerprint namespace, entry-TTL dispositions
-- ============================================================
-- Owner decisions (docs/research/ME_INVERSE_SHORT_STALE_ENTRY_GUARD_V1_ANALYSIS.md):
--   Q1: entry signal TTL = 120 minutes from signal_time
--       (frozen execution spec field entry_max_age_minutes = 120).
--   Q2: the entry-admissibility change is a SUBSTANTIVE research-protocol
--       change -> variant B: a NEW experiment ME_LONG_V2_INVERSE_SHORT_V2
--       with its own fingerprint, its own activation row and its own
--       clean prospective holdout.  V1 is NEVER modified by this file.
--
-- Tables created (all brand-new, in the existing dds schema; the file is
-- structurally the union of migrations 085 + 086 with V2 identity and the
-- V2 four-state event disposition):
--
--   dds.me_long_v2_inverse_short_v2_activation  — activation lifecycle
--       (status + protocol_fingerprint + freeze_ts).  Seeded
--       READY_TO_START with NULL fingerprint/freeze: the contour is inert
--       (NOT_DEPLOYED) until a separate activation step freezes the
--       CURRENT V2 protocol fingerprint together with freeze_ts.
--
--   dds.me_long_v2_inverse_short_v2_event       — source-event ledger
--       (append-only, idempotent on the deterministic event_key, full
--       lineage JSON back to the source signal).  FOUR-state disposition:
--         CAPTURED          — eligible (age < entry TTL)
--         ENTERED           — a position row exists for this event_key
--         EXPIRED           — TERMINAL, age >= entry TTL (generic cause)
--         CAPACITY_REJECTED — TERMINAL, age >= entry TTL while
--                             max_open_positions was the binding refusal
--       Terminal refusals must carry BOTH the cause (status_reason in
--       ('ENTRY_TTL','ENTRY_CAP')) and the decision timestamp (status_at).
--       CHECK (signal_time > activation_freeze_ts) makes historical
--       backfill impossible by construction.
--
--   dds.me_long_v2_inverse_short_v2_position    — isolated SHORT paper
--       positions keyed by the NAMESPACED position_key
--       ('ME_LONG_V2_INVERSE_SHORT_V2:<event_key>') so the contour can
--       never collide with V1, with the shared production Paper engine
--       state, or with any other strategy's state.  entered_at /
--       deadline_at are written once at entry and are enforced IMMUTABLE
--       by the trigger below.
--
--   dds.me_long_v2_inverse_short_v2_close_outbox — durable close staging
--       (exact terminal snapshot BEFORE the status flip), same contract
--       as V1 migration 086.
--
-- Migration-number safety: 087 is the next free number after
-- 086_me_long_v2_inverse_short_v1_close_outbox.sql.
--
-- WHAT THIS MIGRATION NEVER DOES (acceptance: V1 isolation):
--   * never ALTERs, DROPs or writes into any V1 table
--     (dds.me_long_v2_inverse_short_v1_*) or any production/research
--     table (dds.paper_trade, dds.paper_shadow_trade, dds.scanner_setup,
--     dds.signal_outcome, research.*);
--   * never touches the V1 activation row, V1 fingerprint or V1 freeze_ts;
--   * never activates anything (seeded READY_TO_START, fail-closed);
--   * never backfills: no event row can be older than its own frozen
--     activation boundary.
--
-- This migration is source-controlled only. It is NOT applied to any
-- database by this task (deploy / PostgreSQL changes are a separately
-- approved stage).
-- ============================================================

BEGIN;

-- ── 1. Activation lifecycle record (V2) ──────────────────────
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v2_activation (
  experiment_id text PRIMARY KEY
    CHECK (experiment_id = 'ME_LONG_V2_INVERSE_SHORT_V2'),
  protocol_version text NOT NULL DEFAULT 'ME_LONG_V2_INVERSE_SHORT_V2',
  status text NOT NULL DEFAULT 'READY_TO_START'
    CHECK (status IN ('READY_TO_START', 'RUNNING', 'DRAINING', 'CANCELLED')),
  protocol_fingerprint text,
  freeze_ts timestamptz,
  notes text,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  -- READY_TO_START is fail-closed (no fingerprint, no freeze); every other
  -- state must carry BOTH the frozen fingerprint and the freeze timestamp.
  CHECK (
    (status = 'READY_TO_START' AND protocol_fingerprint IS NULL AND freeze_ts IS NULL)
    OR (status <> 'READY_TO_START' AND protocol_fingerprint IS NOT NULL AND freeze_ts IS NOT NULL)
  )
);

COMMENT ON TABLE dds.me_long_v2_inverse_short_v2_activation
  IS 'ME_LONG_V2_INVERSE_SHORT_V2 activation: RUNNING + frozen V2 protocol_fingerprint + freeze_ts are both required before any capture or entry (fail-closed, prospective-only, paper-only). The V1 activation row is a separate, untouched row.';

INSERT INTO dds.me_long_v2_inverse_short_v2_activation (experiment_id, status)
VALUES ('ME_LONG_V2_INVERSE_SHORT_V2', 'READY_TO_START')
ON CONFLICT DO NOTHING;

-- ── 2. Source-event ledger (V2, four-state disposition) ──────
-- One row per ORIGINAL MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 detector event,
-- captured BEFORE the trading direction gate and the expectancy filter
-- (both untouched by this contour).
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v2_event (
  event_key text PRIMARY KEY,
  strategy_id text NOT NULL DEFAULT 'ME_LONG_V2_INVERSE_SHORT_V2',
  protocol_version text NOT NULL,
  protocol_hash text NOT NULL,
  source_scanner text NOT NULL
    CHECK (source_scanner = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2'),
  source_scanner_version text NOT NULL DEFAULT '',
  source_direction text NOT NULL CHECK (source_direction = 'LONG'),
  source_setup_id text NOT NULL DEFAULT '',
  symbol text NOT NULL CHECK (symbol <> ''),
  direction text NOT NULL DEFAULT 'SHORT' CHECK (direction = 'SHORT'),
  entry_timeframe text NOT NULL DEFAULT '5m',
  signal_time timestamptz NOT NULL,
  captured_at timestamptz NOT NULL,
  activation_freeze_ts timestamptz NOT NULL,
  signal_candle_open_time bigint NOT NULL CHECK (signal_candle_open_time > 0),
  source_reference_price numeric,
  source_invalidation_price numeric,
  source_target_price numeric,
  source_target_2 numeric,
  source_score numeric,
  market_regime text,
  capture_route text NOT NULL CHECK (capture_route IN ('PRE_FILTER_SOURCE')),
  -- V2 four-state disposition (V1 only had CAPTURED / ENTERED).
  status text NOT NULL DEFAULT 'CAPTURED'
    CHECK (status IN ('CAPTURED', 'ENTERED', 'EXPIRED', 'CAPACITY_REJECTED')),
  position_key text,
  -- V2 disposition audit: WHY and WHEN the row left CAPTURED.
  status_reason text
    CHECK (status_reason IS NULL OR status_reason IN ('ENTRY_TTL', 'ENTRY_CAP')),
  status_at timestamptz,
  lineage jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  -- STRICT PROSPECTIVE BOUNDARY: no historical backfill by construction.
  CHECK (signal_time > activation_freeze_ts),
  -- A terminal refusal is only meaningful with its cause and timestamp.
  CHECK (
    status NOT IN ('EXPIRED', 'CAPACITY_REJECTED')
    OR (status_reason IS NOT NULL AND status_at IS NOT NULL)
  )
);

COMMENT ON TABLE dds.me_long_v2_inverse_short_v2_event
  IS 'Prospective-only source events of ME_LONG_V2_INVERSE_SHORT_V2; deterministic event_key gives idempotent duplicate delivery; four-state disposition with refusal reason + timestamp; CHECK (signal_time > activation_freeze_ts) forbids historical backfill.';

CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v2_event_status
    ON dds.me_long_v2_inverse_short_v2_event (status, signal_time);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v2_event_symbol
    ON dds.me_long_v2_inverse_short_v2_event (symbol, status);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v2_event_source
    ON dds.me_long_v2_inverse_short_v2_event (source_scanner, symbol, signal_candle_open_time);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v2_event_setup
    ON dds.me_long_v2_inverse_short_v2_event (source_setup_id);

-- ── 3. Isolated SHORT positions (V2) ─────────────────────────
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v2_position (
  position_key text PRIMARY KEY
    CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_V2:%'),
  event_key text NOT NULL UNIQUE
    REFERENCES dds.me_long_v2_inverse_short_v2_event (event_key),
  strategy_id text NOT NULL DEFAULT 'ME_LONG_V2_INVERSE_SHORT_V2',
  symbol text NOT NULL CHECK (symbol <> ''),
  direction text NOT NULL DEFAULT 'SHORT' CHECK (direction = 'SHORT'),
  source_scanner text NOT NULL DEFAULT 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2',
  source_setup_id text NOT NULL DEFAULT '',
  market_regime text,
  source_score numeric,
  -- entry facts (written ONCE at entry, immutable by trigger below)
  entry_market_price numeric NOT NULL CHECK (entry_market_price > 0),
  entry_price numeric NOT NULL CHECK (entry_price > 0),
  entry_fee numeric NOT NULL DEFAULT 0 CHECK (entry_fee >= 0),
  entry_slippage numeric NOT NULL DEFAULT 0 CHECK (entry_slippage >= 0),
  -- frozen SHORT geometry: 10% stop above / 10% target below the entry
  stop_price numeric NOT NULL CHECK (stop_price > 0),
  target_price numeric NOT NULL CHECK (target_price > 0),
  position_size numeric NOT NULL CHECK (position_size > 0),
  risk_usdt numeric NOT NULL DEFAULT 0,
  balance_before numeric NOT NULL,
  entered_at timestamptz NOT NULL,
  deadline_at timestamptz NOT NULL,
  -- status & terminal facts
  status text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
  funding_paid numeric NOT NULL DEFAULT 0,
  funding_periods integer NOT NULL DEFAULT 0 CHECK (funding_periods >= 0),
  highest_since_entry numeric,
  lowest_since_entry numeric,
  mfe numeric NOT NULL DEFAULT 0,
  mae numeric NOT NULL DEFAULT 0,
  exit_price numeric,
  exit_reason text
    CHECK (exit_reason IS NULL OR exit_reason IN
           ('STOP_LOSS', 'STOP_LOSS_GAP', 'TAKE_PROFIT_1', 'TIMEOUT')),
  exit_fee numeric NOT NULL DEFAULT 0,
  slippage numeric NOT NULL DEFAULT 0,
  gross_pnl numeric NOT NULL DEFAULT 0,
  net_pnl numeric NOT NULL DEFAULT 0,
  closed_at timestamptz,
  duration_sec numeric,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  -- the ORIGINAL deadline is entered_at + 7200 minutes
  CHECK (deadline_at > entered_at),
  -- SHORT geometry: stop above entry, target below entry
  CHECK (stop_price > entry_price AND target_price < entry_price),
  -- terminal consistency
  CHECK (
    (status = 'OPEN' AND exit_reason IS NULL AND closed_at IS NULL)
    OR (status = 'CLOSED' AND exit_reason IS NOT NULL AND closed_at IS NOT NULL)
  )
);

COMMENT ON TABLE dds.me_long_v2_inverse_short_v2_position
  IS 'Isolated SHORT paper positions of ME_LONG_V2_INVERSE_SHORT_V2 (namespaced position_key, no shared open_trades[symbol]); entered_at/deadline_at immutable by trigger. V1 positions are a separate, untouched set of rows.';

CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v2_position_status
    ON dds.me_long_v2_inverse_short_v2_position (status, entered_at);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v2_position_symbol
    ON dds.me_long_v2_inverse_short_v2_position (symbol, status);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v2_position_deadline
    ON dds.me_long_v2_inverse_short_v2_position (deadline_at)
    WHERE status = 'OPEN';
-- At most one OPEN row per position_key (mirrors migration 085).
CREATE UNIQUE INDEX IF NOT EXISTS
    uq_me_inverse_short_v2_position_open
    ON dds.me_long_v2_inverse_short_v2_position (position_key, status)
    WHERE status = 'OPEN';

-- ── 4. Durable close outbox (V2, same contract as migration 086) ──
-- The contour stages the EXACT terminal snapshot of a position in this
-- table BEFORE the position row flips to CLOSED.  If the status UPDATE
-- fails (DB hiccup) or the process restarts mid-close, the snapshot is
-- replayed byte-for-byte on recovery — same position_key, closed_at,
-- exit_reason, exit_price, PnL and funding — so an exit can never lose
-- its facts, be recomputed by STOP/TP/TIMEOUT, or be booked twice.
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v2_close_outbox (
    position_key TEXT PRIMARY KEY
        CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_V2:%'),
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

COMMENT ON TABLE dds.me_long_v2_inverse_short_v2_close_outbox
  IS 'ME_LONG_V2_INVERSE_SHORT_V2 durable terminal-close staging (exact snapshot BEFORE the position row flips to CLOSED).';

-- ── 5. Immutability guard for entry facts + deadline (V2) ────
-- Restart recovery and every UPDATE must preserve the original entry
-- time and the original deadline; this trigger makes a rewrite a hard
-- database error instead of a silent drift.
CREATE OR REPLACE FUNCTION dds.fn_me_long_v2_inverse_short_v2_guard()
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
     OR NEW.position_size IS DISTINCT FROM OLD.position_size THEN
    RAISE EXCEPTION
      'me_long_v2_inverse_short_v2: entry facts and deadline are immutable '
      '(position_key=%, entered_at=%, deadline_at=%)',
      NEW.position_key, NEW.entered_at, NEW.deadline_at;
  END IF;

  IF OLD.status = 'CLOSED' THEN
    IF NEW.status <> 'CLOSED'
       OR NEW.exit_reason IS DISTINCT FROM OLD.exit_reason
       OR NEW.net_pnl IS DISTINCT FROM OLD.net_pnl
       OR NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
      RAISE EXCEPTION
        'me_long_v2_inverse_short_v2: closed position is terminal (position_key=%)',
        OLD.position_key;
    END IF;
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_me_long_v2_inverse_short_v2_guard
BEFORE UPDATE ON dds.me_long_v2_inverse_short_v2_position
FOR EACH ROW
EXECUTE FUNCTION dds.fn_me_long_v2_inverse_short_v2_guard();

COMMIT;
