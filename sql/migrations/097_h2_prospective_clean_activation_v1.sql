-- H2 clean prospective holdout boundary. Applied after 095 and 096.
-- Own schema only. It never changes the upstream #305 freeze or trading tables.
BEGIN;
CREATE TABLE IF NOT EXISTS research_me_entry_quality_h2_prospective.clean_activation (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    activation_ts_us BIGINT NOT NULL CHECK (activation_ts_us > 0),
    policy_fingerprints TEXT[] NOT NULL CHECK (cardinality(policy_fingerprints) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE OR REPLACE FUNCTION research_me_entry_quality_h2_prospective.deny_clean_activation_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'H2_CLEAN_ACTIVATION_IMMUTABLE' USING ERRCODE = '23514';
END;
$$;
DROP TRIGGER IF EXISTS h2_clean_activation_immutable
ON research_me_entry_quality_h2_prospective.clean_activation;
CREATE TRIGGER h2_clean_activation_immutable
BEFORE UPDATE OR DELETE ON research_me_entry_quality_h2_prospective.clean_activation
FOR EACH ROW EXECUTE FUNCTION research_me_entry_quality_h2_prospective.deny_clean_activation_mutation();
COMMIT;
