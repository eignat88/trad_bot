-- ============================================================
-- Migration 082: SRR SHORT Reference-to-Execution Alignment V1
-- Issue #259 (parent) — independent, research-only prospective
-- fill-feasibility capture for SRR SHORT signals.
-- ============================================================
-- SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1
--
-- Purpose: measure, for EVERY new SRR SHORT prospective observation,
-- whether the frozen model entry (reference_price) is still executable
-- against the real Bybit order book at signal time.
--
-- Safety invariants (installation alone changes nothing):
--   * The activation record is seeded DISABLED with activation_ts NULL.
--     No capture can run without BOTH the runtime feature flag
--     ``SRR_SHORT_FILL_CAPTURE_ENABLED=1`` AND an explicit operator
--     activation (DISABLED -> RUNNING with an immutable activation_ts).
--   * No historical backfill: a DB CHECK enforces signal_time > activation_ts.
--   * No scanner / paper / live object is touched. No trading gate,
--     regime filter, scoring, order placement or ME SHORT A behaviour
--     changes. The frozen SRR evaluator and all existing SRR outcomes
--     are untouched (only two brand-new research tables are created).
--   * Evidence rows are append-only: only the PENDING claim row may be
--     finalized once, and DELETE is always prohibited.
--
-- This migration is source-controlled only. It is NOT applied to
-- production PostgreSQL as part of this task.
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research;

-- ── 1. Activation lifecycle record ───────────────────────────
-- Seeded DISABLED with activation_ts NULL: installation can never
-- activate capture. A separate, explicitly reviewed operator step
-- transitions DISABLED -> RUNNING and freezes activation_ts.
-- activation_ts is immutable once set; terminal states are final.

CREATE TABLE IF NOT EXISTS research.srr_short_fill_activation_v1 (
  experiment_id text PRIMARY KEY
    CHECK (experiment_id='SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1'),
  status text NOT NULL
    CHECK (status IN ('DISABLED','RUNNING','COMPLETED','CANCELLED')),
  activation_ts timestamptz,
  notes text,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  CHECK ((status='DISABLED' AND activation_ts IS NULL)
      OR (status<>'DISABLED' AND activation_ts IS NOT NULL))
);

COMMENT ON TABLE research.srr_short_fill_activation_v1
  IS 'Issue #259 SRR SHORT fill capture activation lifecycle; fail-closed on DISABLED / missing activation_ts (Issue #259)';
COMMENT ON COLUMN research.srr_short_fill_activation_v1.activation_ts
  IS 'Immutable activation boundary; only events with signal_time > activation_ts are eligible (no backfill)';

INSERT INTO research.srr_short_fill_activation_v1 (experiment_id, status)
VALUES ('SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1', 'DISABLED')
ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.srr_short_fill_activation_v1'::regclass
          AND tgname = 'trg_srr_short_fill_activation_updated_at'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_srr_short_fill_activation_updated_at
        BEFORE UPDATE ON research.srr_short_fill_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;
END $$;

-- ── 2. Activation state machine and immutability ─────────────
-- Immutable after INSERT: experiment_id.
-- activation_ts: may be set exactly once (DISABLED -> RUNNING); once set
-- it may never change or be cleared. Restarting the Scanner never writes
-- this table (the runtime only reads it).
-- Permitted status transitions:
--   DISABLED -> RUNNING      (explicit operator activation, freezes activation_ts)
--   DISABLED -> CANCELLED    (abandon before activation)
--   RUNNING   -> COMPLETED   (forward protocol closed)
--   RUNNING   -> CANCELLED   (protocol aborted)
-- Terminal states never transition again; no reactivation after any
-- terminal state. DELETE of an activation record is always prohibited.

CREATE OR REPLACE FUNCTION research.fn_guard_srr_short_fill_activation()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id THEN
        RAISE EXCEPTION
            'SRR SHORT fill activation experiment_id is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.activation_ts IS NOT NULL
       AND NEW.activation_ts IS DISTINCT FROM OLD.activation_ts THEN
        RAISE EXCEPTION
            'SRR SHORT fill activation activation_ts is immutable once set: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'DISABLED' AND NEW.status IN ('RUNNING', 'CANCELLED'))
            OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'CANCELLED'))
        ) THEN
            RAISE EXCEPTION
                'SRR SHORT fill activation status transition % → % is not permitted: experiment_id=%',
                OLD.status, NEW.status, OLD.experiment_id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    IF NEW.status = 'RUNNING' AND NEW.activation_ts IS NULL THEN
        RAISE EXCEPTION
            'SRR SHORT fill activation requires an explicit activation_ts: experiment_id=%',
            NEW.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.srr_short_fill_activation_v1'::regclass
          AND tgname = 'trg_srr_short_fill_activation_guard'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_srr_short_fill_activation_guard
        BEFORE UPDATE ON research.srr_short_fill_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_guard_srr_short_fill_activation();
    END IF;
END $$;

CREATE OR REPLACE FUNCTION research.fn_block_srr_short_fill_activation_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    RAISE EXCEPTION
        'SRR SHORT fill activation record cannot be deleted: experiment_id=%',
        OLD.experiment_id
        USING ERRCODE = 'restrict_violation';
