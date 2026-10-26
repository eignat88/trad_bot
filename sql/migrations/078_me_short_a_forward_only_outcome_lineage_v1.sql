-- ============================================================
-- Migration 078: ME SHORT A Forward-Only Outcome and Lineage Capture V1
-- Issue #208 — ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1
-- ============================================================
-- Independent, strictly prospective, observe-only outcome + lineage
-- collector for the ongoing #202 Phase 1 snapshots. It preserves the
-- frozen ME SHORT A geometry exactly as registered in migration 071 and
-- enables the read-only price-alignment counterfactual of #206 / draft
-- PR #207 WITHOUT touching any frozen artifact.
--
-- FROZEN PROTOCOL (never retuned here):
--   scanner       = MOMENTUM_EXHAUSTION
--   direction     = SHORT only
--   entry         = reference_price  (frozen A: max(last five 5m highs))
--   stop          = invalidation_price (absolute, > reference_price)
--   target        = target_1           (absolute, < reference_price)
--   max hold      = 240 minutes
--   intrabar      = STOP_FIRST (ambiguity resolves to the stop)
--   touch basis   = CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL
--   exit fills    = MODELED_UNKNOWN (never observed by this collector)
--
-- IDENTITY:
--   event_key = sha256(symbol|SHORT|<utc-iso signal_time>|<ref>|<stop>|<target>)
--   This is BYTE-IDENTICAL to the identity already frozen in migration
--   077 and consumed by PR #207, so signal -> Phase 1 snapshot -> outcome
--   join on one deterministic, collision-resistant key.
--
-- WHAT THIS MIGRATION NEVER DOES:
--   * never writes to research.me_short_a_fill_feasibility_v1 (Phase 1),
--     never back-fills its source_observation_ids,
--   * never writes to research.prospective_observation /
--     research.prospective_outcome (the closed 380-row frozen OOS),
--   * never touches scanner / paper / live / direction-gate state,
--   * never activates anything: the seeded lifecycle row is
--     READY_TO_START with freeze_ts NULL (fail-closed).
--
-- This migration is source-controlled only. It is NOT applied to
-- production PostgreSQL as part of this task.
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research;

-- ── 0. Dependency precondition (migration 077) ───────────────
-- The lineage view joins the Phase 1 evidence table created by 077.
-- Fail loudly and early instead of producing a half-built schema.

DO $$
BEGIN
    IF to_regclass('research.me_short_a_fill_feasibility_v1') IS NULL
       OR to_regclass('research.me_short_a_fill_activation_v1') IS NULL THEN
        RAISE EXCEPTION
            'ME SHORT A forward lineage precondition failed: migration 077 '
            '(research.me_short_a_fill_activation_v1 / '
            'research.me_short_a_fill_feasibility_v1) must be applied first';
    END IF;
END $$;

-- ── 1. Activation lifecycle record ───────────────────────────
-- Seeded READY_TO_START with freeze_ts NULL by THIS migration.
-- A separate, explicitly approved activation step transitions
-- READY_TO_START -> RUNNING and freezes freeze_ts. freeze_ts is immutable
-- once set; terminal states are final. No backfill is possible because
-- every capture table carries CHECK (signal_time > freeze_ts).

CREATE TABLE IF NOT EXISTS research.me_short_a_lineage_activation_v1 (
  experiment_id text PRIMARY KEY
    CHECK (experiment_id = 'ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1'),
  protocol_version text NOT NULL
    DEFAULT 'ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1',
  protocol_hash text NOT NULL,
  status text NOT NULL CHECK (status IN ('READY_TO_START','RUNNING','COMPLETED','CANCELLED')),
  freeze_ts timestamptz,
  notes text,
  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  CHECK ((status='READY_TO_START' AND freeze_ts IS NULL)
      OR (status <> 'READY_TO_START' AND freeze_ts IS NOT NULL))
);

COMMENT ON TABLE research.me_short_a_lineage_activation_v1
  IS 'ME SHORT A forward-only outcome/lineage activation lifecycle; capture is fail-closed on RUNNING + freeze_ts (migration 078)';
