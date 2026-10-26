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
--   trad_bot_me_short_a_runtime_audit -> SELECT ONLY on the heartbeat log
--        and the runtime status view.
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
--   * any privilege on scanner / paper / live / direction-gate objects.
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
