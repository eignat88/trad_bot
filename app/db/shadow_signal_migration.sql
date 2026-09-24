-- ============================================================
-- Shadow Signal Collection: ATR Wick Rejection Short V1
-- Idempotent migration — safe to re-run without data loss.
-- ============================================================

-- ── signal table ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dds.shadow_signal (
    signal_id BIGSERIAL PRIMARY KEY,
    experiment_id TEXT NOT NULL DEFAULT 'ATR_WICK_REJECTION_SHORT_V1',
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '5m',
    signal_time TIMESTAMPTZ NOT NULL, signal_price NUMERIC NOT NULL,
    open NUMERIC NOT NULL, high NUMERIC NOT NULL, low NUMERIC NOT NULL,
    close NUMERIC NOT NULL, volume NUMERIC NOT NULL,
    atr NUMERIC NOT NULL, atr_pct NUMERIC NOT NULL,
    wick_size NUMERIC NOT NULL, wick_atr NUMERIC NOT NULL,
    upper_wick_pct NUMERIC NOT NULL, close_location NUMERIC NOT NULL,
    rsi NUMERIC NOT NULL, stoch_rsi NUMERIC,
    bb_upper NUMERIC NOT NULL, bb_mid NUMERIC NOT NULL, bb_lower NUMERIC NOT NULL,
    bb_width NUMERIC NOT NULL, distance_to_upper_bb NUMERIC NOT NULL,
    ema_fast NUMERIC NOT NULL, ema_medium NUMERIC NOT NULL,
    ema_slow NUMERIC NOT NULL, ema_slope NUMERIC NOT NULL,
    volume_ratio NUMERIC NOT NULL,
    signal_version TEXT NOT NULL DEFAULT '1.0.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT shadow_signal_timeframe_chk CHECK (timeframe = '5m'),
    CONSTRAINT shadow_signal_direction_chk CHECK (experiment_id = 'ATR_WICK_REJECTION_SHORT_V1')
);

-- strict_pass (idempotent)
ALTER TABLE dds.shadow_signal ADD COLUMN IF NOT EXISTS strict_pass BOOLEAN NOT NULL DEFAULT FALSE;

-- OOS filter flags — normalise old schema if previously applied with NOT NULL/DEFAULT
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='dds' AND table_name='shadow_signal' AND column_name='oos_a_stoch_08') THEN
        ALTER TABLE dds.shadow_signal ALTER COLUMN oos_a_stoch_08 DROP NOT NULL;
        ALTER TABLE dds.shadow_signal ALTER COLUMN oos_a_stoch_08 DROP DEFAULT;
        ALTER TABLE dds.shadow_signal ALTER COLUMN oos_b_stoch_08_vol_10 DROP NOT NULL;
        ALTER TABLE dds.shadow_signal ALTER COLUMN oos_b_stoch_08_vol_10 DROP DEFAULT;
        ALTER TABLE dds.shadow_signal ALTER COLUMN oos_c_stoch_06_vol_10 DROP NOT NULL;
        ALTER TABLE dds.shadow_signal ALTER COLUMN oos_c_stoch_06_vol_10 DROP DEFAULT;
    END IF;
END $$;