COMMENT ON COLUMN research.me_short_a_lineage_activation_v1.freeze_ts
  IS 'Immutable activation boundary; only events with signal_time > freeze_ts are eligible';
COMMENT ON COLUMN research.me_short_a_lineage_activation_v1.protocol_hash
  IS 'sha256 of the frozen protocol payload; every captured row stores and is verified against it';

INSERT INTO research.me_short_a_lineage_activation_v1
  (experiment_id, protocol_version, protocol_hash, status)
VALUES
  ('ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1',
   'ME_SHORT_A_FORWARD_ONLY_OUTCOME_AND_LINEAGE_CAPTURE_V1',
   '87f5a5bad48c2e969d28f5a5049cf4b27f68f7888ff2e0f7e3cf9312d785f7eb',
   'READY_TO_START')
ON CONFLICT (experiment_id) DO NOTHING;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.me_short_a_lineage_activation_v1'::regclass
          AND tgname = 'trg_me_short_a_lineage_activation_updated_at'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_lineage_activation_updated_at
        BEFORE UPDATE ON research.me_short_a_lineage_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_set_updated_at();
    END IF;
END $$;

-- ── 2. Source signal detail capture (append-only) ────────────
-- Separate forward-only supplement for the fields PR #207 reported as
-- MISSING from Phase 1 evidence: trigger_5m_close, entry_zone_low/high,
-- the five 5m candles that formed reference_price, and signal-time market
-- context. Missing/unavailable values are recorded EXPLICITLY in
-- missing_fields; they are never invented and never back-filled.

CREATE TABLE IF NOT EXISTS research.me_short_a_source_signal_v1 (
  event_key text PRIMARY KEY,
  protocol_version text NOT NULL,
  protocol_hash text NOT NULL,
  symbol text NOT NULL,
  direction text NOT NULL CHECK (direction = 'SHORT'),
  scanner_name text NOT NULL CHECK (scanner_name = 'MOMENTUM_EXHAUSTION'),
  signal_time timestamptz NOT NULL,
  captured_at timestamptz NOT NULL,
  freeze_ts timestamptz NOT NULL,

  -- frozen A geometry (absolute prices, exactly as the frozen protocol)
  reference_price numeric NOT NULL CHECK (reference_price > 0),
  stop_price numeric NOT NULL CHECK (stop_price > reference_price),
  target_price numeric NOT NULL CHECK (target_price > 0 AND target_price < reference_price),
  structural_r numeric NOT NULL CHECK (structural_r = stop_price - reference_price),

  -- source signal detail previously missing from Phase 1 evidence
  trigger_5m_close numeric,
  entry_zone_low numeric,
  entry_zone_high numeric,
  signal_candle_open_time timestamptz,
  signal_candle_close_time timestamptz,

  -- the five 5m candles that formed reference_price = max(high)
  recent_high_window jsonb NOT NULL DEFAULT '[]'::jsonb,
  recent_high_window_candles int,
  recent_high_window_start timestamptz,
  recent_high_window_end timestamptz,

  market_context jsonb NOT NULL DEFAULT '{}'::jsonb,
  signal_time_quote jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- explicit, never silently filled
  missing_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
  no_lookahead_flags jsonb NOT NULL DEFAULT '[]'::jsonb,

  source_observation_ids jsonb,
  created_at timestamptz NOT NULL DEFAULT NOW(),

  -- strict forward-only boundary and no-lookahead invariants
  CHECK (signal_time > freeze_ts),
  CHECK (captured_at >= signal_time),
  CHECK (recent_high_window_end IS NULL OR recent_high_window_end <= signal_time),
  CHECK (recent_high_window_start IS NULL
         OR recent_high_window_end IS NULL
         OR recent_high_window_start <= recent_high_window_end),
  CHECK (signal_candle_close_time IS NULL OR signal_candle_close_time <= signal_time),
  CHECK (recent_high_window_candles IS NULL
         OR recent_high_window_candles = jsonb_array_length(recent_high_window))
);

