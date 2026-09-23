-- ============================================================
-- Shadow/Control Experiment: V2 Exhaustion Magnitude Filter
-- ME_REVERSE_LONG_V2_EXHAUSTION_SHADOW_V1
-- ============================================================
-- Hypothesis: exhaustion_magnitude <= 0.4 filters out losing entries
-- from MOMENTUM_EXHAUSTION_REVERSE_LONG_V2 without removing too many winners.
--
-- This table records observations for every V2 signal, classified by
-- the fixed threshold. NO production behavior is changed.
-- ============================================================

CREATE TABLE IF NOT EXISTS dds.shadow_exhaustion_observation (
    observation_id      BIGSERIAL PRIMARY KEY,
    experiment_id       TEXT NOT NULL DEFAULT 'V2_EXHAUSTION_SHADOW_V1',
    setup_id            TEXT NOT NULL REFERENCES dds.scanner_setup(setup_id),
    source_trade_id     BIGINT REFERENCES dds.paper_trade(trade_id),
    scanner_name        TEXT NOT NULL,
    scanner_version     TEXT,
    symbol              TEXT NOT NULL,
    instrument_id       BIGINT,
    direction           TEXT NOT NULL DEFAULT 'LONG',
    detected_at         TIMESTAMPTZ,
    signal_candle_open_time BIGINT,

    -- Feature under test
    exhaustion_magnitude NUMERIC,
    threshold           NUMERIC NOT NULL DEFAULT 0.4,

    -- Classification
    filter_result       TEXT NOT NULL CHECK (filter_result IN ('PASS', 'REJECT', 'MISSING_FEATURE')),

    -- If V2 actually opened a paper trade for this signal
    has_trade           BOOLEAN NOT NULL DEFAULT FALSE,
    entry_price         NUMERIC,
    stop_price          NUMERIC,
    target_1            NUMERIC,
    entered_at          TIMESTAMPTZ,

    -- Outcome (filled when trade closes or shadow outcome computed)
    exit_price          NUMERIC,
    exit_reason         TEXT,
    closed_at           TIMESTAMPTZ,
    pnl_usdt            NUMERIC,
    pnl_r               NUMERIC,
    mfe_r               NUMERIC,
    mae_r               NUMERIC,
    holding_minutes     NUMERIC,

    -- Status
    status              TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'OPEN', 'CLOSED', 'NO_TRADE')),
    outcome_source      TEXT CHECK (outcome_source IN ('LIVE_TRADE', 'SHADOW_COUNTERFACTUAL', NULL)),

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_shadow_exhaust_experiment
    ON dds.shadow_exhaustion_observation (experiment_id);
CREATE INDEX IF NOT EXISTS idx_shadow_exhaust_setup
    ON dds.shadow_exhaustion_observation (setup_id);
CREATE INDEX IF NOT EXISTS idx_shadow_exhaust_filter
    ON dds.shadow_exhaustion_observation (filter_result);
CREATE INDEX IF NOT EXISTS idx_shadow_exhaust_status
    ON dds.shadow_exhaustion_observation (status);
CREATE INDEX IF NOT EXISTS idx_shadow_exhaust_symbol
    ON dds.shadow_exhaustion_observation (symbol, detected_at);

-- Unique: one observation per experiment per setup
CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_exhaust_experiment_setup
    ON dds.shadow_exhaustion_observation (experiment_id, setup_id);

-- ============================================================
-- Analytics view: V2_ALL vs PASS vs REJECT
-- ============================================================
CREATE OR REPLACE VIEW dds.v_v2_exhaustion_shadow_analytics AS
SELECT
    filter_result,
    COUNT(*) AS signals,
    COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed_outcomes,
    COUNT(*) FILTER (WHERE pnl_r > 0) AS wins,
    COUNT(*) FILTER (WHERE pnl_r <= 0) AS losses,
    COUNT(*) FILTER (WHERE pnl_r <= -0.9) AS hard_losses,
    ROUND(
        (COUNT(*) FILTER (WHERE pnl_r > 0))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE status = 'CLOSED'), 0),
        4
    ) AS win_rate,
    ROUND(SUM(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS total_r,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS expectancy_r,
    CASE
        WHEN SUM(ABS(pnl_r)) FILTER (WHERE status = 'CLOSED' AND pnl_r < 0) > 0
        THEN ROUND(
            SUM(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r > 0)
            / SUM(ABS(pnl_r)) FILTER (WHERE status = 'CLOSED' AND pnl_r < 0),
            4)
        ELSE NULL
    END AS profit_factor,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r > 0), 4) AS avg_win_r,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED' AND pnl_r <= 0), 4) AS avg_loss_r,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pnl_r) FILTER (WHERE status = 'CLOSED'))::numeric, 4) AS median_r,
    ROUND(AVG(mfe_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_mae_r
FROM dds.shadow_exhaustion_observation
WHERE experiment_id = 'V2_EXHAUSTION_SHADOW_V1'
GROUP BY filter_result
ORDER BY filter_result;
