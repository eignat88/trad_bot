-- ============================================================
-- ME_SHORT_A_FORWARD_OUTCOME_RUNTIME_INTEGRATION_V1
-- Least-privilege grants (Issue #208 follow-up)
-- ============================================================
-- REVIEW ONLY. Do not apply to production without explicit approval.
-- Apply only AFTER migration 079 creates the heartbeat table and view,
-- and AFTER the migration 078 grants file has been applied.
--
-- These roles are SEPARATE from every existing application role and from
-- the Phase 1 / frozen-OOS roles. The systemd service authenticates as
-- `trad_bot_me_short_a_runtime` and nothing else in the application uses
-- that login.
--
-- Roles (created idempotently, migration 075 convention):
--   trad_bot_me_short_a_runtime       -> the observe-only runtime writer.
--        INSERT ONLY on the four migration 078 capture tables and on the
--        migration 079 heartbeat log; SELECT ONLY on the activation row,
--        on the lineage/outcome tables it needs to derive pending events,
--        and on Phase 1 for the shared-event_key linkage lookup.
--        USAGE on schema dds plus SELECT ONLY on dds.scanner_setup and
--        dds.instrument, which is the candidate-intake read path
--        (DbScannerSetupCandidateSource). That is the COMPLETE dds grant:
--        two read-only tables, nothing else.
--   trad_bot_me_short_a_runtime_audit -> SELECT ONLY on the heartbeat log
--        and the runtime status view. No dds access at all.
--
-- ROLE SEPARATION — the runtime never authenticates as the collector role.
-- The systemd unit passes ME_SHORT_A_RUNTIME_DB_USER=trad_bot_me_short_a_runtime,
-- the runner requires that variable and refuses to fall back to application
-- credentials, and `trad_bot_me_short_a_collector` (Issue #208) is a different
-- login used by the collector API only. Mixing them would defeat the audit
-- boundary: the collector role has NO privilege on dds and NO privilege on
-- Phase 1, while the runtime role does not own the capture tables' writes'
-- provenance. This file therefore never grants anything to the collector role
-- and the test suite asserts that the two roles stay distinct.
--
-- Why the explicit REVOKE first:
--   migration 049/050 install ALTER DEFAULT PRIVILEGES ... GRANT SELECT,
--   INSERT, UPDATE ON TABLES TO trad_bot for schema research, so the new
--   heartbeat table would otherwise inherit write privileges for the broad
--   runtime role. Those inherited rights are removed before the narrow
--   grants are added (the same REVOKE also exists in migration 079, so the
--   file is safe to re-apply).
--
-- What this file deliberately NEVER grants:
--   * UPDATE / DELETE / TRUNCATE on ANY table of this protocol — the
--     runtime is append-only and the database blocks mutation by trigger,
--   * INSERT / UPDATE / DELETE / TRUNCATE on
--     research.me_short_a_fill_feasibility_v1 (Phase 1). Phase 1 access is
--     SELECT ONLY and exists purely so the runtime can look up whether a
--     Phase 1 snapshot already shares the deterministic event_key. It can
--     never create, change or retro-fit Phase 1 evidence, and Phase 1 is
--     itself append-only (migration 077 trigger).
--   * INSERT / UPDATE / DELETE / TRUNCATE on research.prospective_observation
--     / research.prospective_outcome (the closed frozen OOS cohort). The
--     runtime has NO privilege at all on those tables.
--   * INSERT / UPDATE / DELETE / TRUNCATE on ANYTHING in schema dds. The
--     runtime reads exactly two dds tables (dds.scanner_setup,
--     dds.instrument) and can never write scanner state, paper state, live
--     state or direction gates. This is enforced twice: nothing is granted,
--     and the write verbs are revoked again at the end of the file so a
--     default-privilege inheritance or a later stray GRANT cannot leave
--     them behind.
--   * any privilege on scanner / paper / live / direction-gate objects —
--     specifically dds.scanner_direction_config (001),
--     dds.paper_safety_event (002), config.scanner_direction_gate and
--     config.scanner_direction_gate_history (004), dds.paper_shadow_trade
--     (039). Each is explicitly revoked below.
--
-- Must be applied by a role allowed to create roles (e.g. postgres).
-- ============================================================

BEGIN;

-- ── 1. Roles (idempotent) ────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot_me_short_a_runtime') THEN
        EXECUTE 'CREATE ROLE trad_bot_me_short_a_runtime WITH LOGIN NOINHERIT';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot_me_short_a_runtime_audit') THEN
        EXECUTE 'CREATE ROLE trad_bot_me_short_a_runtime_audit WITH LOGIN NOINHERIT';
    END IF;
END $$;

-- ── 2. Schema visibility ─────────────────────────────────────

GRANT USAGE ON SCHEMA research TO trad_bot_me_short_a_runtime;
GRANT USAGE ON SCHEMA research TO trad_bot_me_short_a_runtime_audit;

-- dds is visible to the RUNTIME ONLY: it is the candidate-intake read path.
-- The auditor reads the heartbeat log and the status view and needs no dds
-- visibility. USAGE without CREATE: the runtime cannot create objects there.
GRANT USAGE ON SCHEMA dds TO trad_bot_me_short_a_runtime;
REVOKE CREATE ON SCHEMA dds FROM trad_bot_me_short_a_runtime;
REVOKE ALL ON SCHEMA dds FROM trad_bot_me_short_a_runtime_audit;

-- ── 2b. dds read-only candidate intake (the COMPLETE dds grant) ──
-- DbScannerSetupCandidateSource runs one read-only SELECT:
--     FROM dds.scanner_setup s
--     JOIN dds.instrument i ON i.instrument_id = s.instrument_id
-- Both sides of the JOIN need SELECT. dds.instrument is granted explicitly
-- and separately so the symbol join can never be reached through a wider
-- grant, and so the privilege is visible in information_schema on its own.
-- No INSERT, no UPDATE, no DELETE, no TRUNCATE, no REFERENCES, no TRIGGER,
-- no DEFAULT PRIVILEGES on anything else in dds.

GRANT SELECT ON dds.scanner_setup TO trad_bot_me_short_a_runtime;
GRANT SELECT ON dds.instrument TO trad_bot_me_short_a_runtime;

-- ── 2c. Defence in depth: the runtime may never WRITE anything in dds ──
-- Nothing above grants a dds write privilege, but revoke them explicitly too
-- so that migration-time ALTER DEFAULT PRIVILEGES or a later stray GRANT
-- cannot leave a write path behind. SELECT is deliberately NOT revoked here.
-- (Sequences are included: USAGE on a sequence is what lets a caller use a
-- serial default, and the runtime has no business doing that either.)

DO $$
DECLARE
    rol text;
BEGIN
    FOREACH rol IN ARRAY ARRAY[
        'trad_bot_me_short_a_runtime',
        'trad_bot_me_short_a_runtime_audit'
    ] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rol) THEN
            EXECUTE format(
                'REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER '
                'ON ALL TABLES IN SCHEMA dds FROM %I', rol);
            EXECUTE format(
                'REVOKE USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA dds FROM %I', rol);
        END IF;
    END LOOP;
END $$;

-- ── 2d. Scanner / paper / live / direction-gate objects are OFF LIMITS ──
-- These are the operational control-plane objects of the scanner, the paper
-- engine, the live safety gate and the direction gates. The observe-only
-- runtime has no privilege on any of them — not even SELECT — and this block
-- removes any that may exist from an inherited default. `to_regclass` keeps
-- the script idempotent on a database where one of them was never created.
-- The two intake tables (dds.scanner_setup, dds.instrument) are deliberately
-- NOT in this list: their SELECT is granted in 2b and must survive.

DO $$
DECLARE
    obj text;
    rol text;
BEGIN
    FOREACH rol IN ARRAY ARRAY[
        'trad_bot_me_short_a_runtime',
        'trad_bot_me_short_a_runtime_audit'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rol) THEN
            CONTINUE;
        END IF;
        FOREACH obj IN ARRAY ARRAY[
            'dds.scanner_direction_config',          -- 001 direction config
            'dds.paper_safety_event',                -- 002 paper safety
            'config.scanner_direction_gate',         -- 004 direction gate
            'config.scanner_direction_gate_history', -- 004 gate history
            'dds.paper_shadow_trade'                 -- 039 paper shadow
        ] LOOP
            IF to_regclass(obj) IS NOT NULL THEN
                EXECUTE format('REVOKE ALL ON %s FROM %I', obj, rol);
            END IF;
        END LOOP;
    END LOOP;
END $$;

-- ── 3. Drop every inherited/broad privilege on the heartbeat log ─

DO $$
DECLARE
    rol text;
BEGIN
    FOREACH rol IN ARRAY ARRAY[
        'trad_bot',
        'analytics_agent',
        'analytics_runner',
        'trad_bot_me_short_a_collector',
        'trad_bot_me_short_a_audit',
        'trad_bot_me_short_a_runtime',
        'trad_bot_me_short_a_runtime_audit'
    ] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rol) THEN
            EXECUTE format(
                'REVOKE ALL ON research.me_short_a_forward_outcome_runtime_heartbeat_v1 FROM %I',
                rol);
        END IF;
    END LOOP;