COMMENT ON TABLE research.me_short_a_source_signal_v1
  IS 'Issue #208 forward-only append-only source signal detail capture; never back-filled, never updated (migration 078)';
COMMENT ON COLUMN research.me_short_a_source_signal_v1.event_key
  IS 'sha256(symbol|SHORT|<utc iso signal_time>|<ref>|<stop>|<target>) — identical identity to Phase 1 event_key';
COMMENT ON COLUMN research.me_short_a_source_signal_v1.recent_high_window
  IS 'The exact closed 5m candles whose highs formed reference_price; closes must be <= signal_time (no lookahead)';
COMMENT ON COLUMN research.me_short_a_source_signal_v1.missing_fields
  IS 'Explicit list of required fields that were unavailable at capture time; absence is data, never imputed';

CREATE INDEX IF NOT EXISTS idx_me_a_lineage_src_signal_time
  ON research.me_short_a_source_signal_v1 (signal_time);

-- ── 3. Post-signal 5m OHLC provenance (append-only) ──────────
-- Future closed-candle provenance for the 240m outcome window and for the
-- reference limit-touch classification. Candle TOUCH is explicitly not an
-- order-book fill; partial fills stay unknown unless independently captured.

CREATE TABLE IF NOT EXISTS research.me_short_a_post_signal_candle_v1 (
  event_key text NOT NULL,
  candle_open_time timestamptz NOT NULL,
  candle_close_time timestamptz NOT NULL,
  symbol text NOT NULL,
  signal_time timestamptz NOT NULL,
  freeze_ts timestamptz NOT NULL,
  open_price numeric NOT NULL,
  high_price numeric NOT NULL,
  low_price numeric NOT NULL,
  close_price numeric NOT NULL,
  is_closed boolean NOT NULL,
  source text NOT NULL,
  retrieved_at timestamptz NOT NULL,
  high_touches_stop boolean NOT NULL DEFAULT FALSE,
  low_touches_target boolean NOT NULL DEFAULT FALSE,
  high_touches_reference boolean NOT NULL DEFAULT FALSE,
  touch_semantics text NOT NULL DEFAULT 'CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL',
  created_at timestamptz NOT NULL DEFAULT NOW(),
  PRIMARY KEY (event_key, candle_open_time),

  -- forward-only + no-lookahead + closed-candle-only invariants
  CHECK (signal_time > freeze_ts),
  CHECK (is_closed),
  CHECK (candle_close_time > candle_open_time),
  CHECK (candle_open_time > signal_time),
  CHECK (candle_close_time <= retrieved_at),
  CHECK (high_price >= GREATEST(open_price, close_price)),
  CHECK (low_price <= LEAST(open_price, close_price)),
  CHECK (high_price >= low_price),
  CHECK (touch_semantics = 'CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL')
);

COMMENT ON TABLE research.me_short_a_post_signal_candle_v1
  IS 'Issue #208 append-only provenance of CLOSED post-signal 5m candles; candle touch only, never an order-book fill (migration 078)';
COMMENT ON COLUMN research.me_short_a_post_signal_candle_v1.touch_semantics
  IS 'A level touch here is a candle OHLC touch. It is NOT evidence of a fill, of a full fill, or of an exit fill.';
COMMENT ON COLUMN research.me_short_a_post_signal_candle_v1.high_touches_reference
  IS 'TRUE when this candle high >= reference_price: the limit-at-reference would have been touched (candle basis).';

CREATE INDEX IF NOT EXISTS idx_me_a_lineage_candle_event
  ON research.me_short_a_post_signal_candle_v1 (event_key, candle_open_time);

-- ── 4. Deterministic event lineage linkage (append-only) ─────
-- One row per event, keyed by the same event_key, carrying the identity
-- components and the verification result. Mismatches are QUARANTINED, not
-- repaired; existing Phase 1 rows are never rewritten and old
-- source_observation_ids are never retro-fitted.

