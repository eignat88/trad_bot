-- Migration 017: Trade Fact Build
-- Populates analytics.trade_fact from dds.paper_trade + dds.signal_outcome.
--
-- Recomputes R values from candles (does NOT inherit from signal_outcome).
-- All PnL metrics use the canonical formulas defined in migration 015.
--
-- Idempotent: uses INSERT ... ON CONFLICT DO UPDATE.

-- ============================================================
-- Main trade_fact build
-- ============================================================
INSERT INTO analytics.trade_fact (
    run_id, trade_id, setup_id, scanner_name, scanner_version, symbol, direction,
    setup_at, signal_at, first_attempt_at, entered_at, dca_filled_at, closed_at,
    reference_price, entry_zone_low, entry_zone_high,
    initial_fill_price, dca_fill_price, avg_entry_price,
    initial_stop, final_stop, target_1, target_2, exit_price,
    risk_usdt, initial_risk_distance,
    initial_quantity, dca_quantity, final_quantity,
    fees, slippage, funding,
    status, exit_reason, gross_pnl, net_pnl, pnl_r, mfe_r, mae_r,
    market_regime, config_hash, strategy_hash, metric_version,
    coverage_status, ambiguity_status, excluded_reasons,
    dataset_version, published, created_at
)
WITH
-- Get the latest PROVISIONAL or FINAL run
latest_run AS (
    SELECT run_id, observation_cutoff
    FROM analytics.analysis_run
    WHERE status = 'SUCCEEDED'
    ORDER BY created_at DESC
    LIMIT 1
),
-- Build canonical trade facts
trade_build AS (
    SELECT
        lr.run_id,
        pt.trade_id,
        pt.setup_id,
        pt.scanner_name,
        ss.scanner_version,
        pt.symbol,
        pt.direction,

        -- Timeline
        ss.detected_at AS setup_at,
        ms.first_detected_at AS signal_at,
        pt.entered_at AS first_attempt_at,
        pt.entered_at,
        pt.dca_filled_at,
        pt.closed_at,

        -- Prices
        ss.reference_price,
        ss.entry_zone_low,
        ss.entry_zone_high,
        pt.initial_fill_price,
        pt.dca_fill_price,
        pt.avg_entry_price,
        pt.stop_price AS initial_stop,
        pt.stop_price AS final_stop,  -- SL never moves for DCA
        pt.target_1,
        pt.target_2,
        pt.exit_price,

        -- Risk
        pt.risk_usdt,
        -- initial_risk_distance = |reference_price - initial_stop|
        ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price) AS initial_risk_distance,

        -- Quantities
        pt.initial_fill_qty AS initial_quantity,
        pt.dca_fill_qty AS dca_quantity,
        COALESCE(pt.initial_fill_qty, 0) + COALESCE(pt.dca_fill_qty, 0) AS final_quantity,

        -- Costs
        COALESCE(pt.entry_fee, 0) + COALESCE(pt.exit_fee, 0) + COALESCE(pt.dca_fee, 0) AS fees,
        COALESCE(pt.slippage, 0) + COALESCE(pt.dca_slippage, 0) AS slippage,
        COALESCE(pt.funding_paid, 0) AS funding,

        -- Outcome
        pt.status,
        pt.exit_reason,
        pt.gross_pnl,
        -- net_pnl = gross_pnl - total fees - total slippage - funding
        pt.gross_pnl
            - COALESCE(pt.entry_fee, 0) - COALESCE(pt.dca_fee, 0) - COALESCE(pt.exit_fee, 0)
            - COALESCE(pt.funding_paid, 0)
            - COALESCE(pt.slippage, 0) - COALESCE(pt.dca_slippage, 0)
            AS net_pnl,
        -- pnl_r recomputed from candles: direction_sign * (exit - ref) / risk_distance
        CASE pt.direction
            WHEN 'LONG' THEN
                CASE WHEN pt.exit_price IS NOT NULL AND ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price) > 0
                     THEN (pt.exit_price - COALESCE(ss.reference_price, pt.entry_price))
                          / ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price)
                     ELSE NULL END
            WHEN 'SHORT' THEN
                CASE WHEN pt.exit_price IS NOT NULL AND ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price) > 0
                     THEN (COALESCE(ss.reference_price, pt.entry_price) - pt.exit_price)
                          / ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price)
                     ELSE NULL END
        END AS pnl_r,
        -- mfe_r: already computed in paper_trade (recomputed later in horizon metrics)
        pt.mfe_r,
        -- mae_r: already computed in paper_trade (recomputed later in horizon metrics)
        pt.mae_r,

        -- Context
        pt.market_regime,
        NULL AS config_hash,   -- populated in migration 019
        NULL AS strategy_hash, -- populated in migration 019
        '1.0.0' AS metric_version,

        -- Quality
        'UNKNOWN' AS coverage_status,  -- updated by quality gate
        'CLEAR' AS ambiguity_status,   -- updated by horizon metrics
        '[]'::jsonb AS excluded_reasons,

        -- Versioning
        '00000000.0' AS dataset_version,  -- updated at publication
        FALSE AS published,
        NOW() AS created_at

    FROM dds.paper_trade pt
    JOIN latest_run lr ON TRUE
    LEFT JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
    LEFT JOIN dds.market_signal ms ON ms.instrument_id = (
        SELECT instrument_id FROM dds.instrument WHERE symbol = pt.symbol LIMIT 1
    )
        AND ms.direction = pt.direction
        AND ms.status = 'EXECUTED'
)
INSERT INTO analytics.trade_fact AS tf
SELECT * FROM trade_build
ON CONFLICT (run_id, trade_id) DO UPDATE SET
    setup_id = EXCLUDED.setup_id,
    scanner_name = EXCLUDED.scanner_name,
    scanner_version = EXCLUDED.scanner_version,
    symbol = EXCLUDED.symbol,
    direction = EXCLUDED.direction,
    setup_at = EXCLUDED.setup_at,
    signal_at = EXCLUDED.signal_at,
    entered_at = EXCLUDED.entered_at,
    dca_filled_at = EXCLUDED.dca_filled_at,
    closed_at = EXCLUDED.closed_at,
    reference_price = EXCLUDED.reference_price,
    entry_zone_low = EXCLUDED.entry_zone_low,
    entry_zone_high = EXCLUDED.entry_zone_high,
    initial_fill_price = EXCLUDED.initial_fill_price,
    dca_fill_price = EXCLUDED.dca_fill_price,
    avg_entry_price = EXCLUDED.avg_entry_price,
    initial_stop = EXCLUDED.initial_stop,
    final_stop = EXCLUDED.final_stop,
    target_1 = EXCLUDED.target_1,
    target_2 = EXCLUDED.target_2,
    exit_price = EXCLUDED.exit_price,
    risk_usdt = EXCLUDED.risk_usdt,
    initial_risk_distance = EXCLUDED.initial_risk_distance,
    initial_quantity = EXCLUDED.initial_quantity,
    dca_quantity = EXCLUDED.dca_quantity,
    final_quantity = EXCLUDED.final_quantity,
    fees = EXCLUDED.fees,
    slippage = EXCLUDED.slippage,
    funding = EXCLUDED.funding,
    status = EXCLUDED.status,
    exit_reason = EXCLUDED.exit_reason,
    gross_pnl = EXCLUDED.gross_pnl,
    net_pnl = EXCLUDED.net_pnl,
    pnl_r = EXCLUDED.pnl_r,
    mfe_r = EXCLUDED.mfe_r,
    mae_r = EXCLUDED.mae_r,
    market_regime = EXCLUDED.market_regime;
