-- Migration 032: Stage 3 observability views
-- Adds Grafana-friendly views for agent run observability,
-- daily pipeline status, and LLM cost/reliability metrics.
--
-- Idempotent: uses CREATE OR REPLACE VIEW.

-- analytics.v_agent_run_observability
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
    arr.started_at,
    arr.finished_at,
    arr.created_at
FROM analytics.agent_run arr
JOIN analytics.analysis_run ar ON ar.run_id = arr.analysis_run_id;

COMMENT ON VIEW analytics.v_agent_run_observability IS 'Stage 3 agent execution observability for Grafana dashboards';

-- analytics.v_daily_stage3_status
CREATE OR REPLACE VIEW analytics.v_daily_stage3_status AS
SELECT 
    ar.business_date,
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
LEFT JOIN analytics.daily_trading_report dtr 
    ON dtr.analysis_run_id = ar.run_id 
    AND dtr.maturity = ar.maturity
    AND dtr.status IN ('VALIDATED', 'DRAFT')
WHERE ar.status IN ('SUCCEEDED', 'FAILED')
ORDER BY ar.business_date DESC, ar.maturity;

COMMENT ON VIEW analytics.v_daily_stage3_status IS 'Daily pipeline status with report information for Grafana';

-- analytics.v_llm_cost_daily
CREATE OR REPLACE VIEW analytics.v_llm_cost_daily AS
SELECT 
    ar.business_date,
    ar.maturity,
    arr.agent_name,
    SUM(arr.input_tokens) AS total_input_tokens,
    SUM(arr.output_tokens) AS total_output_tokens,
    SUM(arr.total_tokens) AS total_tokens,
    AVG(arr.latency_ms) AS avg_latency_ms,
    COUNT(*) FILTER (WHERE arr.status = 'SUCCEEDED') AS success_count,
    COUNT(*) FILTER (WHERE arr.status = 'FAILED') AS failure_count,
    COUNT(*) FILTER (WHERE arr.status = 'DEGRADED') AS degraded_count
FROM analytics.agent_run arr
JOIN analytics.analysis_run ar ON ar.run_id = arr.analysis_run_id
GROUP BY ar.business_date, ar.maturity, arr.agent_name
ORDER BY ar.business_date DESC, arr.agent_name;

COMMENT ON VIEW analytics.v_llm_cost_daily IS 'Daily LLM token usage and reliability metrics per agent';
