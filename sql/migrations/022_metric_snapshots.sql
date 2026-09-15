-- Migration 022: Metric Snapshots (24h/7d/30d)
-- Computes period aggregates with semi-open windows [from, to).
--
-- Segments:
--   scanner          — scanner_name = value
--   direction        — direction = value
--   scanner_direction — scanner_name || ':' || direction = value
--   symbol           — symbol = value
--   regime           — COALESCE(market_regime, 'UNKNOWN') = value
--   exit_reason      — COALESCE(exit_reason, 'UNKNOWN') = value
--
-- NOTE: dca_state segment is deferred to a future migration (027)
--       because analytics.trade_fact does not yet have dca_state column.
--
-- Idempotent: uses INSERT ... ON CONFLICT DO UPDATE.

-- ============================================================
-- Helper: compute metric snapshot for a period, filtered by segment
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.compute_metric_snapshot(
    p_run_id UUID,
    p_period TEXT,
    p_segment_prefix TEXT,
    p_segment_value TEXT,
    p_window_from TIMESTAMPTZ,
    p_window_to TIMESTAMPTZ
) RETURNS BIGINT AS $$
DECLARE
    v_inserted BIGINT := 0;
    v_segment TEXT;
    v_count BIGINT;
    v_win_rate NUMERIC;
    v_profit_factor NUMERIC;
    v_avg_r NUMERIC;
    v_total_pnl NUMERIC;
    v_sharpe NUMERIC;
    v_expectancy NUMERIC;
