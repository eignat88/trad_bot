-- Migration 020: Horizon Metrics Computation
-- Computes forward-looking metrics at fixed time horizons from candles.
--
-- Horizon definitions (from the design document):
--   signal:  1m, 3m, 5m, 15m, 30m, entry_time
--   entry:   1m, 3m, 5m, 15m, 30m
--   dca:     1m, 5m, 15m, 30m
--   exit:    5m, 15m, 30m, 1h, 2h, 4h
--
-- Idempotent: uses INSERT ... ON CONFLICT DO UPDATE.

-- ============================================================
-- Helper: compute horizon metric for a single trade
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.compute_horizon_metric(
    p_trade_id BIGINT,
    p_anchor TEXT,
    p_horizon TEXT,
    p_anchor_price NUMERIC,
    p_anchor_time TIMESTAMPTZ,
    p_direction TEXT,
    p_initial_risk_distance NUMERIC,
    p_instrument_id BIGINT,
    p_cutoff TIMESTAMPTZ
) RETURNS TABLE (
    favorable_move_r NUMERIC,
    adverse_move_r NUMERIC,
    coverage_status TEXT
) AS $$
DECLARE
    horizon_interval INTERVAL;
    end_time TIMESTAMPTZ;
    max_price NUMERIC;
    min_price NUMERIC;
    candle_count BIGINT;
    direction_sign NUMERIC;
BEGIN
    -- Convert horizon string to interval
    CASE p_horizon
        WHEN '1m' THEN horizon_interval := INTERVAL '1 minute';
        WHEN '3m' THEN horizon_interval := INTERVAL '3 minutes';
        WHEN '5m' THEN horizon_interval := INTERVAL '5 minutes';
        WHEN '15m' THEN horizon_interval := INTERVAL '15 minutes';
        WHEN '30m' THEN horizon_interval := INTERVAL '30 minutes';
        WHEN '1h' THEN horizon_interval := INTERVAL '1 hour';
        WHEN '2h' THEN horizon_interval := INTERVAL '2 hours';
        WHEN '4h' THEN horizon_interval := INTERVAL '4 hours';
        WHEN 'entry_time' THEN horizon_interval := INTERVAL '0 minutes';  -- special case
        ELSE horizon_interval := INTERVAL '5 minutes';
    END CASE;

    end_time := p_anchor_time + horizon_interval;

    -- Direction sign: LONG=+1, SHORT=-1
    direction_sign := CASE p_direction WHEN 'LONG' THEN 1.0 ELSE -1.0 END;

    -- Check PIT: end_time must be <= cutoff
    IF end_time > p_cutoff THEN
        favorable_move_r := NULL;
        adverse_move_r := NULL;
        coverage_status := 'INCOMPLETE';
        RETURN NEXT;
        RETURN;
    END IF;

    -- Get max/min prices from closed candles in the horizon window
    SELECT
        MAX(high),
        MIN(low),
        COUNT(*)
    INTO max_price, min_price, candle_count
    FROM market.candle
    WHERE instrument_id = p_instrument_id
      AND open_time >= p_anchor_time
      AND close_time <= end_time
      AND is_closed = TRUE;

    -- Check coverage
    IF candle_count = 0 THEN
        favorable_move_r := NULL;
        adverse_move_r := NULL;
        coverage_status := 'NO_DATA';
        RETURN NEXT;
        RETURN;
    END IF;

    -- Compute R metrics
    IF p_initial_risk_distance > 0 THEN
        favorable_move_r := direction_sign * (max_price - p_anchor_price) / p_initial_risk_distance;
        adverse_move_r := direction_sign * (min_price - p_anchor_price) / p_initial_risk_distance;
    ELSE
        favorable_move_r := NULL;
        adverse_move_r := NULL;
    END IF;

    coverage_status := 'COMPLETE';
    RETURN NEXT;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Main horizon metrics build
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_horizon_metrics(
    p_run_id UUID
) RETURNS BIGINT AS $$
DECLARE
    v_trade RECORD;
    v_anchor RECORD;
    v_horizon TEXT;
    v_result RECORD;
    v_instrument_id BIGINT;
    v_inserted BIGINT := 0;
    v_cutoff TIMESTAMPTZ;
