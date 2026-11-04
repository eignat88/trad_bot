-- ============================================================
-- Migration 083: SRR SHORT Fill Feasibility — Persistent
-- Coverage Accounting (F3, Issue #259 pre-activation hardening)
-- ============================================================
-- SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1
--
-- Purpose: persist ONE row for every prospective source event whose
-- capture attempt did NOT produce an evidence row, so the coverage
-- analysis has an explicit per-event disposition instead of silent
-- losses:
--
--   * POLICY_DENIED         - config / activation / boundary refusal
--   * API_BUDGET_DENIED     - per-cycle or per-run API budget exhausted
--   * LATENCY_BUDGET_DENIED - F1 cumulative/per-capture window exhausted
--   * DUPLICATE             - evidence row already exists (replay)
--   * CLAIM_RACE_LOST       - concurrent claimant owns this event
--
-- Coverage DENOMINATOR invariant (F3): the denominator always comes
-- from the independent prospective source (research.prospective_observation
-- for the frozen SRR SHORT experiment), NEVER from capture rows and
-- NEVER from this table. A missing event can only DECREASE measured
-- coverage — no row here can inflate it. Analysis precedence: an
-- evidence row (srr_short_fill_feasibility_v1) always overrides a
-- coverage row for the same observation_id.
--
-- Safety invariants (installation alone changes nothing):
--   * This table is never read by the scanner path; it is audit data.
--   * First disposition wins app-side (ON CONFLICT DO NOTHING on the
--     primary key); the table itself creates no new write path to any
--     existing object.
--   * No trading gate, scoring, scanner, paper, live, ME SHORT A or
--     frozen SRR outcome object is touched. Migration 082's tables are
--     NOT altered (this migration only ADDS one table).
--   * No Bybit calls are associated with this table (DB-only accounting).
--
-- This migration is source-controlled only. It is NOT applied to
-- production PostgreSQL as part of this task.
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS research;

-- ── 1. Coverage ledger: non-capturing attempts, one row per event ──────
CREATE TABLE IF NOT EXISTS research.srr_short_fill_coverage_v1 (
    observation_id bigint PRIMARY KEY
        REFERENCES research.prospective_observation(observation_id),
    experiment_id text NOT NULL
        CHECK (experiment_id='SRR_SHORT_REFERENCE_TO_EXECUTION_ALIGNMENT_V1'),
    symbol text NOT NULL,
    signal_time timestamptz NOT NULL,
    disposition text NOT NULL
        CHECK (disposition IN (
            'POLICY_DENIED',
            'API_BUDGET_DENIED',
            'LATENCY_BUDGET_DENIED',
            'DUPLICATE',
            'CLAIM_RACE_LOST'
        )),
    reason text,
    recorded_at timestamptz NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE research.srr_short_fill_coverage_v1 IS
    'Issue #259 F3: one row per prospective source event whose SRR SHORT fill capture attempt produced no evidence row (first disposition wins). Denominator for coverage comes from research.prospective_observation, never from capture or coverage rows.';
COMMENT ON COLUMN research.srr_short_fill_coverage_v1.observation_id IS
    'FK to the independent prospective source row (denominator key).';
COMMENT ON COLUMN research.srr_short_fill_coverage_v1.disposition IS
    'Why capture did not happen: POLICY_DENIED / API_BUDGET_DENIED / LATENCY_BUDGET_DENIED / DUPLICATE / CLAIM_RACE_LOST.';
COMMENT ON COLUMN research.srr_short_fill_coverage_v1.reason IS
    'Free-form detail (budget reason, remaining latency ms, conflict marker).';

-- ── 2. Audit indexes ───────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_srr_short_fill_coverage_disposition
    ON research.srr_short_fill_coverage_v1 (disposition);

CREATE INDEX IF NOT EXISTS idx_srr_short_fill_coverage_symbol_time
    ON research.srr_short_fill_coverage_v1 (symbol, signal_time);

-- ── 3. Verify (read-only) ──────────────────────────────────────────────
--
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_schema='research'
--   AND table_name='srr_short_fill_coverage_v1'
-- ORDER BY ordinal_position;
--
-- -- Coverage denominator vs disposition (never from capture rows alone):
-- SELECT c.disposition, count(*)
-- FROM research.srr_short_fill_coverage_v1 c
-- GROUP BY c.disposition ORDER BY 2 DESC;

COMMIT;

-- ============================================================
-- PRODUCTION STATUS:
--   NOT APPLIED
--   NOT ACTIVATED (adds an audit table only; capture stays gated by
--   the 082 activation record + SRR_SHORT_FILL_CAPTURE_ENABLED=1)
-- Existing prospective_observation / prospective_outcome / SRR frozen
-- outcomes / scanner / paper / live / ME SHORT A data are not modified.
-- ============================================================
