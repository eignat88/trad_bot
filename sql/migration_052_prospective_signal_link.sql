-- Migration 052: Add prospective_observation_id to research_signal
-- and batch-promote existing unlinked prospective observations.
--
-- PROBLEM: prospective_observation rows are created but never promoted
-- to research.research_signal, so the evaluator never sees them.
--
-- FIX:
--   1. Add nullable prospective_observation_id column + FK
--   2. Batch-promote all existing unlinked prospective observations
--   3. Backfill observation_id=3 (BTCUSDT BREAKOUT_RETEST LONG 2026-09-28)

BEGIN;

-- ── 1. Schema change ──────────────────────────────────────────

ALTER TABLE research.research_signal
ADD COLUMN IF NOT EXISTS prospective_observation_id bigint NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'research_signal_prospective_obs_fkey'
    ) THEN
        ALTER TABLE research.research_signal
        ADD CONSTRAINT research_signal_prospective_obs_fkey
        FOREIGN KEY (prospective_observation_id)
        REFERENCES research.prospective_observation(observation_id)
        ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_rs_prospective_obs
ON research.research_signal(prospective_observation_id)
WHERE prospective_observation_id IS NOT NULL;

-- ── 2. Batch-promote all existing unlinked prospective observations ──
-- Uses the same ON CONFLICT DO NOTHING pattern as the resolver,
-- so it's idempotent and safe to re-run.

INSERT INTO research.research_signal (
    observation_id, experiment_id, scanner_name, parameter_set_id,
    symbol, direction, signal_time, signal_candle_open_time,
    reference_price, invalidation_price, target_1, target_2,
    score, features, parameters, market_regime,
    prospective_observation_id
)
SELECT
    po.observation_id,
    po.experiment_id,
    'PROSPECTIVE',
    'prospective_oos',
    po.symbol,
    po.direction,
    po.signal_time,
    0,
    po.reference_price,
    po.invalidation_price,
    po.target_1,
    po.target_2,
    po.score,
    po.features,
    po.parameters,
    po.market_regime,
    po.observation_id
FROM research.prospective_observation po
WHERE po.reference_price > 0
  AND po.invalidation_price > 0
  AND po.target_1 > 0
  AND NOT EXISTS (
      SELECT 1 FROM research.research_signal rs
      WHERE rs.prospective_observation_id = po.observation_id
  )
ON CONFLICT (experiment_id, symbol, direction, signal_candle_open_time)
WHERE signal_candle_open_time > 0
DO NOTHING;

COMMIT;

-- ── 3. Verification ──

-- observation #3 should now have exactly 1 research_signal
SELECT
    po.observation_id,
    po.experiment_id,
    po.symbol,
    po.signal_time,
    rs.signal_id,
    rs.prospective_observation_id
FROM research.prospective_observation po
LEFT JOIN research.research_signal rs
    ON rs.prospective_observation_id = po.observation_id
WHERE po.observation_id = 3;

-- Overall counts
SELECT
    (SELECT COUNT(*) FROM research.prospective_observation
     WHERE experiment_id IN (
         'BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1',
         'FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1',
         'TREND_PULLBACK_V3_HIGH_VOL_OOS_V1'
     )) AS prospective_observations,
    (SELECT COUNT(*) FROM research.research_signal
     WHERE prospective_observation_id IS NOT NULL) AS promoted_signals,
    (SELECT COUNT(*) FROM research.prospective_observation po
     WHERE po.experiment_id IN (
         'BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1',
         'FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1',
         'TREND_PULLBACK_V3_HIGH_VOL_OOS_V1'
     )
     AND po.invalidation_price > 0
     AND po.target_1 > 0
     AND NOT EXISTS (
         SELECT 1 FROM research.research_signal rs
         WHERE rs.prospective_observation_id = po.observation_id
     )) AS orphan_observations;