CREATE TABLE IF NOT EXISTS research.me_short_a_event_lineage_v1 (
  event_key text PRIMARY KEY,
  protocol_version text NOT NULL,
  protocol_hash text NOT NULL,
  symbol text NOT NULL,
  direction text NOT NULL CHECK (direction = 'SHORT'),
  signal_time timestamptz NOT NULL,
  captured_at timestamptz NOT NULL,
  freeze_ts timestamptz NOT NULL,

  -- identity components, stored verbatim so the key is re-derivable
  reference_price numeric NOT NULL CHECK (reference_price > 0),
  stop_price numeric NOT NULL CHECK (stop_price > reference_price),
  target_price numeric NOT NULL CHECK (target_price > 0 AND target_price < reference_price),

  event_key_recomputed text NOT NULL,
  event_key_integrity boolean NOT NULL,
  link_status text NOT NULL CHECK (link_status IN ('PENDING','LINKED','QUARANTINED')),
  quarantine_reason text,
  linkage_checks jsonb NOT NULL DEFAULT '{}'::jsonb,

  source_signal_present boolean NOT NULL DEFAULT FALSE,
  source_observation_id bigint,
  phase1_event_key text,
  phase1_linked boolean NOT NULL DEFAULT FALSE,
  phase1_observation_id bigint,
  created_at timestamptz NOT NULL DEFAULT NOW(),

  CHECK (signal_time > freeze_ts),
  CHECK (captured_at >= signal_time),
  CHECK ((event_key_integrity = TRUE) OR (link_status = 'QUARANTINED')),
  CHECK ((link_status <> 'QUARANTINED') OR (quarantine_reason IS NOT NULL)),
  CHECK ((link_status <> 'LINKED') OR (source_signal_present AND event_key_integrity)),
  CHECK ((NOT phase1_linked) OR (phase1_event_key = event_key))
);

COMMENT ON TABLE research.me_short_a_event_lineage_v1
  IS 'Issue #208 append-only deterministic event_key linkage: source signal -> Phase 1 snapshot -> outcome (migration 078)';
COMMENT ON COLUMN research.me_short_a_event_lineage_v1.event_key_recomputed
  IS 'Key recomputed from the stored identity components; must equal event_key or the row is quarantined';
COMMENT ON COLUMN research.me_short_a_event_lineage_v1.phase1_event_key
  IS 'The Phase 1 evidence event_key for the SAME event; equals event_key by construction, never invented when absent';
COMMENT ON COLUMN research.me_short_a_event_lineage_v1.linkage_checks
  IS 'Per-field verification (symbol, direction, signal_time, reference, stop, target, protocol version, protocol hash)';

CREATE INDEX IF NOT EXISTS idx_me_a_lineage_link_signal_time
  ON research.me_short_a_event_lineage_v1 (signal_time);

-- ── 5. Outcome observation (append-only) ─────────────────────
-- Autonomous 240m outcome finalization on authoritative CLOSED 5m candles.
-- No premature finalization: is_final requires a complete closed-candle
-- coverage and an ASOF-clean horizon boundary. Exit market fills stay
-- MODELED_UNKNOWN and partial fills stay unknown unless independently
-- captured — this collector never claims an observed fill.

