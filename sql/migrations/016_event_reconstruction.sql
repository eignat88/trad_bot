-- Migration 016: Event Reconstruction
-- Populates analytics.trade_event from dds.paper_trade + dds.scanner_setup.
--
-- Scoped to latest SUCCEEDED analysis_run only — never mixes data
-- from different analytics runs.  PIT-invariant: all source timestamps
-- must be < observation_cutoff; violations are excluded.
--
-- IMPORTANT: This migration is designed to be RE-RUN safely.
--   - Uses INSERT ... ON CONFLICT (source_event_key) DO NOTHING
--   - Never deletes existing events
--   - Append-only semantics enforced by triggers (migration 014)

-- ============================================================
-- 0. Context: latest SUCCEEDED run + its cutoff
-- ============================================================

-- ============================================================
-- 1. SETUP_READY events (scoped to run's observation_cutoff)
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
CROSS JOIN (
    SELECT observation_cutoff
    FROM analytics.analysis_run
    WHERE status = 'SUCCEEDED'
    ORDER BY created_at DESC
    LIMIT 1
) cutoff
WHERE ss.detected_at IS NOT NULL
  AND ss.detected_at < cutoff.observation_cutoff
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
CROSS JOIN (
    SELECT observation_cutoff
    FROM analytics.analysis_run
    WHERE status = 'SUCCEEDED'
    ORDER BY created_at DESC
    LIMIT 1
) cutoff
WHERE pt.entered_at IS NOT NULL
  AND pt.entered_at < cutoff.observation_cutoff
ON CONFLICT (source_event_key) DO NOTHING;

-- ============================================================
-- 3. ENTRY_ATTEMPTED events
--    Derivable: setup READY_TO_TRADE but no paper_trade fill,
--    or setup expired/invalidated without fill.
-- ============================================================
INSERT INTO analytics.trade_event
    (trade_id, setup_id, event_type, event_at, observed_at,
     reason_code, payload_json, source_event_key)
SELECT
    0 AS trade_id,  -- no trade was created
    ss.setup_id,
    'ENTRY_ATTEMPTED',
    COALESCE(ss.expired_at, ss.invalidated_at, ss.updated_at),
    ss.created_at,
    ss.status,
    jsonb_build_object(
        'result', CASE
            WHEN ss.status = 'EXPIRED' THEN 'EXPIRED'
            WHEN ss.status = 'INVALIDATED' THEN 'CANCELLED'
            ELSE 'REJECTED'
        END,
        'scanner_name', ss.scanner_name,
        'symbol', i.symbol,
        'direction', ss.direction
    ),
    'entry_attempted:' || ss.setup_id
FROM dds.scanner_setup ss
JOIN dds.instrument i ON i.instrument_id = ss.instrument_id
CROSS JOIN (
    SELECT observation_cutoff
    FROM analytics.analysis_run
    WHERE status = 'SUCCEEDED'
    ORDER BY created_at DESC
    LIMIT 1
) cutoff
WHERE ss.status IN ('EXPIRED', 'INVALIDATED')
  AND NOT EXISTS (
      SELECT 1 FROM dds.paper_trade pt WHERE pt.setup_id = ss.setup_id
  )
  AND ss.updated_at < cutoff.observation_cutoff
ON CONFLICT (source_event_key) DO NOTHING;

-- ============================================================
-- 4. DCA_PLACED events (virtual order placed at entry time)
-- ============================================================
INSERT INTO analytics.trade_event
    (trade_id, setup_id, event_type, event_at, observed_at,
     price, payload_json, source_event_key)
SELECT
    pt.trade_id,
    pt.setup_id,
    'DCA_PLACED',
    pt.entered_at,  -- DCA virtual order is created when entry is filled
    pt.created_at,
    pt.dca_price,
    jsonb_build_object(
        'dca_level_atr', pt.dca_level_atr,
        'dca_price', pt.dca_price,
        'dca_target_pct', pt.dca_target_pct,
        'atr_at_entry', pt.atr_at_entry
    ),
    'dca_placed:' || pt.trade_id
FROM dds.paper_trade pt
CROSS JOIN (
    SELECT observation_cutoff
    FROM analytics.analysis_run
    WHERE status = 'SUCCEEDED'
    ORDER BY created_at DESC
    LIMIT 1
) cutoff
WHERE pt.dca_enabled = TRUE
  AND pt.dca_price IS NOT NULL
  AND pt.dca_price > 0
  AND pt.entered_at < cutoff.observation_cutoff
ON CONFLICT (source_event_key) DO NOTHING;

-- ============================================================
-- 5. DCA_FILLED events
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
CROSS JOIN (
    SELECT observation_cutoff
    FROM analytics.analysis_run
    WHERE status = 'SUCCEEDED'
    ORDER BY created_at DESC
    LIMIT 1
) cutoff
WHERE pt.dca_filled_at IS NOT NULL
  AND pt.dca_fill_price IS NOT NULL
  AND pt.dca_fill_price > 0
  AND pt.dca_filled_at < cutoff.observation_cutoff
ON CONFLICT (source_event_key) DO NOTHING;

-- ============================================================
-- 6. TRADE_CLOSED events
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
CROSS JOIN (
    SELECT observation_cutoff
    FROM analytics.analysis_run
    WHERE status = 'SUCCEEDED'
    ORDER BY created_at DESC
    LIMIT 1
) cutoff
WHERE pt.closed_at IS NOT NULL
  AND pt.exit_price IS NOT NULL
  AND pt.closed_at < cutoff.observation_cutoff
ON CONFLICT (source_event_key) DO NOTHING;