END $$;

-- ── 4. Runtime writer: INSERT ONLY on the append-only evidence ─

GRANT SELECT, INSERT ON research.me_short_a_source_signal_v1
  TO trad_bot_me_short_a_runtime;
GRANT SELECT, INSERT ON research.me_short_a_post_signal_candle_v1
  TO trad_bot_me_short_a_runtime;
GRANT SELECT, INSERT ON research.me_short_a_event_lineage_v1
  TO trad_bot_me_short_a_runtime;
GRANT SELECT, INSERT ON research.me_short_a_outcome_observation_v1
  TO trad_bot_me_short_a_runtime;

-- Read-only activation gate (the runtime READS the lifecycle, never writes it).
GRANT SELECT ON research.me_short_a_lineage_activation_v1
  TO trad_bot_me_short_a_runtime;

-- Read-only Phase 1 linkage lookup on the shared deterministic event_key.
-- SELECT ONLY: no INSERT, no UPDATE, no DELETE, no TRUNCATE.
GRANT SELECT ON research.me_short_a_fill_feasibility_v1
  TO trad_bot_me_short_a_runtime;

-- Operational heartbeat / diagnostics log.
GRANT INSERT ON research.me_short_a_forward_outcome_runtime_heartbeat_v1
  TO trad_bot_me_short_a_runtime;
