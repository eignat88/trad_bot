-- Migration 023: Quality Rules
-- Implements all quality checks for the canonical analytics layer.
--
-- Quality rules from the design document:
--   1. Duplicate trade/setup grain
--   2. Orphan setup (no corresponding trade)
--   3. Orphan trade (no corresponding setup)
--   4. entered_at without ENTRY_FILLED event
--   5. Closed trade without exit event
--   6. DCA state != DCA event
--   7. Invalid MFE/MAE sign
--   8. Missing config_hash
--   9. Incomplete candle coverage
--   10. PIT violation
--
-- PIT violation => run FAILED
--
-- IMPORTANT: jsonb_agg with LIMIT is not valid PostgreSQL syntax.
-- All functions use CTE/subquery pattern to separate COUNT(*) from
-- the sample LIMIT 10 aggregation.

-- ============================================================
-- 1. Check: duplicate trade grain
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_duplicate_trade_grain(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_total BIGINT;
    v_dupes BIGINT;
    v_samples JSONB;
BEGIN
    -- Duplicates = trade_id appears more than once in the same run
    WITH numbered AS (
        SELECT trade_id,
               ROW_NUMBER() OVER (PARTITION BY run_id, trade_id ORDER BY created_at) AS rn
        FROM analytics.trade_fact
        WHERE run_id = p_run_id
    ),
    affected AS (
        SELECT trade_id FROM numbered WHERE rn > 1
    ),
    sample AS (
        SELECT trade_id FROM affected LIMIT 10
    )
    SELECT
        COUNT(*) INTO v_dupes
    FROM affected;

    SELECT COUNT(*) INTO v_total FROM analytics.trade_fact WHERE run_id = p_run_id;

    SELECT COALESCE(jsonb_agg(trade_id), '[]'::jsonb) INTO v_samples FROM sample;

    RETURN QUERY SELECT
        'duplicate_trade_grain'::TEXT,
        'BLOCKING'::TEXT,
        CASE WHEN v_dupes > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_dupes,
        jsonb_build_object('total_rows', v_total, 'duplicate_count', v_dupes, 'sample_ids', v_samples);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 2. Check: orphan setup (setup_fact without trade_fact)
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_orphan_setup(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_count BIGINT;
    v_samples JSONB;
BEGIN
    WITH affected AS (
        SELECT sf.setup_id
        FROM analytics.setup_fact sf
        LEFT JOIN analytics.entry_attempt_fact eaf
            ON eaf.run_id = sf.run_id AND eaf.setup_id = sf.setup_id
        WHERE sf.run_id = p_run_id
          AND eaf.attempt_id IS NULL
    ),
    sample AS (
        SELECT setup_id FROM affected LIMIT 10
    )
    SELECT COUNT(*) INTO v_count FROM affected;

    SELECT COALESCE(jsonb_agg(setup_id), '[]'::jsonb) INTO v_samples FROM sample;

    RETURN QUERY SELECT
        'orphan_setup'::TEXT,
        'WARNING'::TEXT,
        CASE WHEN v_count > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_count,
        jsonb_build_object('orphan_count', v_count, 'sample_ids', v_samples);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 3. Check: orphan trade (trade_fact without setup_fact)
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_orphan_trade(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_count BIGINT;
    v_samples JSONB;
BEGIN
    WITH affected AS (
        SELECT tf.trade_id
        FROM analytics.trade_fact tf
        LEFT JOIN analytics.setup_fact sf
            ON sf.run_id = tf.run_id AND sf.setup_id = tf.setup_id
        WHERE tf.run_id = p_run_id
          AND sf.setup_id IS NULL
    ),
    sample AS (
        SELECT trade_id FROM affected LIMIT 10
    )
    SELECT COUNT(*) INTO v_count FROM affected;

    SELECT COALESCE(jsonb_agg(trade_id), '[]'::jsonb) INTO v_samples FROM sample;

    RETURN QUERY SELECT
        'orphan_trade'::TEXT,
        'BLOCKING'::TEXT,
        CASE WHEN v_count > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_count,
        jsonb_build_object('orphan_count', v_count, 'sample_ids', v_samples);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 4. Check: entered_at without ENTRY_FILLED event
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_entered_without_fill(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_count BIGINT;
    v_samples JSONB;
BEGIN
    WITH affected AS (
        SELECT tf.trade_id
        FROM analytics.trade_fact tf
        LEFT JOIN analytics.trade_event te
            ON te.trade_id = tf.trade_id
            AND te.event_type = 'ENTRY_FILLED'
        WHERE tf.run_id = p_run_id
          AND tf.entered_at IS NOT NULL
          AND te.event_id IS NULL
    ),
    sample AS (
        SELECT trade_id FROM affected LIMIT 10
    )
    SELECT COUNT(*) INTO v_count FROM affected;

    SELECT COALESCE(jsonb_agg(trade_id), '[]'::jsonb) INTO v_samples FROM sample;

    RETURN QUERY SELECT
        'entered_without_fill'::TEXT,
        'BLOCKING'::TEXT,
        CASE WHEN v_count > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_count,
        jsonb_build_object('count', v_count, 'sample_ids', v_samples);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 5. Check: closed trade without exit event
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_closed_without_exit_event(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_count BIGINT;
    v_samples JSONB;
BEGIN
    WITH affected AS (
        SELECT tf.trade_id
        FROM analytics.trade_fact tf
        LEFT JOIN analytics.trade_event te
            ON te.trade_id = tf.trade_id
            AND te.event_type = 'TRADE_CLOSED'
        WHERE tf.run_id = p_run_id
          AND tf.status = 'CLOSED'
          AND te.event_id IS NULL
    ),
    sample AS (
        SELECT trade_id FROM affected LIMIT 10
    )
    SELECT COUNT(*) INTO v_count FROM affected;

    SELECT COALESCE(jsonb_agg(trade_id), '[]'::jsonb) INTO v_samples FROM sample;

    RETURN QUERY SELECT
        'closed_without_exit_event'::TEXT,
        'BLOCKING'::TEXT,
        CASE WHEN v_count > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_count,
        jsonb_build_object('count', v_count, 'sample_ids', v_samples);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 6. Check: DCA state consistency
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_dca_state_consistency(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_count BIGINT;
    v_samples JSONB;
BEGIN
    WITH affected AS (
        SELECT tf.trade_id
        FROM analytics.trade_fact tf
        WHERE tf.run_id = p_run_id
          AND tf.dca_filled_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM analytics.trade_event te
              WHERE te.trade_id = tf.trade_id
                AND te.event_type = 'DCA_FILLED'
          )
    ),
    sample AS (
        SELECT trade_id FROM affected LIMIT 10
    )
    SELECT COUNT(*) INTO v_count FROM affected;

    SELECT COALESCE(jsonb_agg(trade_id), '[]'::jsonb) INTO v_samples FROM sample;

    RETURN QUERY SELECT
        'dca_state_inconsistency'::TEXT,
        'DEGRADED'::TEXT,
        CASE WHEN v_count > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_count,
        jsonb_build_object('count', v_count, 'sample_ids', v_samples);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 7. Check: invalid MFE/MAE sign
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_mfe_mae_sign(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_count BIGINT;
    v_samples JSONB;
BEGIN
    WITH affected AS (
        SELECT trade_id
        FROM analytics.trade_fact
        WHERE run_id = p_run_id
          AND status = 'CLOSED'
          AND (
              mfe_r < 0   -- MFE should always be >= 0
              OR mae_r > 0 -- MAE should always be <= 0
          )
    ),
    sample AS (
        SELECT trade_id FROM affected LIMIT 10
    )
    SELECT COUNT(*) INTO v_count FROM affected;

    SELECT COALESCE(jsonb_agg(trade_id), '[]'::jsonb) INTO v_samples FROM sample;

    RETURN QUERY SELECT
        'invalid_mfe_mae_sign'::TEXT,
        'BLOCKING'::TEXT,
        CASE WHEN v_count > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_count,
        jsonb_build_object('count', v_count, 'sample_ids', v_samples);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 8. Check: missing config_hash
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_missing_config_hash(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_count BIGINT;
BEGIN
    WITH affected AS (
        SELECT trade_id
        FROM analytics.trade_fact
        WHERE run_id = p_run_id
          AND config_hash IS NULL
    )
    SELECT COUNT(*) INTO v_count FROM affected;

    RETURN QUERY SELECT
        'missing_config_hash'::TEXT,
        'WARNING'::TEXT,
        CASE WHEN v_count > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_count,
        jsonb_build_object('count', v_count);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 9. Check: incomplete candle coverage
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_candle_coverage(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_count BIGINT;
    v_samples JSONB;
BEGIN
    WITH affected AS (
        SELECT thm.trade_id
        FROM analytics.trade_horizon_metric thm
        WHERE thm.trade_id IN (
            SELECT trade_id FROM analytics.trade_fact WHERE run_id = p_run_id
        )
          AND thm.coverage_status != 'COMPLETE'
    ),
    sample AS (
        SELECT trade_id FROM affected LIMIT 10
    )
    SELECT COUNT(*) INTO v_count FROM affected;

    SELECT COALESCE(jsonb_agg(trade_id), '[]'::jsonb) INTO v_samples FROM sample;

    RETURN QUERY SELECT
        'incomplete_candle_coverage'::TEXT,
        'DEGRADED'::TEXT,
        CASE WHEN v_count > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_count,
        jsonb_build_object('count', v_count, 'sample_ids', v_samples);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 10. Check: PIT violation
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.check_pit_violation(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
DECLARE
    v_cutoff TIMESTAMPTZ;
    v_count BIGINT;
    v_samples JSONB;
BEGIN
    SELECT observation_cutoff INTO v_cutoff
    FROM analytics.analysis_run
    WHERE run_id = p_run_id;

    WITH affected AS (
        SELECT trade_id
        FROM analytics.trade_fact
        WHERE run_id = p_run_id
          AND (
              entered_at > v_cutoff
              OR closed_at > v_cutoff
              OR dca_filled_at > v_cutoff
          )
    ),
    sample AS (
        SELECT trade_id FROM affected LIMIT 10
    )
    SELECT COUNT(*) INTO v_count FROM affected;

    SELECT COALESCE(jsonb_agg(trade_id), '[]'::jsonb) INTO v_samples FROM sample;

    RETURN QUERY SELECT
        'pit_violation'::TEXT,
        'BLOCKING'::TEXT,
        CASE WHEN v_count > 0 THEN 'FAIL' ELSE 'PASS' END::TEXT,
        v_count,
        jsonb_build_object('count', v_count, 'cutoff', v_cutoff, 'sample_ids', v_samples);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Master function: run all quality checks
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.run_quality_checks(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    severity TEXT,
    status TEXT,
    affected_count BIGINT,
    details JSONB
) AS $$
BEGIN
    RETURN QUERY
    SELECT * FROM analytics.check_duplicate_trade_grain(p_run_id)
    UNION ALL
    SELECT * FROM analytics.check_orphan_setup(p_run_id)
    UNION ALL
    SELECT * FROM analytics.check_orphan_trade(p_run_id)
    UNION ALL
    SELECT * FROM analytics.check_entered_without_fill(p_run_id)
    UNION ALL
    SELECT * FROM analytics.check_closed_without_exit_event(p_run_id)
    UNION ALL
    SELECT * FROM analytics.check_dca_state_consistency(p_run_id)
    UNION ALL
    SELECT * FROM analytics.check_mfe_mae_sign(p_run_id)
    UNION ALL
    SELECT * FROM analytics.check_missing_config_hash(p_run_id)
    UNION ALL
    SELECT * FROM analytics.check_candle_coverage(p_run_id)
    UNION ALL
    SELECT * FROM analytics.check_pit_violation(p_run_id);
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Quality gate: check if all BLOCKING checks passed
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.quality_gate(
    p_run_id UUID
) RETURNS TABLE (
    passed BOOLEAN,
    blocking_failures BIGINT,
    degraded_failures BIGINT,
    warning_failures BIGINT,
    total_checks BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        NOT EXISTS (
            SELECT 1 FROM analytics.run_quality_checks(p_run_id)
            WHERE severity = 'BLOCKING' AND status = 'FAIL'
        ) AS passed,
        COUNT(*) FILTER (WHERE severity = 'BLOCKING' AND status = 'FAIL')::BIGINT,
        COUNT(*) FILTER (WHERE severity = 'DEGRADED' AND status = 'FAIL')::BIGINT,
        COUNT(*) FILTER (WHERE severity = 'WARNING' AND status = 'FAIL')::BIGINT,
        COUNT(*)::BIGINT
    FROM analytics.run_quality_checks(p_run_id);
END;
$$ LANGUAGE plpgsql;