BEGIN
    -- Get observation_cutoff for PIT enforcement
    SELECT observation_cutoff INTO v_cutoff
    FROM analytics.analysis_run
    WHERE run_id = p_run_id;

    -- Get instrument_id mapping
    FOR v_trade IN
        SELECT tf.trade_id, tf.symbol, tf.direction,
               tf.initial_risk_distance, tf.reference_price,
               tf.entered_at, tf.dca_filled_at, tf.closed_at,
               tf.signal_at
        FROM analytics.trade_fact tf
        WHERE tf.run_id = p_run_id
          AND tf.status IN ('CLOSED', 'EXPIRED')
    LOOP
        -- Resolve instrument_id
        SELECT instrument_id INTO v_instrument_id
        FROM dds.instrument
        WHERE symbol = v_trade.symbol
        LIMIT 1;

        IF v_instrument_id IS NULL THEN
            CONTINUE;
        END IF;

        -- Process each anchor type
        FOR v_anchor IN
            SELECT * FROM (VALUES
                ('signal', v_trade.signal_at, v_trade.reference_price),
                ('entry', v_trade.entered_at, v_trade.reference_price),
                ('dca_fill', v_trade.dca_filled_at, v_trade.avg_entry_price),
                ('exit', v_trade.closed_at, v_trade.exit_price)
            ) AS t(anchor, anchor_time, anchor_price)
            WHERE t.anchor_time IS NOT NULL AND t.anchor_price IS NOT NULL
        LOOP
            -- Signal horizons
            IF v_anchor.anchor = 'signal' THEN
                FOREACH v_horizon IN ARRAY ARRAY['1m', '3m', '5m', '15m', '30m'] LOOP
                    SELECT * INTO v_result
                    FROM analytics.compute_horizon_metric(
                        v_trade.trade_id, v_anchor.anchor, v_horizon,
                        v_anchor.anchor_price, v_anchor.anchor_time,
                        v_trade.direction, v_trade.initial_risk_distance,
                        v_instrument_id, v_cutoff
                    );

                    INSERT INTO analytics.trade_horizon_metric (
                        run_id, trade_id, anchor, horizon, metric_version,
                        favorable_move_r, adverse_move_r, coverage_status
                    ) VALUES (
                        p_run_id, v_trade.trade_id, v_anchor.anchor, v_horizon, '1.0.0',
                        v_result.favorable_move_r, v_result.adverse_move_r, v_result.coverage_status
                    )
                    ON CONFLICT (run_id, trade_id, anchor, horizon, metric_version) DO UPDATE SET
                        favorable_move_r = EXCLUDED.favorable_move_r,
                        adverse_move_r = EXCLUDED.adverse_move_r,
                        coverage_status = EXCLUDED.coverage_status;

                    v_inserted := v_inserted + 1;
                END LOOP;
            END IF;

            -- Entry horizons
            IF v_anchor.anchor = 'entry' THEN
                FOREACH v_horizon IN ARRAY ARRAY['1m', '3m', '5m', '15m', '30m'] LOOP
                    SELECT * INTO v_result
                    FROM analytics.compute_horizon_metric(
                        v_trade.trade_id, v_anchor.anchor, v_horizon,
                        v_anchor.anchor_price, v_anchor.anchor_time,
                        v_trade.direction, v_trade.initial_risk_distance,
                        v_instrument_id, v_cutoff
                    );

                    INSERT INTO analytics.trade_horizon_metric (
                        run_id, trade_id, anchor, horizon, metric_version,
                        favorable_move_r, adverse_move_r, coverage_status
                    ) VALUES (
                        p_run_id, v_trade.trade_id, v_anchor.anchor, v_horizon, '1.0.0',
                        v_result.favorable_move_r, v_result.adverse_move_r, v_result.coverage_status
                    )
                    ON CONFLICT (run_id, trade_id, anchor, horizon, metric_version) DO UPDATE SET
                        favorable_move_r = EXCLUDED.favorable_move_r,
                        adverse_move_r = EXCLUDED.adverse_move_r,
                        coverage_status = EXCLUDED.coverage_status;

                    v_inserted := v_inserted + 1;
                END LOOP;
            END IF;

            -- DCA fill horizons
            IF v_anchor.anchor = 'dca_fill' THEN
                FOREACH v_horizon IN ARRAY ARRAY['1m', '5m', '15m', '30m'] LOOP
                    SELECT * INTO v_result
                    FROM analytics.compute_horizon_metric(
                        v_trade.trade_id, v_anchor.anchor, v_horizon,
                        v_anchor.anchor_price, v_anchor.anchor_time,
                        v_trade.direction, v_trade.initial_risk_distance,
                        v_instrument_id, v_cutoff
                    );

                    INSERT INTO analytics.trade_horizon_metric (
                        run_id, trade_id, anchor, horizon, metric_version,
                        favorable_move_r, adverse_move_r, coverage_status
                    ) VALUES (
                        p_run_id, v_trade.trade_id, v_anchor.anchor, v_horizon, '1.0.0',
                        v_result.favorable_move_r, v_result.adverse_move_r, v_result.coverage_status
                    )
                    ON CONFLICT (run_id, trade_id, anchor, horizon, metric_version) DO UPDATE SET
                        favorable_move_r = EXCLUDED.favorable_move_r,
                        adverse_move_r = EXCLUDED.adverse_move_r,
                        coverage_status = EXCLUDED.coverage_status;

                    v_inserted := v_inserted + 1;
                END LOOP;
            END IF;

            -- Exit horizons
            IF v_anchor.anchor = 'exit' THEN
                FOREACH v_horizon IN ARRAY ARRAY['5m', '15m', '30m', '1h', '2h', '4h'] LOOP
                    SELECT * INTO v_result
                    FROM analytics.compute_horizon_metric(
                        v_trade.trade_id, v_anchor.anchor, v_horizon,
                        v_anchor.anchor_price, v_anchor.anchor_time,
                        v_trade.direction, v_trade.initial_risk_distance,
                        v_instrument_id, v_cutoff
                    );

                    INSERT INTO analytics.trade_horizon_metric (
                        run_id, trade_id, anchor, horizon, metric_version,
                        favorable_move_r, adverse_move_r, coverage_status
                    ) VALUES (
                        p_run_id, v_trade.trade_id, v_anchor.anchor, v_horizon, '1.0.0',
                        v_result.favorable_move_r, v_result.adverse_move_r, v_result.coverage_status
                    )
                    ON CONFLICT (run_id, trade_id, anchor, horizon, metric_version) DO UPDATE SET
                        favorable_move_r = EXCLUDED.favorable_move_r,
                        adverse_move_r = EXCLUDED.adverse_move_r,
                        coverage_status = EXCLUDED.coverage_status;

                    v_inserted := v_inserted + 1;
                END LOOP;
            END IF;
        END LOOP;
    END LOOP;

    RETURN v_inserted;
END;
$$ LANGUAGE plpgsql;
