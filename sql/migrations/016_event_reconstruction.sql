-- Migration 016: Event Reconstruction
-- Populates analytics.trade_event from dds.paper_trade + dds.scanner_setup.
--
-- Reconstructs the canonical event journal for all historical trades.
-- Each event has a deterministic source_event_key for idempotency.
--
-- IMPORTANT: This migration is designed to be RE-RUN safely.
--   - Uses INSERT ... ON CONFLICT (source_event_key) DO NOTHING
--   - Never deletes existing events
--   - Append-only semantics enforced by trigger (migration 014)

-- ============================================================
-- 1. SETUP_READY events
-- ============================================================
INSERT INTO analytics.trade_event
    (trade_id, setup_id, event_type, event_at, observed_at,
     price, reason_code, payload_json, source_event_key)
SELECT
    pt.trade_id,
    pt.setup_id,
    'SETUP_READY',
    ss.detected_at,
    ss.created_at,
    ss.reference_price,
    ss.status,
    jsonb_build_object(
        'scanner_name', ss.scanner_name,
        'scanner_version', ss.scanner_version,
        'score', ss.score,
        'market_regime', ss.market_regime,
        'direction', ss.direction
    ),
    'setup_ready:' || ss.setup_id
FROM dds.paper_trade pt
JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
WHERE ss.detected_at IS NOT NULL
ON CONFLICT (source_event_key) DO NOTHING;

-- ============================================================
-- 2. ENTRY_FILLED events
-- ============================================================
INSERT INTO analytics.trade_event
    (trade_id, setup_id, event_type, event_at, observed_at,
     price, quantity, reason_code, payload_json, source_event_key)
SELECT
    pt.trade_id,
    pt.setup_id,
    'ENTRY_FILLED',
    pt.entered_at,
    pt.created_at,
    pt.entry_price,
    pt.initial_fill_qty,
    pt.status,
    jsonb_build_object(
        'entry_fee', pt.entry_fee,
        'entry_slippage', pt.slippage,
        'entry_market_price', pt.entry_market_price,
        'position_size', pt.position_size,
        'scanner_name', pt.scanner_name
    ),
    'entry_filled:' || pt.trade_id
FROM dds.paper_trade pt
WHERE pt.entered_at IS NOT NULL
ON CONFLICT (source_event_key) DO NOTHING;

-- ============================================================
-- 3. DCA_PLACED events (from dca_data JSONB)
-- ============================================================
INSERT INTO analytics.trade_event
    (trade_id, setup_id, event_type, event_at, observed_at,
     price, quantity, payload_json, source_event_key)
SELECT
    pt.trade_id,
    pt.setup_id,
    'DCA_PLACED',
    pt.entered_at,  -- DCA order placed at entry time (virtual order)
    pt.created_at,
    pt.dca_price,
    (pt.dca_data->>'dca_fill_qty')::NUMERIC,
    jsonb_build_object(
        'dca_level_atr', pt.dca_level_atr,
        'dca_price', pt.dca_price,
        'dca_target_pct', pt.dca_target_pct,
        'atr_at_entry', pt.atr_at_entry
    ),
    'dca_placed:' || pt.trade_id
FROM dds.paper_trade pt
WHERE pt.dca_enabled = TRUE
  AND pt.dca_price IS NOT NULL
  AND pt.dca_price > 0
ON CONFLICT (source_event_key) DO NOTHING;

-- ============================================================
-- 4. DCA_FILLED events
-- ============================================================
INSERT INTO analytics.trade_event
    (trade_id, setup_id, event_type, event_at, observed_at,
     price, quantity, payload_json, source_event_key)
SELECT
    pt.trade_id,
    pt.setup_id,
    'DCA_FILLED',
    pt.dca_filled_at,
    pt.updated_at,
    pt.dca_fill_price,
    pt.dca_fill_qty,
    jsonb_build_object(
        'dca_fee', pt.dca_fee,
        'dca_slippage', pt.dca_slippage,
        'avg_entry_price', pt.avg_entry_price,
        'dca_state', pt.dca_state
    ),
    'dca_filled:' || pt.trade_id
FROM dds.paper_trade pt
WHERE pt.dca_filled_at IS NOT NULL
  AND pt.dca_fill_price IS NOT NULL
  AND pt.dca_fill_price > 0
ON CONFLICT (source_event_key) DO NOTHING;

-- ============================================================
-- 5. TRADE_CLOSED events
-- ============================================================
INSERT INTO analytics.trade_event
    (trade_id, setup_id, event_type, event_at, observed_at,
     price, reason_code, payload_json, source_event_key)
SELECT
    pt.trade_id,
    pt.setup_id,
    'TRADE_CLOSED',
    pt.closed_at,
    pt.updated_at,
    pt.exit_price,
    pt.exit_reason,
    jsonb_build_object(
        'exit_fee', pt.exit_fee,
        'gross_pnl', pt.gross_pnl,
        'pnl_usdt', pt.pnl_usdt,
        'pnl_r', pt.pnl_r,
        'mfe_r', pt.mfe_r,
        'mae_r', pt.mae_r,
        'duration_sec', pt.duration_sec,
        'funding_paid', pt.funding_paid,
        'slippage', pt.slippage
    ),
    'trade_closed:' || pt.trade_id
FROM dds.paper_trade pt
WHERE pt.closed_at IS NOT NULL
  AND pt.exit_price IS NOT NULL
ON CONFLICT (source_event_key) DO NOTHING;
