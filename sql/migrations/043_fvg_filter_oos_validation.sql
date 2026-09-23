-- ============================================================
-- Shadow/OOS Experiment: FVG Reaction Long Local Struct Filter
-- FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1
-- ============================================================
-- Hypothesis: the combination
--   bars_to_touch <= 2
--   AND fvg_atr >= 1.10
--   AND c2_body_ratio >= 0.85
-- filters for positive-edge FVG LONG signals without changing
-- production FVG_REACTION_LONG_LOCAL_STRUCT_V1 logic.
--
-- Three variants are tracked simultaneously to check threshold
-- robustness (frozen, no post-launch tuning):
--   Variant A (production candidate):
--     bars_to_touch <= 2 AND fvg_atr >= 1.10 AND c2_body_ratio >= 0.85
--   Variant B:
--     bars_to_touch <= 2 AND fvg_atr >= 1.10 AND c2_body_ratio >= 0.80
--   Variant C:
--     bars_to_touch <= 2 AND fvg_atr >= 1.10 AND c2_body_ratio >= 0.90
--
-- This table records observations for every FVG LONG signal,
-- classified by all three variant thresholds. NO production
-- behavior is changed.
-- ============================================================

CREATE TABLE IF NOT EXISTS dds.shadow_fvg_filter_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL DEFAULT 'FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1',
    setup_id            TEXT NOT NULL REFERENCES dds.scanner_setup(setup_id),

    -- Source signal metadata
    scanner_name        TEXT NOT NULL,
    direction           TEXT NOT NULL DEFAULT 'LONG',
    symbol              TEXT NOT NULL,
    instrument_id       BIGINT,

    -- Timestamps
    detected_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Core features under test (snapshot at observation time)
    bars_to_touch       INTEGER,
    fvg_atr             NUMERIC,
    c2_body_ratio       NUMERIC,

    -- Additional feature snapshot for analysis
    c2_body_atr         NUMERIC,
    risk_pct            NUMERIC,
    score               NUMERIC,
    market_regime       TEXT,
    fvg_size            NUMERIC,
    rr                  NUMERIC,
    entry_price         NUMERIC,
    sl_price            NUMERIC,
    tp_price            NUMERIC,

    -- Classification results for each variant
    variant_a_result    TEXT NOT NULL CHECK (variant_a_result IN ('PASS', 'REJECT', 'MISSING_FEATURE')),
    variant_a_reason    TEXT,
    variant_b_result    TEXT NOT NULL CHECK (variant_b_result IN ('PASS', 'REJECT', 'MISSING_FEATURE')),
    variant_b_reason    TEXT,
    variant_c_result    TEXT NOT NULL CHECK (variant_c_result IN ('PASS', 'REJECT', 'MISSING_FEATURE')),
    variant_c_reason    TEXT,

    -- Primary classification (Variant A = production candidate)
    filter_result       TEXT NOT NULL CHECK (filter_result IN ('PASS', 'REJECT', 'MISSING_FEATURE')),
    filter_reason       TEXT,

    -- Outcome (filled by outcome evaluator)
    status              TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'EVALUATED')),
    first_event         TEXT,
    result_r            NUMERIC,
    fee_slippage_adjusted_result_r NUMERIC,
    mfe_r               NUMERIC,
    mae_r               NUMERIC,
    evaluated_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_shadow_fvg_filter_experiment
    ON dds.shadow_fvg_filter_observation (experiment_id);
CREATE INDEX IF NOT EXISTS idx_shadow_fvg_filter_setup
    ON dds.shadow_fvg_filter_observation (setup_id);
CREATE INDEX IF NOT EXISTS idx_shadow_fvg_filter_result
    ON dds.shadow_fvg_filter_observation (filter_result);
CREATE INDEX IF NOT EXISTS idx_shadow_fvg_filter_status
    ON dds.shadow_fvg_filter_observation (status);
CREATE INDEX IF NOT EXISTS idx_shadow_fvg_filter_symbol
    ON dds.shadow_fvg_filter_observation (symbol, detected_at);
CREATE INDEX IF NOT EXISTS idx_shadow_fvg_filter_variant_a
    ON dds.shadow_fvg_filter_observation (variant_a_result);

-- Unique: one observation per experiment per setup
CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_fvg_filter_experiment_setup
    ON dds.shadow_fvg_filter_observation (experiment_id, setup_id);

-- ============================================================
-- Analytics view: PASS vs REJECT comparison (Variant A)
-- ============================================================
CREATE OR REPLACE VIEW dds.v_fvg_filter_shadow_analytics AS
SELECT
    filter_result,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'EVALUATED') AS evaluated,
    COUNT(*) FILTER (WHERE first_event = 'TP1') AS tp1,
    COUNT(*) FILTER (WHERE first_event = 'TP2') AS tp2,
    COUNT(*) FILTER (WHERE first_event = 'SL') AS sl,
    COUNT(*) FILTER (WHERE first_event = 'EXPIRED') AS expired,
    COUNT(*) FILTER (WHERE first_event = 'EXPIRED_BE') AS expired_be,
    COUNT(*) FILTER (WHERE first_event = 'OPEN') AS open_trades,
    ROUND(
        (COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE status = 'EVALUATED'), 0),
        4
    ) AS win_rate,
    ROUND(
        (COUNT(*) FILTER (WHERE first_event = 'SL'))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE status = 'EVALUATED'), 0),
        4
    ) AS sl_rate,
    ROUND(AVG(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS avg_r,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY result_r) FILTER (WHERE status = 'EVALUATED'))::numeric, 4) AS median_r,
    ROUND(SUM(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS total_r,
    ROUND(AVG(fee_slippage_adjusted_result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS avg_r_after_costs,
    ROUND(SUM(fee_slippage_adjusted_result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS total_r_after_costs,
    CASE
        WHEN SUM(ABS(result_r)) FILTER (WHERE status = 'EVALUATED' AND result_r < 0) > 0
        THEN ROUND(
            SUM(result_r) FILTER (WHERE status = 'EVALUATED' AND result_r > 0)
            / SUM(ABS(result_r)) FILTER (WHERE status = 'EVALUATED' AND result_r < 0),
            4)
        ELSE NULL
    END AS profit_factor,
    ROUND(AVG(mfe_r) FILTER (WHERE status = 'EVALUATED'), 4) AS avg_mfe_r,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mfe_r) FILTER (WHERE status = 'EVALUATED'))::numeric, 4) AS median_mfe_r,
    ROUND(AVG(mae_r) FILTER (WHERE status = 'EVALUATED'), 4) AS avg_mae_r,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mae_r) FILTER (WHERE status = 'EVALUATED'))::numeric, 4) AS median_mae_r
