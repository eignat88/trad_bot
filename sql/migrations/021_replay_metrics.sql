-- Migration 021: Replay Metrics Computation
-- Computes scenario-based replay metrics for closed trades.
--
-- Scenario families:
--   entry:  ACTUAL, NEXT_BAR_MARKET, DIRECTIONAL_STOP, FIRST_1M_RETEST
--   dca:    ACTUAL, RISK_NORMALIZED_NO_DCA
--   exit:   ACTUAL, FIXED_TP, BREAKEVEN, PARTIAL_TP, TRAILING, ATR_TRAILING, TIME_EXIT
--   stop:   ACTUAL, WIDER_STOP_DIAGNOSTIC
--
-- Idempotent: uses INSERT ... ON CONFLICT DO UPDATE.

-- ============================================================
-- 1. ACTUAL scenario — mirrors the real trade outcome
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_replay_actual(
    p_run_id UUID
) RETURNS BIGINT AS $$
DECLARE
    v_inserted BIGINT := 0;
BEGIN
    INSERT INTO analytics.trade_replay_metric (
        run_id, trade_id, scenario, metric_version,
        simulated_pnl_r, simulated_exit_price, simulated_exit_at, simulated_exit_reason,
        scenario_family, scenario_params,
        coverage_status, ambiguity_status
    )
    SELECT
        p_run_id,
        tf.trade_id,
        'ACTUAL',
        '1.0.0',
        tf.pnl_r,
        tf.exit_price,
        tf.closed_at,
        tf.exit_reason,
        'actual',
        '{}'::jsonb,
        'COMPLETE',
        'CLEAR'
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.pnl_r IS NOT NULL
    ON CONFLICT (run_id, trade_id, scenario, metric_version) DO UPDATE SET
        simulated_pnl_r = EXCLUDED.simulated_pnl_r,
        simulated_exit_price = EXCLUDED.simulated_exit_price,
        simulated_exit_at = EXCLUDED.simulated_exit_at,
        simulated_exit_reason = EXCLUDED.simulated_exit_reason;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 2. FIXED_TP scenario — what if we used a fixed TP?
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_replay_fixed_tp(
    p_run_id UUID,
    p_tp_multiplier NUMERIC DEFAULT 2.0
) RETURNS BIGINT AS $$
DECLARE
    v_inserted BIGINT := 0;
BEGIN
    INSERT INTO analytics.trade_replay_metric (
        run_id, trade_id, scenario, metric_version,
        simulated_pnl_r, simulated_exit_price, simulated_exit_at, simulated_exit_reason,
        scenario_family, scenario_params,
        coverage_status, ambiguity_status
    )
    SELECT
        p_run_id,
        tf.trade_id,
        'FIXED_TP_' || REPLACE(p_tp_multiplier::TEXT, '.', '_'),
        '1.0.0',
        CASE tf.direction
            WHEN 'LONG' THEN
                (tf.reference_price + p_tp_multiplier * tf.initial_risk_distance - tf.initial_fill_price)
                / NULLIF(tf.initial_risk_distance, 0)
            WHEN 'SHORT' THEN
                (tf.initial_fill_price - (tf.reference_price - p_tp_multiplier * tf.initial_risk_distance))
                / NULLIF(tf.initial_risk_distance, 0)
        END,
        CASE tf.direction
            WHEN 'LONG' THEN tf.reference_price + p_tp_multiplier * tf.initial_risk_distance
            WHEN 'SHORT' THEN tf.reference_price - p_tp_multiplier * tf.initial_risk_distance
        END,
        tf.entered_at + INTERVAL '1 hour',  -- placeholder: candle-based simulation not yet implemented
        'FIXED_TP',
        'exit',
        jsonb_build_object('tp_multiplier', p_tp_multiplier),
        'INCOMPLETE',  -- requires candle replay for accurate exit_at
        'CLEAR'
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.initial_risk_distance > 0
    ON CONFLICT (run_id, trade_id, scenario, metric_version) DO UPDATE SET
        simulated_pnl_r = EXCLUDED.simulated_pnl_r,
        simulated_exit_price = EXCLUDED.simulated_exit_price;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 3. BREAKEVEN scenario — what if we exited at breakeven?
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_replay_breakeven(
    p_run_id UUID
) RETURNS BIGINT AS $$
DECLARE
    v_inserted BIGINT := 0;
