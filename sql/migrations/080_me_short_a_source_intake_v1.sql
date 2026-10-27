-- ============================================================
-- Migration 080: ME SHORT A Prospective Source Intake V1
-- Issue #208 corrective — ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1
-- ============================================================
-- Forward-only, research-only intake for the ORIGINAL
-- MOMENTUM_EXHAUSTION SHORT events, captured independently of the
-- trading filters (direction gate / regime filter / expectancy filter).
--
-- WHY THIS EXISTS (root cause, Issue #208 gap):
--   The forward outcome runtime reads its source candidates from
--   dds.scanner_setup.  Candidates dropped by the TRADING regime filter
--   (e.g. SHORT in TREND_UP) never reach dds.scanner_setup, while the
--   Phase 1 observer (research.me_short_a_fill_feasibility_v1) still
--   captures them.  The result is a Phase 1 snapshot with an empty
--   #208 lineage chain (confirmed incident: TSLAUSDT 2026-10-27
--   10:43:57.672455+00, event_key ecd13b2e…).
--   This table is the research-only source line that survives every
--   trading filter, so the runtime can always observe the original event.
--
-- IDENTITY (byte-identical to migration 077/078):
--   event_key = sha256(symbol|SHORT|<utc isoformat signal_time>|<ref>|<stop>|<target>)
--   One event joins across
--     research.me_short_a_source_intake_v1      (this migration, capture line)
--     research.me_short_a_fill_feasibility_v1   (Phase 1 evidence, read-only)
--     research.me_short_a_source_signal_v1      (Issue #208 source layer)
--     research.me_short_a_event_lineage_v1      (Issue #208 lineage)
--     research.me_short_a_outcome_observation_v1(Issue #208 outcome)
--   on that single deterministic key.
--
-- WHAT THIS MIGRATION NEVER DOES:
--   * never writes to research.me_short_a_fill_feasibility_v1 (Phase 1) or
--     back-fills its source_observation_ids,
--   * never writes to the four Issue #208 capture tables (their only writer
--     is the forward outcome runtime),
--   * never writes to research.prospective_observation /
--     research.prospective_outcome (the closed frozen OOS cohort),
--   * never touches scanner / paper / live / direction-gate / regime-filter
--     state,
--   * never activates anything: the seeded lifecycle row is READY_TO_START
--     with intake_freeze_ts NULL (fail-closed),
--   * never changes the Issue #208 freeze_ts or its frozen protocol hash.
--
-- FORWARD-ONLY BOUNDARY OF THE FIXED MECHANISM:
--   The activation row's intake_freeze_ts is frozen at the explicit
--   activation step (after this fix is deployed).  Every intake row
--   carries CHECK (signal_time > intake_freeze_ts), so the confirmed
--   incident event (TSLAUSDT 2026-10-27 10:43:57.672455+00) and any other
--   pre-activation observation can never enter the new cohort — there is
--   no historical backfill by construction.
--
-- This migration is source-controlled only. It is NOT applied to
-- production PostgreSQL as part of this task.
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research;

-- ── 1. Activation lifecycle record ───────────────────────────
-- READY_TO_START is seeded by this migration with intake_freeze_ts NULL.
-- A separate activation step (explicit human approval) transitions
-- READY_TO_START -> RUNNING and freezes intake_freeze_ts.
-- intake_freeze_ts is immutable once set; terminal states are final.

CREATE TABLE IF NOT EXISTS research.me_short_a_source_intake_activation_v1 (
  experiment_id text PRIMARY KEY
    CHECK (experiment_id = 'ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1'),
  protocol_version text NOT NULL
    DEFAULT 'ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1',
  status text NOT NULL
    CHECK (status IN ('READY_TO_START','RUNNING','COMPLETED','CANCELLED')),
  intake_freeze_ts timestamptz,
  notes text,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  CHECK ((status='READY_TO_START' AND intake_freeze_ts IS NULL)
      OR (status <> 'READY_TO_START' AND intake_freeze_ts IS NOT NULL))
);

COMMENT ON TABLE research.me_short_a_source_intake_activation_v1
  IS 'ME SHORT A prospective source intake activation lifecycle; capture is fail-closed on RUNNING + intake_freeze_ts';

COMMENT ON COLUMN research.me_short_a_source_intake_activation_v1.intake_freeze_ts
  IS 'Immutable activation boundary of the FIXED mechanism; only events with signal_time > intake_freeze_ts are eligible';

INSERT INTO research.me_short_a_source_intake_activation_v1 (experiment_id, status)
VALUES ('ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1', 'READY_TO_START')
ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.me_short_a_source_intake_activation_v1'::regclass
          AND tgname = 'trg_me_short_a_source_intake_activation_updated_at'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_source_intake_activation_updated_at
        BEFORE UPDATE ON research.me_short_a_source_intake_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;
END $$;

-- ── 2. Source intake evidence (append-only) ──────────────────
-- One row per ORIGINAL new MOMENTUM_EXHAUSTION SHORT event, observed at
-- signal time before the trading regime filter could drop it.  The row is
-- written by the scanner-side research hook (fail-open) and read by the
-- forward outcome runtime as an additional candidate source.

CREATE TABLE IF NOT EXISTS research.me_short_a_source_intake_v1 (
  event_key text PRIMARY KEY,
  protocol_version text NOT NULL,
  protocol_hash text NOT NULL,
  scanner_name text NOT NULL CHECK (scanner_name = 'MOMENTUM_EXHAUSTION'),
  direction text NOT NULL CHECK (direction = 'SHORT'),
  symbol text NOT NULL,
  signal_time timestamptz NOT NULL,
  captured_at timestamptz NOT NULL,
  intake_freeze_ts timestamptz NOT NULL,
  reference_price numeric NOT NULL CHECK (reference_price > 0),
  stop_price numeric NOT NULL CHECK (stop_price > reference_price),
  target_price numeric NOT NULL
    CHECK (target_price > 0 AND target_price < reference_price),
  structural_r numeric NOT NULL
    CHECK (structural_r = stop_price - reference_price),
  target_2 numeric,
  trigger_5m_close numeric,
  entry_zone_low numeric,
  entry_zone_high numeric,
  signal_candle_open_time timestamptz,
  recent_high_window jsonb NOT NULL DEFAULT '[]'::jsonb,
  recent_high_window_candles integer,
  recent_high_window_start timestamptz,
  recent_high_window_end timestamptz,
  market_context jsonb NOT NULL DEFAULT '{}'::jsonb,
  signal_time_quote jsonb NOT NULL DEFAULT '{}'::jsonb,
  capture_route text NOT NULL CHECK (capture_route IN (
      'TRADEABLE_FLOW',
      'SHADOW_CONTROL',
      'OBSERVE_ONLY',
      'DIRECTION_GATE_BLOCKED',
      'EXPECTANCY_FILTER_REJECTED',
      'REGIME_FILTER_REJECTED')),
  blocked_stage text,
  blocked_reason text,
  market_regime text,
  setup_id text,
  missing_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
  no_lookahead_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_observation_ids jsonb,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  -- Forward-only: the fixed mechanism only accepts strictly new events.
  CHECK (signal_time > intake_freeze_ts),
  -- No-lookahead: nothing observed before the signal may be newer than it.
  CHECK (captured_at >= signal_time),
  CHECK (signal_candle_open_time IS NULL OR signal_candle_open_time <= signal_time),
  CHECK (recent_high_window_end IS NULL OR recent_high_window_end <= signal_time),
  CHECK (recent_high_window_candles IS NULL
      OR recent_high_window_candles = jsonb_array_length(recent_high_window)),
  CHECK (recent_high_window_start IS NULL
      OR recent_high_window_end IS NULL
      OR recent_high_window_start <= recent_high_window_end)
);

COMMENT ON TABLE research.me_short_a_source_intake_v1
  IS 'Issue #208 corrective: forward-only research-only intake of ORIGINAL MOMENTUM_EXHAUSTION SHORT events captured before/independent of the trading regime filter; append-only; never routed to paper/live';

COMMENT ON COLUMN research.me_short_a_source_intake_v1.event_key
  IS 'sha256(symbol|SHORT|<utc isoformat signal_time>|<ref>|<stop>|<target>); byte-identical to Phase 1 / Issue #208 identity';

COMMENT ON COLUMN research.me_short_a_source_intake_v1.capture_route
  IS 'Which research route captured the event; REGIME_FILTER_REJECTED marks events the trading regime filter excluded but research kept';

COMMENT ON COLUMN research.me_short_a_source_intake_v1.intake_freeze_ts
  IS 'Activation boundary of the fixed mechanism actually applied at capture time';

CREATE INDEX IF NOT EXISTS idx_me_short_a_source_intake_keyset
  ON research.me_short_a_source_intake_v1 (signal_time, event_key);

CREATE INDEX IF NOT EXISTS idx_me_short_a_source_intake_route
  ON research.me_short_a_source_intake_v1 (capture_route, signal_time);

-- ── 3. Activation state machine and freeze immutability ──────
-- Mirrors migration 077/078: experiment_id immutable, intake_freeze_ts set
-- exactly once, strict status transitions, DELETE always prohibited.

CREATE OR REPLACE FUNCTION research.fn_guard_me_short_a_source_intake_activation()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id THEN
        RAISE EXCEPTION
            'ME SHORT A source intake activation experiment_id is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.intake_freeze_ts IS NOT NULL
       AND NEW.intake_freeze_ts IS DISTINCT FROM OLD.intake_freeze_ts THEN
        RAISE EXCEPTION
            'ME SHORT A source intake activation intake_freeze_ts is immutable once set: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.protocol_version IS DISTINCT FROM OLD.protocol_version THEN
        RAISE EXCEPTION
            'ME SHORT A source intake activation protocol_version is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'READY_TO_START' AND NEW.status IN ('RUNNING', 'CANCELLED'))
            OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'CANCELLED'))
        ) THEN
            RAISE EXCEPTION
                'ME SHORT A source intake activation status transition % -> % is not permitted: experiment_id=%',
                OLD.status, NEW.status, OLD.experiment_id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    IF NEW.status = 'RUNNING' AND NEW.intake_freeze_ts IS NULL THEN
        RAISE EXCEPTION
            'ME SHORT A source intake activation requires an explicit intake_freeze_ts: experiment_id=%',
            NEW.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.me_short_a_source_intake_activation_v1'::regclass
          AND tgname = 'trg_me_short_a_source_intake_activation_guard'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_source_intake_activation_guard
        BEFORE UPDATE ON research.me_short_a_source_intake_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_guard_me_short_a_source_intake_activation();
    END IF;
