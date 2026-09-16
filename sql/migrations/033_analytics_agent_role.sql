-- Migration 033: Analytics Agent Role
-- Creates a dedicated `analytics_agent` role (separate from `analytics_runner`)
-- with strict least-privilege permissions for Stage 3 agent operations.
--
-- analytics_agent:
--   - CAN read analytics.*, market.candle, dds.* (source data)
--   - CAN write only on Stage 3 analytics-owned tables
--   - CANNOT touch config.*, dds paper trading tables, or market.candle (write)
--   - NO DDL, NO TRUNCATE, NO superuser/bypassrls
--
-- Idempotent: safe to run multiple times.

-- ============================================================
-- 1. Create role (idempotent)
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_agent') THEN
        CREATE ROLE analytics_agent WITH LOGIN NOINHERIT;
    END IF;
END
$$;

-- ============================================================
-- 2. Set role attributes to safe defaults (idempotent)
-- ============================================================
ALTER ROLE analytics_agent
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS;

-- ============================================================
-- 3. Database connection
-- ============================================================
GRANT CONNECT ON DATABASE trad_bot TO analytics_agent;

-- ============================================================
-- 4. Schema USAGE
-- ============================================================
GRANT USAGE ON SCHEMA analytics TO analytics_agent;
GRANT USAGE ON SCHEMA market    TO analytics_agent;
GRANT USAGE ON SCHEMA dds       TO analytics_agent;

-- ============================================================
-- 5. READ permissions (SELECT)
-- ============================================================

-- 5a. analytics.* — full read on all analytics tables
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO analytics_agent;

-- 5b. market.candle — read-only market data
GRANT SELECT ON market.candle TO analytics_agent;

-- 5c. dds.* — read-only source tables (all existing and future dds tables)
GRANT SELECT ON ALL TABLES IN SCHEMA dds TO analytics_agent;

-- ============================================================
-- 6. WRITE permissions (INSERT / UPDATE) on Stage 3 tables only
-- ============================================================

-- 6a. INSERT on agent execution tables (append-only)
GRANT INSERT ON analytics.agent_run              TO analytics_agent;
GRANT INSERT ON analytics.agent_input_manifest   TO analytics_agent;
GRANT INSERT ON analytics.agent_result           TO analytics_agent;

-- 6b. INSERT on report and quality tables
GRANT INSERT ON analytics.daily_trading_report   TO analytics_agent;
GRANT INSERT ON analytics.data_quality_result    TO analytics_agent;
GRANT INSERT ON analytics.analysis_run           TO analytics_agent;
GRANT INSERT ON analytics.analysis_stage_run     TO analytics_agent;

-- 6c. INSERT, UPDATE on mutable agent definitions
GRANT INSERT, UPDATE ON analytics.agent_definition      TO analytics_agent;
GRANT INSERT, UPDATE ON analytics.dataset_publication   TO analytics_agent;

-- 6d. SELECT, INSERT, UPDATE on publication event log
GRANT SELECT, INSERT, UPDATE ON analytics.dataset_publication_log TO analytics_agent;

-- 6e. Sequences needed for INSERT (auto-generated PKs / BIGSERIAL)
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA analytics TO analytics_agent;

-- ============================================================
-- 7. DENY write on config.* tables (all config tables)
-- ============================================================
DO $$
BEGIN
    -- config.scanner_direction_gate
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_agent')
       AND EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'config' AND table_name = 'scanner_direction_gate')
    THEN
        REVOKE INSERT, UPDATE, DELETE ON config.scanner_direction_gate FROM analytics_agent;
    END IF;

    -- config.scanner_direction_gate_history
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_agent')
       AND EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'config' AND table_name = 'scanner_direction_gate_history')
    THEN
        REVOKE INSERT, UPDATE, DELETE ON config.scanner_direction_gate_history FROM analytics_agent;
    END IF;

    -- config.scanner_grafana_visibility
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_agent')
       AND EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'config' AND table_name = 'scanner_grafana_visibility')
    THEN
        REVOKE INSERT, UPDATE, DELETE ON config.scanner_grafana_visibility FROM analytics_agent;
    END IF;

    -- Revoke USAGE on config schema to prevent any future implicit access
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_agent')
    THEN
        REVOKE USAGE ON SCHEMA config FROM analytics_agent;
    END IF;
END
$$;

-- ============================================================
-- 8. DENY write on dds paper trading tables
-- ============================================================
DO $$
BEGIN
    -- dds.paper_trade
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_agent')
       AND EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'dds' AND table_name = 'paper_trade')
    THEN
        REVOKE INSERT, UPDATE, DELETE ON dds.paper_trade FROM analytics_agent;
    END IF;

    -- dds.paper_account
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_agent')
       AND EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'dds' AND table_name = 'paper_account')
    THEN
        REVOKE INSERT, UPDATE, DELETE ON dds.paper_account FROM analytics_agent;
    END IF;

    -- dds.paper_trade_stats
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_agent')
       AND EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'dds' AND table_name = 'paper_trade_stats')
    THEN
        REVOKE INSERT, UPDATE, DELETE ON dds.paper_trade_stats FROM analytics_agent;
    END IF;
END
$$;

-- ============================================================
-- 9. DENY write on market.candle (agent is read-only)
-- ============================================================
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_agent')
       AND EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'market' AND table_name = 'candle')
    THEN
        REVOKE INSERT, UPDATE, DELETE ON market.candle FROM analytics_agent;
    END IF;
END
$$;

-- ============================================================
-- 10. DENY TRUNCATE everywhere
-- ============================================================
DO $$
DECLARE
    _tbl RECORD;
BEGIN
    FOR _tbl IN
        SELECT schemaname || '.' || tablename AS fq
        FROM pg_tables
        WHERE schemaname IN ('analytics', 'market', 'dds', 'config')
    LOOP
        BEGIN
            EXECUTE format('REVOKE TRUNCATE ON %s FROM analytics_agent', _tbl.fq);
        EXCEPTION WHEN insufficient_privilege OR undefined_table THEN
            -- role never had the privilege — safe to ignore
            NULL;
        END;
    END LOOP;
END
$$;

-- ============================================================
-- 11. DENY ALL DDL — no CREATE/ALTER/DROP anywhere
-- ============================================================
-- PostgreSQL does not support REVOKE CREATE on individual schemas,
-- but we ensure the role has no CREATEDB/CREATEROLE (set above).
-- Revoke CREATE on all schemas to be explicit:
DO $$
DECLARE
    _schema RECORD;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
    LOOP
        BEGIN
            EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM analytics_agent', _schema.schema_name);
        EXCEPTION WHEN insufficient_privilege THEN
            -- role never had the privilege — safe to ignore
            NULL;
        END;
    END LOOP;
END
$$;

-- ============================================================
-- 12. Documentation comments
-- ============================================================
COMMENT ON ROLE analytics_agent
    IS 'Stage 3 agent runtime role — read on market/dds, write only on analytics-owned tables (migration 033)';
