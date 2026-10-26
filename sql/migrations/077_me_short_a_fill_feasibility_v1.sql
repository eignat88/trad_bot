-- Issue #202 Phase 1, strict independent prospective observer.
-- Installation alone never activates capture. No order routing or backfill.
BEGIN;
CREATE TABLE IF NOT EXISTS research.me_short_a_fill_activation_v1 (
  experiment_id text PRIMARY KEY CHECK (experiment_id='ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1'),
  status text NOT NULL CHECK (status IN ('READY_TO_START','RUNNING','COMPLETED','CANCELLED')),
  freeze_ts timestamptz,
  CHECK ((status='READY_TO_START' AND freeze_ts IS NULL)
      OR (status <> 'READY_TO_START' AND freeze_ts IS NOT NULL))
);
INSERT INTO research.me_short_a_fill_activation_v1 (experiment_id,status)
VALUES ('ME_SHORT_A_EXECUTION_FILL_FEASIBILITY_AUDIT_V1','READY_TO_START')
ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS research.me_short_a_fill_feasibility_v1 (
  event_key text PRIMARY KEY,
  symbol text NOT NULL,
  signal_time timestamptz NOT NULL,
  captured_at timestamptz NOT NULL,
  freeze_ts timestamptz NOT NULL,
  reference_price numeric NOT NULL CHECK(reference_price>0),
  stop_price numeric NOT NULL CHECK(stop_price>reference_price),
  target_price numeric NOT NULL CHECK(target_price<reference_price),
  bid numeric, ask numeric, book_time_ms bigint,
  spread_bps numeric, reference_to_bid_bps numeric,
  depth_bid_10bps_usdt numeric, requested_notional_usdt numeric,
  estimated_fill numeric, entry_slippage_bps numeric,
  estimated_roundtrip_cost_r numeric,
  feasibility_status text NOT NULL CHECK(feasibility_status IN
     ('OK','INSUFFICIENT_DEPTH','STALE_BOOK','INVALID_BOOK','ERROR')),
  reason text,
  raw_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (signal_time>freeze_ts)
);
CREATE INDEX IF NOT EXISTS idx_me_a_fill_signal_time
 ON research.me_short_a_fill_feasibility_v1(signal_time);
COMMIT;
