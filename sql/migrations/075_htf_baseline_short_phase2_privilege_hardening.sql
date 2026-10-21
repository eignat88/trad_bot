-- 075_htf_baseline_short_phase2_privilege_hardening.sql
-- Issue #176 Phase-2: PostgreSQL privilege hardening (CP1).
--
-- Applies AFTER 074_htf_baseline_short_phase2_registration.sql.
--
-- Design:
--   * dedicated role trad_bot_htf_p2_observer (LOGIN, least privilege): reads
--     the Phase-2 tables and executes ONLY the insert function;
--   * dedicated role trad_bot_htf_p2_admin (NOLOGIN, operator `SET ROLE`):
--     executes ONLY the activation function — the administrative activation
--     procedure is structurally unavailable to the runtime observer;
--   * direct INSERT/UPDATE/DELETE on the Phase-2 tables is revoked from
--     PUBLIC and from the existing application roles (trad_bot and the
--     analytics roles when present). Writes are possible ONLY through
--     research.htf_baseline_short_phase2_insert_observation(...), which is
--     SECURITY DEFINER with a pinned search_path and keeps the eligibility
--     checks of 074 (plus the guard triggers) as defence in depth;
--   * the freeze registry accepts writes only through the activation
--     function; direct manipulation is revoked from everyone but the owner;
--   * grants on ALL pre-existing research objects are untouched — this
--     migration only ever names the Phase-2 objects.
--
-- Atomic / fail-closed: the entire migration is ONE statement (a single DO
-- block), so it commits atomically even when applied under autocommit. Any
-- failed precondition raises and rolls the whole migration back.
--
-- Existing default privileges of schema research (049/050 grant
-- SELECT, INSERT, UPDATE on new research tables to trad_bot) are taken into
-- account: their effect on the Phase-2 tables is explicitly revoked below.
--
-- Must be applied by a role allowed to create roles (e.g. postgres).

DO $mig075$
DECLARE
    _db        TEXT := current_database();
    _rol       TEXT;
    _obs_canlogin BOOLEAN;
    _obs_super BOOLEAN;
    _obs_createdb BOOLEAN;
    _obs_createrole BOOLEAN;
    _adm_canlogin BOOLEAN;
    _adm_super BOOLEAN;
    _fn_insert CONSTANT TEXT :=
        'research.htf_baseline_short_phase2_insert_observation('
        'text,text,text,text,text,timestamp with time zone,timestamp with time zone,'
        'timestamp with time zone,numeric,numeric,boolean,boolean,jsonb)';
    _fn_activate CONSTANT TEXT :=
        'research.htf_baseline_short_phase2_activate(text,timestamp with time zone,text)';
