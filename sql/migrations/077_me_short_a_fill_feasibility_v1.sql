-- ============================================================
-- Migration 077: ME SHORT A Execution Fill Feasibility Audit V1
-- Issue #202 Phase 1 — strict independent prospective observer.
-- ============================================================
-- Installation alone never activates capture. Activation requires an
-- explicit, separately reviewed freeze of freeze_ts (RUNNING status) and
-- the runtime feature flag ME_SHORT_A_FILL_CAPTURE_ENABLED=1.
-- No order routing. No historical backfill. Paper/live untouched.
--
-- This migration is source-controlled only. It is NOT applied to
-- production PostgreSQL as part of this task.
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research;

-- ── 1. Activation lifecycle record ───────────────────────────
-- READY_TO_START is seeded by this migration with freeze_ts NULL.
-- A separate activation step (explicit human approval) transitions
-- READY_TO_START → RUNNING and freezes freeze_ts. freeze_ts is immutable
-- once set; terminal states (COMPLETED, CANCELLED) are final.

CREATE TABLE IF NOT EXISTS research.me_short_a_fill_activation_v1 (
  experiment_id text PRIMARY KEY CHECK (experiment_id='ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1'),
  status text NOT NULL CHECK (status IN ('READY_TO_START','RUNNING','COMPLETED','CANCELLED')),
  freeze_ts timestamptz,
  notes text,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  CHECK ((status='READY_TO_START' AND freeze_ts IS NULL)
      OR (status <> 'READY_TO_START' AND freeze_ts IS NOT NULL))
);

COMMENT ON TABLE research.me_short_a_fill_activation_v1
  IS 'ME SHORT A fill feasibility Phase 1 activation lifecycle; capture is fail-closed on RUNNING + freeze_ts';

COMMENT ON COLUMN research.me_short_a_fill_activation_v1.freeze_ts
  IS 'Immutable activation boundary; only events with signal_time > freeze_ts are eligible';

INSERT INTO research.me_short_a_fill_activation_v1 (experiment_id,status)
VALUES ('ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1','READY_TO_START')
ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.me_short_a_fill_activation_v1'::regclass
          AND tgname = 'trg_me_short_a_fill_activation_updated_at'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_fill_activation_updated_at
        BEFORE UPDATE ON research.me_short_a_fill_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;
END $$;

-- ── 2. Signal-time fill feasibility evidence (append-only) ──

CREATE TABLE IF NOT EXISTS research.me_short_a_fill_feasibility_v1 (
  event_key text PRIMARY KEY,
  symbol text NOT NULL,
  signal_time timestamptz NOT NULL,
  captured_at timestamptz NOT NULL,
  freeze_ts timestamptz NOT NULL,
  reference_price numeric NOT NULL CHECK(reference_price>0),
  stop_price numeric NOT NULL CHECK(stop_price>reference_price),
  target_price numeric NOT NULL CHECK(target_price<reference_price),
  structural_r numeric,
  structural_r_pct numeric,
  bid numeric, ask numeric, book_time_ms bigint, book_age_ms bigint,
  spread_bps numeric, reference_to_bid_bps numeric,
  depth_bid_10bps_usdt numeric,
  requested_notional_usdt numeric, requested_qty numeric, executable_qty numeric,
  min_order_qty numeric, qty_step numeric, min_notional numeric,
  estimated_fill numeric, entry_slippage_bps numeric,
  estimated_roundtrip_cost_pct numeric,
  estimated_roundtrip_cost_r numeric,
  cost_class text CHECK (cost_class IS NULL OR
      cost_class IN ('NORMAL','ELEVATED','ABOVE_ELEVATED')),
  feasibility_status text NOT NULL CHECK(feasibility_status IN
     ('OK','INSUFFICIENT_DEPTH','STALE_BOOK','INVALID_BOOK','BELOW_EXCHANGE_MIN','ERROR')),
  reason text,
  scenario_results jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_observation_ids jsonb,
  raw_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (signal_time>freeze_ts)
);

COMMENT ON TABLE research.me_short_a_fill_feasibility_v1
  IS 'Issue #202 Phase 1 observe-only SHORT fill feasibility snapshots; append-only evidence; no order routing';

COMMENT ON COLUMN research.me_short_a_fill_feasibility_v1.event_key
  IS 'sha256(symbol|SHORT|signal_time|reference|stop|target); idempotent event identity';

