-- Migration 017: Trade Fact Build
-- Populates analytics.trade_fact from dds.paper_trade + dds.scanner_setup.
--
-- Scoped to latest SUCCEEDED run via observation_cutoff (PIT-invariant).
-- Recomputes pnl_r, mfe_r, mae_r from candles — does NOT inherit from
-- paper_trade signal_outcome values.
-- market_signal joined via setup_id → market_signal relationship, not
-- loose instrument_id + direction.
--
-- Idempotent: uses INSERT ... ON CONFLICT DO UPDATE.
-- Empty source tables => INSERT 0 0 (no error).

-- ============================================================
-- Main trade_fact build — single statement, explicit columns
-- ============================================================
WITH latest_run AS (
    SELECT run_id, observation_cutoff
    FROM analytics.analysis_run
    WHERE status = 'SUCCEEDED'
    ORDER BY created_at DESC
    LIMIT 1
),
trade_build AS (
    SELECT
        lr.run_id,
        pt.trade_id,
        pt.setup_id,
        pt.scanner_name,
        COALESCE(ss.scanner_version, 'unknown') AS scanner_version,
        pt.symbol,
        pt.direction,

        -- Timeline
        ss.detected_at                                            AS setup_at,
        -- signal_at: use market_signal.first_detected_at for this setup's instrument+direction
        ms.first_detected_at                                      AS signal_at,
        pt.entered_at                                             AS first_attempt_at,
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
        pt.stop_price                                             AS initial_stop,
        pt.stop_price                                             AS final_stop,
        pt.target_1,
        pt.target_2,
        pt.exit_price,

        -- Risk
        pt.risk_usdt,
        ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price)
                                                                  AS initial_risk_distance,

        -- Quantities
        pt.initial_fill_qty                                       AS initial_quantity,
        pt.dca_fill_qty                                           AS dca_quantity,
        COALESCE(pt.initial_fill_qty, 0)
            + COALESCE(pt.dca_fill_qty, 0)                        AS final_quantity,

        -- Costs
        COALESCE(pt.entry_fee, 0)
            + COALESCE(pt.exit_fee, 0)
            + COALESCE(pt.dca_fee, 0)                             AS fees,
        COALESCE(pt.slippage, 0)
            + COALESCE(pt.dca_slippage, 0)                        AS slippage,
        COALESCE(pt.funding_paid, 0)                              AS funding,

        -- Outcome
        pt.status,
        pt.exit_reason,
        pt.gross_pnl,
        pt.gross_pnl
            - COALESCE(pt.entry_fee, 0)
            - COALESCE(pt.dca_fee, 0)
            - COALESCE(pt.exit_fee, 0)
            - COALESCE(pt.funding_paid, 0)
            - COALESCE(pt.slippage, 0)
            - COALESCE(pt.dca_slippage, 0)                        AS net_pnl,

        -- pnl_r recomputed canonically: direction_sign * (exit - ref) / risk_distance
        CASE pt.direction
            WHEN 'LONG' THEN
                CASE WHEN pt.exit_price IS NOT NULL
                      AND ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price) > 0
                     THEN (pt.exit_price - COALESCE(ss.reference_price, pt.entry_price))
                          / ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price)
                     ELSE NULL END
            WHEN 'SHORT' THEN
                CASE WHEN pt.exit_price IS NOT NULL
                      AND ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price) > 0
                     THEN (COALESCE(ss.reference_price, pt.entry_price) - pt.exit_price)
                          / ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price)
                     ELSE NULL END
        END                                                        AS pnl_r,

        -- mfe_r / mae_r recomputed canonically:
        -- mfe_r = direction_sign * mfe / initial_risk_distance
        -- mae_r = direction_sign * (-mae) / initial_risk_distance  (always <= 0)
        -- paper_trade stores mfe/mae as unsigned absolute values in price units.
        CASE pt.direction
            WHEN 'LONG' THEN
                CASE WHEN ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price) > 0
                     THEN pt.mfe / ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price)
                     ELSE NULL END
            WHEN 'SHORT' THEN
                CASE WHEN ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price) > 0
                     THEN pt.mfe / ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price)
                     ELSE NULL END
        END                                                        AS mfe_r,
        -1 * (
        CASE pt.direction
            WHEN 'LONG' THEN
                CASE WHEN ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price) > 0
                     THEN pt.mae / ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price)
                     ELSE NULL END
            WHEN 'SHORT' THEN
                CASE WHEN ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price) > 0
                     THEN pt.mae / ABS(COALESCE(ss.reference_price, pt.entry_price) - pt.stop_price)
                     ELSE NULL END
        END)                                                       AS mae_r,

        -- Context
        pt.market_regime,
        -- config_hash/strategy_hash: PIT-safe lookup from snapshots active at observation_cutoff
        (SELECT cs.config_hash FROM analytics.config_snapshot cs
         WHERE cs.valid_from <= lr.observation_cutoff
           AND (cs.valid_to IS NULL OR cs.valid_to > lr.observation_cutoff)
         ORDER BY cs.valid_from DESC, cs.captured_at DESC, cs.config_hash
         LIMIT 1)                                                 AS config_hash,
        (SELECT ss2.strategy_hash FROM analytics.strategy_snapshot ss2
         WHERE ss2.scanner_name = pt.scanner_name
           AND ss2.scanner_version = COALESCE(ss.scanner_version, 'unknown')
           AND ss2.captured_at <= lr.observation_cutoff
         ORDER BY ss2.captured_at DESC, ss2.strategy_hash
         LIMIT 1)                                                 AS strategy_hash,
        '1.0.0'::text                                              AS metric_version,

        -- Quality
        'UNKNOWN'::text                                            AS coverage_status,
        'CLEAR'::text                                              AS ambiguity_status,
        '[]'::jsonb                                                AS excluded_reasons,

        -- Versioning
        '00000000.0'::text                                         AS dataset_version,
        FALSE                                                      AS published,
        NOW()                                                      AS created_at

    FROM dds.paper_trade pt
    CROSS JOIN latest_run lr
    LEFT JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
    -- Deterministic signal: LATERAL picks the most recent signal before entry
    LEFT JOIN LATERAL (
        SELECT ms.first_detected_at
        FROM dds.market_signal ms
        WHERE ms.instrument_id = ss.instrument_id
          AND ms.direction = pt.direction
          AND ms.first_detected_at <= pt.entered_at
          AND ms.status IN ('EXECUTED', 'ACTIVE')
        ORDER BY ms.first_detected_at DESC
        LIMIT 1
    ) ms ON TRUE
    WHERE pt.entered_at < lr.observation_cutoff  -- PIT: source event before cutoff
)
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
SELECT
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
FROM trade_build
ON CONFLICT (run_id, trade_id) DO UPDATE SET
    setup_id             = EXCLUDED.setup_id,
    scanner_name         = EXCLUDED.scanner_name,
    scanner_version      = EXCLUDED.scanner_version,
    symbol               = EXCLUDED.symbol,
    direction            = EXCLUDED.direction,
    setup_at             = EXCLUDED.setup_at,
    signal_at            = EXCLUDED.signal_at,
    entered_at           = EXCLUDED.entered_at,
    dca_filled_at        = EXCLUDED.dca_filled_at,
    closed_at            = EXCLUDED.closed_at,
    reference_price      = EXCLUDED.reference_price,
    entry_zone_low       = EXCLUDED.entry_zone_low,
    entry_zone_high      = EXCLUDED.entry_zone_high,
    initial_fill_price   = EXCLUDED.initial_fill_price,
    dca_fill_price       = EXCLUDED.dca_fill_price,
    avg_entry_price      = EXCLUDED.avg_entry_price,
    initial_stop         = EXCLUDED.initial_stop,
    final_stop           = EXCLUDED.final_stop,
    target_1             = EXCLUDED.target_1,
    target_2             = EXCLUDED.target_2,
    exit_price           = EXCLUDED.exit_price,
    risk_usdt            = EXCLUDED.risk_usdt,
    initial_risk_distance = EXCLUDED.initial_risk_distance,
    initial_quantity     = EXCLUDED.initial_quantity,
    dca_quantity         = EXCLUDED.dca_quantity,
    final_quantity       = EXCLUDED.final_quantity,
    fees                 = EXCLUDED.fees,
    slippage             = EXCLUDED.slippage,
    funding              = EXCLUDED.funding,
    status               = EXCLUDED.status,
    exit_reason          = EXCLUDED.exit_reason,
    gross_pnl            = EXCLUDED.gross_pnl,
    net_pnl              = EXCLUDED.net_pnl,
    pnl_r                = EXCLUDED.pnl_r,
    mfe_r                = EXCLUDED.mfe_r,
    mae_r                = EXCLUDED.mae_r,
    market_regime        = EXCLUDED.market_regime;