BEGIN
    -- ---------------------------------------------------------- preconditions
    IF to_regclass('research.htf_baseline_short_phase2_experiment') IS NULL
       OR to_regclass('research.htf_baseline_short_phase2_observation') IS NULL
       OR to_regclass('research.htf_baseline_short_phase2_freeze_registry') IS NULL THEN
        RAISE EXCEPTION 'P2_MIGRATION_074_REQUIRED: apply 074 before 075';
    END IF;
    IF to_regprocedure(_fn_insert) IS NULL OR to_regprocedure(_fn_activate) IS NULL THEN
        RAISE EXCEPTION 'P2_MIGRATION_074_FUNCTIONS_REQUIRED';
    END IF;

    -- ----------------------------------------------------------------- roles
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot_htf_p2_observer') THEN
        EXECUTE 'CREATE ROLE trad_bot_htf_p2_observer WITH LOGIN NOINHERIT';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trad_bot_htf_p2_admin') THEN
        EXECUTE 'CREATE ROLE trad_bot_htf_p2_admin WITH NOLOGIN NOINHERIT';
    END IF;

    SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole
      INTO _obs_canlogin, _obs_super, _obs_createdb, _obs_createrole
      FROM pg_roles WHERE rolname = 'trad_bot_htf_p2_observer';
    IF NOT _obs_canlogin OR _obs_super OR _obs_createdb OR _obs_createrole THEN
        RAISE EXCEPTION 'P2_OBSERVER_ROLE_ATTRS_UNSAFE';
    END IF;
    SELECT rolcanlogin, rolsuper INTO _adm_canlogin, _adm_super
      FROM pg_roles WHERE rolname = 'trad_bot_htf_p2_admin';
    IF _adm_canlogin OR _adm_super THEN
        RAISE EXCEPTION 'P2_ADMIN_ROLE_ATTRS_UNSAFE';
    END IF;

    EXECUTE format('GRANT CONNECT ON DATABASE %I TO trad_bot_htf_p2_observer, trad_bot_htf_p2_admin', _db);
    EXECUTE 'GRANT USAGE ON SCHEMA research TO trad_bot_htf_p2_observer, trad_bot_htf_p2_admin';

    -- ------------------------------------------ revoke PUBLIC on the surface
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', _fn_insert);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', _fn_activate);
    EXECUTE 'REVOKE ALL ON FUNCTION research.htf_baseline_short_phase2_experiment_guard() FROM PUBLIC';
    EXECUTE 'REVOKE ALL ON FUNCTION research.htf_baseline_short_phase2_observation_guard() FROM PUBLIC';
    EXECUTE 'REVOKE ALL ON FUNCTION research.htf_baseline_short_phase2_freeze_registry_guard() FROM PUBLIC';
    EXECUTE 'REVOKE ALL ON research.htf_baseline_short_phase2_experiment FROM PUBLIC';
    EXECUTE 'REVOKE ALL ON research.htf_baseline_short_phase2_observation FROM PUBLIC';
    EXECUTE 'REVOKE ALL ON research.htf_baseline_short_phase2_freeze_registry FROM PUBLIC';
    EXECUTE 'REVOKE ALL ON SEQUENCE research.htf_baseline_short_phase2_observation_observation_id_seq FROM PUBLIC';

    -- --------------------------- revoke application roles on the P2 surface
    -- (default privileges of schema research may have granted rights at
    -- CREATE time; direct DML is not part of the application contract)
    FOREACH _rol IN ARRAY ARRAY['trad_bot', 'analytics_runner', 'analytics_agent'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = _rol) THEN
            EXECUTE format('REVOKE ALL ON research.htf_baseline_short_phase2_experiment FROM %I', _rol);
            EXECUTE format('REVOKE ALL ON research.htf_baseline_short_phase2_observation FROM %I', _rol);
            EXECUTE format('REVOKE ALL ON research.htf_baseline_short_phase2_freeze_registry FROM %I', _rol);
            EXECUTE format('REVOKE ALL ON SEQUENCE research.htf_baseline_short_phase2_observation_observation_id_seq FROM %I', _rol);
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I', _fn_insert, _rol);
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I', _fn_activate, _rol);
        END IF;
    END LOOP;

    -- --------------------- write/activation functions: SECURITY DEFINER wall
    EXECUTE format('ALTER FUNCTION %s SECURITY DEFINER', _fn_insert);
    EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, research', _fn_insert);
    EXECUTE format('ALTER FUNCTION %s SECURITY DEFINER', _fn_activate);
    EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, research', _fn_activate);

    -- ----------------------------------------------------- minimal grants
    EXECUTE 'GRANT SELECT ON research.htf_baseline_short_phase2_experiment TO trad_bot_htf_p2_observer';
    EXECUTE 'GRANT SELECT ON research.htf_baseline_short_phase2_observation TO trad_bot_htf_p2_observer';
    EXECUTE 'GRANT SELECT ON research.htf_baseline_short_phase2_freeze_registry TO trad_bot_htf_p2_observer';
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO trad_bot_htf_p2_observer', _fn_insert);
    -- activation stays an administrative procedure: admin role only
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO trad_bot_htf_p2_admin', _fn_activate);
    EXECUTE 'GRANT SELECT ON research.htf_baseline_short_phase2_experiment TO trad_bot_htf_p2_admin';
    EXECUTE 'GRANT SELECT ON research.htf_baseline_short_phase2_observation TO trad_bot_htf_p2_admin';
    EXECUTE 'GRANT SELECT ON research.htf_baseline_short_phase2_freeze_registry TO trad_bot_htf_p2_admin';
END;
$mig075$;
