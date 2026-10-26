-- Issue #202 Phase 1: observation-only market-depth evidence.
-- Does not reactivate closed research, write orders, or enable paper/live.
BEGIN;
CREATE TABLE IF NOT EXISTS research.me_short_a_fill_feasibility_v1 (
  observation_id bigint PRIMARY KEY REFERENCES research.prospective_observation(observation_id),
  source_signal_id bigint NOT NULL,
  symbol text NOT NULL,
  signal_time timestamptz NOT NULL,
  captured_at timestamptz NOT NULL,
  reference_price numeric NOT NULL CHECK (reference_price > 0),
  stop_price numeric NOT NULL CHECK (stop_price > reference_price),
  target_price numeric NOT NULL CHECK (target_price < reference_price),
  bid numeric,
  ask numeric,
  book_time_ms bigint,
  spread_bps numeric,
  reference_to_bid_bps numeric,
  depth_bid_10bps_usdt numeric,
  requested_notional_usdt numeric,
  estimated_fill numeric,
  entry_slippage_bps numeric,
  estimated_roundtrip_cost_r numeric,
  feasibility_status text NOT NULL,
  reason text,
  raw_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (feasibility_status IN ('OK','INSUFFICIENT_DEPTH','STALE_BOOK','INVALID_BOOK','ERROR'))
);
CREATE INDEX IF NOT EXISTS idx_me_a_fill_signal_time
  ON research.me_short_a_fill_feasibility_v1(signal_time);
-- No historical replay/backfill in this migration.
COMMIT;
