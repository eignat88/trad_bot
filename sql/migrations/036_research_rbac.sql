-- Migration 036: Research schema RBAC (corrected)
-- Grants analytics_runner and analytics_agent access to the research schema.
-- Least privilege: no blanket UPDATE/DELETE on immutable artifacts.
--
-- Idempotent: GRANT/REVOKE are idempotent in PostgreSQL.

-- ============================================================
-- 1. analytics_runner — application lifecycle role
-- ============================================================
GRANT USAGE ON SCHEMA research TO analytics_runner;
GRANT SELECT ON ALL TABLES IN SCHEMA research TO analytics_runner;
GRANT INSERT ON ALL TABLES IN SCHEMA research TO analytics_runner;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO analytics_runner;

-- Application lifecycle needs UPDATE on mutable entities:
GRANT UPDATE ON research.finding TO analytics_runner;
GRANT UPDATE ON research.hypothesis TO analytics_runner;
GRANT UPDATE ON research.experiment TO analytics_runner;
GRANT UPDATE ON research.experiment_run TO analytics_runner;
GRANT UPDATE ON research.change_candidate TO analytics_runner;
GRANT UPDATE ON research.production_change TO analytics_runner;
GRANT UPDATE ON research.monitoring_result TO analytics_runner;

-- App lifecycle may need to link hypothesis_finding
GRANT INSERT ON research.hypothesis_finding TO analytics_runner;

-- ============================================================
-- 2. analytics_agent — Stage 3 agent runtime
-- ============================================================
GRANT USAGE ON SCHEMA research TO analytics_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA research TO analytics_agent;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO analytics_agent;

-- Agents may create findings and occurrences as source artifacts
GRANT INSERT ON research.finding TO analytics_agent;
GRANT INSERT ON research.finding_occurrence TO analytics_agent;
GRANT UPDATE ON research.finding TO analytics_agent;
GRANT INSERT ON research.transition_history TO analytics_agent;

-- ============================================================
-- 3. DENY blanket lifecycle changes from analytics_agent
-- ============================================================
REVOKE UPDATE ON research.hypothesis FROM analytics_agent;
REVOKE UPDATE ON research.experiment FROM analytics_agent;
REVOKE UPDATE ON research.experiment_run FROM analytics_agent;
REVOKE UPDATE ON research.validation_result FROM analytics_agent;
REVOKE UPDATE ON research.change_candidate FROM analytics_agent;
REVOKE UPDATE ON research.production_change FROM analytics_agent;
REVOKE UPDATE ON research.monitoring_result FROM analytics_agent;

-- ============================================================
-- 4. DENY all mutation on immutable artifacts
-- ============================================================
REVOKE UPDATE, DELETE ON research.transition_history FROM analytics_agent;
REVOKE DELETE ON research.finding FROM analytics_agent;
REVOKE DELETE ON research.hypothesis FROM analytics_agent;
REVOKE DELETE ON research.experiment FROM analytics_agent;
REVOKE DELETE ON research.finding_occurrence FROM analytics_agent;

REVOKE UPDATE, DELETE ON research.transition_history FROM analytics_runner;
REVOKE DELETE ON research.finding FROM analytics_runner;
REVOKE DELETE ON research.hypothesis FROM analytics_runner;
REVOKE DELETE ON research.experiment FROM analytics_runner;
REVOKE DELETE ON research.validation_result FROM analytics_runner;

-- ============================================================
-- 5. Verification
-- ============================================================
SELECT grantee, privilege_type, table_name
FROM information_schema.table_privileges
WHERE table_schema = 'research'
  AND grantee IN ('analytics_runner', 'analytics_agent')
ORDER BY grantee, table_name, privilege_type;
