-- ============================================================
-- ME_R_LONG_CLOSE_LOCATION_OOS — Prospective Experiment
-- ============================================================
-- Prospective OOS experiment for close_location >= 0.70 adverse
-- filter on MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG signals.
--
-- Hypothesis:
--   For MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG signals,
--   close_location >= 0.70 indicates buyers defending the upper
--   part of the candle range, producing higher quality signals.
--
-- Architecture:
--   - Separate tables: dds.me_r_long_close_location_oos_signal,
--     dds.me_r_long_close_location_oos_outcome
--   - Prospective only: signals recorded from T0 (deploy time) forward
--   - No backfill of historical observations
--   - Independent outcome evaluation pipeline
--
-- Filter: close_location >= 0.70 (frozen threshold)
-- Direction: LONG only
-- Base scanner: MOMENTUM_EXHAUSTION_REVERSE_LONG_V1
--
-- Registry dependency:
--   dds.shadow_oos_experiment_registry is a shared canonical table
--   created by earlier migrations.
--
-- Idempotent migration — safe to re-run.
-- ============================================================

-- ── signal table ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dds.me_r_long_close_location_oos_signal (
    signal_id           BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL DEFAULT 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1',
    symbol              TEXT NOT NULL,
    timeframe           TEXT NOT NULL DEFAULT '5m',
    direction           TEXT NOT NULL DEFAULT 'LONG',

    -- price data
    signal_time         TIMESTAMPTZ NOT NULL,
    signal_price        NUMERIC NOT NULL,
    open                NUMERIC NOT NULL,
    high                NUMERIC NOT NULL,
    low                 NUMERIC NOT NULL,
    close               NUMERIC NOT NULL,
    volume              NUMERIC NOT NULL,

    -- close_location filter
    close_location      NUMERIC,
    close_location_threshold NUMERIC NOT NULL DEFAULT 0.70,
    filter_passed       BOOLEAN NOT NULL,

    -- RSI / indicators (from base scanner)
    rsi                 NUMERIC,
    atr                 NUMERIC,

    -- metadata
    signal_version      TEXT NOT NULL DEFAULT '1.0.0',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- constraints
    CONSTRAINT me_r_long_cl_oos_signal_experiment_chk
        CHECK (experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'),
    CONSTRAINT me_r_long_cl_oos_signal_direction_chk
        CHECK (direction = 'LONG')
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_me_r_long_cl_oos_signal_experiment
    ON dds.me_r_long_close_location_oos_signal (experiment_id);
CREATE INDEX IF NOT EXISTS idx_me_r_long_cl_oos_signal_symbol
    ON dds.me_r_long_close_location_oos_signal (symbol, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_me_r_long_cl_oos_signal_time
    ON dds.me_r_long_close_location_oos_signal (signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_me_r_long_cl_oos_signal_filter
    ON dds.me_r_long_close_location_oos_signal (filter_passed);
CREATE UNIQUE INDEX IF NOT EXISTS uq_me_r_long_cl_oos_signal_experiment_symbol_time
    ON dds.me_r_long_close_location_oos_signal (experiment_id, symbol, signal_time);

-- ── outcome table ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dds.me_r_long_close_location_oos_outcome (
    signal_id           BIGINT PRIMARY KEY REFERENCES dds.me_r_long_close_location_oos_signal(signal_id) ON DELETE CASCADE,
    experiment_id       TEXT NOT NULL DEFAULT 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1',
    symbol              TEXT NOT NULL,

    -- MFE/MAE by horizon (in R from entry, LONG direction)
    mfe_15m   NUMERIC, mae_15m   NUMERIC,
    mfe_30m   NUMERIC, mae_30m   NUMERIC,
    mfe_60m   NUMERIC, mae_60m   NUMERIC,
    mfe_120m  NUMERIC, mae_120m  NUMERIC,
    mfe_240m  NUMERIC, mae_240m  NUMERIC,

    -- MFE/MAE in R units
    mfe_15m_r   NUMERIC, mae_15m_r   NUMERIC,
    mfe_30m_r   NUMERIC, mae_30m_r   NUMERIC,
    mfe_60m_r   NUMERIC, mae_60m_r   NUMERIC,
    mfe_120m_r  NUMERIC, mae_120m_r  NUMERIC,
    mfe_240m_r  NUMERIC, mae_240m_r  NUMERIC,

    -- Target hit flags
    hit_0_5r    BOOLEAN NOT NULL DEFAULT FALSE,
    hit_1r      BOOLEAN NOT NULL DEFAULT FALSE,
    hit_1_5r    BOOLEAN NOT NULL DEFAULT FALSE,
    hit_2r      BOOLEAN NOT NULL DEFAULT FALSE,

    -- Per-horizon evaluation timestamps
    evaluated_15m_at  TIMESTAMPTZ,
    evaluated_30m_at  TIMESTAMPTZ,
    evaluated_60m_at  TIMESTAMPTZ,
    evaluated_120m_at TIMESTAMPTZ,
    evaluated_240m_at TIMESTAMPTZ,

    is_final        BOOLEAN NOT NULL DEFAULT FALSE,

    -- metadata
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT me_r_long_cl_oos_outcome_experiment_chk
        CHECK (experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1')
);

CREATE INDEX IF NOT EXISTS idx_me_r_long_cl_oos_outcome_experiment
    ON dds.me_r_long_close_location_oos_outcome (experiment_id);
CREATE INDEX IF NOT EXISTS idx_me_r_long_cl_oos_outcome_symbol
    ON dds.me_r_long_close_location_oos_outcome (symbol);
CREATE INDEX IF NOT EXISTS idx_me_r_long_cl_oos_outcome_mfe
    ON dds.me_r_long_close_location_oos_outcome (mfe_60m DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_me_r_long_cl_oos_outcome_is_final
    ON dds.me_r_long_close_location_oos_outcome (is_final) WHERE is_final = FALSE;
CREATE INDEX IF NOT EXISTS idx_me_r_long_cl_oos_outcome_filter
    ON dds.me_r_long_close_location_oos_outcome (filter_passed);

-- ── Experiment registry ─────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM dds.shadow_oos_experiment_registry
        WHERE experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
    ) THEN
        INSERT INTO dds.shadow_oos_experiment_registry (
            experiment_id,
            status,
            hypothesis,
            filter_definition,
            created_at
        ) VALUES (
            'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1',
            'PROSPECTIVE_OOS',
            'For MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG signals, close_location >= 0.70 indicates buyers defending the upper part of the candle range, producing higher quality signals.',
            'close_location >= 0.70 (frozen threshold)',
            now()
        );
    END IF;
END $$;

UPDATE dds.shadow_oos_experiment_registry
SET
    status = 'PROSPECTIVE_OOS',
    hypothesis = 'For MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG signals, close_location >= 0.70 indicates buyers defending the upper part of the candle range, producing higher quality signals.',
    filter_definition = 'close_location >= 0.70 (frozen threshold)',
    prospective_started_at = COALESCE(prospective_started_at, now()),
    updated_at = now()
WHERE experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1';

-- ── Observability view ──────────────────────────────────────

CREATE OR REPLACE VIEW dds.v_me_r_long_cl_oos_accumulation AS
SELECT
    s.experiment_id,
    COUNT(*)                                                        AS signals_collected,
    COUNT(*) FILTER (WHERE s.filter_passed = TRUE)                  AS pass_count,
    COUNT(*) FILTER (WHERE s.filter_passed = FALSE)                 AS reject_count,
    COUNT(o.signal_id)                                              AS outcomes_created,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_15m_at IS NOT NULL)  AS outcomes_15m,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_30m_at IS NOT NULL)  AS outcomes_30m,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_60m_at IS NOT NULL)  AS outcomes_60m,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_120m_at IS NOT NULL) AS outcomes_120m,
    COUNT(o.signal_id) FILTER (WHERE o.evaluated_240m_at IS NOT NULL) AS outcomes_240m,
    COUNT(o.signal_id) FILTER (WHERE o.is_final = TRUE)               AS outcomes_final,
    MIN(s.signal_time)                                            AS oldest_signal,
    MAX(s.signal_time)                                            AS newest_signal,
    ROUND(AVG(s.close_location)::numeric, 4)                      AS avg_close_location,
    COUNT(o.signal_id) FILTER (WHERE o.is_final = FALSE AND o.signal_id IS NOT NULL) AS pending_evaluation
FROM dds.me_r_long_close_location_oos_signal s
LEFT JOIN dds.me_r_long_close_location_oos_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
GROUP BY s.experiment_id;

-- ── PASS vs REJECT comparison view ──────────────────────────

CREATE OR REPLACE VIEW dds.v_me_r_long_cl_oos_pass_reject AS
SELECT
    CASE WHEN s.filter_passed THEN 'PASS' ELSE 'REJECT' END AS group_name,
    COUNT(*) AS n,
    ROUND(AVG(s.close_location)::numeric, 4) AS avg_close_location,
    ROUND(AVG(o.mfe_15m)::numeric, 4) AS avg_mfe_15m,
    ROUND(AVG(o.mae_15m)::numeric, 4) AS avg_mae_15m,
    ROUND(AVG(o.mfe_30m)::numeric, 4) AS avg_mfe_30m,
    ROUND(AVG(o.mae_30m)::numeric, 4) AS avg_mae_30m,
    ROUND(AVG(o.mfe_60m)::numeric, 4) AS avg_mfe_60m,
    ROUND(AVG(o.mae_60m)::numeric, 4) AS avg_mae_60m,
    ROUND(AVG(o.mfe_120m)::numeric, 4) AS avg_mfe_120m,
    ROUND(AVG(o.mae_120m)::numeric, 4) AS avg_mae_120m,
    ROUND(AVG(o.mfe_240m)::numeric, 4) AS avg_mfe_240m,
    ROUND(AVG(o.mae_240m)::numeric, 4) AS avg_mae_240m,
    ROUND(AVG(o.mfe_60m_r)::numeric, 4) AS avg_mfe_60m_r,
    ROUND(AVG(o.mae_60m_r)::numeric, 4) AS avg_mae_60m_r,
    ROUND(AVG(o.mfe_240m_r)::numeric, 4) AS avg_mfe_240m_r,
    ROUND(AVG(o.mae_240m_r)::numeric, 4) AS avg_mae_240m_r,
    ROUND((COUNT(o.signal_id) FILTER (WHERE o.hit_1r)::numeric / NULLIF(COUNT(o.signal_id), 0) * 100)::numeric, 1) AS p_hit_1r
FROM dds.me_r_long_close_location_oos_signal s
LEFT JOIN dds.me_r_long_close_location_oos_outcome o ON o.signal_id = s.signal_id
WHERE s.experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
GROUP BY CASE WHEN s.filter_passed THEN 'PASS' ELSE 'REJECT' END;

-- ── Bucket analysis view ────────────────────────────────────

CREATE OR REPLACE VIEW dds.v_me_r_long_cl_oos_bucket_analysis AS
WITH buckets AS (
    SELECT
        s.close_location,
        s.filter_passed,
        o.mfe_60m,
        o.mae_60m,
        o.mfe_60m_r,
        o.mae_60m_r,
        o.hit_1r,
        CASE
            WHEN s.close_location >= 0.0 AND s.close_location < 0.1 THEN '0.0-0.1'
            WHEN s.close_location >= 0.1 AND s.close_location < 0.2 THEN '0.1-0.2'
            WHEN s.close_location >= 0.2 AND s.close_location < 0.3 THEN '0.2-0.3'
            WHEN s.close_location >= 0.3 AND s.close_location < 0.4 THEN '0.3-0.4'
            WHEN s.close_location >= 0.4 AND s.close_location < 0.5 THEN '0.4-0.5'
            WHEN s.close_location >= 0.5 AND s.close_location < 0.6 THEN '0.5-0.6'
            WHEN s.close_location >= 0.6 AND s.close_location < 0.7 THEN '0.6-0.7'
            WHEN s.close_location >= 0.7 AND s.close_location < 0.8 THEN '0.7-0.8'
            WHEN s.close_location >= 0.8 AND s.close_location < 0.9 THEN '0.8-0.9'
            WHEN s.close_location >= 0.9 AND s.close_location <= 1.0 THEN '0.9-1.0'
            ELSE 'unknown'
        END AS bucket
    FROM dds.me_r_long_close_location_oos_signal s
    LEFT JOIN dds.me_r_long_close_location_oos_outcome o ON o.signal_id = s.signal_id
    WHERE s.experiment_id = 'ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1'
)
SELECT
    bucket,
    COUNT(*) AS n,
    ROUND(AVG(mfe_60m)::numeric, 4) AS avg_mfe_60m,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mfe_60m) AS median_mfe_60m,
    ROUND(AVG(mae_60m)::numeric, 4) AS avg_mae_60m,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mae_60m) AS median_mae_60m,
    ROUND(AVG(mfe_60m_r)::numeric, 4) AS avg_mfe_60m_r,
    ROUND(AVG(mae_60m_r)::numeric, 4) AS avg_mae_60m_r,
    ROUND((COUNT(*) FILTER (WHERE hit_1r)::numeric / NULLIF(COUNT(*), 0) * 100)::numeric, 1) AS p_hit_1r
FROM buckets
WHERE bucket != 'unknown'
GROUP BY bucket
ORDER BY bucket;

-- ============================================================
-- ME_R_LONG_CLOSE_LOCATION_OOS is a RESEARCH experiment only.
-- It does NOT create paper trades or live positions.
-- MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 is BLOCKED for new entries.
-- ============================================================