END;
$trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.srr_short_fill_activation_v1'::regclass
          AND tgname = 'trg_srr_short_fill_activation_block_delete'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_srr_short_fill_activation_block_delete
        BEFORE DELETE ON research.srr_short_fill_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_block_srr_short_fill_activation_delete();
    END IF;
END $$;

-- ── 3. Signal-time fill feasibility evidence ─────────────────
-- Two-phase write contract:
--   (a) atomic CLAIM insert with feasibility_status='PENDING' and
--       ON CONFLICT DO NOTHING on (event_key, observation_id) — this
--       reservation commits BEFORE any Bybit request so a concurrent
--       duplicate can never trigger a second orderbook call;
--   (b) one finalizing UPDATE ... WHERE feasibility_status='PENDING'
--       that stores the quote, timings and feasibility verdict.
-- A crash between (a) and (b) leaves a visible PENDING row (diagnosable
-- via the audit query in the runbook); it never blocks the scanner and
-- is never retried automatically.

CREATE TABLE IF NOT EXISTS research.srr_short_fill_feasibility_v1 (
  event_key text PRIMARY KEY,
  observation_id bigint NOT NULL UNIQUE
    REFERENCES research.prospective_observation(observation_id),
  experiment_id text NOT NULL
    CHECK (experiment_id='SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1'),
  source_experiment_id text NOT NULL
    CHECK (source_experiment_id='SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'),
  scanner_name text NOT NULL CHECK (scanner_name='SUPPORT_RESISTANCE_REACTION'),
  symbol text NOT NULL,
  direction text NOT NULL CHECK (direction='SHORT'),
  signal_time timestamptz NOT NULL,
  activation_ts timestamptz NOT NULL,
  captured_at timestamptz NOT NULL,
  capture_started_at timestamptz NOT NULL,
  capture_finished_at timestamptz NOT NULL,
  reference_price numeric NOT NULL CHECK (reference_price>0),
  invalidation_price numeric NOT NULL CHECK (invalidation_price>reference_price),
  structural_r numeric NOT NULL CHECK (structural_r>0),
  execution_r numeric NOT NULL CHECK (execution_r>0),
  variant_stop numeric NOT NULL CHECK (variant_stop>reference_price),
  variant_target numeric NOT NULL CHECK (variant_target<reference_price),
  book_time_ms bigint,
  book_age_ms bigint,
  signal_to_capture_ms bigint,
  signal_to_quote_ms bigint,
  api_request_ms bigint,
  bid numeric, ask numeric,
  spread_bps numeric,
  reference_to_bid_bps numeric,
  depth_bid_10bps_usdt numeric,
  requested_notional_usdt numeric,
  requested_qty numeric,
  executable_qty numeric,
  min_order_qty numeric,
  qty_step numeric,
  min_notional numeric,
  estimated_fill numeric,
  entry_slippage_bps numeric,
  observed_roundtrip_cost_pct numeric,
  observed_cost_r numeric,
  frozen_normal_cost_r numeric,
  frozen_elevated_cost_r numeric,
  extra_cost_r_vs_frozen_normal numeric,
  cost_class text CHECK (cost_class IS NULL OR
      cost_class IN ('NORMAL','ELEVATED','ABOVE_ELEVATED')),
  quote_quality_status text NOT NULL DEFAULT 'PENDING' CHECK
      (quote_quality_status IN
       ('PENDING','FRESH','DELAYED','STALE_BOOK','FUTURE_BOOK','INVALID_BOOK','NO_QUOTE')),
  feasibility_status text NOT NULL DEFAULT 'PENDING' CHECK
      (feasibility_status IN
       ('PENDING','OK','INSUFFICIENT_DEPTH','STALE_BOOK','FUTURE_BOOK',
        'INVALID_BOOK','BELOW_EXCHANGE_MIN','ERROR')),
  eligible_for_execution_stats boolean NOT NULL DEFAULT false,
  failure_reason text,
  exchange_constraints jsonb NOT NULL DEFAULT '{}'::jsonb,
  scenario_results jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb,
  raw_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  claimed_at timestamptz NOT NULL DEFAULT NOW(),
  CHECK (signal_time > activation_ts),
  CHECK (capture_finished_at >= capture_started_at),
  -- Full signal->quote latency (DB claim + instrument lookup + limiter
  -- wait + HTTP) always contains the capture-start delay; a row without
  -- its full latency can never enter execution statistics.
  CHECK (signal_to_quote_ms IS NULL
      OR signal_to_quote_ms >= signal_to_capture_ms),
  CHECK (eligible_for_execution_stats = false
      OR signal_to_quote_ms IS NOT NULL),
  -- Only a genuinely fresh, valid, depth-feasible snapshot may enter
  -- eligible execution statistics. Constraints-known is enforced by the
  -- runtime (constraints are not a column-level fact of the book itself)
  -- and is additionally recorded in exchange_constraints.known.
  CHECK (eligible_for_execution_stats = false
      OR (feasibility_status='OK' AND quote_quality_status='FRESH'))
);

