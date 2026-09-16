-- Migration 030: Canonical Build Functions
-- Wraps migrations 016-018 procedural SQL into callable functions
-- that accept p_run_id UUID instead of targeting "latest SUCCEEDED run".
--
-- Also adds row-count verification helpers for the Python runner.
--
-- Idempotent: uses CREATE OR REPLACE FUNCTION.

-- ============================================================
-- Helper: set the search_path for all functions in this migration
-- ============================================================

-- ============================================================
-- 1. analytics.build_events(p_run_id UUID) RETURNS BIGINT
--    Wraps migration 016: event reconstruction
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_events(p_run_id UUID)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_count BIGINT;
    v_observation_cutoff TIMESTAMPTZ;
BEGIN
    -- Resolve observation_cutoff from the run
    SELECT observation_cutoff INTO v_observation_cutoff
    FROM analytics.analysis_run
    WHERE run_id = p_run_id;

    IF v_observation_cutoff IS NULL THEN
        RAISE EXCEPTION 'analysis_run % not found', p_run_id;
    END IF;

    -- ============================================================
    -- 1. SETUP_READY events
    -- ============================================================
    INSERT INTO analytics.trade_event
        (run_id, trade_id, setup_id, event_type, event_at, observed_at,
         price, reason_code, payload_json, source_event_key)
    SELECT
        p_run_id, pt.trade_id, pt.setup_id,
        'SETUP_READY', ss.detected_at, ss.created_at,
        ss.reference_price, ss.status,
        jsonb_build_object(
            'scanner_name', ss.scanner_name, 'scanner_version', ss.scanner_version,
            'score', ss.score, 'market_regime', ss.market_regime, 'direction', ss.direction
        ),
        p_run_id || ':setup_ready:' || ss.setup_id
    FROM dds.paper_trade pt
    JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
    WHERE ss.detected_at IS NOT NULL AND ss.detected_at < v_observation_cutoff
    ON CONFLICT (source_event_key) DO NOTHING;

    -- ============================================================
    -- 2. ENTRY_FILLED events
    -- ============================================================
    INSERT INTO analytics.trade_event
        (run_id, trade_id, setup_id, event_type, event_at, observed_at,
         price, quantity, reason_code, payload_json, source_event_key)
    SELECT
        p_run_id, pt.trade_id, pt.setup_id,
        'ENTRY_FILLED', pt.entered_at, pt.created_at,
        pt.entry_price, pt.initial_fill_qty, pt.status,
        jsonb_build_object(
            'entry_fee', pt.entry_fee, 'entry_slippage', pt.slippage,
            'entry_market_price', pt.entry_market_price,
            'position_size', pt.position_size, 'scanner_name', pt.scanner_name
        ),
        p_run_id || ':entry_filled:' || pt.trade_id
    FROM dds.paper_trade pt
    WHERE pt.entered_at IS NOT NULL AND pt.entered_at < v_observation_cutoff
    ON CONFLICT (source_event_key) DO NOTHING;

    -- ============================================================
    -- 3. ENTRY_ATTEMPTED events (expired/invalidated setups without fill)
    -- ============================================================
    INSERT INTO analytics.trade_event
        (run_id, trade_id, setup_id, event_type, event_at, observed_at,
         reason_code, payload_json, source_event_key)
    SELECT
        p_run_id, 0, ss.setup_id,
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
            'scanner_name', ss.scanner_name, 'symbol', i.symbol, 'direction', ss.direction
        ),
        p_run_id || ':entry_attempted:' || ss.setup_id
    FROM dds.scanner_setup ss
    JOIN dds.instrument i ON i.instrument_id = ss.instrument_id
    WHERE ss.status IN ('EXPIRED', 'INVALIDATED')
      AND NOT EXISTS (SELECT 1 FROM dds.paper_trade pt WHERE pt.setup_id = ss.setup_id)
      AND ss.updated_at < v_observation_cutoff
    ON CONFLICT (source_event_key) DO NOTHING;

    -- ============================================================
    -- 4. DCA_PLACED events (virtual order placed at entry time)
    -- ============================================================
    INSERT INTO analytics.trade_event
        (run_id, trade_id, setup_id, event_type, event_at, observed_at,
         price, payload_json, source_event_key)
    SELECT
        p_run_id, pt.trade_id, pt.setup_id,
        'DCA_PLACED', pt.entered_at, pt.created_at,
        pt.dca_price,
        jsonb_build_object(
            'dca_level_atr', pt.dca_level_atr, 'dca_price', pt.dca_price,
            'dca_target_pct', pt.dca_target_pct, 'atr_at_entry', pt.atr_at_entry
        ),
        p_run_id || ':dca_placed:' || pt.trade_id
    FROM dds.paper_trade pt
    WHERE pt.dca_enabled = TRUE AND pt.dca_price IS NOT NULL AND pt.dca_price > 0
      AND pt.entered_at < v_observation_cutoff
    ON CONFLICT (source_event_key) DO NOTHING;

    -- ============================================================
    -- 5. DCA_FILLED events
    -- ============================================================
    INSERT INTO analytics.trade_event
        (run_id, trade_id, setup_id, event_type, event_at, observed_at,
         price, quantity, payload_json, source_event_key)
    SELECT
        p_run_id, pt.trade_id, pt.setup_id,
        'DCA_FILLED', pt.dca_filled_at, pt.updated_at,
        pt.dca_fill_price, pt.dca_fill_qty,
        jsonb_build_object(
            'dca_fee', pt.dca_fee, 'dca_slippage', pt.dca_slippage,
            'avg_entry_price', pt.avg_entry_price, 'dca_state', pt.dca_state
        ),
        p_run_id || ':dca_filled:' || pt.trade_id
    FROM dds.paper_trade pt
    WHERE pt.dca_filled_at IS NOT NULL
      AND pt.dca_fill_price IS NOT NULL AND pt.dca_fill_price > 0
      AND pt.dca_filled_at < v_observation_cutoff
    ON CONFLICT (source_event_key) DO NOTHING;

    -- ============================================================
    -- 6. TRADE_CLOSED events
    -- ============================================================
    INSERT INTO analytics.trade_event
        (run_id, trade_id, setup_id, event_type, event_at, observed_at,
         price, reason_code, payload_json, source_event_key)
    SELECT
        p_run_id, pt.trade_id, pt.setup_id,
        'TRADE_CLOSED', pt.closed_at, pt.updated_at,
        pt.exit_price, pt.exit_reason,
        jsonb_build_object(
            'exit_fee', pt.exit_fee, 'gross_pnl', pt.gross_pnl,
            'pnl_usdt', pt.pnl_usdt, 'pnl_r', pt.pnl_r,
            'mfe_r', pt.mfe_r, 'mae_r', pt.mae_r,
            'duration_sec', pt.duration_sec,
            'funding_paid', pt.funding_paid, 'slippage', pt.slippage
        ),
        p_run_id || ':trade_closed:' || pt.trade_id
    FROM dds.paper_trade pt
    WHERE pt.closed_at IS NOT NULL
      AND pt.exit_price IS NOT NULL
      AND pt.closed_at < v_observation_cutoff
    ON CONFLICT (source_event_key) DO NOTHING;

    -- Count and return total events inserted
    SELECT COUNT(*) INTO v_count
    FROM analytics.trade_event
    WHERE run_id = p_run_id;

    RETURN v_count;
