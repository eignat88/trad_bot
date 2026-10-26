-- ME SHORT A fill feasibility Phase 1 least-privilege grants V1
-- Issue #202. REVIEW ONLY. Do not apply to production without approval.
-- Apply only after migration 077 creates the activation/evidence tables.
-- Runtime (role trad_bot) may only append evidence and read activation state.
-- It can never activate, re-freeze, mutate, or delete audit evidence.

BEGIN;

REVOKE ALL ON research.me_short_a_fill_activation_v1 FROM trad_bot;

GRANT SELECT
    ON research.me_short_a_fill_activation_v1
    TO trad_bot;

REVOKE ALL ON research.me_short_a_fill_feasibility_v1 FROM trad_bot;

GRANT SELECT, INSERT
    ON research.me_short_a_fill_feasibility_v1
    TO trad_bot;

COMMIT;
