-- Migration 025: RBAC for Canonical Analytics Tables
-- Grants analytics_runner SELECT permissions on new canonical tables
-- and additional DDS source tables needed for reconstruction.
--
-- Idempotent: uses GRANT IF EXISTS pattern.

-- ============================================================
-- 1. Grant SELECT on new analytics canonical tables
-- ============================================================
GRANT SELECT ON analytics.config_snapshot TO analytics_runner;
GRANT SELECT ON analytics.strategy_snapshot TO analytics_runner;
GRANT SELECT ON analytics.setup_fact TO analytics_runner;
GRANT SELECT ON analytics.entry_attempt_fact TO analytics_runner;
GRANT SELECT ON analytics.trade_fact TO analytics_runner;
GRANT SELECT ON analytics.trade_event TO analytics_runner;
GRANT SELECT ON analytics.trade_horizon_metric TO analytics_runner;
GRANT SELECT ON analytics.trade_replay_metric TO analytics_runner;
GRANT SELECT ON analytics.setup_counterfactual TO analytics_runner;
GRANT SELECT ON analytics.metric_snapshot TO analytics_runner;
GRANT SELECT ON analytics.metric_registry TO analytics_runner;

-- ============================================================
-- 2. Grant SELECT on DDS source tables needed for reconstruction
-- ============================================================
GRANT SELECT ON dds.scanner_setup TO analytics_runner;
GRANT SELECT ON dds.signal_outcome TO analytics_runner;
GRANT SELECT ON dds.scanner_event TO analytics_runner;
GRANT SELECT ON dds.paper_account TO analytics_runner;
GRANT SELECT ON dds.scanner_run TO analytics_runner;
GRANT SELECT ON dds.scanner_run_stat TO analytics_runner;
GRANT SELECT ON dds.market_signal TO analytics_runner;

-- ============================================================
-- 3. Grant SELECT on market.candle (already granted in migration 010)
-- ============================================================
-- No additional grant needed — already has SELECT, INSERT, UPDATE

-- ============================================================
-- 4. Grant USAGE on analytics schema functions
-- ============================================================
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA analytics TO analytics_runner;

-- ============================================================
-- 5. Add comments for documentation
-- ============================================================
COMMENT ON TABLE analytics.config_snapshot IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.strategy_snapshot IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.setup_fact IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.entry_attempt_fact IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.trade_fact IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.trade_event IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.trade_horizon_metric IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.trade_replay_metric IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.setup_counterfactual IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.metric_snapshot IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.metric_registry IS 'analytics_runner: SELECT';