ALTER TABLE dds.shadow_signal ADD COLUMN IF NOT EXISTS oos_a_stoch_08 BOOLEAN NULL;
ALTER TABLE dds.shadow_signal ADD COLUMN IF NOT EXISTS oos_b_stoch_08_vol_10 BOOLEAN NULL;
ALTER TABLE dds.shadow_signal ADD COLUMN IF NOT EXISTS oos_c_stoch_06_vol_10 BOOLEAN NULL;
ALTER TABLE dds.shadow_signal ADD COLUMN IF NOT EXISTS oos_filter_version TEXT NULL;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_shadow_signal_experiment ON dds.shadow_signal (experiment_id);
CREATE INDEX IF NOT EXISTS idx_shadow_signal_symbol ON dds.shadow_signal (symbol, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_signal_time ON dds.shadow_signal (signal_time DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_signal_experiment_symbol_time
ON dds.shadow_signal (experiment_id, symbol, signal_time);

-- ── outcome table ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dds.shadow_signal_outcome (
    signal_id BIGINT PRIMARY KEY REFERENCES dds.shadow_signal(signal_id) ON DELETE CASCADE,
    experiment_id TEXT NOT NULL DEFAULT 'ATR_WICK_REJECTION_SHORT_V1',
    symbol TEXT NOT NULL,
    mfe_15m NUMERIC, mae_15m NUMERIC, mfe_30m NUMERIC, mae_30m NUMERIC,
    mfe_60m NUMERIC, mae_60m NUMERIC, mfe_120m NUMERIC, mae_120m NUMERIC,
    mfe_240m NUMERIC, mae_240m NUMERIC, mfe_eod NUMERIC, mae_eod NUMERIC,
    reached_minus_0_5 BOOLEAN NOT NULL DEFAULT FALSE,
    reached_minus_1_0 BOOLEAN NOT NULL DEFAULT FALSE,
    reached_minus_1_5 BOOLEAN NOT NULL DEFAULT FALSE,
    reached_minus_2_0 BOOLEAN NOT NULL DEFAULT FALSE,
    hit_plus_0_5_before_target BOOLEAN NOT NULL DEFAULT FALSE,
    hit_plus_1_0_before_target BOOLEAN NOT NULL DEFAULT FALSE,
    hit_plus_1_5_before_target BOOLEAN NOT NULL DEFAULT FALSE,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT shadow_outcome_experiment_chk CHECK (experiment_id = 'ATR_WICK_REJECTION_SHORT_V1')
);
CREATE INDEX IF NOT EXISTS idx_shadow_outcome_experiment ON dds.shadow_signal_outcome (experiment_id);
CREATE INDEX IF NOT EXISTS idx_shadow_outcome_symbol ON dds.shadow_signal_outcome (symbol);
CREATE INDEX IF NOT EXISTS idx_shadow_outcome_mfe ON dds.shadow_signal_outcome (mfe_60m DESC NULLS LAST);

-- Per-horizon timestamps + is_final (idempotent, NO destructive UPDATE)
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_15m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_30m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_60m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_120m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_240m_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS evaluated_eod_at TIMESTAMPTZ NULL;
ALTER TABLE dds.shadow_signal_outcome ADD COLUMN IF NOT EXISTS is_final BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_shadow_outcome_is_final ON dds.shadow_signal_outcome (is_final) WHERE is_final = FALSE;

-- ── General reporting views ─────────────────────────────────

CREATE OR REPLACE VIEW dds.v_shadow_signal_summary AS
SELECT
    s.symbol,
    COUNT(*) AS signals,
    COUNT(o.signal_id) AS evaluated,
    ROUND(AVG(s.atr_pct), 4) AS avg_atr_pct,
    ROUND(AVG(s.rsi), 2) AS avg_rsi,
    ROUND(AVG(s.wick_atr), 4) AS avg_wick_atr,
    ROUND(AVG(s.close_location), 4) AS avg_close_location,
    ROUND(AVG(s.bb_width), 4) AS avg_bb_width,
    ROUND(AVG(s.volume_ratio), 4) AS avg_volume_ratio
FROM dds.shadow_signal s
LEFT JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
GROUP BY s.symbol
ORDER BY signals DESC;

CREATE OR REPLACE VIEW dds.v_shadow_mfe_mae_distribution AS
SELECT
    o.symbol,
    COUNT(*) AS samples,
    ROUND(AVG(o.mfe_60m), 4) AS avg_mfe_60m,
    ROUND(AVG(o.mae_60m), 4) AS avg_mae_60m,
    ROUND(AVG(o.mfe_120m), 4) AS avg_mfe_120m,
    ROUND(AVG(o.mae_120m), 4) AS avg_mae_120m,
    ROUND(
        COUNT(*) FILTER (WHERE o.reached_minus_0_5)::numeric / COUNT(*), 4
    ) AS pct_reached_0_5,
    ROUND(
        COUNT(*) FILTER (WHERE o.reached_minus_1_0)::numeric / COUNT(*), 4
    ) AS pct_reached_1_0,
    ROUND(
        COUNT(*) FILTER (WHERE o.reached_minus_1_5)::numeric / COUNT(*), 4
    ) AS pct_reached_1_5,
    ROUND(
        COUNT(*) FILTER (WHERE o.reached_minus_2_0)::numeric / COUNT(*), 4
    ) AS pct_reached_2_0,
    ROUND(
        COUNT(*) FILTER (WHERE o.hit_plus_0_5_before_target)::numeric / COUNT(*), 4
    ) AS pct_hit_0_5_adverse
FROM dds.shadow_signal_outcome o
WHERE o.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
GROUP BY o.symbol
HAVING COUNT(*) >= 3
ORDER BY avg_mfe_60m DESC NULLS LAST;

CREATE OR REPLACE VIEW dds.v_shadow_feature_comparison AS
SELECT
    o.symbol,
    CASE WHEN o.mfe_60m > 0 THEN 'profitable' ELSE 'losing' END AS outcome,
    COUNT(*) AS samples,
    ROUND(AVG(s.atr_pct), 4) AS avg_atr_pct,
    ROUND(AVG(s.rsi), 2) AS avg_rsi,
    ROUND(AVG(s.wick_atr), 4) AS avg_wick_atr,
    ROUND(AVG(s.close_location), 4) AS avg_close_location,
    ROUND(AVG(s.bb_width), 4) AS avg_bb_width,
    ROUND(AVG(s.distance_to_upper_bb), 4) AS avg_distance_to_upper_bb,
    ROUND(AVG(s.volume_ratio), 4) AS avg_volume_ratio,
    ROUND(AVG(s.ema_slope), 6) AS avg_ema_slope
FROM dds.shadow_signal s
JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
WHERE o.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
  AND o.mfe_60m IS NOT NULL
GROUP BY o.symbol, CASE WHEN o.mfe_60m > 0 THEN 'profitable' ELSE 'losing' END
HAVING COUNT(*) >= 2
ORDER BY o.symbol, outcome;

-- ============================================================
-- OOS Analysis Views
-- Cohort filter: s.oos_filter_version = 'ATR_WICK_FILTER_OOS_V1'
-- GOOD = mfe_60m >= 1.0 AND mae_60m <= 0.5
-- BAD  = mae_60m >= 1.0 AND mfe_60m < 1.0
-- Target hits: denominator = passes AND is_final = TRUE
-- ============================================================

CREATE OR REPLACE VIEW dds.v_shadow_oos_filter_summary AS
WITH oos_cohort AS (
    SELECT s.symbol, s.oos_filter_version,
        s.oos_a_stoch_08, s.oos_b_stoch_08_vol_10, s.oos_c_stoch_06_vol_10,
        o.mfe_60m, o.mae_60m, o.is_final,
        o.reached_minus_0_5, o.reached_minus_1_0,
        o.reached_minus_1_5, o.reached_minus_2_0,
        (o.mfe_60m >= 1.0 AND o.mae_60m <= 0.5) AS is_good,
        (o.mae_60m >= 1.0 AND o.mfe_60m < 1.0) AS is_bad
    FROM dds.shadow_signal s
    JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
    WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
      AND s.oos_filter_version = 'ATR_WICK_FILTER_OOS_V1'
      AND o.evaluated_60m_at IS NOT NULL
),
variants AS (
    SELECT *, 'A: stoch>=0.8' AS variant, oos_a_stoch_08 AS passes
    FROM oos_cohort WHERE oos_a_stoch_08 IS NOT NULL
    UNION ALL
    SELECT *, 'B: stoch>=0.8 & vol<=1.0', oos_b_stoch_08_vol_10
    FROM oos_cohort WHERE oos_b_stoch_08_vol_10 IS NOT NULL
    UNION ALL
    SELECT *, 'C: stoch>=0.6 & vol<=1.0', oos_c_stoch_06_vol_10
    FROM oos_cohort WHERE oos_c_stoch_06_vol_10 IS NOT NULL
)
SELECT variant,
    COUNT(*) AS n,
    COUNT(*) FILTER (WHERE passes) AS passing,
    ROUND(COUNT(*) FILTER (WHERE passes)::numeric / NULLIF(COUNT(*), 0) * 100, 1) AS coverage_pct,
    COUNT(*) FILTER (WHERE passes AND is_good) AS good_count,
    COUNT(*) FILTER (WHERE passes AND is_bad) AS bad_count,
    ROUND(COUNT(*) FILTER (WHERE passes AND is_good)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes AND is_bad), 0), 4) AS good_bad_ratio,
    ROUND(COUNT(*) FILTER (WHERE passes AND is_good)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes), 0) * 100, 1) AS good_pct,
    ROUND(COUNT(*) FILTER (WHERE passes AND is_bad)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes), 0) * 100, 1) AS bad_pct,
    ROUND(AVG(mfe_60m) FILTER (WHERE passes), 4) AS avg_mfe_60m,
    ROUND(AVG(mae_60m) FILTER (WHERE passes), 4) AS avg_mae_60m,
    -- Target hit rates: denominator is passes AND is_final
    ROUND(COUNT(*) FILTER (WHERE passes AND is_final AND reached_minus_0_5)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes AND is_final), 0) * 100, 1) AS hit_0_5_pct,
    ROUND(COUNT(*) FILTER (WHERE passes AND is_final AND reached_minus_1_0)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes AND is_final), 0) * 100, 1) AS hit_1_0_pct,
    ROUND(COUNT(*) FILTER (WHERE passes AND is_final AND reached_minus_1_5)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes AND is_final), 0) * 100, 1) AS hit_1_5_pct,
    ROUND(COUNT(*) FILTER (WHERE passes AND is_final AND reached_minus_2_0)::numeric /
          NULLIF(COUNT(*) FILTER (WHERE passes AND is_final), 0) * 100, 1) AS hit_2_0_pct
