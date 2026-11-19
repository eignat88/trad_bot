-- ============================================================
-- Migration 091: ME_LONG_V2_INVERSE_SHORT_OOS2_V2 (Issue #287
--                ME_INVERSE_SHORT_OOS2_ENTRY_GUARDS_AND_CAPACITY_V1)
--                — OOS-2 v2 entry guards and capacity
-- ============================================================
-- The OOS-2 v2 successor contour of the FROZEN ME_LONG_V2_INVERSE_SHORT_OOS2_V1
-- (source, detector and execution geometry IDENTICAL to OOS-2 v1) with the
-- Issue #287 entry guards and capacity:
--
--   * max 12 OPEN logical positions counting the OOS-2 family (OPEN legacy
--     v1 + OPEN v2; a partially closed TP1/TP2 position is still OPEN and
--     keeps its slot).  V2/V3 positions never count;
--   * max 1 OPEN logical position per symbol within the OOS-2 family
--     (legacy v1 duplicates are only ever allowed to run out);
--   * 12h per-symbol entry cooldown from the last COMMITTED entered_at
--     (OOS-2 family, any status — a CLOSED position keeps cooling its symbol
--     down; refused signals never write entered_at and never extend it).
--
-- This migration is ADDITIVE and IDEMPOTENT (CREATE ... IF NOT EXISTS):
-- it NEVER touches the frozen OOS-2 v1 tables (088/089), V1/V2/V3 or any
-- production object, and it never fails on historical duplicate OPEN rows
-- of the legacy ledger (which it does not read or write at all).
--
-- Tables created (all brand-new, in the existing dds schema; structurally
-- the union of migrations 088 + 089 with OOS-2 v2 identity, the Issue #287
-- five-state event disposition and the guard indexes):
--
--   dds.me_long_v2_inverse_short_oos2_v2_activation  — activation lifecycle
--       (status + protocol_fingerprint + freeze_ts).  Seeded
--       READY_TO_START with NULL fingerprint/freeze: the contour is inert
--       (NOT_DEPLOYED) until a separate, approval-gated activation step
--       freezes the CURRENT OOS-2 v2 protocol fingerprint together with a
--       FRESH prospective-only freeze_ts.  The frozen OOS-2 v1 activation
--       row and its freeze_ts are never touched.
--
--   dds.me_long_v2_inverse_short_oos2_v2_event       — source-event ledger
--       (append-only, idempotent on the deterministic event_key, full
--       lineage JSON back to the source signal).  FIVE-state disposition:
--         CAPTURED          — eligible (age < entry TTL)
--         ENTERED           — a position row exists for this event_key
--         EXPIRED           — TERMINAL, age >= entry TTL (generic cause)
--         CAPACITY_REJECTED — TERMINAL, age >= entry TTL while the OOS-2
--                             family 12-cap was the binding refusal
--         SYMBOL_REJECTED   — TERMINAL (Issue #287): refused at decision
--                             time by SINGLE_ACTIVE_SYMBOL
--                             (SYMBOL_ALREADY_OPEN) or the 12h per-symbol
--                             entry cooldown (SYMBOL_COOLDOWN_12H).  The
--                             event is NEVER left CAPTURED for a later
--                             replay — after the cooldown a NEW eligible
--                             signal is required.
--       Terminal refusals must carry BOTH the cause (status_reason in
--       ('ENTRY_TTL','ENTRY_CAP','SYMBOL_ALREADY_OPEN',
--       'SYMBOL_COOLDOWN_12H')) and the decision timestamp (status_at).
--       CHECK (signal_time > activation_freeze_ts) makes historical
--       backfill impossible by construction.
--
--   dds.me_long_v2_inverse_short_oos2_v2_position    — isolated SHORT paper
--       positions keyed by the NAMESPACED position_key
--       ('ME_LONG_V2_INVERSE_SHORT_OOS2_V2:<event_key>') so the contour can
--       never collide with OOS-2 v1, with V1/V2/V3, with the shared
--       production Paper engine state, or with any other strategy's state.
--       entered_at / deadline_at / position_size and the entry geometry are
--       written once at entry and enforced IMMUTABLE by the trigger below.
--
--   dds.me_long_v2_inverse_short_oos2_v2_close_leg   — durable leg ledger
--       (partial TP1/TP2 + terminal), PRIMARY KEY (position_key, leg_seq).
--   dds.me_long_v2_inverse_short_oos2_v2_close_outbox — durable close
--       staging (exact terminal snapshot BEFORE the status flip).
--   dds.me_long_v2_inverse_short_oos2_v2_funding_obligation — observed-rate
--       funding obligations (migration 089 contract).
--
-- Issue #287 guard support:
--
--   * uq_me_inverse_short_oos2_v2_position_symbol_open — partial UNIQUE
--     index UNIQUE (symbol) WHERE status = 'OPEN': at most ONE OPEN v2
--     position per symbol, enforced by the database even for concurrent,
--     different event_keys.  (The legacy OOS-2 v1 duplicate OPEN rows are
--     NOT part of this table and can never break this migration; the
--     admission transaction additionally reads the legacy ledger
--     READ-ONLY, so no new duplicate can be created across the family.)
--   * uq_me_inverse_short_oos2_v2_position_key_open — partial UNIQUE
--     index UNIQUE (position_key) WHERE status = 'OPEN' (defence in depth
--     on top of the PRIMARY KEY).
--   * idx_me_inverse_short_oos2_v2_position_symbol_entered — the 12h
--     cooldown lookup (symbol, entered_at DESC).
--
-- Production note: this migration is NOT applied to any production
-- database by this task; application is a separately approved cutover
-- step (see docs/me_inverse_short_oos2_entry_guards_v1.md).
-- ============================================================
-- Issue #276 (ME Inverse SHORT OOS-2): a NEW prospective-only paper
-- contour with the frozen three-stage PARTIAL take-profit ladder
-- (TP1 +5% -> 50% of original, TP2 +7% -> 75% of then-remaining,
-- TP3 +10% -> all remaining, SL -10% on all remaining), a 120-minute
-- entry-signal TTL, a 7200-minute max hold and an OOS-2-LOCAL capacity
-- of 12 concurrent OPEN positions.
--
-- This is a NEW experiment, not a retroactive parameter edit: it ships
-- with its own experiment row, its own freeze_ts, its own protocol
-- fingerprint and its own clean prospective holdout.  V1 and V2 are
-- NEVER modified by this file.
--
-- Tables created (all brand-new, in the existing dds schema; the file
-- is structurally migration 087 with OOS-2 identity + the partial-ladder
-- extensions, plus one brand-new leg ledger):
--
--   dds.me_long_v2_inverse_short_oos2_v2_activation — activation lifecycle
--       (status + protocol_fingerprint + freeze_ts).  Seeded
--       READY_TO_START with NULL fingerprint/freeze: the contour is inert
--       (NOT_DEPLOYED) until a separate activation step freezes the
--       CURRENT OOS-2 protocol fingerprint together with freeze_ts.
--
--   dds.me_long_v2_inverse_short_oos2_v2_event — source-event ledger
--       (append-only, idempotent on the deterministic event_key, full
--       lineage JSON back to the source signal).  FOUR-state disposition:
--         CAPTURED          — eligible (age < entry TTL)
--         ENTERED           — a position row exists for this event_key
--         EXPIRED           — TERMINAL, age >= entry TTL (generic cause)
--         CAPACITY_REJECTED — TERMINAL, age >= entry TTL while the
--                             OOS-2-LOCAL capacity (12) was the binding
--                             refusal
--       Terminal refusals must carry BOTH the cause (status_reason in
--       ('ENTRY_TTL','ENTRY_CAP')) and the decision timestamp (status_at).
--       CHECK (signal_time > activation_freeze_ts) makes historical
--       backfill impossible by construction.
--
--   dds.me_long_v2_inverse_short_oos2_v2_position — isolated SHORT paper
--       positions keyed by the NAMESPACED position_key
--       ('ME_LONG_V2_INVERSE_SHORT_OOS2_V2:<event_key>') so the contour
--       can never collide with V1, with V2, with the shared production
--       Paper engine state, or with any other strategy's state.
--       entered_at / deadline_at / position_size (the ORIGINAL quantity)
--       and the entry geometry (stop, tp1=target, tp2, tp3) are written
--       once at entry and are enforced IMMUTABLE by the trigger below.
--       Partial-ladder state (Issue #276): remaining_size, the once-only
--       tp1/tp2/tp3 flags, realized_legs_net (cumulative
--       gross − exit fee − exit slippage of committed legs) and
--       entry_cost_allocated (Σ leg entry-cost shares).  Invariants:
--         * 0 <= remaining_size <= position_size, and remaining_size = 0
--           for CLOSED rows (> 0 while OPEN — no negative remainder, no
--           double close can ever be stored);
--         * tp2 => tp1, tp3 => tp2, tp3 => terminal;
--         * the OPEN remainder matches the exact ladder multiple
--           (100% / 50% / 12.5% of the original) within a 1e-6 relative
--           tolerance (float rounding / dust absorption).
--
--   dds.me_long_v2_inverse_short_oos2_v2_close_leg — DURABLE close-leg
--       ledger (Issue #276, brand new): one row per executed leg, either
--       a PARTIAL take-profit leg (TP1 seq=1 / TP2 seq=2 — the position
--       STAYS OPEN and keeps its capacity slot) or the TERMINAL leg
--       (seq=3: TP3 / STOP_LOSS / STOP_LOSS_GAP / TIMEOUT — the position
--       flips CLOSED).  PRIMARY KEY (position_key, leg_seq) is the
--       exactly-once identity: ON CONFLICT DO NOTHING makes a retried
--       dirty leg, a re-issued ambiguous COMMIT and a terminal re-stage
--       resolve against the existing row — never a second booking of
--       quantity, fee or PnL.
--
--   dds.me_long_v2_inverse_short_oos2_v2_close_outbox — durable close
--       staging (exact terminal snapshot BEFORE the status flip), same
--       contract as V1 migration 086 / V2 migration 087; the terminal
--       leg row is staged in the SAME transaction as this snapshot, so
--       the leg ledger can never lag the position flip.
--
-- Migration-number safety: 088 is the next free number after
-- 087_me_long_v2_inverse_short_v2.sql.
--
-- WHAT THIS MIGRATION NEVER DOES (acceptance: V1/V2 isolation):
--   * never ALTERs, DROPs or writes into any V1/V2 table
--     (dds.me_long_v2_inverse_short_v1_*, dds.me_long_v2_inverse_short_v2_*)
--     or any production/research table (dds.paper_trade,
--     dds.paper_shadow_trade, dds.scanner_setup, dds.signal_outcome,
--     research.*);
--   * never touches the V1/V2 activation rows, fingerprints or freeze_ts;
--   * never activates anything (seeded READY_TO_START, fail-closed);
--   * never backfills: no event row can be older than its own frozen
--     activation boundary.
--
-- This migration is source-controlled only. It is NOT applied to any
-- database by this task (deploy / PostgreSQL changes are a separately
-- approved stage).
-- ============================================================

BEGIN;

-- ── 1. Activation lifecycle record (OOS-2) ───────────────────
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_oos2_v2_activation (
  experiment_id text PRIMARY KEY
    CHECK (experiment_id = 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2'),
  protocol_version text NOT NULL DEFAULT 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2',
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

COMMENT ON TABLE dds.me_long_v2_inverse_short_oos2_v2_activation
  IS 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2 (Issue #276) activation: RUNNING + frozen OOS-2 protocol_fingerprint + freeze_ts are both required before any capture or entry (fail-closed, prospective-only, paper-only). The V1 and V2 activation rows are separate, untouched rows.';

INSERT INTO dds.me_long_v2_inverse_short_oos2_v2_activation (experiment_id, status)
VALUES ('ME_LONG_V2_INVERSE_SHORT_OOS2_V2', 'READY_TO_START')
ON CONFLICT DO NOTHING;

-- ── 2. Source-event ledger (OOS-2, five-state disposition) ──
-- One row per ORIGINAL MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 detector event,
-- captured BEFORE the trading direction gate and the expectancy filter
-- (both untouched by this contour).
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_oos2_v2_event (
  event_key text PRIMARY KEY,
  strategy_id text NOT NULL DEFAULT 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2',
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
  -- Four-state disposition (same contract as V2).
  status text NOT NULL DEFAULT 'CAPTURED'
    CHECK (status IN ('CAPTURED', 'ENTERED', 'EXPIRED', 'CAPACITY_REJECTED',
                      'SYMBOL_REJECTED')),
  position_key text,
  -- Disposition audit: WHY and WHEN the row left CAPTURED.
  status_reason text
    CHECK (status_reason IS NULL OR status_reason IN
           ('ENTRY_TTL', 'ENTRY_CAP', 'SYMBOL_ALREADY_OPEN',
            'SYMBOL_COOLDOWN_12H')),
  status_at timestamptz,
  lineage jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  -- STRICT PROSPECTIVE BOUNDARY: no historical backfill by construction.
  CHECK (signal_time > activation_freeze_ts)
  -- The terminal-refusal audit rule (cause + timestamp for EVERY terminal
  -- disposition, SYMBOL_REJECTED included) is the NAMED constraint
  -- me_long_v2_inverse_short_oos2_v2_event_terminal_refusal_check, created
  -- idempotently right below so re-applying this migration converges even
  -- on a table created by an earlier revision of it.
);

COMMENT ON TABLE dds.me_long_v2_inverse_short_oos2_v2_event
  IS 'Prospective-only source events of ME_LONG_V2_INVERSE_SHORT_OOS2_V2 (Issues #276/#287); deterministic event_key gives idempotent duplicate delivery; five-state disposition with refusal reason + timestamp (SYMBOL_REJECTED = terminal SINGLE_ACTIVE_SYMBOL / 12h-cooldown refusal); CHECK (signal_time > activation_freeze_ts) forbids historical backfill.';
-- Terminal-refusal audit rule as a NAMED, idempotently re-appliable
-- constraint (Issue #287): every TERMINAL disposition вЂ” EXPIRED,
-- CAPACITY_REJECTED and SYMBOL_REJECTED alike вЂ” must carry its cause AND
-- its decision timestamp.  DROP + ADD makes re-application converge even
-- on a table created by an earlier revision of this migration.
ALTER TABLE dds.me_long_v2_inverse_short_oos2_v2_event
  DROP CONSTRAINT IF EXISTS
    me_long_v2_inverse_short_oos2_v2_event_terminal_refusal_check;
ALTER TABLE dds.me_long_v2_inverse_short_oos2_v2_event
  ADD CONSTRAINT me_long_v2_inverse_short_oos2_v2_event_terminal_refusal_check
  CHECK (
    status NOT IN ('EXPIRED', 'CAPACITY_REJECTED', 'SYMBOL_REJECTED')
    OR (status_reason IS NOT NULL AND status_at IS NOT NULL)
  );

CREATE INDEX IF NOT EXISTS idx_me_inverse_short_oos2_v2_event_status
    ON dds.me_long_v2_inverse_short_oos2_v2_event (status, signal_time);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_oos2_v2_event_symbol
    ON dds.me_long_v2_inverse_short_oos2_v2_event (symbol, status);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_oos2_v2_event_source
    ON dds.me_long_v2_inverse_short_oos2_v2_event (source_scanner, symbol, signal_candle_open_time);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_oos2_v2_event_setup
    ON dds.me_long_v2_inverse_short_oos2_v2_event (source_setup_id);

-- ── 3. Isolated SHORT positions (OOS-2, partial TP ladder) ───
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_oos2_v2_position (
  position_key text PRIMARY KEY
    CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2:%'),
  event_key text NOT NULL UNIQUE
    REFERENCES dds.me_long_v2_inverse_short_oos2_v2_event (event_key),
  strategy_id text NOT NULL DEFAULT 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2',
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
  -- frozen OOS-2 SHORT geometry: -10% stop above / ladder 5/7/10% below
  -- the ACTUAL entry price (price movement, not ROE)
  stop_price numeric NOT NULL CHECK (stop_price > 0),
  target_price numeric NOT NULL CHECK (target_price > 0),  -- TP1 = entry*0.95
  tp2_price numeric NOT NULL CHECK (tp2_price > 0),        -- TP2 = entry*0.93
  tp3_price numeric NOT NULL CHECK (tp3_price > 0),        -- TP3 = entry*0.90
  position_size numeric NOT NULL CHECK (position_size > 0),  -- ORIGINAL qty
  remaining_size numeric NOT NULL DEFAULT 0,                  -- live qty
  risk_usdt numeric NOT NULL DEFAULT 0,
  balance_before numeric NOT NULL,
  entered_at timestamptz NOT NULL,
  deadline_at timestamptz NOT NULL,
  -- status & terminal facts
  status text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
  -- once-only partial TP stage flags (Issue #276)
  tp1_executed boolean NOT NULL DEFAULT FALSE,
  tp2_executed boolean NOT NULL DEFAULT FALSE,
  tp3_executed boolean NOT NULL DEFAULT FALSE,
  -- cumulative accounting of committed legs
  realized_legs_net numeric NOT NULL DEFAULT 0,
  entry_cost_allocated numeric NOT NULL DEFAULT 0
    CHECK (entry_cost_allocated >= 0),
  funding_paid numeric NOT NULL DEFAULT 0,
  funding_periods integer NOT NULL DEFAULT 0 CHECK (funding_periods >= 0),
  highest_since_entry numeric,
  lowest_since_entry numeric,
  mfe numeric NOT NULL DEFAULT 0,
  mae numeric NOT NULL DEFAULT 0,
  -- Last observed mark price (written once per cycle): lets the dashboard
  -- compute UNREALIZED PnL — (entry_price − last_mark_price) ×
  -- remaining_size — directly from this row.
  last_mark_price numeric CHECK (last_mark_price IS NULL OR last_mark_price > 0),
  exit_price numeric,
  exit_reason text
    CHECK (exit_reason IS NULL OR exit_reason IN
           ('STOP_LOSS', 'STOP_LOSS_GAP', 'TAKE_PROFIT_1', 'TAKE_PROFIT_2',
            'TAKE_PROFIT_3', 'TIMEOUT')),
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
  -- OOS-2 SHORT geometry: stop above entry, ladder strictly falling below
  CHECK (stop_price > entry_price),
  CHECK (target_price < entry_price),
  CHECK (tp2_price < target_price),
  CHECK (tp3_price < tp2_price),
  -- partial-ladder quantity invariants (Issue #276): never negative, never
  -- more than the immutable original (no overclose), zero only when CLOSED.
  CHECK (remaining_size >= 0 AND remaining_size <= position_size),
  CHECK (entry_cost_allocated <= entry_fee + entry_slippage + 0.000000001),
  -- stage flags are monotone; TP3 is terminal-only
  CHECK (NOT tp2_executed OR tp1_executed),
  CHECK (NOT tp3_executed OR tp2_executed),
  -- the OPEN remainder is exactly the ladder multiple of the original
  -- (100% pre-TP1, 50% after TP1, 12.5% after TP2), tolerant to the
  -- HALF-EVEN rounding / dust policy of the frozen spec
  CHECK (
    status <> 'OPEN'
    OR (
      CASE
        WHEN tp2_executed THEN abs(remaining_size - position_size * 0.125)
        WHEN tp1_executed THEN abs(remaining_size - position_size * 0.5)
        ELSE remaining_size - position_size
      END
      <= GREATEST(position_size * 0.000001, 0.000000001)
    )
  ),
  -- terminal consistency: a CLOSED row has fully exited (remaining 0, TP3
  -- or protective/time reason recorded); an OPEN row never carries TP3.
  CHECK (
    (status = 'OPEN'
      AND exit_reason IS NULL AND closed_at IS NULL
      AND remaining_size > 0 AND NOT tp3_executed)
    OR (status = 'CLOSED'
      AND exit_reason IS NOT NULL AND closed_at IS NOT NULL
      AND remaining_size = 0)
  )
);

COMMENT ON TABLE dds.me_long_v2_inverse_short_oos2_v2_position
  IS 'Isolated SHORT paper positions of ME_LONG_V2_INVERSE_SHORT_OOS2_V2 (Issue #276): immutable original position_size + entry geometry (stop/tp1/tp2/tp3), live remaining_size, once-only stage flags; entered_at/deadline_at/position_size immutable by trigger. V1/V2 positions are separate, untouched rows.';

CREATE INDEX IF NOT EXISTS idx_me_inverse_short_oos2_v2_position_status
    ON dds.me_long_v2_inverse_short_oos2_v2_position (status, entered_at);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_oos2_v2_position_symbol
    ON dds.me_long_v2_inverse_short_oos2_v2_position (symbol, status);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_oos2_v2_position_deadline
    ON dds.me_long_v2_inverse_short_oos2_v2_position (deadline_at)
    WHERE status = 'OPEN';
-- At most one OPEN row per position_key (mirrors migrations 085/087).
CREATE UNIQUE INDEX IF NOT EXISTS
    uq_me_inverse_short_oos2_v2_position_open
    ON dds.me_long_v2_inverse_short_oos2_v2_position (position_key, status)
    WHERE status = 'OPEN';

-- Issue #287 guard indexes:
--   * at most ONE OPEN v2 position per symbol, enforced by the DATABASE
--     even for concurrent distinct event_keys (the admission advisory lock
--     is the primary defence; this is the schema-level backstop).  The
--     legacy OOS-2 v1 duplicate OPEN rows live in a DIFFERENT table and can
--     never break this index — the admission transaction reads that ledger
--     READ-ONLY so no new family duplicate can be created;
--   * the 12h cooldown lookup (symbol, entered_at DESC).
CREATE UNIQUE INDEX IF NOT EXISTS
    uq_me_inverse_short_oos2_v2_position_symbol_open
    ON dds.me_long_v2_inverse_short_oos2_v2_position (symbol)
    WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS
    idx_me_inverse_short_oos2_v2_position_symbol_entered
    ON dds.me_long_v2_inverse_short_oos2_v2_position (symbol, entered_at DESC);

-- ── 4. Durable close-leg ledger (Issue #276, brand new) ──────
-- One row per executed leg: a PARTIAL take-profit leg (position stays
-- OPEN, keeps its capacity slot, entry facts unchanged) or the TERMINAL
-- leg (position flips CLOSED).  PRIMARY KEY (position_key, leg_seq) is
-- the exactly-once identity of every leg commit — a replayed dirty leg,
-- a re-issued ambiguous COMMIT or a terminal re-stage all resolve against
-- the existing row instead of booking quantity/fee/PnL twice.
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_oos2_v2_close_leg (
  position_key TEXT NOT NULL
    CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2:%'),
  leg_seq INTEGER NOT NULL CHECK (leg_seq IN (1, 2, 3)),
  event_key TEXT NOT NULL DEFAULT '',
  symbol TEXT NOT NULL CHECK (symbol <> ''),
  exit_reason TEXT NOT NULL
    CHECK (exit_reason IN
           ('TAKE_PROFIT_1', 'TAKE_PROFIT_2', 'TAKE_PROFIT_3',
            'STOP_LOSS', 'STOP_LOSS_GAP', 'TIMEOUT')),
  is_terminal BOOLEAN NOT NULL,
  quantity NUMERIC NOT NULL CHECK (quantity > 0),
  remaining_after NUMERIC NOT NULL CHECK (remaining_after >= 0),
  reference_price NUMERIC NOT NULL CHECK (reference_price > 0),
  fill_price NUMERIC NOT NULL CHECK (fill_price > 0),
  gross_pnl NUMERIC NOT NULL DEFAULT 0,
  exit_fee NUMERIC NOT NULL DEFAULT 0 CHECK (exit_fee >= 0),
  exit_slippage NUMERIC NOT NULL DEFAULT 0 CHECK (exit_slippage >= 0),
  entry_cost_share NUMERIC NOT NULL DEFAULT 0,
  funding_paid_to_date NUMERIC NOT NULL DEFAULT 0,
  net_pnl NUMERIC NOT NULL DEFAULT 0,
  cumulative_realized_net NUMERIC NOT NULL DEFAULT 0,
  executed_at TIMESTAMPTZ NOT NULL,
  status_after TEXT NOT NULL CHECK (status_after IN ('OPEN', 'CLOSED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (position_key, leg_seq),
  -- seq identity: TP1=1, TP2=2, terminal=3
  CHECK (
    (leg_seq = 1 AND exit_reason = 'TAKE_PROFIT_1' AND NOT is_terminal AND status_after = 'OPEN')
    OR (leg_seq = 2 AND exit_reason = 'TAKE_PROFIT_2' AND NOT is_terminal AND status_after = 'OPEN')
    OR (leg_seq = 3 AND is_terminal AND status_after = 'CLOSED')
  ),
  -- a partial leg never clears the remainder, a terminal leg always does
  CHECK (is_terminal OR remaining_after > 0),
  CHECK (NOT is_terminal OR remaining_after = 0)
);

COMMENT ON TABLE dds.me_long_v2_inverse_short_oos2_v2_close_leg
  IS 'Durable close-leg ledger of ME_LONG_V2_INVERSE_SHORT_OOS2_V2 (Issue #276): partial TP1/TP2 legs (position stays OPEN) and the terminal leg; PRIMARY KEY (position_key, leg_seq) is the exactly-once identity — replayed or redelivered commits can never book a leg twice.';

CREATE INDEX IF NOT EXISTS idx_me_inverse_short_oos2_v2_leg_executed
    ON dds.me_long_v2_inverse_short_oos2_v2_close_leg (executed_at, position_key);
CREATE INDEX IF NOT EXISTS idx_me_inverse_short_oos2_v2_leg_reason
    ON dds.me_long_v2_inverse_short_oos2_v2_close_leg (exit_reason, executed_at);

-- ── 5. Durable close outbox (OOS-2, same contract as 086/087) ─
-- The contour stages the EXACT terminal snapshot of a position in this
-- table BEFORE the position row flips to CLOSED — together with the
-- terminal leg row (one transaction).  If the status UPDATE fails (DB
-- hiccup) or the process restarts mid-close, the snapshot is replayed
-- byte-for-byte on recovery — same position_key, closed_at, exit_reason,
-- exit_price, PnL and funding — so an exit can never lose its facts, be
-- recomputed by STOP/TP/TIMEOUT, or be booked twice.
CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_oos2_v2_close_outbox (
    position_key TEXT PRIMARY KEY
        CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2:%'),
    symbol TEXT NOT NULL CHECK (symbol <> ''),
    exit_reason TEXT NOT NULL
        CHECK (exit_reason IN
               ('STOP_LOSS', 'STOP_LOSS_GAP', 'TAKE_PROFIT_1', 'TAKE_PROFIT_2',
                'TAKE_PROFIT_3', 'TIMEOUT')),
    closed_at TIMESTAMPTZ NOT NULL,
    -- Full InverseShortPosition.to_dict() payload: every immutable entry
    -- fact (entered_at, deadline_at, entry geometry) plus the terminal
    -- exit facts needed to re-run the position UPDATE unchanged.
    snapshot JSONB NOT NULL,
    staged_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE dds.me_long_v2_inverse_short_oos2_v2_close_outbox
  IS 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2 durable terminal-close staging (exact snapshot BEFORE the position row flips to CLOSED; terminal leg staged in the same transaction).';

-- ── 6. Immutability guard for entry facts + deadline (OOS-2) ──
-- Restart recovery and every UPDATE must preserve the original entry
-- time, the original deadline, the ORIGINAL quantity and the frozen
-- entry geometry; this trigger makes a rewrite a hard database error
-- instead of a silent drift.  It additionally guards the partial-ladder
-- monotonicity required by Issue #276: the remainder only ever shrinks,
-- stage flags only ever turn ON, and a position never un-closes.
CREATE OR REPLACE FUNCTION dds.fn_me_long_v2_inverse_short_oos2_v2_guard()
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
      'me_long_v2_inverse_short_oos2_v2: entry facts and deadline are immutable '
      '(position_key=%, entered_at=%, deadline_at=%)',
      NEW.position_key, NEW.entered_at, NEW.deadline_at;
  END IF;

  -- Partial-ladder monotonicity (Issue #276): the remainder never grows
  -- (no re-opening, no double close), stage flags never regress.
  IF NEW.remaining_size > OLD.remaining_size THEN
    RAISE EXCEPTION
      'me_long_v2_inverse_short_oos2_v2: remaining_size may only shrink '
      '(position_key=%, % -> %)',
      NEW.position_key, OLD.remaining_size, NEW.remaining_size;
  END IF;
  IF OLD.tp1_executed AND NOT NEW.tp1_executed
     OR OLD.tp2_executed AND NOT NEW.tp2_executed
     OR OLD.tp3_executed AND NOT NEW.tp3_executed THEN
    RAISE EXCEPTION
      'me_long_v2_inverse_short_oos2_v2: executed TP stages are permanent '
      '(position_key=%)',
      NEW.position_key;
  END IF;

  IF OLD.status = 'CLOSED' THEN
    IF NEW.status <> 'CLOSED'
       OR NEW.exit_reason IS DISTINCT FROM OLD.exit_reason
       OR NEW.net_pnl IS DISTINCT FROM OLD.net_pnl
       OR NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
      RAISE EXCEPTION
        'me_long_v2_inverse_short_oos2_v2: closed position is terminal (position_key=%)',
        NEW.position_key;
    END IF;
  END IF;

  RETURN NEW;
END;
$$;

-- The trigger is DROPped first so re-applying this migration is idempotent
-- (parity with migration 090's guard-trigger pattern).
DROP TRIGGER IF EXISTS trg_me_long_v2_inverse_short_oos2_v2_guard
    ON dds.me_long_v2_inverse_short_oos2_v2_position;
CREATE TRIGGER trg_me_long_v2_inverse_short_oos2_v2_guard
BEFORE UPDATE ON dds.me_long_v2_inverse_short_oos2_v2_position
FOR EACH ROW
EXECUTE FUNCTION dds.fn_me_long_v2_inverse_short_oos2_v2_guard();

COMMIT;


BEGIN;

CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_oos2_v2_funding_obligation (
  position_key TEXT PRIMARY KEY
    CHECK (position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_OOS2_V2:%'),
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

COMMENT ON TABLE dds.me_long_v2_inverse_short_oos2_v2_funding_obligation
  IS 'OOS-2 durable funding obligation (Issue #276, round 6): observed funding rate + observation window persisted when close-leg history was unreadable at a terminal close; settled exactly once by the recovery pass when history is readable again. OOS-2 namespace only; additive to migration 088.';

CREATE INDEX IF NOT EXISTS
    idx_me_inverse_short_oos2_v2_funding_obligation_status
    ON dds.me_long_v2_inverse_short_oos2_v2_funding_obligation (status)
    WHERE status = 'OPEN';

-- ---------------------------------------------------------------------------
-- Guard refinement (same transaction): CREATE OR REPLACE of the 088 guard.
-- The trigger trg_me_long_v2_inverse_short_oos2_v2_guard keeps executing
-- this function — no trigger is dropped or recreated here.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dds.fn_me_long_v2_inverse_short_oos2_v2_guard()
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
      'me_long_v2_inverse_short_oos2_v2: entry facts and deadline are immutable '
      '(position_key=%, entered_at=%, deadline_at=%)',
      NEW.position_key, NEW.entered_at, NEW.deadline_at;
  END IF;

  -- Partial-ladder monotonicity (Issue #276): the remainder never grows
  -- (no re-opening, no double close), stage flags never regress.
  IF NEW.remaining_size > OLD.remaining_size THEN
    RAISE EXCEPTION
      'me_long_v2_inverse_short_oos2_v2: remaining_size may only shrink '
      '(position_key=%, % -> %)',
      NEW.position_key, OLD.remaining_size, NEW.remaining_size;
  END IF;
  IF OLD.tp1_executed AND NOT NEW.tp1_executed
     OR OLD.tp2_executed AND NOT NEW.tp2_executed
     OR OLD.tp3_executed AND NOT NEW.tp3_executed THEN
    RAISE EXCEPTION
      'me_long_v2_inverse_short_oos2_v2: executed TP stages are permanent '
      '(position_key=%)',
      NEW.position_key;
  END IF;

  IF OLD.status = 'CLOSED' THEN
    IF NEW.status <> 'CLOSED'
       OR NEW.exit_reason IS DISTINCT FROM OLD.exit_reason
       OR NEW.closed_at IS DISTINCT FROM OLD.closed_at THEN
      RAISE EXCEPTION
        'me_long_v2_inverse_short_oos2_v2: closed position is terminal (position_key=%)',
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
        'me_long_v2_inverse_short_oos2_v2: funding_periods may only advance on a closed position (position_key=%, % -> %)',
        NEW.position_key, OLD.funding_periods, NEW.funding_periods;
    END IF;
    IF (NEW.net_pnl IS DISTINCT FROM OLD.net_pnl
        OR NEW.funding_paid IS DISTINCT FROM OLD.funding_paid)
       AND NEW.funding_periods <= OLD.funding_periods THEN
      RAISE EXCEPTION
        'me_long_v2_inverse_short_oos2_v2: closed position is terminal (position_key=%)',
        NEW.position_key;
    END IF;
  END IF;

  RETURN NEW;
END;
$$;

COMMIT;