BEGIN
    v_segment := p_segment_prefix || ':' || p_segment_value;

    -- ================================================================
    -- Segment filtering: each aggregate query must include the matching
    -- WHERE clause so that metrics are scoped to the requested segment.
    --
    -- Unknown prefix => return 0 (no metrics) rather than silently
    -- computing global aggregates that would be mislabelled.
    -- ================================================================

    IF p_segment_prefix NOT IN (
        'scanner', 'direction', 'scanner_direction',
        'symbol', 'regime', 'exit_reason'
    ) THEN
        -- Unknown segment prefix — do not compute global metrics.
        RETURN 0;
    END IF;

    -- Basic counts — with segment filter
    SELECT COUNT(*) INTO v_count
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.closed_at >= p_window_from
      AND tf.closed_at < p_window_to  -- semi-open [from, to)
      AND (
          (p_segment_prefix = 'scanner' AND tf.scanner_name = p_segment_value)
          OR (p_segment_prefix = 'direction' AND tf.direction = p_segment_value)
          OR (p_segment_prefix = 'scanner_direction' AND (tf.scanner_name || ':' || tf.direction) = p_segment_value)
          OR (p_segment_prefix = 'symbol' AND tf.symbol = p_segment_value)
          OR (p_segment_prefix = 'regime' AND COALESCE(tf.market_regime, 'UNKNOWN') = p_segment_value)
          OR (p_segment_prefix = 'exit_reason' AND COALESCE(tf.exit_reason, 'UNKNOWN') = p_segment_value)
      );

    IF v_count = 0 THEN
        RETURN 0;
    END IF;

    -- Win rate
    SELECT
        COUNT(CASE WHEN tf.pnl_r > 0 THEN 1 END)::NUMERIC / v_count
    INTO v_win_rate
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.closed_at >= p_window_from
      AND tf.closed_at < p_window_to
      AND (
          (p_segment_prefix = 'scanner' AND tf.scanner_name = p_segment_value)
          OR (p_segment_prefix = 'direction' AND tf.direction = p_segment_value)
          OR (p_segment_prefix = 'scanner_direction' AND (tf.scanner_name || ':' || tf.direction) = p_segment_value)
          OR (p_segment_prefix = 'symbol' AND tf.symbol = p_segment_value)
          OR (p_segment_prefix = 'regime' AND COALESCE(tf.market_regime, 'UNKNOWN') = p_segment_value)
          OR (p_segment_prefix = 'exit_reason' AND COALESCE(tf.exit_reason, 'UNKNOWN') = p_segment_value)
      );

    -- Profit factor
    SELECT
        SUM(CASE WHEN tf.pnl_r > 0 THEN tf.pnl_r ELSE 0 END)
        / NULLIF(ABS(SUM(CASE WHEN tf.pnl_r < 0 THEN tf.pnl_r ELSE 0 END)), 0)
    INTO v_profit_factor
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.closed_at >= p_window_from
      AND tf.closed_at < p_window_to
      AND (
          (p_segment_prefix = 'scanner' AND tf.scanner_name = p_segment_value)
          OR (p_segment_prefix = 'direction' AND tf.direction = p_segment_value)
          OR (p_segment_prefix = 'scanner_direction' AND (tf.scanner_name || ':' || tf.direction) = p_segment_value)
          OR (p_segment_prefix = 'symbol' AND tf.symbol = p_segment_value)
          OR (p_segment_prefix = 'regime' AND COALESCE(tf.market_regime, 'UNKNOWN') = p_segment_value)
          OR (p_segment_prefix = 'exit_reason' AND COALESCE(tf.exit_reason, 'UNKNOWN') = p_segment_value)
      );

    -- Average R
    SELECT AVG(tf.pnl_r) INTO v_avg_r
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.closed_at >= p_window_from
      AND tf.closed_at < p_window_to
      AND (
          (p_segment_prefix = 'scanner' AND tf.scanner_name = p_segment_value)
          OR (p_segment_prefix = 'direction' AND tf.direction = p_segment_value)
          OR (p_segment_prefix = 'scanner_direction' AND (tf.scanner_name || ':' || tf.direction) = p_segment_value)
          OR (p_segment_prefix = 'symbol' AND tf.symbol = p_segment_value)
          OR (p_segment_prefix = 'regime' AND COALESCE(tf.market_regime, 'UNKNOWN') = p_segment_value)
          OR (p_segment_prefix = 'exit_reason' AND COALESCE(tf.exit_reason, 'UNKNOWN') = p_segment_value)
      );

    -- Total PnL
    SELECT SUM(tf.net_pnl) INTO v_total_pnl
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.closed_at >= p_window_from
      AND tf.closed_at < p_window_to
      AND (
          (p_segment_prefix = 'scanner' AND tf.scanner_name = p_segment_value)
          OR (p_segment_prefix = 'direction' AND tf.direction = p_segment_value)
          OR (p_segment_prefix = 'scanner_direction' AND (tf.scanner_name || ':' || tf.direction) = p_segment_value)
          OR (p_segment_prefix = 'symbol' AND tf.symbol = p_segment_value)
          OR (p_segment_prefix = 'regime' AND COALESCE(tf.market_regime, 'UNKNOWN') = p_segment_value)
          OR (p_segment_prefix = 'exit_reason' AND COALESCE(tf.exit_reason, 'UNKNOWN') = p_segment_value)
      );

    -- Sharpe approximation
    SELECT
        AVG(tf.pnl_r) / NULLIF(STDDEV(tf.pnl_r), 0)
    INTO v_sharpe
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.closed_at >= p_window_from
      AND tf.closed_at < p_window_to
      AND (
          (p_segment_prefix = 'scanner' AND tf.scanner_name = p_segment_value)
          OR (p_segment_prefix = 'direction' AND tf.direction = p_segment_value)
          OR (p_segment_prefix = 'scanner_direction' AND (tf.scanner_name || ':' || tf.direction) = p_segment_value)
          OR (p_segment_prefix = 'symbol' AND tf.symbol = p_segment_value)
          OR (p_segment_prefix = 'regime' AND COALESCE(tf.market_regime, 'UNKNOWN') = p_segment_value)
          OR (p_segment_prefix = 'exit_reason' AND COALESCE(tf.exit_reason, 'UNKNOWN') = p_segment_value)
      );

    -- Expectancy
    SELECT
        v_win_rate * AVG(CASE WHEN tf.pnl_r > 0 THEN tf.pnl_r END)
        + (1 - v_win_rate) * AVG(CASE WHEN tf.pnl_r < 0 THEN tf.pnl_r END)
    INTO v_expectancy
    FROM analytics.trade_fact tf
    WHERE tf.run_id = p_run_id
      AND tf.status IN ('CLOSED', 'EXPIRED')
      AND tf.closed_at >= p_window_from
      AND tf.closed_at < p_window_to
      AND (
          (p_segment_prefix = 'scanner' AND tf.scanner_name = p_segment_value)
          OR (p_segment_prefix = 'direction' AND tf.direction = p_segment_value)
          OR (p_segment_prefix = 'scanner_direction' AND (tf.scanner_name || ':' || tf.direction) = p_segment_value)
          OR (p_segment_prefix = 'symbol' AND tf.symbol = p_segment_value)
          OR (p_segment_prefix = 'regime' AND COALESCE(tf.market_regime, 'UNKNOWN') = p_segment_value)
          OR (p_segment_prefix = 'exit_reason' AND COALESCE(tf.exit_reason, 'UNKNOWN') = p_segment_value)
      );

    -- Insert all metrics
    INSERT INTO analytics.metric_snapshot
        (run_id, period, segment, metric_name, metric_version,
         metric_value, sample_count, window_from, window_to)
    VALUES
        (p_run_id, p_period, v_segment, 'win_rate', '1.0.0',
         v_win_rate, v_count, p_window_from, p_window_to),
        (p_run_id, p_period, v_segment, 'profit_factor', '1.0.0',
         v_profit_factor, v_count, p_window_from, p_window_to),
        (p_run_id, p_period, v_segment, 'avg_r', '1.0.0',
         v_avg_r, v_count, p_window_from, p_window_to),
        (p_run_id, p_period, v_segment, 'total_pnl_usdt', '1.0.0',
         v_total_pnl, v_count, p_window_from, p_window_to),
        (p_run_id, p_period, v_segment, 'sharpe_approx', '1.0.0',
         v_sharpe, v_count, p_window_from, p_window_to),
        (p_run_id, p_period, v_segment, 'expectancy_r', '1.0.0',
         v_expectancy, v_count, p_window_from, p_window_to)
    ON CONFLICT (run_id, period, segment, metric_name, metric_version) DO UPDATE SET
        metric_value = EXCLUDED.metric_value,
        sample_count = EXCLUDED.sample_count,
        window_from = EXCLUDED.window_from,
        window_to = EXCLUDED.window_to;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Master function: build all metric snapshots for a run
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.build_metric_snapshots(
    p_run_id UUID
) RETURNS JSONB AS $$
DECLARE
    v_total BIGINT := 0;
    v_period TEXT;
    v_interval INTERVAL;
    v_window_from TIMESTAMPTZ;
    v_window_to TIMESTAMPTZ;
    v_seg RECORD;
