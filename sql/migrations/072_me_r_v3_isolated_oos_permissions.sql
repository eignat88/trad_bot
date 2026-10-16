-- Issue #154: explicit least-privilege grants for V3 research-only tables.
-- Must be applied only after 068..071, on approved isolated DBs.
BEGIN;
DO $$
BEGIN
    IF current_database() NOT IN (
        'trad_bot_me_r_v3_oos',
        'trad_bot_me_r_v3_oos_pg_test'
    ) THEN
        RAISE EXCEPTION 'ME_R_V3_ISOLATED_DATABASE_REQUIRED';
    END IF;
    IF to_regclass('research_me_early_mae_v3.source_event') IS NULL
       OR to_regclass('research_me_early_mae_v3.variant_outcome') IS NULL
       OR to_regclass('research_me_early_mae_v3.pipeline_run') IS NULL
       OR to_regclass('research_me_early_mae_v3.protocol') IS NULL
       OR to_regclass('research_me_early_mae_v3.variant') IS NULL THEN
        RAISE EXCEPTION 'ME_R_V3_SCHEMA_INCOMPLETE';
    END IF;
END $$;

GRANT CONNECT ON DATABASE trad_bot_me_r_v3_oos_pg_test TO trad_bot_me_r_v3_oos;
GRANT USAGE ON SCHEMA research_me_early_mae_v3 TO trad_bot_me_r_v3_oos;
GRANT SELECT, INSERT, UPDATE ON
    research_me_early_mae_v3.protocol,
    research_me_early_mae_v3.variant,
    research_me_early_mae_v3.source_event,
    research_me_early_mae_v3.variant_outcome,
    research_me_early_mae_v3.pipeline_run
    TO trad_bot_me_r_v3_oos;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA research_me_early_mae_v3
    TO trad_bot_me_r_v3_oos;
COMMIT;