CREATE TABLE IF NOT EXISTS research.me_short_a_outcome_observation_v1 (
  event_key text PRIMARY KEY,
  protocol_version text NOT NULL,
  protocol_hash text NOT NULL,
  symbol text NOT NULL,
  direction text NOT NULL CHECK (direction = 'SHORT'),
  signal_time timestamptz NOT NULL,
  freeze_ts timestamptz NOT NULL,

  reference_price numeric NOT NULL CHECK (reference_price > 0),
  stop_price numeric NOT NULL CHECK (stop_price > reference_price),
  target_price numeric NOT NULL CHECK (target_price > 0 AND target_price < reference_price),
  structural_r numeric NOT NULL CHECK (structural_r = stop_price - reference_price),

  horizon_minutes int NOT NULL CHECK (horizon_minutes = 240),
  horizon_end_time timestamptz NOT NULL
    CHECK (horizon_end_time = signal_time + INTERVAL '240 minutes'),
  intrabar_policy text NOT NULL CHECK (intrabar_policy = 'STOP_FIRST'),

  outcome_status text NOT NULL
    CHECK (outcome_status IN ('OUTCOME_UNMATURED','SOURCE_INCOMPLETE','MATCHED_FINALIZED')),
  path_class text
    CHECK (path_class IS NULL
           OR path_class IN ('STOP_HIT','TARGET_HIT','TIMEOUT_240M','UNRESOLVED')),
  sl_before_tp boolean NOT NULL DEFAULT FALSE,
  tp_before_sl boolean NOT NULL DEFAULT FALSE,
  ambiguous_intrabar boolean NOT NULL DEFAULT FALSE,
  time_to_sl_minutes numeric,
  time_to_tp_minutes numeric,

  exit_price numeric,
  exit_basis text,
  gross_r numeric,
  net_r_normal numeric,
  net_r_elevated numeric,
  return_at_240m numeric,
  mae_240m numeric,
  mfe_240m numeric,

  -- candle-touch semantics, explicitly not an order-book fill
  limit_at_reference_touch boolean NOT NULL DEFAULT FALSE,
  touch_basis text NOT NULL DEFAULT 'CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL',
  exit_fill_semantics text NOT NULL DEFAULT 'MODELED_UNKNOWN',
  partial_fill_status text NOT NULL DEFAULT 'UNKNOWN_NOT_INDEPENDENTLY_CAPTURED',

  -- controlled finalization evidence
  is_final boolean NOT NULL DEFAULT FALSE,
  finalized_at timestamptz,
  finalization_candle_close_time timestamptz,
  asof_time timestamptz NOT NULL,
  max_candle_close_time timestamptz,

  -- source coverage (the exact required count is derived from the frozen
  -- window inequality signal_time < candle_open < signal_time + 240m, so it
  -- is 47 for a grid-aligned signal and 48 otherwise — never assumed)
  candles_expected int NOT NULL DEFAULT 48
    CHECK (candles_expected > 0 AND candles_expected <= 48),
  candles_present int NOT NULL DEFAULT 0 CHECK (candles_present >= 0),
  candles_missing jsonb NOT NULL DEFAULT '[]'::jsonb,
  coverage_complete boolean NOT NULL DEFAULT FALSE,
  asof_ok boolean NOT NULL DEFAULT FALSE,

  source_quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
  missing_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_observation_id bigint,
  created_at timestamptz NOT NULL DEFAULT NOW(),

  -- forward-only boundary
  CHECK (signal_time > freeze_ts),
  CHECK (candles_present <= candles_expected),
  CHECK (coverage_complete = (candles_present = candles_expected)),
  CHECK (max_candle_close_time IS NULL OR max_candle_close_time <= asof_time),
  CHECK (asof_ok = (max_candle_close_time IS NOT NULL
                    AND max_candle_close_time <= asof_time)),

  -- semantics that this collector is structurally unable to fake
  CHECK (touch_basis = 'CANDLE_OHLC_TOUCH_NOT_ORDERBOOK_FILL'),
  CHECK (exit_fill_semantics = 'MODELED_UNKNOWN'),
  CHECK (partial_fill_status = 'UNKNOWN_NOT_INDEPENDENTLY_CAPTURED'),

  -- STOP_FIRST ambiguity resolves to the stop
  CHECK (tp_before_sl = FALSE OR sl_before_tp = FALSE),
  CHECK (ambiguous_intrabar = FALSE
         OR (sl_before_tp = TRUE AND tp_before_sl = FALSE)),
  CHECK (path_class IS NULL OR path_class <> 'STOP_HIT' OR sl_before_tp),
  CHECK (path_class IS NULL OR path_class <> 'TARGET_HIT' OR tp_before_sl),
  CHECK (path_class IS NULL OR gross_r IS NULL OR path_class <> 'STOP_HIT'
         OR gross_r = -1),
  CHECK (path_class IS NULL OR path_class <> 'TIMEOUT_240M'
         OR (sl_before_tp = FALSE AND tp_before_sl = FALSE AND ambiguous_intrabar = FALSE)),

  -- controlled finalization: closed 5m candles only, no premature final
  CHECK ((is_final = FALSE) OR (
           outcome_status = 'MATCHED_FINALIZED'
       AND finalized_at IS NOT NULL
       AND finalization_candle_close_time IS NOT NULL
       AND coverage_complete
       AND asof_ok
       AND finalization_candle_close_time >= horizon_end_time
       AND finalization_candle_close_time <= asof_time
       AND path_class IS NOT NULL
       AND exit_price IS NOT NULL
       AND gross_r IS NOT NULL)),
  CHECK ((is_final = TRUE) OR (outcome_status <> 'MATCHED_FINALIZED')),
  CHECK ((outcome_status <> 'MATCHED_FINALIZED') OR (
           coverage_complete AND asof_ok AND is_final)),

  -- OUTCOME_UNMATURED is a derived/defensive state only: it can never carry
  -- a resolved path and can never be finalized.
  CHECK ((outcome_status <> 'OUTCOME_UNMATURED')
         OR (is_final = FALSE AND path_class IS NULL)),

  -- SOURCE_INCOMPLETE never carries economics: no source-missing row can be
  -- silently read as a result. What was observable stays in coverage/quality
  -- fields; every price/R field stays NULL.
  CHECK ((outcome_status <> 'SOURCE_INCOMPLETE') OR (
           is_final = FALSE
       AND path_class IS NULL
       AND exit_price IS NULL
       AND gross_r IS NULL
       AND net_r_normal IS NULL
       AND net_r_elevated IS NULL
       AND return_at_240m IS NULL))
);

