-- Migration 036: Research schema RBAC (corrected — least privilege)
--
-- analytics_agent: source artifact creation only
-- analytics_runner: controlled lifecycle mutation
-- transition_history: append-only for both roles
--
-- Idempotent: GRANT/REVOKE are idempotent in PostgreSQL.

-- ============================================================
-- 1. analytics_agent — Stage 3 agent runtime (source artifacts ONLY)
-- ============================================================
GRANT USAGE ON SCHEMA research TO analytics_agent;

-- SELECT: required research tables (read context for assembly)
GRANT SELECT ON ALL TABLES IN SCHEMA research TO analytics_agent;

-- INSERT: only source artifacts (finding, occurrence)
GRANT INSERT ON research.finding TO analytics_agent;
GRANT INSERT ON research.finding_occurrence TO analytics_agent;

-- REVOKE: no lifecycle mutation whatsoever
REVOKE UPDATE ON research.finding FROM analytics_agent;
REVOKE UPDATE ON research.hypothesis FROM analytics_agent;
REVOKE UPDATE ON research.experiment FROM analytics_agent;
REVOKE UPDATE ON research.experiment_run FROM analytics_agent;
REVOKE UPDATE ON research.validation_result FROM analytics_agent;
REVOKE UPDATE ON research.change_candidate FROM analytics_agent;
REVOKE UPDATE ON research.production_change FROM analytics_agent;
REVOKE UPDATE ON research.monitoring_result FROM analytics_agent;

-- REVOKE: no transition_history access (audit belongs to lifecycle layer)
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON research.transition_history FROM analytics_agent;

-- REVOKE: no DELETE anywhere
REVOKE DELETE ON research.finding FROM analytics_agent;
REVOKE DELETE ON research.finding_occurrence FROM analytics_agent;
REVOKE DELETE ON research.hypothesis FROM analytics_agent;
REVOKE DELETE ON research.experiment FROM analytics_agent;

-- REVOKE: no truncation anywhere
REVOKE TRUNCATE ON ALL TABLES IN SCHEMA research FROM analytics_agent;

-- Sequences: needed for INSERT (gen_random_uuid defaults don't need it, but defensive)
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO analytics_agent;

-- ============================================================
-- 2. analytics_runner — application lifecycle role
-- ============================================================
GRANT USAGE ON SCHEMA research TO analytics_runner;

-- SELECT: all research tables
GRANT SELECT ON ALL TABLES IN SCHEMA research TO analytics_runner;

-- INSERT: all research tables (including transition_history)
GRANT INSERT ON ALL TABLES IN SCHEMA research TO analytics_runner;

-- UPDATE: only mutable lifecycle entities
GRANT UPDATE ON research.finding TO analytics_runner;
GRANT UPDATE ON research.hypothesis TO analytics_runner;
GRANT UPDATE ON research.experiment TO analytics_runner;
GRANT UPDATE ON research.experiment_run TO analytics_runner;
GRANT UPDATE ON research.change_candidate TO analytics_runner;
GRANT UPDATE ON research.production_change TO analytics_runner;
GRANT UPDATE ON research.monitoring_result TO analytics_runner;

-- Sequences
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA research TO analytics_runner;

-- ============================================================
-- 3. transition_history: append-only for BOTH roles
-- ============================================================
-- Trigger is defence-in-depth; REVOKE is the primary guard.

-- analytics_runner: INSERT + SELECT only
REVOKE UPDATE, DELETE, TRUNCATE ON research.transition_history FROM analytics_runner;
REVOKE UPDATE, DELETE, TRUNCATE ON research.transition_history FROM analytics_agent;

-- ============================================================
-- 4. Immutable artifacts: no DELETE for anyone except postgres
-- ============================================================
REVOKE DELETE ON research.finding FROM analytics_runner;
REVOKE DELETE ON research.hypothesis FROM analytics_runner;
REVOKE DELETE ON research.experiment FROM analytics_runner;
REVOKE DELETE ON research.validation_result FROM analytics_runner;

-- ============================================================
-- 5. Verification (runs during migration, informational)
-- ============================================================
DO $$
DECLARE
    r RECORD;
BEGIN
    RAISE NOTICE 'RBAC migration 036 applied. Verify grants:';
    FOR r IN
        SELECT grantee, privilege_type, table_name
        FROM information_schema.table_privileges
        WHERE table_schema = 'research'
          AND grantee IN ('analytics_runner', 'analytics_agent')
        ORDER BY grantee, table_name, privilege_type
    LOOP
        RAISE NOTICE '  % | % | %', r.grantee, r.privilege_type, r.table_name;
    END LOOP;
END $$;
