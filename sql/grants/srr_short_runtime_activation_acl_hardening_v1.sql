-- SRR SHORT runtime activation ACL hardening V1
-- REVIEW ONLY. Do not apply to production without approval.
-- Apply only after migration 062 creates the activation table.
-- Must be rechecked after any broad research schema grants.

BEGIN;

REVOKE INSERT, UPDATE
    ON research.srr_short_writer_activation
    FROM trad_bot;

-- Column-level UPDATE permits SELECT ... FOR SHARE.
-- Runtime cannot change activation status or immutable fields.
GRANT SELECT
    ON research.srr_short_writer_activation
    TO trad_bot;

GRANT UPDATE (notes)
    ON research.srr_short_writer_activation
    TO trad_bot;

COMMIT;