COMMENT ON TABLE research.me_short_a_outcome_observation_v1
  IS 'Issue #208 autonomous 240m STOP_FIRST outcome capture on closed 5m candles; append-only, no premature finalization (migration 078)';
COMMENT ON COLUMN research.me_short_a_outcome_observation_v1.outcome_status
  IS 'OUTCOME_UNMATURED | SOURCE_INCOMPLETE | MATCHED_FINALIZED; only MATCHED_FINALIZED is analysis-eligible';
COMMENT ON COLUMN research.me_short_a_outcome_observation_v1.exit_fill_semantics
  IS 'Always MODELED_UNKNOWN: exit market fills are modelled, never observed. Enforced by CHECK.';
COMMENT ON COLUMN research.me_short_a_outcome_observation_v1.asof_time
  IS 'Data-availability cutoff used for the finalization decision; every used candle must close at or before it (no lookahead)';
COMMENT ON COLUMN research.me_short_a_outcome_observation_v1.candles_missing
  IS 'Explicit list of missing 5m candle open times inside the 240m window; absence is data, never imputed';

CREATE INDEX IF NOT EXISTS idx_me_a_outcome_signal_time
  ON research.me_short_a_outcome_observation_v1 (signal_time);
CREATE INDEX IF NOT EXISTS idx_me_a_outcome_status
  ON research.me_short_a_outcome_observation_v1 (outcome_status);

-- ── 6. Append-only enforcement ───────────────────────────────
-- Every capture table is immutable audit evidence: UPDATE and DELETE are
-- always prohibited. The lineage/outcome tables are never corrected in
-- place; a mismatch is quarantined in a NEW row of the linkage table.

CREATE OR REPLACE FUNCTION research.fn_block_me_short_a_lineage_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    RAISE EXCEPTION
        'ME SHORT A forward lineage rows are append-only (table=%, event_key=%)',
        TG_TABLE_NAME,
        OLD.event_key
        USING ERRCODE = 'restrict_violation';
END;
$trigger$;

