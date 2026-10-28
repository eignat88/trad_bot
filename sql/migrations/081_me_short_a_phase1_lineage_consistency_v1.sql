-- ME_SHORT_A_PHASE1_LINEAGE_CONSISTENCY_FIX_V1 (Issues #202 / #206 / #208).
--
-- Problem: prospective-only ME SHORT A lineage rows were written with
-- link_status = 'LINKED' while phase1_linked = FALSE / phase1_event_key =
-- NULL (confirmed incidents: KORUUSDT 2026-10-28 05:35:14 UTC, DOGEUSDT
-- 2026-10-28 08:40:52 UTC). The Phase 1 snapshot is committed by a
-- different process/transaction, so at READ COMMITTED the runtime's
-- linkage lookup can miss it; append-only + ON CONFLICT DO NOTHING then
-- made the false record permanent.
--
-- Fix: the writer may now record the explicit diagnostic state
-- 'PHASE1_PENDING' — independent capture verified (identity + source
-- signal), Phase 1 linkage NOT confirmed after a bounded READ-ONLY retry.
-- Such rows never claim a fully confirmed relation and are excluded from
-- the Issue #206 eligible cohort (link_status = 'LINKED' AND phase1_linked).
--
-- Scope: this migration only widens the link_status CHECK and adds one
-- defensive consistency CHECK. It performs NO UPDATE/DELETE on any row:
-- the confirmed KORUUSDT/DOGEUSDT records and every other append-only row
-- stay byte-identical. No freeze timestamps, event identity, outcome
-- policy, scanner/regime/scoring/trading gates are touched.
--
-- Idempotent: safe to reapply (DROP CONSTRAINT IF EXISTS + ADD).
BEGIN;

-- Migration 078 installed the inline CHECK
--   link_status text NOT NULL CHECK (link_status IN ('PENDING','LINKED','QUARANTINED'))
-- whose PostgreSQL auto-generated constraint name is
-- me_short_a_event_lineage_v1_link_status_check. Replace it with the
-- identical original statuses plus ONE scoped diagnostic status.
ALTER TABLE research.me_short_a_event_lineage_v1
    DROP CONSTRAINT IF EXISTS me_short_a_event_lineage_v1_link_status_check;
ALTER TABLE research.me_short_a_event_lineage_v1
    ADD CONSTRAINT me_short_a_event_lineage_v1_link_status_check
    CHECK (link_status IN ('PENDING','LINKED','QUARANTINED','PHASE1_PENDING'));

-- Defensive consistency: a PHASE1_PENDING record must never assert a
-- confirmed Phase 1 linkage.
ALTER TABLE research.me_short_a_event_lineage_v1
    DROP CONSTRAINT IF EXISTS chk_me_short_a_event_lineage_phase1_pending;
ALTER TABLE research.me_short_a_event_lineage_v1
    ADD CONSTRAINT chk_me_short_a_event_lineage_phase1_pending
    CHECK ((link_status <> 'PHASE1_PENDING') OR (phase1_linked = FALSE));

COMMENT ON COLUMN research.me_short_a_event_lineage_v1.link_status IS
  'PENDING | LINKED | QUARANTINED | PHASE1_PENDING. LINKED requires source '
  'signal confirmed, event_key integrity and an OBSERVED Phase 1 row with '
  'the same event_key. PHASE1_PENDING (ME_SHORT_A_PHASE1_LINEAGE_CONSISTENCY'
  '_FIX_V1) = independent capture verified, Phase 1 linkage unconfirmed '
  'after bounded retry; never eligible for the Issue #206 verdict.';

COMMIT;