COMMENT ON TABLE research.srr_short_fill_feasibility_v1
  IS 'Issue #259 SRR SHORT reference-to-execution alignment: signal-time orderbook evidence per prospective observation; research-only; no order routing';
COMMENT ON COLUMN research.srr_short_fill_feasibility_v1.event_key
  IS 'sha256(source observation_id|symbol|SHORT|signal_time|reference_price); deterministic idempotent event identity';
COMMENT ON COLUMN research.srr_short_fill_feasibility_v1.observation_id
  IS 'Exactly one SRR SHORT prospective observation id (research.prospective_observation); unique per research run';
COMMENT ON COLUMN research.srr_short_fill_feasibility_v1.signal_to_capture_ms
  IS 'Delay from signal generation to the START of the quote request; independent of book_age_ms';
COMMENT ON COLUMN research.srr_short_fill_feasibility_v1.signal_to_quote_ms
  IS 'FULL delay from signal generation to actual orderbook receipt (includes DB claim, instrument lookup, limiter wait and HTTP); NULL when no quote was received; gates FRESH vs DELAYED';
COMMENT ON COLUMN research.srr_short_fill_feasibility_v1.book_age_ms
  IS 'Exchange orderbook timestamp age at capture time; freshness of the book, NOT of the signal-time alignment';
COMMENT ON COLUMN research.srr_short_fill_feasibility_v1.observed_cost_r
  IS 'Quote-side round-trip cost normalized by frozen execution_R = 2.00 * structural_R; NOT an executable Net R';
COMMENT ON COLUMN research.srr_short_fill_feasibility_v1.eligible_for_execution_stats
  IS 'True only for FRESH + OK + known exchange constraints; everything else is excluded from execution statistics';

CREATE INDEX IF NOT EXISTS idx_srr_short_fill_signal_time
  ON research.srr_short_fill_feasibility_v1(signal_time);
CREATE INDEX IF NOT EXISTS idx_srr_short_fill_symbol_signal_time
  ON research.srr_short_fill_feasibility_v1(symbol, signal_time);

-- ── 4. Evidence immutability guard ───────────────────────────
-- Exactly one transition is allowed: PENDING claim -> finalized row.
-- Identity columns are immutable. Finalized rows can never be updated
-- again. DELETE is always prohibited (append-only evidence).

CREATE OR REPLACE FUNCTION research.fn_guard_srr_short_fill_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'SRR SHORT fill feasibility rows are append-only (event_key=%)',
            OLD.event_key
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.feasibility_status <> 'PENDING' THEN
        RAISE EXCEPTION
            'SRR SHORT fill feasibility rows are finalized and immutable (event_key=%)',
            OLD.event_key
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.event_key IS DISTINCT FROM OLD.event_key
       OR NEW.observation_id IS DISTINCT FROM OLD.observation_id
       OR NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
       OR NEW.source_experiment_id IS DISTINCT FROM OLD.source_experiment_id
       OR NEW.scanner_name IS DISTINCT FROM OLD.scanner_name
       OR NEW.symbol IS DISTINCT FROM OLD.symbol
       OR NEW.direction IS DISTINCT FROM OLD.direction
       OR NEW.signal_time IS DISTINCT FROM OLD.signal_time
       OR NEW.activation_ts IS DISTINCT FROM OLD.activation_ts
       OR NEW.reference_price IS DISTINCT FROM OLD.reference_price
       OR NEW.invalidation_price IS DISTINCT FROM OLD.invalidation_price
       OR NEW.structural_r IS DISTINCT FROM OLD.structural_r
       OR NEW.execution_r IS DISTINCT FROM OLD.execution_r
       OR NEW.variant_stop IS DISTINCT FROM OLD.variant_stop
       OR NEW.variant_target IS DISTINCT FROM OLD.variant_target
       OR NEW.capture_started_at IS DISTINCT FROM OLD.capture_started_at THEN
        RAISE EXCEPTION
            'SRR SHORT fill feasibility identity columns are immutable (event_key=%)',
            OLD.event_key
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.srr_short_fill_feasibility_v1'::regclass
          AND tgname = 'trg_srr_short_fill_event_guard'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_srr_short_fill_event_guard
        BEFORE UPDATE OR DELETE ON research.srr_short_fill_feasibility_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_guard_srr_short_fill_event_mutation();
    END IF;
END $$;

-- ── 5. Verify (read-only) ────────────────────────────────────
--
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_schema='research'
--   AND table_name IN ('srr_short_fill_activation_v1',
--                      'srr_short_fill_feasibility_v1')
-- ORDER BY table_name, ordinal_position;
--
-- SELECT experiment_id, status, activation_ts
-- FROM research.srr_short_fill_activation_v1;

COMMIT;

-- ============================================================
-- PRODUCTION STATUS:
--   NOT APPLIED
--   NOT ACTIVATED (seed row is DISABLED with activation_ts NULL)
-- Existing prospective_observation / prospective_outcome / SRR frozen
-- outcomes / scanner / paper / live / ME SHORT A data are not modified.
-- ============================================================