END;
$$;

COMMENT ON FUNCTION analytics.build_events(UUID)
    IS 'Build trade_event rows for a specific run (wraps migration 016). Returns total event count.';


-- ============================================================
-- 2. analytics.build_trade_fact(p_run_id UUID) RETURNS BIGINT
--    Wraps migration 017: trade fact build
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_trade_fact(p_run_id UUID)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_count BIGINT;
    v_observation_cutoff TIMESTAMPTZ;
BEGIN
    -- Resolve observation_cutoff from the run
    SELECT observation_cutoff INTO v_observation_cutoff
    FROM analytics.analysis_run
    WHERE run_id = p_run_id;

    IF v_observation_cutoff IS NULL THEN
        RAISE EXCEPTION 'analysis_run % not found', p_run_id;
    END IF;

    -- Main trade_fact build — single statement, explicit columns
    WITH trade_build AS (
        SELECT
            p_run_id AS run_id,
            pt.trade_id,
            pt.setup_id,
            pt.scanner_name,
            COALESCE(ss.scanner_version, 'unknown') AS scanner_version,
            pt.symbol,
            pt.direction,

            -- Timeline
            ss.detected_at                                            AS setup_at,
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
            NULL::NUMERIC                          AS final_stop,
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

            -- pnl_r recomputed canonically
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

            -- mfe_r / mae_r recomputed canonically
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
            (SELECT cs.config_hash FROM analytics.config_snapshot cs
             WHERE cs.valid_from <= v_observation_cutoff
               AND (cs.valid_to IS NULL OR cs.valid_to > v_observation_cutoff)
             ORDER BY cs.valid_from DESC, cs.captured_at DESC, cs.config_hash
             LIMIT 1)                                                 AS config_hash,
            (SELECT ss2.strategy_hash FROM analytics.strategy_snapshot ss2
             WHERE ss2.scanner_name = pt.scanner_name
               AND ss2.scanner_version = COALESCE(ss.scanner_version, 'unknown')
               AND ss2.captured_at <= v_observation_cutoff
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
        LEFT JOIN dds.scanner_setup ss ON ss.setup_id = pt.setup_id
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
        WHERE pt.entered_at < v_observation_cutoff
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

    -- Count trade facts for this run
    SELECT COUNT(*) INTO v_count
    FROM analytics.trade_fact
    WHERE run_id = p_run_id;

    RETURN v_count;
END;
$$;

COMMENT ON FUNCTION analytics.build_trade_fact(UUID)
    IS 'Build trade_fact rows for a specific run (wraps migration 017). Returns total trade_fact count.';


-- ============================================================
-- 3. analytics.build_setup_fact(p_run_id UUID) RETURNS BIGINT
--    Wraps migration 018: setup fact build
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_setup_fact(p_run_id UUID)
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    v_count BIGINT;
    v_observation_cutoff TIMESTAMPTZ;
BEGIN
    -- Resolve observation_cutoff from the run
    SELECT observation_cutoff INTO v_observation_cutoff
    FROM analytics.analysis_run
    WHERE run_id = p_run_id;

    IF v_observation_cutoff IS NULL THEN
        RAISE EXCEPTION 'analysis_run % not found', p_run_id;
    END IF;

    INSERT INTO analytics.setup_fact (
        run_id, setup_id, scanner_name, scanner_version, symbol, direction,
        setup_at, signal_at, setup_status,
        entry_zone_low, entry_zone_high, reference_price,
        config_hash, strategy_hash, market_regime,
        coverage_status, ambiguity_status, excluded_reasons,
        dataset_version, published, created_at
    )
    SELECT
        p_run_id,
        ss.setup_id,
        ss.scanner_name,
        ss.scanner_version,
        i.symbol,
        ss.direction,
        ss.setup_started_at AS setup_at,
        ss.detected_at AS signal_at,
        ss.status AS setup_status,
        ss.entry_zone_low,
        ss.entry_zone_high,
        ss.reference_price,
        NULL AS config_hash,
        NULL AS strategy_hash,
        ss.market_regime,
        'UNKNOWN' AS coverage_status,
        'CLEAR' AS ambiguity_status,
        '[]'::jsonb AS excluded_reasons,
        '00000000.0' AS dataset_version,
        FALSE AS published,
        NOW() AS created_at
    FROM dds.scanner_setup ss
    JOIN dds.instrument i ON i.instrument_id = ss.instrument_id
    WHERE ss.detected_at < v_observation_cutoff
    ON CONFLICT (run_id, setup_id) DO UPDATE SET
        scanner_name = EXCLUDED.scanner_name,
        scanner_version = EXCLUDED.scanner_version,
        symbol = EXCLUDED.symbol,
        direction = EXCLUDED.direction,
        setup_at = EXCLUDED.setup_at,
        signal_at = EXCLUDED.signal_at,
        setup_status = EXCLUDED.setup_status,
        entry_zone_low = EXCLUDED.entry_zone_low,
        entry_zone_high = EXCLUDED.entry_zone_high,
        reference_price = EXCLUDED.reference_price,
        market_regime = EXCLUDED.market_regime;

    -- Count setup facts for this run
    SELECT COUNT(*) INTO v_count
    FROM analytics.setup_fact
    WHERE run_id = p_run_id;

    RETURN v_count;
END;
$$;

COMMENT ON FUNCTION analytics.build_setup_fact(UUID)
    IS 'Build setup_fact rows for a specific run (wraps migration 018). Returns total setup_fact count.';


-- ============================================================
-- 4. Row count verification helpers
-- ============================================================

CREATE OR REPLACE FUNCTION analytics.count_trade_facts(p_run_id UUID)
RETURNS BIGINT
LANGUAGE sql
STABLE
AS $$
    SELECT COUNT(*)::BIGINT FROM analytics.trade_fact WHERE run_id = p_run_id;
$$;

COMMENT ON FUNCTION analytics.count_trade_facts(UUID)
    IS 'Count trade_fact rows for a given run_id (verification helper)';

CREATE OR REPLACE FUNCTION analytics.count_setup_facts(p_run_id UUID)
RETURNS BIGINT
LANGUAGE sql
STABLE
AS $$
    SELECT COUNT(*)::BIGINT FROM analytics.setup_fact WHERE run_id = p_run_id;
$$;

COMMENT ON FUNCTION analytics.count_setup_facts(UUID)
    IS 'Count setup_fact rows for a given run_id (verification helper)';

CREATE OR REPLACE FUNCTION analytics.count_trade_events(p_run_id UUID)
RETURNS BIGINT
LANGUAGE sql
STABLE
AS $$
    SELECT COUNT(*)::BIGINT FROM analytics.trade_event WHERE run_id = p_run_id;
$$;

COMMENT ON FUNCTION analytics.count_trade_events(UUID)
    IS 'Count trade_event rows for a given run_id (verification helper)';

-- ============================================================
-- Done
-- ============================================================
