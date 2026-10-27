-- ME SHORT A prospective source intake least-privilege grants V1
-- Issue #208 corrective. REVIEW ONLY. Do not apply to production without
-- approval. Apply only after migration 080 creates the intake tables.
--
-- Writer: the scanner-side research hook (role trad_bot) may only append
-- intake evidence and read activation state. It can never activate,
-- re-freeze, mutate, or delete evidence.
--
-- Reader: the forward outcome runtime / audit roles may only read. The
-- runtime's candidate intake connection is a READ ONLY session and never
-- writes anywhere.
--
-- The four Issue #208 capture tables keep their existing grants
-- (me_short_a_forward_outcome_lineage_least_privilege_v1.sql): the forward
-- outcome runtime remains their ONLY writer.

BEGIN;

REVOKE ALL ON research.me_short_a_source_intake_activation_v1 FROM trad_bot;

GRANT SELECT
    ON research.me_short_a_source_intake_activation_v1
    TO trad_bot;

REVOKE ALL ON research.me_short_a_source_intake_v1 FROM trad_bot;

GRANT SELECT, INSERT
    ON research.me_short_a_source_intake_v1
    TO trad_bot;

-- Append-only: never grant UPDATE/DELETE/TRUNCATE to anyone in this file.

GRANT SELECT
    ON research.me_short_a_source_intake_activation_v1
    TO trad_bot_me_short_a_runtime;

GRANT SELECT
    ON research.me_short_a_source_intake_v1
    TO trad_bot_me_short_a_runtime;

GRANT SELECT
    ON research.me_short_a_source_intake_activation_v1
    TO trad_bot_me_short_a_audit;

GRANT SELECT
    ON research.me_short_a_source_intake_v1
    TO trad_bot_me_short_a_audit;

COMMIT;
