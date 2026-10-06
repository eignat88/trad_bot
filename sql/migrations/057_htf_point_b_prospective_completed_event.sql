-- ============================================================
-- Migration 057: HTF Point-B Prospective Completion Storage
-- ============================================================
-- Adds the minimum durable lifecycle store for prospective HTF
-- Point-B completions.
--
-- Lifecycle:
--   HTF_LEVEL -> TOUCH -> REACTION -> STRUCTURE_BREAK ->
--   FIRST_VALID_POINT_B -> COMPLETED_IMMUTABLE
--
-- FIRST VALID POINT-B WINS.  The detector remains a causal geometry
-- engine; immutable completion state is owned by the observer/scanner
-- research lifecycle.
--
-- Durability comes from PostgreSQL, not from scanner process memory.
-- The PRIMARY KEY makes first-writer-wins a database guarantee:
-- INSERT ... ON CONFLICT ... DO NOTHING returns the existing row, so
-- later candidates lose without any UPDATE of immutable geometry.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS.  No existing rows are
-- rewritten and no scanner/paper/live behavior is modified.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS research.prospective_completed_event (
    experiment_id       TEXT NOT NULL,
    setup_event_id      TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'COMPLETED_IMMUTABLE'
                        CHECK (status = 'COMPLETED_IMMUTABLE'),
    snapshot            JSONB NOT NULL DEFAULT '{}'::jsonb,
    frozen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (experiment_id, setup_event_id)
);

COMMENT ON TABLE research.prospective_completed_event
    IS 'Immutable first Point-B completions for prospective HTF research lifecycle (migration 057)';
COMMENT ON COLUMN research.prospective_completed_event.status
    IS 'Terminal lifecycle state; currently COMPLETED_IMMUTABLE only';
COMMENT ON COLUMN research.prospective_completed_event.snapshot
    IS 'Full first valid Point-B completion geometry frozen at first write';
COMMENT ON COLUMN research.prospective_completed_event.frozen_at
    IS 'UTC timestamp when the first valid Point-B completion was frozen';

COMMIT;
