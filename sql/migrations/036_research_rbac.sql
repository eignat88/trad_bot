-- Migration 036: Research schema RBAC
-- Grants analytics_runner and analytics_agent access to the new research schema.
--
-- Idempotent: GRANT is idempotent in PostgreSQL.

-- ============================================================
-- 1. analytics_runner
-- ============================================================
GRANT USAGE ON SCHEMA research TO analytics_runner;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA research TO analytics_runner;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO analytics_runner;

-- ============================================================
-- 2. analytics_agent
-- ============================================================
GRANT USAGE ON SCHEMA research TO analytics_agent;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA research TO analytics_agent;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO analytics_agent;