DO $$
DECLARE
    tbl text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'me_short_a_source_signal_v1',
        'me_short_a_post_signal_candle_v1',
        'me_short_a_event_lineage_v1',
        'me_short_a_outcome_observation_v1'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = ('research.' || tbl)::regclass
              AND tgname = 'trg_me_short_a_lineage_block_mutation'
              AND NOT tgisinternal
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER trg_me_short_a_lineage_block_mutation '
                'BEFORE UPDATE OR DELETE ON research.%I '
                'FOR EACH ROW EXECUTE FUNCTION '
                'research.fn_block_me_short_a_lineage_mutation()', tbl);
        END IF;
    END LOOP;
END $$;

-- ── 7. Activation state machine and freeze immutability ──────
-- Same fail-closed discipline as migration 077: experiment_id immutable,
-- freeze_ts settable exactly once, restricted transitions, no DELETE.

CREATE OR REPLACE FUNCTION research.fn_guard_me_short_a_lineage_activation()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    IF NEW.experiment_id IS DISTINCT FROM OLD.experiment_id THEN
        RAISE EXCEPTION
            'ME SHORT A lineage activation experiment_id is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.protocol_version IS DISTINCT FROM OLD.protocol_version
       OR NEW.protocol_hash IS DISTINCT FROM OLD.protocol_hash THEN
        RAISE EXCEPTION
            'ME SHORT A lineage activation protocol is immutable: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.freeze_ts IS NOT NULL AND NEW.freeze_ts IS DISTINCT FROM OLD.freeze_ts THEN
        RAISE EXCEPTION
            'ME SHORT A lineage activation freeze_ts is immutable once set: experiment_id=%',
            OLD.experiment_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'READY_TO_START' AND NEW.status IN ('RUNNING', 'CANCELLED'))
            OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'CANCELLED'))
        ) THEN
            RAISE EXCEPTION
                'ME SHORT A lineage activation status transition % → % is not permitted: experiment_id=%',
                OLD.status, NEW.status, OLD.experiment_id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;
    IF NEW.status = 'RUNNING' AND NEW.freeze_ts IS NULL THEN
        RAISE EXCEPTION
            'ME SHORT A lineage activation requires an explicit freeze_ts: experiment_id=%',
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
        WHERE tgrelid = 'research.me_short_a_lineage_activation_v1'::regclass
          AND tgname = 'trg_me_short_a_lineage_activation_guard'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_lineage_activation_guard
        BEFORE UPDATE ON research.me_short_a_lineage_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_guard_me_short_a_lineage_activation();
    END IF;
END $$;

CREATE OR REPLACE FUNCTION research.fn_block_me_short_a_lineage_activation_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $trigger$
BEGIN
    RAISE EXCEPTION
        'ME SHORT A lineage activation record cannot be deleted: experiment_id=%',
        OLD.experiment_id
        USING ERRCODE = 'restrict_violation';
END;
$trigger$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'research.me_short_a_lineage_activation_v1'::regclass
          AND tgname = 'trg_me_short_a_lineage_activation_block_delete'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_me_short_a_lineage_activation_block_delete
        BEFORE DELETE ON research.me_short_a_lineage_activation_v1
        FOR EACH ROW
        EXECUTE FUNCTION research.fn_block_me_short_a_lineage_activation_delete();
    END IF;
END $$;

-- ── 8. Lineage status view ───────────────────────────────────
-- Exactly four output statuses, in precedence order:
--   MISSING_LINEAGE   - the deterministic chain cannot be verified
--   OUTCOME_UNMATURED - chain verified, 240m horizon not yet settled
--   SOURCE_INCOMPLETE - chain verified and matured, but not analysis-safe
--   MATCHED_FINALIZED - chain verified, matured, complete, ASOF-clean
-- Only MATCHED_FINALIZED rows are eligible for any economic reading.

