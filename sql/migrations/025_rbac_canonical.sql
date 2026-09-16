-- Migration 025: RBAC for Canonical Analytics Tables
-- Grants analytics_runner minimum runtime permissions on canonical tables
-- and additional DDS source tables needed for reconstruction.
--
-- Idempotent: uses GRANT IF EXISTS pattern.

-- ============================================================
-- 1. Grant minimum runtime privileges on analytics canonical tables
-- ============================================================
GRANT SELECT, INSERT, UPDATE ON analytics.config_snapshot TO analytics_runner;
GRANT SELECT, INSERT ON analytics.strategy_snapshot TO analytics_runner;
GRANT SELECT, INSERT, UPDATE ON analytics.setup_fact TO analytics_runner;
GRANT SELECT ON analytics.entry_attempt_fact TO analytics_runner;
GRANT SELECT, INSERT, UPDATE ON analytics.trade_fact TO analytics_runner;
GRANT SELECT, INSERT ON analytics.trade_event TO analytics_runner;
GRANT SELECT, INSERT, UPDATE ON analytics.trade_horizon_metric TO analytics_runner;
GRANT SELECT, INSERT, UPDATE ON analytics.trade_replay_metric TO analytics_runner;
GRANT SELECT ON analytics.setup_counterfactual TO analytics_runner;
GRANT SELECT, INSERT, UPDATE ON analytics.metric_snapshot TO analytics_runner;
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
-- No additional grant needed - privileges are already granted by migration 010

-- ============================================================
-- 4. Grant USAGE on analytics schema functions
-- ============================================================
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA analytics TO analytics_runner;

-- ============================================================
-- 5. Add comments for documentation
-- ============================================================
COMMENT ON TABLE analytics.config_snapshot IS 'analytics_runner: SELECT, INSERT, UPDATE';
COMMENT ON TABLE analytics.strategy_snapshot IS 'analytics_runner: SELECT, INSERT';
COMMENT ON TABLE analytics.setup_fact IS 'analytics_runner: SELECT, INSERT, UPDATE';
COMMENT ON TABLE analytics.entry_attempt_fact IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.trade_fact IS 'analytics_runner: SELECT, INSERT, UPDATE';
COMMENT ON TABLE analytics.trade_event IS 'analytics_runner: SELECT, INSERT';
COMMENT ON TABLE analytics.trade_horizon_metric IS 'analytics_runner: SELECT, INSERT, UPDATE';
COMMENT ON TABLE analytics.trade_replay_metric IS 'analytics_runner: SELECT, INSERT, UPDATE';
COMMENT ON TABLE analytics.setup_counterfactual IS 'analytics_runner: SELECT';
COMMENT ON TABLE analytics.metric_snapshot IS 'analytics_runner: SELECT, INSERT, UPDATE';
COMMENT ON TABLE analytics.metric_registry IS 'analytics_runner: SELECT';
