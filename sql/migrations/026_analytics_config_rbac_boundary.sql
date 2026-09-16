-- Migration 026: Analytics Config RBAC Boundary
-- Enforces that analytics_runner has NO direct privileges on config tables.
--
-- Stage 2 consumes analytics.config_snapshot instead of config.* directly.
-- analytics_runner must not SELECT/INSERT/UPDATE/DELETE on config tables.
--
-- This migration is idempotent: safe to run multiple times.
-- It does NOT modify migration 010 — it only tightens the boundary.

-- Revoke ALL privileges on config tables from analytics_runner.
-- IF NOT EXISTS pattern: check if role and table exist before revoking.

DO $$
BEGIN
    -- config.scanner_direction_gate
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_runner')
       AND EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'config' AND table_name = 'scanner_direction_gate')
    THEN
        REVOKE ALL PRIVILEGES ON config.scanner_direction_gate FROM analytics_runner;
    END IF;

    -- config.scanner_grafana_visibility
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_runner')
       AND EXISTS (SELECT 1 FROM information_schema.tables
                   WHERE table_schema = 'config' AND table_name = 'scanner_grafana_visibility')
    THEN
        REVOKE ALL PRIVILEGES ON config.scanner_grafana_visibility FROM analytics_runner;
    END IF;

    -- Revoke USAGE on config schema to prevent any future implicit access
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_runner')
    THEN
        REVOKE USAGE ON SCHEMA config FROM analytics_runner;
    END IF;
END
$$;

COMMENT ON TABLE config.scanner_direction_gate IS
    'Operational config — analytics_runner has no direct access (migration 026)';
COMMENT ON TABLE config.scanner_grafana_visibility IS
    'Operational config — analytics_runner has no direct access (migration 026)';
