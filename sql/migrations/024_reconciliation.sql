-- Migration 024: Reconciliation Functions
-- Compares source tables with canonical facts before publication.
--
-- Reconciliation checks:
--   1. Source setup count vs canonical setup count
--   2. Source trade count vs canonical trade count
--   3. Closed source trades vs canonical closed trades
--   4. DCA trades vs DCA events
--   5. Trade PnL totals
--
-- Severity: PASS / DEGRADED / BLOCKING

-- ============================================================
-- 1. Setup count reconciliation
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.reconcile_setup_count(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    source_count BIGINT,
    canonical_count BIGINT,
    delta BIGINT,
    severity TEXT,
    status TEXT
) AS $$
DECLARE
    v_source BIGINT;
    v_canonical BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_source
    FROM dds.scanner_setup ss
    WHERE ss.detected_at < (SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = p_run_id);

    SELECT COUNT(*) INTO v_canonical
    FROM analytics.setup_fact
    WHERE run_id = p_run_id;

    delta := v_source - v_canonical;

    RETURN QUERY SELECT
        'setup_count'::TEXT,
        v_source,
        v_canonical,
        delta,
        CASE
            WHEN delta = 0 THEN 'PASS'::TEXT
            WHEN ABS(delta) <= 5 THEN 'DEGRADED'::TEXT
            ELSE 'BLOCKING'::TEXT
        END,
        CASE WHEN delta = 0 THEN 'PASS' ELSE 'FAIL' END::TEXT;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 2. Trade count reconciliation
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.reconcile_trade_count(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    source_count BIGINT,
    canonical_count BIGINT,
    delta BIGINT,
    severity TEXT,
    status TEXT
) AS $$
DECLARE
    v_source BIGINT;
    v_canonical BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_source
    FROM dds.paper_trade pt
    WHERE pt.entered_at < (SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = p_run_id);

    SELECT COUNT(*) INTO v_canonical
    FROM analytics.trade_fact
    WHERE run_id = p_run_id;

    delta := v_source - v_canonical;

    RETURN QUERY SELECT
        'trade_count'::TEXT,
        v_source,
        v_canonical,
        delta,
        CASE
            WHEN delta = 0 THEN 'PASS'::TEXT
            WHEN ABS(delta) <= 2 THEN 'DEGRADED'::TEXT
            ELSE 'BLOCKING'::TEXT
        END,
        CASE WHEN delta = 0 THEN 'PASS' ELSE 'FAIL' END::TEXT;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 3. Closed trade count reconciliation
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.reconcile_closed_trade_count(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    source_count BIGINT,
    canonical_count BIGINT,
    delta BIGINT,
    severity TEXT,
    status TEXT
) AS $$
DECLARE
    v_source BIGINT;
    v_canonical BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_source
    FROM dds.paper_trade pt
    WHERE pt.status = 'CLOSED'
      AND pt.closed_at < (SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = p_run_id);

    SELECT COUNT(*) INTO v_canonical
    FROM analytics.trade_fact
    WHERE run_id = p_run_id
      AND status = 'CLOSED';

    delta := v_source - v_canonical;

    RETURN QUERY SELECT
        'closed_trade_count'::TEXT,
        v_source,
        v_canonical,
        delta,
        CASE
            WHEN delta = 0 THEN 'PASS'::TEXT
            WHEN ABS(delta) <= 2 THEN 'DEGRADED'::TEXT
            ELSE 'BLOCKING'::TEXT
        END,
        CASE WHEN delta = 0 THEN 'PASS' ELSE 'FAIL' END::TEXT;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 4. DCA trade count reconciliation
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.reconcile_dca_count(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    source_count BIGINT,
    canonical_count BIGINT,
    delta BIGINT,
    severity TEXT,
    status TEXT
) AS $$
DECLARE
    v_source BIGINT;
    v_canonical BIGINT;
