-- ============================================================
-- Migration 085: ME_LONG_V2_INVERSE_SHORT_V1 independent
--                prospective SHORT paper contour
-- ============================================================
-- The contour trades the INVERSE (SHORT) side of the ORIGINAL
-- MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 detector events under a frozen
-- protocol: TP 10% / SL 10% of the ACTUAL entry price, max hold
-- 7200 minutes, DCA / trailing / breakeven OFF, prospective-only,
-- paper-only, NO historical backfill.
--
-- Tables created (all in the existing dds paper schema, completely
-- separate from dds.paper_trade / dds.paper_shadow_trade):
--
--   dds.me_long_v2_inverse_short_v1_activation  — activation lifecycle
--       (status + protocol_fingerprint + freeze_ts).  Seeded
--       READY_TO_START with NULL fingerprint/freeze: the contour is
--       inert (NOT_DEPLOYED) until a separate activation step freezes
--       the CURRENT protocol fingerprint together with freeze_ts.
--
--   dds.me_long_v2_inverse_short_v1_event       — source-event ledger
--       (append-only, idempotent on the deterministic event_key, full
--       lineage JSON back to the source signal).  CHECK
--       (signal_time > activation_freeze_ts) makes historical backfill
--       impossible by construction.
--
--   dds.me_long_v2_inverse_short_v1_position    — isolated SHORT paper
--       positions keyed by the NAMESPACED position_key
--       ('ME_LONG_V2_INVERSE_SHORT_V1:<event_key>') so the contour can
--       never collide with the shared open_trades[symbol] state of the
--       production Paper engine.  entered_at / deadline_at are written
--       once at entry and are enforced IMMUTABLE by trigger below, so
--       restart recovery always restores the original entry time and
--       the original 7200-minute deadline.
--
-- WHAT THIS MIGRATION NEVER DOES:
--   * never touches dds.paper_trade, dds.paper_shadow_trade,
--     dds.scanner_setup, dds.signal_outcome or any research.* table;
--   * never changes the MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 direction
--     gate blocklist or any expectancy data;
--   * never activates anything: the seeded activation row is
--     READY_TO_START with protocol_fingerprint NULL and freeze_ts NULL
--     (fail-closed; every capture and every entry refuses to run);
--   * never backfills: no event row can be older than its own frozen
--     activation boundary.
--
-- This migration is source-controlled only. It is NOT applied to
-- production PostgreSQL as part of this task.
-- ============================================================

BEGIN;

-- ── 1. Activation lifecycle record ─────────────────────────────
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v1_activation (
  experiment_id text PRIMARY KEY
    CHECK (experiment_id = 'ME_LONG_V2_INVERSE_SHORT_V1'),
  protocol_version text NOT NULL DEFAULT 'ME_LONG_V2_INVERSE_SHORT_V1',
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

COMMENT ON TABLE dds.me_long_v2_inverse_short_v1_activation
  IS 'ME_LONG_V2_INVERSE_SHORT_V1 activation: RUNNING + frozen protocol_fingerprint + freeze_ts are both required before any capture or entry (fail-closed, prospective-only, paper-only).';

INSERT INTO dds.me_long_v2_inverse_short_v1_activation (experiment_id, status)
VALUES ('ME_LONG_V2_INVERSE_SHORT_V1', 'READY_TO_START')
ON CONFLICT DO NOTHING;

-- ── 2. Source-event ledger (append-only, idempotent) ───────────
-- One row per ORIGINAL MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 detector event,
-- captured BEFORE the trading direction gate and the expectancy filter
-- (both untouched by this contour).
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v1_event (
  event_key text PRIMARY KEY,
  strategy_id text NOT NULL DEFAULT 'ME_LONG_V2_INVERSE_SHORT_V1',
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
  status text NOT NULL DEFAULT 'CAPTURED'
    CHECK (status IN ('CAPTURED', 'ENTERED')),
  position_key text,
  lineage jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  -- STRICT PROSPECTIVE BOUNDARY: no historical backfill by construction.
  CHECK (signal_time > activation_freeze_ts)
);

COMMENT ON TABLE dds.me_long_v2_inverse_short_v1_event
  IS 'Prospective-only source events of ME_LONG_V2_INVERSE_SHORT_V1; deterministic event_key gives idempotent duplicate delivery; CHECK (signal_time > activation_freeze_ts) forbids historical backfill.';

CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v1_event_status
    ON dds.me_long_v2_inverse_short_v1_event (status, signal_time);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v1_event_symbol
    ON dds.me_long_v2_inverse_short_v1_event (symbol, status);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v1_event_source
    ON dds.me_long_v2_inverse_short_v1_event (source_scanner, symbol, signal_candle_open_time);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v1_event_setup
    ON dds.me_long_v2_inverse_short_v1_event (source_setup_id);

-- ── 3. Isolated SHORT paper positions ──────────────────────────
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v1_position (
  position_key text PRIMARY KEY
    CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_V1:%'),
  event_key text NOT NULL UNIQUE
    REFERENCES dds.me_long_v2_inverse_short_v1_event (event_key),
  strategy_id text NOT NULL DEFAULT 'ME_LONG_V2_INVERSE_SHORT_V1',
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

COMMENT ON TABLE dds.me_long_v2_inverse_short_v1_position
  IS 'Isolated SHORT paper positions of ME_LONG_V2_INVERSE_SHORT_V1 (namespaced position_key, no shared open_trades[symbol]); entered_at/deadline_at immutable by trigger.';

CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v1_position_status
    ON dds.me_long_v2_inverse_short_v1_position (status, entered_at);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v1_position_symbol
    ON dds.me_long_v2_inverse_short_v1_position (symbol, status);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_v1_position_deadline
    ON dds.me_long_v2_inverse_short_v1_position (deadline_at)
    WHERE status = 'OPEN';

-- ── 4. Immutability guard for entry facts + deadline ───────────
-- Restart recovery and every UPDATE must preserve the original entry
-- time and the original deadline; this trigger makes a rewrite a hard
-- database error instead of a silent drift.
CREATE OR REPLACE FUNCTION dds.fn_me_long_v2_inverse_short_v1_guard()
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
      'me_long_v2_inverse_short_v1: entry facts and deadline are immutable '
      '(position_key=%, entered_at=%, deadline_at=%)',
      NEW.position_key, NEW.entered_at, NEW.deadline_at;
  END IF;

  IF OLD.status = 'CLOSED' THEN
    IF NEW.status <> 'CLOSED'
       OR NEW.exit_reason IS DISTINCT FROM OLD.exit_reason
       OR NEW.net_pnl IS DISTINCT FROM OLD.net_pnl
       OR NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
      RAISE EXCEPTION
        'me_long_v2_inverse_short_v1: closed position is terminal (position_key=%)',
        OLD.position_key;
    END IF;
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_me_long_v2_inverse_short_v1_guard
BEFORE UPDATE ON dds.me_long_v2_inverse_short_v1_position
FOR EACH ROW
EXECUTE FUNCTION dds.fn_me_long_v2_inverse_short_v1_guard();

COMMIT;