CREATE OR REPLACE VIEW research.v_me_short_a_lineage_status_v1 AS
WITH spine AS (
    SELECT event_key FROM research.me_short_a_source_signal_v1
    UNION
    SELECT event_key FROM research.me_short_a_event_lineage_v1
    UNION
    SELECT event_key FROM research.me_short_a_outcome_observation_v1
)
SELECT
    spine.event_key                                              AS event_key,
    COALESCE(s.symbol, l.symbol, o.symbol)                       AS symbol,
    COALESCE(s.signal_time, l.signal_time, o.signal_time)        AS signal_time,
    COALESCE(s.reference_price, l.reference_price, o.reference_price)
                                                                 AS reference_price,
    COALESCE(s.stop_price, l.stop_price, o.stop_price)           AS stop_price,
    COALESCE(s.target_price, l.target_price, o.target_price)     AS target_price,
    CASE
      WHEN s.event_key IS NULL
        OR l.event_key IS NULL
        OR l.link_status = 'QUARANTINED'
        OR l.event_key_integrity = FALSE
        OR l.phase1_linked = FALSE
        OR p.event_key IS NULL
        THEN 'MISSING_LINEAGE'
      WHEN NOW() < COALESCE(o.horizon_end_time,
                            COALESCE(s.signal_time, l.signal_time, o.signal_time)
                              + INTERVAL '240 minutes')
        THEN 'OUTCOME_UNMATURED'
      WHEN o.event_key IS NULL
        OR o.is_final = FALSE
        OR o.coverage_complete = FALSE
        OR o.asof_ok = FALSE
        OR o.path_class IS NULL
        THEN 'SOURCE_INCOMPLETE'
      ELSE 'MATCHED_FINALIZED'
    END                                                          AS lineage_status,
    (s.event_key IS NOT NULL)                                    AS source_signal_present,
    (l.event_key IS NOT NULL)                                    AS linkage_present,
    (l.link_status = 'LINKED')                                   AS linkage_status_linked,
    COALESCE(l.phase1_linked, FALSE) = FALSE                     AS phase1_missing,
    (p.event_key IS NOT NULL)                                    AS phase1_evidence_present,
    (o.event_key IS NOT NULL)                                    AS outcome_present,
    COALESCE(o.is_final, FALSE)                                  AS outcome_final,
    COALESCE(o.coverage_complete, FALSE)                         AS coverage_complete,
    COALESCE(o.asof_ok, FALSE)                                   AS asof_ok,
    o.path_class,
    o.gross_r,
    o.horizon_end_time,
    o.asof_time,
    l.linkage_checks,
    s.missing_fields                                             AS source_missing_fields,
    o.missing_fields                                             AS outcome_missing_fields,
    o.candles_expected,
    o.candles_present,
    o.candles_missing
FROM spine
LEFT JOIN research.me_short_a_source_signal_v1 s
       ON s.event_key = spine.event_key
LEFT JOIN research.me_short_a_event_lineage_v1 l
       ON l.event_key = spine.event_key
LEFT JOIN research.me_short_a_outcome_observation_v1 o
       ON o.event_key = spine.event_key
LEFT JOIN research.me_short_a_fill_feasibility_v1 p
       ON p.event_key = spine.event_key;

COMMENT ON VIEW research.v_me_short_a_lineage_status_v1
  IS 'Issue #208 lineage status per event: MISSING_LINEAGE | OUTCOME_UNMATURED | SOURCE_INCOMPLETE | MATCHED_FINALIZED';

-- ── 9. Verify (read-only) ─────────────────────────────────────
--
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_schema = 'research'
--   AND table_name LIKE 'me_short_a_%_v1'
-- ORDER BY table_name, ordinal_position;
--
-- SELECT lineage_status, COUNT(*)
-- FROM research.v_me_short_a_lineage_status_v1
-- GROUP BY 1 ORDER BY 1;

COMMIT;

-- ============================================================
-- PRODUCTION STATUS:
--   NOT APPLIED
--   NOT ACTIVATED (seed row is READY_TO_START with freeze_ts NULL)
-- research.me_short_a_fill_feasibility_v1 (Phase 1), its source_observation
-- ids, research.prospective_observation / research.prospective_outcome (the
-- closed frozen OOS cohort), scanner, paper, live and the ME SHORT direction
-- gate are NOT modified by this migration.
-- ============================================================