BEGIN
    SELECT COUNT(*) INTO v_source
    FROM dds.paper_trade pt
    WHERE pt.dca_filled_at IS NOT NULL
      AND pt.entered_at < (SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = p_run_id);

    SELECT COUNT(*) INTO v_canonical
    FROM analytics.trade_fact
    WHERE run_id = p_run_id
      AND dca_filled_at IS NOT NULL;

    delta := v_source - v_canonical;

    RETURN QUERY SELECT
        'dca_count'::TEXT,
        v_source,
        v_canonical,
        delta,
        CASE
            WHEN delta = 0 THEN 'PASS'::TEXT
            WHEN ABS(delta) <= 2 THEN 'DEGRADED'::TEXT
            ELSE 'BLOCKING'::TEXT
        END,
        CASE WHEN delta = 0 THEN 'PASS' ELSE 'FAIL' END::TEXT;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 5. PnL totals reconciliation
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.reconcile_pnl_totals(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    source_total NUMERIC,
    canonical_total NUMERIC,
    delta NUMERIC,
    severity TEXT,
    status TEXT
) AS $$
DECLARE
    v_source NUMERIC;
    v_canonical NUMERIC;
BEGIN
    SELECT SUM(pnl_usdt) INTO v_source
    FROM dds.paper_trade pt
    WHERE pt.status = 'CLOSED'
      AND pt.closed_at < (SELECT observation_cutoff FROM analytics.analysis_run WHERE run_id = p_run_id);

    SELECT SUM(net_pnl) INTO v_canonical
    FROM analytics.trade_fact
    WHERE run_id = p_run_id
      AND status = 'CLOSED';

    delta := COALESCE(v_source, 0) - COALESCE(v_canonical, 0);

    RETURN QUERY SELECT
        'pnl_totals'::TEXT,
        COALESCE(v_source, 0),
        COALESCE(v_canonical, 0),
        delta,
        CASE
            WHEN ABS(delta) < 0.01 THEN 'PASS'::TEXT
            WHEN ABS(delta) < 1.0 THEN 'DEGRADED'::TEXT
            ELSE 'BLOCKING'::TEXT
        END,
        CASE WHEN ABS(delta) < 0.01 THEN 'PASS' ELSE 'FAIL' END::TEXT;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Master function: run all reconciliation checks
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.run_reconciliation(
    p_run_id UUID
) RETURNS TABLE (
    check_name TEXT,
    source_count BIGINT,
    canonical_count BIGINT,
    delta BIGINT,
    source_total NUMERIC,
    canonical_total NUMERIC,
    delta_numeric NUMERIC,
    severity TEXT,
    status TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        r.check_name,
        r.source_count,
        r.canonical_count,
        r.delta,
        NULL::NUMERIC,
        NULL::NUMERIC,
        NULL::NUMERIC,
        r.severity,
        r.status
    FROM analytics.reconcile_setup_count(p_run_id) r
    UNION ALL
    SELECT
        r.check_name,
        r.source_count,
        r.canonical_count,
        r.delta,
        NULL::NUMERIC,
        NULL::NUMERIC,
        NULL::NUMERIC,
        r.severity,
        r.status
    FROM analytics.reconcile_trade_count(p_run_id) r
    UNION ALL
    SELECT
        r.check_name,
        r.source_count,
        r.canonical_count,
        r.delta,
        NULL::NUMERIC,
        NULL::NUMERIC,
        NULL::NUMERIC,
        r.severity,
        r.status
    FROM analytics.reconcile_closed_trade_count(p_run_id) r
    UNION ALL
    SELECT
        r.check_name,
        r.source_count,
        r.canonical_count,
        r.delta,
        NULL::NUMERIC,
        NULL::NUMERIC,
        NULL::NUMERIC,
        r.severity,
        r.status
    FROM analytics.reconcile_dca_count(p_run_id) r
    UNION ALL
    SELECT
        r.check_name,
        NULL::BIGINT,
        NULL::BIGINT,
        NULL::BIGINT,
        r.source_total,
        r.canonical_total,
        r.delta,
        r.severity,
        r.status
    FROM analytics.reconcile_pnl_totals(p_run_id) r;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Reconciliation gate: check if all checks passed
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.reconciliation_gate(
    p_run_id UUID
) RETURNS TABLE (
    passed BOOLEAN,
    blocking_count BIGINT,
    degraded_count BIGINT,
    total_checks BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        NOT EXISTS (
            SELECT 1 FROM analytics.run_reconciliation(p_run_id)
            WHERE severity = 'BLOCKING' AND status = 'FAIL'
        ) AS passed,
        COUNT(*) FILTER (WHERE severity = 'BLOCKING' AND status = 'FAIL')::BIGINT,
        COUNT(*) FILTER (WHERE severity = 'DEGRADED' AND status = 'FAIL')::BIGINT,
        COUNT(*)::BIGINT
    FROM analytics.run_reconciliation(p_run_id);
END;
$$ LANGUAGE plpgsql;