COMMENT ON COLUMN research.me_short_a_fill_feasibility_v1.scenario_results
  IS 'Per notional scenario (registry notional_scenarios_usdt): fill, slippage, cost, min-qty/min-notional feasibility';

CREATE INDEX IF NOT EXISTS idx_me_a_fill_signal_time
 ON research.me_short_a_fill_feasibility_v1(signal_time);

-- ── 3. Activation state machine and freeze immutability ──────
-- Immutable after INSERT: experiment_id.
-- freeze_ts: may be set exactly once (READY_TO_START → RUNNING); once set
-- it may never change or be cleared.
-- Permitted status transitions:
--   READY_TO_START → RUNNING      (explicit activation, freezes freeze_ts)
--   READY_TO_START → CANCELLED    (abandon before activation)
--   RUNNING        → COMPLETED    (audit closed)
--   RUNNING        → CANCELLED    (audit aborted)
-- Terminal states never transition again; no reactivation after any
-- terminal state. DELETE of an activation record is always prohibited.

CREATE OR REPLACE FUNCTION research.fn_guard_me_short_a_fill_activation()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id THEN
        RAISE EXCEPTION
            'ME SHORT A fill activation experiment_id is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.freeze_ts IS NOT NULL AND NEW.freeze_ts IS DISTINCT FROM OLD.freeze_ts THEN
        RAISE EXCEPTION
            'ME SHORT A fill activation freeze_ts is immutable once set: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'READY_TO_START' AND NEW.status IN ('RUNNING', 'CANCELLED'))
            OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'CANCELLED'))
        ) THEN
            RAISE EXCEPTION
                'ME SHORT A fill activation status transition % → % is not permitted: experiment_id=%',
                OLD.status, NEW.status, OLD.experiment_id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    IF NEW.status = 'RUNNING' AND NEW.freeze_ts IS NULL THEN
        RAISE EXCEPTION
            'ME SHORT A fill activation requires an explicit freeze_ts: experiment_id=%',
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
        WHERE tgrelid = 'research.me_short_a_fill_activation_v1'::regclass
          AND tgname = 'trg_me_short_a_fill_activation_guard'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_fill_activation_guard
        BEFORE UPDATE ON research.me_short_a_fill_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_guard_me_short_a_fill_activation();
    END IF;
END $$;

CREATE OR REPLACE FUNCTION research.fn_block_me_short_a_fill_activation_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    RAISE EXCEPTION
        'ME SHORT A fill activation record cannot be deleted: experiment_id=%',
        OLD.experiment_id
        USING ERRCODE = 'restrict_violation';
END;
$trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.me_short_a_fill_activation_v1'::regclass
          AND tgname = 'trg_me_short_a_fill_activation_block_delete'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_fill_activation_block_delete
        BEFORE DELETE ON research.me_short_a_fill_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_block_me_short_a_fill_activation_delete();
    END IF;
END $$;

-- ── 4. Evidence rows are append-only ─────────────────────────
-- Captured snapshots are immutable audit evidence: UPDATE and DELETE on
-- research.me_short_a_fill_feasibility_v1 are always prohibited.

CREATE OR REPLACE FUNCTION research.fn_block_me_short_a_fill_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    RAISE EXCEPTION
        'ME SHORT A fill feasibility rows are append-only (event_key=%)',
        OLD.event_key
        USING ERRCODE = 'restrict_violation';
END;
$trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.me_short_a_fill_feasibility_v1'::regclass
          AND tgname = 'trg_me_short_a_fill_event_block_mutation'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_fill_event_block_mutation
        BEFORE UPDATE OR DELETE ON research.me_short_a_fill_feasibility_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_block_me_short_a_fill_event_mutation();
    END IF;
END $$;

-- ── 5. Verify (read-only) ─────────────────────────────────────
--
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_schema='research'
--   AND table_name IN ('me_short_a_fill_activation_v1','me_short_a_fill_feasibility_v1')
-- ORDER BY table_name, ordinal_position;

COMMIT;

-- ============================================================
-- PRODUCTION STATUS:
--   NOT APPLIED
--   NOT ACTIVATED (seed row is READY_TO_START with freeze_ts NULL)
-- Existing prospective_observation, prospective_outcome, research_outcome,
-- scanner, paper, live, and frozen geometry A data are not modified.
-- ============================================================