GRANT SELECT ON research.me_short_a_forward_outcome_runtime_heartbeat_v1
  TO trad_bot_me_short_a_runtime;
GRANT SELECT ON research.v_me_short_a_forward_outcome_runtime_status_v1
  TO trad_bot_me_short_a_runtime;

-- ── 5. Runtime auditor: SELECT ONLY on the operational log ───

GRANT SELECT ON research.me_short_a_forward_outcome_runtime_heartbeat_v1
  TO trad_bot_me_short_a_runtime_audit;
GRANT SELECT ON research.v_me_short_a_forward_outcome_runtime_status_v1
  TO trad_bot_me_short_a_runtime_audit;

-- ── 6. Defence in depth: no one may mutate the log ───────────
-- The trigger blocks UPDATE/DELETE regardless; removing the privileges too
-- means a misconfiguration cannot even reach the trigger.

DO $$
DECLARE
    rol text;
BEGIN
    FOREACH rol IN ARRAY ARRAY[
        'trad_bot',
        'analytics_agent',
        'analytics_runner',
        'trad_bot_me_short_a_collector',
        'trad_bot_me_short_a_audit',
        'trad_bot_me_short_a_runtime',
        'trad_bot_me_short_a_runtime_audit'
    ] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rol) THEN
            EXECUTE format(
                'REVOKE UPDATE, DELETE, TRUNCATE ON research.me_short_a_forward_outcome_runtime_heartbeat_v1 FROM %I',
                rol);
        END IF;
    END LOOP;
END $$;

COMMIT;