BEGIN
    -- Define periods
    FOR v_period, v_interval IN
        SELECT * FROM (VALUES
            ('24h', INTERVAL '24 hours'),
            ('7d', INTERVAL '7 days'),
            ('30d', INTERVAL '30 days')
        ) AS t(period, interval)
    LOOP
        -- Window: [from, to) where to = run observation_cutoff
        SELECT observation_cutoff INTO v_window_to
        FROM analytics.analysis_run
        WHERE run_id = p_run_id;

        v_window_from := v_window_to - v_interval;

        -- Segment by scanner
        FOR v_seg IN
            SELECT DISTINCT scanner_name
            FROM analytics.trade_fact
            WHERE run_id = p_run_id
              AND status IN ('CLOSED', 'EXPIRED')
        LOOP
            v_total := v_total + analytics.compute_metric_snapshot(
                p_run_id, v_period, 'scanner', v_seg.scanner_name,
                v_window_from, v_window_to
            );
        END LOOP;

        -- Segment by direction
        FOR v_seg IN
            SELECT DISTINCT direction
            FROM analytics.trade_fact
            WHERE run_id = p_run_id
              AND status IN ('CLOSED', 'EXPIRED')
        LOOP
            v_total := v_total + analytics.compute_metric_snapshot(
                p_run_id, v_period, 'direction', v_seg.direction,
                v_window_from, v_window_to
            );
        END LOOP;

        -- Segment by scanner+direction
        FOR v_seg IN
            SELECT DISTINCT scanner_name || ':' || direction AS seg
            FROM analytics.trade_fact
            WHERE run_id = p_run_id
              AND status IN ('CLOSED', 'EXPIRED')
        LOOP
            v_total := v_total + analytics.compute_metric_snapshot(
                p_run_id, v_period, 'scanner_direction', v_seg.seg,
                v_window_from, v_window_to
            );
        END LOOP;

        -- Segment by symbol
        FOR v_seg IN
            SELECT DISTINCT symbol
            FROM analytics.trade_fact
            WHERE run_id = p_run_id
              AND status IN ('CLOSED', 'EXPIRED')
        LOOP
            v_total := v_total + analytics.compute_metric_snapshot(
                p_run_id, v_period, 'symbol', v_seg.symbol,
                v_window_from, v_window_to
            );
        END LOOP;

        -- Segment by market_regime
        FOR v_seg IN
            SELECT DISTINCT COALESCE(market_regime, 'UNKNOWN') AS regime
            FROM analytics.trade_fact
            WHERE run_id = p_run_id
              AND status IN ('CLOSED', 'EXPIRED')
        LOOP
            v_total := v_total + analytics.compute_metric_snapshot(
                p_run_id, v_period, 'regime', v_seg.regime,
                v_window_from, v_window_to
            );
        END LOOP;

        -- Segment by exit_reason
        FOR v_seg IN
            SELECT DISTINCT COALESCE(exit_reason, 'UNKNOWN') AS reason
            FROM analytics.trade_fact
            WHERE run_id = p_run_id
              AND status IN ('CLOSED', 'EXPIRED')
        LOOP
            v_total := v_total + analytics.compute_metric_snapshot(
                p_run_id, v_period, 'exit_reason', v_seg.reason,
                v_window_from, v_window_to
            );
        END LOOP;
    END LOOP;

    RETURN jsonb_build_object('total_metrics', v_total);
END;
$$ LANGUAGE plpgsql;
