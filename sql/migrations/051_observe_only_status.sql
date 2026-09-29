-- Migration 051: Add OBSERVE_ONLY status to config.scanner_direction_gate
--
-- Problem:
--   config.scanner_direction_gate.status currently supports ENABLED/BLOCKED/REGIME.
--   ENABLED means both research capture AND paper execution allowed.
--   There is no way to say "research capture YES, paper execution NO".
--
--   Operators currently use ENABLED + reason text like "observe-only" to
--   document intent, but the code only checks status, not reason text.
--   This means observe-only scanners (e.g. FVG) can reach paper execution.
--
-- Solution:
--   Add OBSERVE_ONLY status. Semantics:
--     - Scanner generates signals normally
--     - Prospective/research OOS capture works normally
--     - Paper execution is BLOCKED (evaluate returns allowed=False)
--
-- Safety:
--   - Additive only: does not change existing ENABLED/BLOCKED/REGIME rows
--   - No automatic conversion of existing rows
--   - Config sync preserves MANUAL source precedence
--   - Manual operator action required to set OBSERVE_ONLY
--
-- Rollback:
--   ALTER TABLE config.scanner_direction_gate
--     DROP CONSTRAINT scanner_direction_gate_status_chk;
--   ALTER TABLE config.scanner_direction_gate
--     ADD CONSTRAINT scanner_direction_gate_status_chk
--     CHECK (status IN ('ENABLED', 'BLOCKED', 'REGIME'));
--
-- Applied: NOT YET (requires deployment)

-- Extend the CHECK constraint to include OBSERVE_ONLY
ALTER TABLE config.scanner_direction_gate
    DROP CONSTRAINT IF EXISTS scanner_direction_gate_status_chk;

ALTER TABLE config.scanner_direction_gate
    ADD CONSTRAINT scanner_direction_gate_status_chk
    CHECK (status IN ('ENABLED', 'BLOCKED', 'REGIME', 'OBSERVE_ONLY'));

COMMENT ON COLUMN config.scanner_direction_gate.status IS
    'Runtime status: ENABLED (research+execution), BLOCKED (no execution), '
    'REGIME (execution only in configured regimes), '
    'OBSERVE_ONLY (research capture YES, paper execution NO). '
    'OBSERVE_ONLY allows scanner signal generation and OOS research capture '
    'while preventing paper/live trade execution.';