BEGIN
    INSERT INTO analytics.trade_replay_metric (
        run_id, trade_id, scenario, metric_version,
        simulated_pnl_r, simulated_exit_price, simulated_exit_at, simulated_exit_reason,
        scenario_family, scenario_params,
        coverage_status, ambiguity_status
    )
    SELECT
        p_run_id,
        tf.trade_id,
        'BREAKEVEN',
        '1.0.0',
        0.0,  -- breakeven = 0 R
        tf.reference_price,  -- exit at reference price
        tf.entered_at + INTERVAL '30 minutes',  -- placeholder: candle-based simulation not yet implemented
        'BREAKEVEN',
        'exit',
        '{}'::jsonb,
        'INCOMPLETE',  -- requires candle replay for accurate exit_at
        'CLEAR'
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
    ON CONFLICT (run_id, trade_id, scenario, metric_version) DO UPDATE SET
        simulated_pnl_r = EXCLUDED.simulated_pnl_r,
        simulated_exit_price = EXCLUDED.simulated_exit_price;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 4. RISK_NORMALIZED_NO_DCA scenario — what if DCA was disabled?
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_replay_no_dca(
    p_run_id UUID
) RETURNS BIGINT AS $$
DECLARE
    v_inserted BIGINT := 0;
BEGIN
    INSERT INTO analytics.trade_replay_metric (
        run_id, trade_id, scenario, metric_version,
        simulated_pnl_r, simulated_exit_price, simulated_exit_at, simulated_exit_reason,
        scenario_family, scenario_params,
        coverage_status, ambiguity_status
    )
    SELECT
        p_run_id,
        tf.trade_id,
        'RISK_NORMALIZED_NO_DCA',
        '1.0.0',
        -- Without DCA, use original entry price and full position
        CASE tf.direction
            WHEN 'LONG' THEN
                (tf.exit_price - tf.initial_fill_price)
                / NULLIF(tf.initial_risk_distance, 0)
            WHEN 'SHORT' THEN
                (tf.initial_fill_price - tf.exit_price)
                / NULLIF(tf.initial_risk_distance, 0)
        END,
        tf.exit_price,
        tf.closed_at,
        tf.exit_reason,
        'dca',
        jsonb_build_object('dca_enabled_original', tf.dca_filled_at IS NOT NULL),
        'COMPLETE',
        'CLEAR'
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.dca_filled_at IS NOT NULL  -- only for trades that had DCA
      AND tf.initial_risk_distance > 0
    ON CONFLICT (run_id, trade_id, scenario, metric_version) DO UPDATE SET
        simulated_pnl_r = EXCLUDED.simulated_pnl_r,
        simulated_exit_price = EXCLUDED.simulated_exit_price;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 5. WIDER_STOP_DIAGNOSTIC — what if stop was wider?
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_replay_wider_stop(
    p_run_id UUID,
    p_wider_multiplier NUMERIC DEFAULT 2.0
) RETURNS BIGINT AS $$
DECLARE
    v_inserted BIGINT := 0;
BEGIN
    INSERT INTO analytics.trade_replay_metric (
        run_id, trade_id, scenario, metric_version,
        simulated_pnl_r, simulated_exit_price, simulated_exit_at, simulated_exit_reason,
        scenario_family, scenario_params,
        coverage_status, ambiguity_status
    )
    SELECT
        p_run_id,
        tf.trade_id,
        'WIDER_STOP_' || REPLACE(p_wider_multiplier::TEXT, '.', '_'),
        '1.0.0',
        tf.pnl_r,  -- wider stop would have kept position open longer
        tf.exit_price,
        tf.closed_at,
        tf.exit_reason,
        'stop',
        jsonb_build_object('wider_multiplier', p_wider_multiplier, 'diagnostic_only', TRUE),
        'INCOMPLETE',  -- requires candle replay for accurate wider-stop simulation
        'CLEAR'
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.exit_reason LIKE '%STOP%'
    ON CONFLICT (run_id, trade_id, scenario, metric_version) DO NOTHING;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Master function: build all replay metrics for a run
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_all_replay_metrics(
    p_run_id UUID
) RETURNS JSONB AS $$
DECLARE
    v_total BIGINT := 0;
    v_actual BIGINT;
    v_fixed_tp BIGINT;
    v_breakeven BIGINT;
    v_no_dca BIGINT;
    v_wider_stop BIGINT;
BEGIN
    v_actual := analytics.build_replay_actual(p_run_id);
    v_total := v_total + v_actual;

    v_fixed_tp := analytics.build_replay_fixed_tp(p_run_id, 2.0);
    v_total := v_total + v_fixed_tp;

    v_breakeven := analytics.build_replay_breakeven(p_run_id);
    v_total := v_total + v_breakeven;

    v_no_dca := analytics.build_replay_no_dca(p_run_id);
    v_total := v_total + v_no_dca;

    v_wider_stop := analytics.build_replay_wider_stop(p_run_id, 2.0);
    v_total := v_total + v_wider_stop;

    RETURN jsonb_build_object(
        'total', v_total,
        'actual', v_actual,
        'fixed_tp', v_fixed_tp,
        'breakeven', v_breakeven,
        'no_dca', v_no_dca,
        'wider_stop', v_wider_stop
    );
END;
$$ LANGUAGE plpgsql;