FROM dds.shadow_fvg_filter_observation
WHERE experiment_id = 'FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1'
GROUP BY filter_result
ORDER BY filter_result;

-- ============================================================
-- Analytics view: Variant comparison (A vs B vs C)
-- ============================================================
CREATE OR REPLACE VIEW dds.v_fvg_filter_variant_comparison AS
SELECT
    'Variant A (c2>=0.85)' AS variant,
    variant_a_result AS result,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'EVALUATED') AS evaluated,
    COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')) AS wins,
    COUNT(*) FILTER (WHERE first_event = 'SL') AS losses,
    ROUND(AVG(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS avg_r,
    ROUND(SUM(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS total_r,
    CASE
        WHEN SUM(ABS(result_r)) FILTER (WHERE status = 'EVALUATED' AND result_r < 0) > 0
        THEN ROUND(
            SUM(result_r) FILTER (WHERE status = 'EVALUATED' AND result_r > 0)
            / SUM(ABS(result_r)) FILTER (WHERE status = 'EVALUATED' AND result_r < 0), 4)
        ELSE NULL
    END AS profit_factor
FROM dds.shadow_fvg_filter_observation
WHERE experiment_id = 'FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1'
GROUP BY variant_a_result

UNION ALL

SELECT
    'Variant B (c2>=0.80)' AS variant,
    variant_b_result AS result,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'EVALUATED') AS evaluated,
    COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')) AS wins,
    COUNT(*) FILTER (WHERE first_event = 'SL') AS losses,
    ROUND(AVG(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS avg_r,
    ROUND(SUM(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS total_r,
    CASE
        WHEN SUM(ABS(result_r)) FILTER (WHERE status = 'EVALUATED' AND result_r < 0) > 0
        THEN ROUND(
            SUM(result_r) FILTER (WHERE status = 'EVALUATED' AND result_r > 0)
            / SUM(ABS(result_r)) FILTER (WHERE status = 'EVALUATED' AND result_r < 0), 4)
        ELSE NULL
    END AS profit_factor
FROM dds.shadow_fvg_filter_observation
WHERE experiment_id = 'FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1'
GROUP BY variant_b_result

UNION ALL

SELECT
    'Variant C (c2>=0.90)' AS variant,
    variant_c_result AS result,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'EVALUATED') AS evaluated,
    COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')) AS wins,
    COUNT(*) FILTER (WHERE first_event = 'SL') AS losses,
    ROUND(AVG(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS avg_r,
    ROUND(SUM(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS total_r,
    CASE
        WHEN SUM(ABS(result_r)) FILTER (WHERE status = 'EVALUATED' AND result_r < 0) > 0
        THEN ROUND(
            SUM(result_r) FILTER (WHERE status = 'EVALUATED' AND result_r > 0)
            / SUM(ABS(result_r)) FILTER (WHERE status = 'EVALUATED' AND result_r < 0), 4)
        ELSE NULL
    END AS profit_factor
FROM dds.shadow_fvg_filter_observation
WHERE experiment_id = 'FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1'
GROUP BY variant_c_result

ORDER BY variant, result;

-- ============================================================
-- Analytics view: distribution by symbol (Variant A)
-- ============================================================
CREATE OR REPLACE VIEW dds.v_fvg_filter_symbol_breakdown AS
SELECT
    symbol,
    filter_result,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'EVALUATED') AS evaluated,
    COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')) AS wins,
    COUNT(*) FILTER (WHERE first_event = 'SL') AS losses,
    ROUND(AVG(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS avg_r,
    ROUND(SUM(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS total_r
FROM dds.shadow_fvg_filter_observation
WHERE experiment_id = 'FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1'
GROUP BY symbol, filter_result
ORDER BY symbol, filter_result;

-- ============================================================
-- Analytics view: distribution by market regime (Variant A)
-- ============================================================
CREATE OR REPLACE VIEW dds.v_fvg_filter_regime_breakdown AS
SELECT
    market_regime,
    filter_result,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'EVALUATED') AS evaluated,
    COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')) AS wins,
    COUNT(*) FILTER (WHERE first_event = 'SL') AS losses,
    ROUND(AVG(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS avg_r,
    ROUND(SUM(result_r) FILTER (WHERE status = 'EVALUATED'), 4) AS total_r
FROM dds.shadow_fvg_filter_observation
WHERE experiment_id = 'FVG_REACTION_LONG_LOCAL_STRUCT_FILTER_OOS_V1'
GROUP BY market_regime, filter_result
ORDER BY market_regime, filter_result;