FROM variants GROUP BY variant ORDER BY variant;

-- By-symbol OOS breakdown: OOS cohort only
CREATE OR REPLACE VIEW dds.v_shadow_oos_by_symbol AS
SELECT s.symbol, COUNT(*) AS n,
    COUNT(*) FILTER (WHERE s.oos_a_stoch_08 IS NOT NULL) AS a_total,
    COUNT(*) FILTER (WHERE s.oos_a_stoch_08 = TRUE) AS a_pass,
    COUNT(*) FILTER (WHERE s.oos_b_stoch_08_vol_10 IS NOT NULL) AS b_total,
    COUNT(*) FILTER (WHERE s.oos_b_stoch_08_vol_10 = TRUE) AS b_pass,
    COUNT(*) FILTER (WHERE s.oos_c_stoch_06_vol_10 IS NOT NULL) AS c_total,
    COUNT(*) FILTER (WHERE s.oos_c_stoch_06_vol_10 = TRUE) AS c_pass,
    ROUND(AVG(o.mfe_60m) FILTER (WHERE s.oos_a_stoch_08 = TRUE), 4) AS avg_mfe_a,
    ROUND(AVG(o.mfe_60m) FILTER (WHERE s.oos_b_stoch_08_vol_10 = TRUE), 4) AS avg_mfe_b,
    ROUND(AVG(o.mfe_60m) FILTER (WHERE s.oos_c_stoch_06_vol_10 = TRUE), 4) AS avg_mfe_c,
    ROUND(AVG(o.mae_60m) FILTER (WHERE s.oos_a_stoch_08 = TRUE), 4) AS avg_mae_a,
    ROUND(AVG(o.mae_60m) FILTER (WHERE s.oos_b_stoch_08_vol_10 = TRUE), 4) AS avg_mae_b,
    ROUND(AVG(o.mae_60m) FILTER (WHERE s.oos_c_stoch_06_vol_10 = TRUE), 4) AS avg_mae_c
FROM dds.shadow_signal s
JOIN dds.shadow_signal_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'ATR_WICK_REJECTION_SHORT_V1'
  AND s.oos_filter_version = 'ATR_WICK_FILTER_OOS_V1'
  AND o.evaluated_60m_at IS NOT NULL
GROUP BY s.symbol HAVING COUNT(*) >= 2 ORDER BY COUNT(*) DESC;