-- ============================================================
-- 7. VERIFICATION (READ-ONLY — run this after applying the file)
-- ============================================================
--
-- 7a. Full privilege matrix of the runtime role:
--
--   SELECT grantee, table_schema, table_name, privilege_type
--     FROM information_schema.role_table_grants
--    WHERE grantee IN ('trad_bot_me_short_a_runtime',
--                      'trad_bot_me_short_a_runtime_audit')
--    ORDER BY grantee, table_schema, table_name, privilege_type;
--
-- Expected for trad_bot_me_short_a_runtime (exactly this set, nothing else):
--   dds        | instrument                          | SELECT
--   dds        | scanner_setup                       | SELECT
--   research   | me_short_a_event_lineage_v1         | INSERT
--   research   | me_short_a_event_lineage_v1         | SELECT
--   research   | me_short_a_fill_feasibility_v1      | SELECT
--   research   | me_short_a_forward_outcome_runtime_heartbeat_v1 | INSERT
--   research   | me_short_a_forward_outcome_runtime_heartbeat_v1 | SELECT
--   research   | me_short_a_lineage_activation_v1    | SELECT
--   research   | me_short_a_outcome_observation_v1   | INSERT
--   research   | me_short_a_outcome_observation_v1   | SELECT
--   research   | me_short_a_post_signal_candle_v1    | INSERT
--   research   | me_short_a_post_signal_candle_v1    | SELECT
--   research   | me_short_a_source_signal_v1         | INSERT
--   research   | me_short_a_source_signal_v1         | SELECT
--   research   | v_me_short_a_forward_outcome_runtime_status_v1 | SELECT
--
-- Expected for trad_bot_me_short_a_runtime_audit:
--   research   | me_short_a_forward_outcome_runtime_heartbeat_v1 | SELECT
--   research   | v_me_short_a_forward_outcome_runtime_status_v1  | SELECT
--
-- 7b. No write verb anywhere outside the two append-only INSERT grants:
--
--   SELECT grantee, table_schema, table_name, privilege_type
--     FROM information_schema.role_table_grants
--    WHERE grantee IN ('trad_bot_me_short_a_runtime',
--                      'trad_bot_me_short_a_runtime_audit')
--      AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE',
--                             'REFERENCES','TRIGGER')
--      AND NOT (table_schema = 'research'
--               AND table_name IN ('me_short_a_source_signal_v1',
--                                  'me_short_a_post_signal_candle_v1',
--                                  'me_short_a_event_lineage_v1',
--                                  'me_short_a_outcome_observation_v1',
--                                  'me_short_a_forward_outcome_runtime_heartbeat_v1')
--               AND privilege_type = 'INSERT')
--    ORDER BY 1, 2, 3, 4;
--
-- Expected: ZERO rows.
--
-- 7c. dds is read-only for the runtime and invisible to the auditor:
--
--   SELECT grantee, table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) AS privs
--     FROM information_schema.role_table_grants
--    WHERE grantee IN ('trad_bot_me_short_a_runtime',
--                      'trad_bot_me_short_a_runtime_audit')
--      AND table_schema = 'dds'
--    GROUP BY grantee, table_name
--    ORDER BY grantee, table_name;
--
-- Expected: one row for trad_bot_me_short_a_runtime / dds.instrument / SELECT
--           one row for trad_bot_me_short_a_runtime / dds.scanner_setup / SELECT
--           no rows at all for trad_bot_me_short_a_runtime_audit
--
-- 7d. The runtime login is the runtime role, never the collector role:
--
--   SELECT rolname, rolcanlogin, rolinherit FROM pg_roles
--    WHERE rolname IN ('trad_bot_me_short_a_runtime',
--                      'trad_bot_me_short_a_runtime_audit',
--                      'trad_bot_me_short_a_collector');
--
-- and ME_SHORT_A_RUNTIME_DB_USER in the systemd EnvironmentFile must equal
-- trad_bot_me_short_a_runtime. The collector role is a DIFFERENT login used by
-- the Issue #208 collector API; it must never be substituted for the runtime
-- role, because it holds no dds privilege and no Phase 1 privilege.
-- ============================================================
