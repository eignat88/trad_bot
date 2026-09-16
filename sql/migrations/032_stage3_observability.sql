-- Migration 032: Stage 3 observability views
-- Adds Grafana-friendly views for agent run observability,
-- daily pipeline status, and LLM usage/reliability metrics.
--
-- Idempotent: uses CREATE OR REPLACE VIEW.

-- ============================================================
-- 1. analytics.v_agent_run_observability
-- ============================================================
CREATE OR REPLACE VIEW analytics.v_agent_run_observability AS
SELECT
    ar.business_date,
    arr.analysis_run_id,
    ar.maturity,
    arr.agent_name,
    arr.attempt,
    arr.model,
    arr.status,
    arr.latency_ms,
    arr.input_tokens,
    arr.output_tokens,
    arr.total_tokens,
    arr.error_class,
    arr.error_message,
    arr.started_at,
    arr.finished_at,
    arr.created_at
FROM analytics.agent_run arr
JOIN analytics.analysis_run ar ON ar.run_id = arr.analysis_run_id;

COMMENT ON VIEW analytics.v_agent_run_observability
    IS 'Stage 3 agent execution observability for Grafana dashboards';

-- ============================================================
-- 2. analytics.v_daily_stage3_status (deduplicated)
-- ============================================================
-- Returns exactly one row per analysis_run: the current (most recent
-- non-superseded) report, or NULL if no report exists.
CREATE OR REPLACE VIEW analytics.v_daily_stage3_status AS
SELECT
    ar.business_date,
    ar.run_id AS analysis_run_id,
    ar.maturity,
    ar.status AS run_status,
    dtr.report_id,
    dtr.report_version,
    dtr.status AS report_status,
    dtr.action_class,
    dtr.partial,
    dtr.missing_agents,
    dtr.created_at AS report_created_at
FROM analytics.analysis_run ar
LEFT JOIN LATERAL (
    SELECT report_id, report_version, status, action_class, partial,
           missing_agents, created_at
    FROM analytics.daily_trading_report dtr
    WHERE dtr.analysis_run_id = ar.run_id
      AND dtr.maturity = ar.maturity
      AND dtr.status IN ('VALIDATED', 'DRAFT')
    ORDER BY dtr.report_version DESC
    LIMIT 1
) dtr ON TRUE
WHERE ar.status IN ('SUCCEEDED', 'FAILED', 'DEGRADED')
ORDER BY ar.business_date DESC, ar.maturity;

COMMENT ON VIEW analytics.v_daily_stage3_status
    IS 'Daily pipeline status with deduplicated current report per run';

-- ============================================================
-- 3. analytics.v_llm_usage_daily
-- ============================================================
-- Renamed from v_llm_cost_daily: shows token usage, not dollar cost.
-- Dollar cost is computed in Python via cost.py with versioned pricing.
CREATE OR REPLACE VIEW analytics.v_llm_usage_daily AS
SELECT
    ar.business_date,
    ar.maturity,
    arr.agent_name,
    arr.model,
    SUM(arr.input_tokens) AS total_input_tokens,
    SUM(arr.output_tokens) AS total_output_tokens,
    SUM(arr.total_tokens) AS total_tokens,
    AVG(arr.latency_ms) AS avg_latency_ms,
    COUNT(*) FILTER (WHERE arr.status = 'SUCCEEDED') AS success_count,
    COUNT(*) FILTER (WHERE arr.status = 'FAILED') AS failure_count,
    COUNT(*) FILTER (WHERE arr.status = 'DEGRADED') AS degraded_count
FROM analytics.agent_run arr
JOIN analytics.analysis_run ar ON ar.run_id = arr.analysis_run_id
GROUP BY ar.business_date, ar.maturity, arr.agent_name, arr.model
ORDER BY ar.business_date DESC, arr.agent_name;

COMMENT ON VIEW analytics.v_llm_usage_daily
    IS 'Daily LLM token usage, reliability, and model breakdown per agent';