END $$;

CREATE OR REPLACE FUNCTION research.fn_block_me_short_a_source_intake_activation_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    RAISE EXCEPTION
        'ME SHORT A source intake activation rows cannot be deleted: experiment_id=%',
        OLD.experiment_id
        USING ERRCODE = 'restrict_violation';
END $trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.me_short_a_source_intake_activation_v1'::regclass
          AND tgname = 'trg_me_short_a_source_intake_activation_block_delete'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_source_intake_activation_block_delete
        BEFORE DELETE ON research.me_short_a_source_intake_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_block_me_short_a_source_intake_activation_delete();
    END IF;
END $$;

-- ── 4. Intake rows are append-only evidence ──────────────────

CREATE OR REPLACE FUNCTION research.fn_block_me_short_a_source_intake_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    RAISE EXCEPTION
        'ME SHORT A source intake rows are append-only (table=%, event_key=%)',
        TG_TABLE_NAME,
        OLD.event_key
        USING ERRCODE = 'restrict_violation';
END $trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.me_short_a_source_intake_v1'::regclass
          AND tgname = 'trg_me_short_a_source_intake_block_mutation'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_source_intake_block_mutation
        BEFORE UPDATE OR DELETE ON research.me_short_a_source_intake_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_block_me_short_a_source_intake_mutation();
    END IF;
END $$;

COMMIT;

-- ============================================================
-- PRODUCTION STATUS:
--   NOT APPLIED
--   NOT ACTIVATED (seed row is READY_TO_START with intake_freeze_ts NULL)
-- research.me_short_a_fill_feasibility_v1 (Phase 1), its source_observation
-- ids, the four Issue #208 capture tables, research.prospective_observation /
-- research.prospective_outcome (the closed frozen OOS cohort), scanner,
-- paper, live, the ME SHORT direction gate, the regime filter and the Issue
-- #208 freeze_ts / frozen protocol hash are NOT modified by this migration.
-- ============================================================
