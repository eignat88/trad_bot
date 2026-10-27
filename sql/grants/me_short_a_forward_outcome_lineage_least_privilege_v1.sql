-- ============================================================
-- ME SHORT A forward-only outcome and lineage capture V1
-- Least-privilege grants (Issue #208)
-- ============================================================
-- REVIEW ONLY. Do not apply to production without explicit approval.
-- Apply only AFTER migration 078 creates the activation / capture tables.
--
-- Roles (created idempotently, following migration 075 convention):
--   trad_bot_me_short_a_collector  -> INSERT ONLY on the capture tables,
--                                     SELECT ONLY on the activation row.
--   trad_bot_me_short_a_audit      -> SELECT ONLY everywhere.
--
-- Why the explicit REVOKE first:
--   migration 049/050 install ALTER DEFAULT PRIVILEGES ... GRANT SELECT,
--   INSERT, UPDATE ON TABLES TO trad_bot for schema research, so every new
--   research table would otherwise inherit write privileges for the whole
--   runtime role. Those inherited rights are removed here before the narrow
--   grants are added.
--
-- What this file deliberately NEVER grants:
--   * UPDATE / DELETE / TRUNCATE on any table of this protocol
--     (the tables are append-only and guarded by triggers anyway),
--   * any privilege on research.me_short_a_fill_feasibility_v1 (Phase 1),
--   * any privilege on research.me_short_a_fill_activation_v1,
--   * any privilege on research.prospective_observation /
--     research.prospective_outcome (the closed frozen OOS cohort),
--   * any privilege on scanner / paper / live / direction-gate objects.
--
-- Must be applied by a role allowed to create roles (e.g. postgres).
-- ============================================================

BEGIN;

-- ── 1. Roles (idempotent) ────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot_me_short_a_collector') THEN
        EXECUTE 'CREATE ROLE trad_bot_me_short_a_collector WITH LOGIN NOINHERIT';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot_me_short_a_audit') THEN
        EXECUTE 'CREATE ROLE trad_bot_me_short_a_audit WITH LOGIN NOINHERIT';
    END IF;
END $$;

-- ── 2. Schema visibility ─────────────────────────────────────

GRANT USAGE ON SCHEMA research TO trad_bot_me_short_a_collector;
GRANT USAGE ON SCHEMA research TO trad_bot_me_short_a_audit;

-- ── 3. Drop every inherited/broad privilege on the new objects ─

DO $$
DECLARE
    tbl text;
    rol text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'me_short_a_lineage_activation_v1',
        'me_short_a_source_signal_v1',
        'me_short_a_post_signal_candle_v1',
        'me_short_a_event_lineage_v1',
        'me_short_a_outcome_observation_v1'
    ] LOOP
        FOREACH rol IN ARRAY ARRAY[
            'trad_bot',
            'analytics_agent',
            'analytics_runner',
            'trad_bot_me_short_a_collector',
            'trad_bot_me_short_a_audit'
        ] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rol) THEN
                EXECUTE format('REVOKE ALL ON research.%I FROM %I', tbl, rol);
            END IF;
        END LOOP;
    END LOOP;
END $$;

-- ── 4. Collector: INSERT ONLY (plus read-only activation gate) ─
-- The collector's idempotency is INSERT ... ON CONFLICT DO NOTHING + rowcount,
-- so it never needs SELECT on the capture tables. The single SELECT is on the
-- activation table and is required for the fail-closed RUNNING + freeze_ts
-- gate and for the WHERE EXISTS boundary guard inside each INSERT.

GRANT SELECT
    ON research.me_short_a_lineage_activation_v1
    TO trad_bot_me_short_a_collector;

GRANT INSERT
    ON research.me_short_a_source_signal_v1
    TO trad_bot_me_short_a_collector;

GRANT INSERT
    ON research.me_short_a_post_signal_candle_v1
    TO trad_bot_me_short_a_collector;

GRANT INSERT
    ON research.me_short_a_event_lineage_v1
    TO trad_bot_me_short_a_collector;

GRANT INSERT
    ON research.me_short_a_outcome_observation_v1
    TO trad_bot_me_short_a_collector;

-- Explicit negative statements: keep the intent greppable and reviewable.
REVOKE UPDATE, DELETE, TRUNCATE
    ON research.me_short_a_source_signal_v1
    FROM trad_bot_me_short_a_collector;
REVOKE UPDATE, DELETE, TRUNCATE
    ON research.me_short_a_post_signal_candle_v1
    FROM trad_bot_me_short_a_collector;
REVOKE UPDATE, DELETE, TRUNCATE
    ON research.me_short_a_event_lineage_v1
    FROM trad_bot_me_short_a_collector;
REVOKE UPDATE, DELETE, TRUNCATE
    ON research.me_short_a_outcome_observation_v1
    FROM trad_bot_me_short_a_collector;
REVOKE UPDATE, DELETE, TRUNCATE, INSERT
    ON research.me_short_a_lineage_activation_v1
    FROM trad_bot_me_short_a_collector;

-- ── 5. Audit: SELECT ONLY ────────────────────────────────────

GRANT SELECT
    ON research.me_short_a_lineage_activation_v1,
       research.me_short_a_source_signal_v1,
       research.me_short_a_post_signal_candle_v1,
       research.me_short_a_event_lineage_v1,
       research.me_short_a_outcome_observation_v1
    TO trad_bot_me_short_a_audit;

GRANT SELECT
    ON research.v_me_short_a_lineage_status_v1
    TO trad_bot_me_short_a_audit;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON research.me_short_a_source_signal_v1,
       research.me_short_a_post_signal_candle_v1,
       research.me_short_a_event_lineage_v1,
       research.me_short_a_outcome_observation_v1
    FROM trad_bot_me_short_a_audit;

-- ── 6. Verification (read-only) ──────────────────────────────
--
-- SELECT grantee, table_name, privilege_type
-- FROM information_schema.role_table_grants
-- WHERE table_schema = 'research'
--   AND table_name LIKE 'me_short_a_%'
--   AND grantee IN ('trad_bot', 'trad_bot_me_short_a_collector',
--                   'trad_bot_me_short_a_audit')
-- ORDER BY table_name, grantee, privilege_type;
--
-- Expected for trad_bot_me_short_a_collector:
--   me_short_a_lineage_activation_v1 : SELECT
--   me_short_a_source_signal_v1      : INSERT
--   me_short_a_post_signal_candle_v1 : INSERT
--   me_short_a_event_lineage_v1      : INSERT
--   me_short_a_outcome_observation_v1: INSERT
-- Expected for trad_bot_me_short_a_audit:
--   every me_short_a_* object : SELECT
-- Expected for trad_bot on me_short_a_*: nothing.

COMMIT;
