-- ============================================================
-- Migration 056: Direction-Level Lifecycle for Shared Prospective Experiments
-- ============================================================
-- Adds research.prospective_experiment_direction_state to enable
-- direction-level lifecycle for shared prospective experiments.
--
-- Motivation:
--   SRR_OOS_SCANNER_V1_PROSPECTIVE captures BOTH LONG and SHORT
--   under one experiment_id. The frozen research result for
--   SRR_DIRECTIONAL_EDGE_REASSESSMENT_V1 concluded:
--     LONG  = NO_EXECUTABLE_EDGE / CLOSE_NEGATIVE
--     SHORT = INCONCLUSIVE / CONTINUE_OOS
--
--   Experiment-level status alone cannot express this split.
--
-- Backward compatibility:
--   - No existing experiment IDs are changed.
--   - No existing observations/outcomes are rewritten.
--   - No scanner config or trading gates are modified.
--   - Experiments without direction-level rows use experiment-level status.
--
-- Semantics:
--   - If a (experiment_id, direction) row exists with a terminal status,
--     that direction's NEW capture is stopped and evaluator skips NEW
--     immature rows for it, but existing immature outcomes continue to mature.
--   - If no row exists, experiment-level status governs.
-- ============================================================

BEGIN;

-- ── 1. Direction-level lifecycle table ────────────────────────────

CREATE TABLE IF NOT EXISTS research.prospective_experiment_direction_state (
    experiment_id       TEXT NOT NULL REFERENCES research.prospective_experiment(experiment_id),
    direction           TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    status              TEXT NOT NULL DEFAULT 'RUNNING'
                        CHECK (status IN ('RUNNING', 'CLOSED_NEGATIVE', 'COMPLETED', 'CANCELLED')),
    closed_at           TIMESTAMPTZ,
    closure_reason      TEXT,
    closure_source      TEXT,
    closure_artifact    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (experiment_id, direction)
);

COMMENT ON TABLE research.prospective_experiment_direction_state
    IS 'Direction-level lifecycle for shared prospective experiments (migration 056)';

-- ── 2. Seed: SRR_OOS_SCANNER_V1_PROSPECTIVE / LONG = CLOSED_NEGATIVE ──

INSERT INTO research.prospective_experiment_direction_state (
    experiment_id, direction, status,
    closed_at, closure_reason, closure_source, closure_artifact
) VALUES (
    'SRR_OOS_SCANNER_V1_PROSPECTIVE', 'LONG', 'CLOSED_NEGATIVE',
    NOW(),
    'NO_EXECUTABLE_EDGE',
    'SRR_DIRECTIONAL_EDGE_REASSESSMENT_V1',
    'docs/research/SRR_DIRECTIONAL_EDGE_REASSESSMENT_V1_2026-10-01.md'
)
ON CONFLICT (experiment_id, direction) DO NOTHING;

-- ── 3. Verify ─────────────────────────────────────────────────────
-- Should return 1 row: SRR_OOS_SCANNER_V1_PROSPECTIVE / LONG / CLOSED_NEGATIVE
-- SELECT * FROM research.prospective_experiment_direction_state
-- WHERE experiment_id = 'SRR_OOS_SCANNER_V1_PROSPECTIVE';

COMMIT;
