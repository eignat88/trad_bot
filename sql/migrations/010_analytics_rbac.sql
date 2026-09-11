-- Migration 010: Analytics RBAC
-- Creates analytics_runner role with least privilege permissions
-- Idempotent: safe to run multiple times

-- 1. Create analytics_runner role (without password - set separately)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_runner') THEN
        CREATE ROLE analytics_runner WITH LOGIN NOINHERIT;
    END IF;
END
$$;

-- 2. Set role attributes to safe defaults (idempotent)
ALTER ROLE analytics_runner
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS;

-- 3. Grant CONNECT to trad_bot database
GRANT CONNECT ON DATABASE trad_bot TO analytics_runner;

-- 4. Grant USAGE on required schemas
GRANT USAGE ON SCHEMA analytics TO analytics_runner;
GRANT USAGE ON SCHEMA market TO analytics_runner;
GRANT USAGE ON SCHEMA dds TO analytics_runner;

-- 5. Analytics schema permissions
-- analytics_runner needs full CRUD on analytics tables
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA analytics TO analytics_runner;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA analytics TO analytics_runner;

-- 6. Market schema permissions
-- analytics_runner needs SELECT, INSERT, UPDATE on market.candle (UPSERT)
GRANT SELECT, INSERT, UPDATE ON market.candle TO analytics_runner;

-- 7. DDS schema permissions (read-only on specific tables only)
-- analytics_runner needs SELECT on dds.paper_trade for post-exit coverage check
GRANT SELECT ON dds.paper_trade TO analytics_runner;
-- analytics_runner needs SELECT on dds.instrument for symbol→instrument_id lookup
GRANT SELECT ON dds.instrument TO analytics_runner;

-- 8. Explicitly deny write permissions on critical tables
-- This ensures no write access even if PUBLIC or other grants exist
DO $$
BEGIN
    -- Deny write on config.scanner_direction_gate
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'config' AND table_name = 'scanner_direction_gate') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON config.scanner_direction_gate FROM analytics_runner';
    END IF;
    
    -- Deny write on config.scanner_grafana_visibility
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'config' AND table_name = 'scanner_grafana_visibility') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON config.scanner_grafana_visibility FROM analytics_runner';
    END IF;
    
    -- Deny write on dds.paper_trade
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'dds' AND table_name = 'paper_trade') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON dds.paper_trade FROM analytics_runner';
    END IF;
    
    -- Deny write on dds.paper_account
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'dds' AND table_name = 'paper_account') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON dds.paper_account FROM analytics_runner';
    END IF;
    
    -- Deny write on dds.paper_trade_stats
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'dds' AND table_name = 'paper_trade_stats') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON dds.paper_trade_stats FROM analytics_runner';
    END IF;
END
$$;

-- 9. Add comments for documentation
COMMENT ON ROLE analytics_runner IS 'Runtime role for analytics pipeline - least privilege access';
COMMENT ON TABLE analytics.analysis_run IS 'analytics_runner has SELECT, INSERT, UPDATE';
COMMENT ON TABLE market.candle IS 'analytics_runner has SELECT, INSERT, UPDATE (UPSERT)';
COMMENT ON TABLE dds.paper_trade IS 'analytics_runner has SELECT only (post-exit coverage check)';