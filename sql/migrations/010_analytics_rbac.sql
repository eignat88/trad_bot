-- Migration 010: Analytics RBAC
-- Creates analytics_runner role with least privilege permissions

-- 1. Create analytics_runner role (without password - set separately)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_runner') THEN
        CREATE ROLE analytics_runner WITH LOGIN NOINHERIT;
    END IF;
END
$$;

-- 2. Grant CONNECT to trad_bot database
GRANT CONNECT ON DATABASE trad_bot TO analytics_runner;

-- 3. Grant USAGE on required schemas
GRANT USAGE ON SCHEMA analytics TO analytics_runner;
GRANT USAGE ON SCHEMA market TO analytics_runner;
GRANT USAGE ON SCHEMA dds TO analytics_runner;
GRANT USAGE ON SCHEMA config TO analytics_runner;
GRANT USAGE ON SCHEMA mart TO analytics_runner;

-- 4. Analytics schema permissions
-- analytics_runner needs full CRUD on analytics tables
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA analytics TO analytics_runner;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA analytics TO analytics_runner;

-- 5. Market schema permissions
-- analytics_runner needs SELECT, INSERT, UPDATE on market.candle (UPSERT)
GRANT SELECT, INSERT, UPDATE ON market.candle TO analytics_runner;
-- Grant sequence usage for BIGSERIAL columns (if any)
DO $$
DECLARE
    seq_name TEXT;
BEGIN
    SELECT pg_get_serial_sequence('market.candle', 'instrument_id') INTO seq_name;
    IF seq_name IS NOT NULL THEN
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %s TO analytics_runner', seq_name);
    END IF;
END
$$;

-- 6. DDS schema permissions (read-only)
-- analytics_runner needs SELECT on dds.paper_trade for post-exit coverage check
GRANT SELECT ON dds.paper_trade TO analytics_runner;

-- 7. Config schema permissions (read-only if needed)
-- Grant SELECT on specific config tables if analytics runtime reads them
-- Currently analytics doesn't read config tables, but grant for future-proofing
GRANT SELECT ON ALL TABLES IN SCHEMA config TO analytics_runner;

-- 8. Mart schema permissions (read-only)
-- Grant SELECT on mart views for monitoring
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO analytics_runner;

-- 9. Revoke dangerous permissions (only if they were granted)
-- Ensure analytics_runner cannot modify scanner gates
DO $$
BEGIN
    -- Revoke from config tables if they exist and have grants
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'config' AND table_name = 'scanner_direction_gate') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON config.scanner_direction_gate FROM analytics_runner';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'config' AND table_name = 'scanner_grafana_visibility') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON config.scanner_grafana_visibility FROM analytics_runner';
    END IF;
    
    -- Revoke from dds tables if they exist and have grants
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'dds' AND table_name = 'paper_trade') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON dds.paper_trade FROM analytics_runner';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'dds' AND table_name = 'paper_account') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON dds.paper_account FROM analytics_runner';
    END IF;
    
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'dds' AND table_name = 'paper_trade_stats') THEN
        EXECUTE 'REVOKE UPDATE, INSERT, DELETE ON dds.paper_trade_stats FROM analytics_runner';
    END IF;
END
$$;

-- 10. Revoke dangerous role attributes (only if they were granted)
DO $$
BEGIN
    -- Check if role has these attributes before revoking
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_runner' AND rolcreatedb = true) THEN
        EXECUTE 'REVOKE CREATEDB FROM analytics_runner';
    END IF;
    
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_runner' AND rolcreaterole = true) THEN
        EXECUTE 'REVOKE CREATEROLE FROM analytics_runner';
    END IF;
    
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_runner' AND rolsuper = true) THEN
        EXECUTE 'REVOKE SUPERUSER FROM analytics_runner';
    END IF;
END
$$;

-- 11. Add comments for documentation
COMMENT ON ROLE analytics_runner IS 'Runtime role for analytics pipeline - least privilege access';
COMMENT ON TABLE analytics.analysis_run IS 'analytics_runner has SELECT, INSERT, UPDATE';
COMMENT ON TABLE market.candle IS 'analytics_runner has SELECT, INSERT, UPDATE (UPSERT)';
COMMENT ON TABLE dds.paper_trade IS 'analytics_runner has SELECT only (post-exit coverage check)';