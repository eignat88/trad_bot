-- Migration 014: Canonical Analytics Data Schema
-- Creates the full Stage 2 canonical analytics layer:
--   config_snapshot, strategy_snapshot,
--   setup_fact, entry_attempt_fact, trade_fact,
--   trade_event (append-only journal),
--   trade_horizon_metric, trade_replay_metric,
--   setup_counterfactual,
--   metric_snapshot, metric_registry
--
-- Idempotent: safe to run multiple times.

-- ============================================================
-- 1. CONFIG SNAPSHOT — effective configuration, versioned by hash
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.config_snapshot (
    config_hash          TEXT PRIMARY KEY,
    effective_config_json JSONB NOT NULL,
    source_map_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    captured_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_from           TIMESTAMPTZ NOT NULL,
    valid_to             TIMESTAMPTZ
);

COMMENT ON TABLE analytics.config_snapshot
    IS 'Canonical effective configuration snapshot, versioned by SHA-256 of canonical JSON';
COMMENT ON COLUMN analytics.config_snapshot.config_hash
    IS 'SHA-256 of sorted, secret-stripped canonical config JSON';
COMMENT ON COLUMN analytics.config_snapshot.source_map_json
    IS 'Per-key source: YAML / ENV / default';
COMMENT ON COLUMN analytics.config_snapshot.valid_from
    IS 'When this config became effective';
COMMENT ON COLUMN analytics.config_snapshot.valid_to
    IS 'When superseded (NULL = currently active)';

-- ============================================================
-- 2. STRATEGY SNAPSHOT — immutable scanner strategy version
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.strategy_snapshot (
    strategy_hash        TEXT PRIMARY KEY,
    git_commit_sha       TEXT,
    scanner_name         TEXT NOT NULL,
    scanner_version      TEXT NOT NULL,
    strategy_params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    captured_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE analytics.strategy_snapshot
    IS 'Immutable scanner strategy version snapshot';
COMMENT ON COLUMN analytics.strategy_snapshot.strategy_hash
    IS 'SHA-256 of (git_commit_sha, scanner_name, scanner_version, strategy_params)';

-- ============================================================
-- 3. SETUP FACT — canonical setup in context of an analytics run
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.setup_fact (
    run_id            UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    setup_id          TEXT NOT NULL,
    scanner_name      TEXT NOT NULL,
    scanner_version   TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    direction         TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    setup_at          TIMESTAMPTZ NOT NULL,
    signal_at         TIMESTAMPTZ,
    setup_status      TEXT NOT NULL,
    entry_zone_low    NUMERIC,
    entry_zone_high   NUMERIC,
    reference_price   NUMERIC NOT NULL,
    config_hash       TEXT,
    strategy_hash     TEXT,
    market_regime     TEXT,
    coverage_status   TEXT NOT NULL DEFAULT 'UNKNOWN',
    ambiguity_status  TEXT NOT NULL DEFAULT 'CLEAR',
    excluded_reasons  JSONB NOT NULL DEFAULT '[]',
    dataset_version   TEXT NOT NULL,
    published         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (run_id, setup_id)
);

COMMENT ON TABLE analytics.setup_fact
    IS 'Canonical setup snapshot within a specific analytics run';

CREATE INDEX IF NOT EXISTS idx_setup_fact_symbol
    ON analytics.setup_fact (symbol);
CREATE INDEX IF NOT EXISTS idx_setup_fact_scanner
    ON analytics.setup_fact (scanner_name);
CREATE INDEX IF NOT EXISTS idx_setup_fact_direction
    ON analytics.setup_fact (direction);
CREATE INDEX IF NOT EXISTS idx_setup_fact_published
    ON analytics.setup_fact (published, dataset_version);
CREATE INDEX IF NOT EXISTS idx_setup_fact_status
    ON analytics.setup_fact (setup_status);

-- ============================================================
-- 4. ENTRY ATTEMPT FACT — separates "setup existed" from "fill occurred"
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.entry_attempt_fact (
    attempt_id        TEXT PRIMARY KEY,
    run_id            UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    setup_id          TEXT NOT NULL,
    trade_id          BIGINT,   -- NULL when no fill occurred
    attempt_at        TIMESTAMPTZ NOT NULL,
    attempt_price     NUMERIC,
    entry_model       TEXT NOT NULL DEFAULT 'PAPER',
    result            TEXT NOT NULL
        CHECK (result IN ('FILLED', 'REJECTED', 'EXPIRED', 'CANCELLED', 'UNKNOWN')),
    reason_code       TEXT,
    rejection_reason  TEXT,
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_event_key  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (run_id, setup_id)
);

COMMENT ON TABLE analytics.entry_attempt_fact
    IS 'Canonical entry attempt record — separates setup signal from actual fill';

CREATE INDEX IF NOT EXISTS idx_entry_attempt_trade
    ON analytics.entry_attempt_fact (trade_id);
CREATE INDEX IF NOT EXISTS idx_entry_attempt_result
    ON analytics.entry_attempt_fact (result);
CREATE INDEX IF NOT EXISTS idx_entry_attempt_run
    ON analytics.entry_attempt_fact (run_id);

-- ============================================================
-- 5. TRADE FACT — canonical trade with full lifecycle
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.trade_fact (
    run_id                UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    trade_id              BIGINT NOT NULL,
    setup_id              TEXT NOT NULL,
    scanner_name          TEXT NOT NULL,
    scanner_version       TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    direction             TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),

    -- Timeline
    setup_at              TIMESTAMPTZ,
    signal_at             TIMESTAMPTZ,
    first_attempt_at      TIMESTAMPTZ,
    entered_at            TIMESTAMPTZ,
    dca_filled_at         TIMESTAMPTZ,
    closed_at             TIMESTAMPTZ,

    -- Prices
    reference_price       NUMERIC NOT NULL,
    entry_zone_low        NUMERIC,
    entry_zone_high       NUMERIC,
    initial_fill_price    NUMERIC,
    dca_fill_price        NUMERIC,
    avg_entry_price       NUMERIC,
    initial_stop          NUMERIC NOT NULL,
    final_stop            NUMERIC,
    target_1              NUMERIC,
    target_2              NUMERIC,
    exit_price            NUMERIC,

    -- Risk
    risk_usdt             NUMERIC NOT NULL,
    initial_risk_distance NUMERIC NOT NULL,
    initial_quantity      NUMERIC,
    dca_quantity          NUMERIC,
    final_quantity        NUMERIC,
    fees                  NUMERIC NOT NULL DEFAULT 0,
    slippage              NUMERIC NOT NULL DEFAULT 0,
    funding               NUMERIC NOT NULL DEFAULT 0,

    -- Outcome
    status                TEXT NOT NULL,
    exit_reason           TEXT,
    gross_pnl             NUMERIC,
    net_pnl               NUMERIC,
    pnl_r                 NUMERIC,
    mfe_r                 NUMERIC,
    mae_r                 NUMERIC,

    -- Context
    market_regime         TEXT,
    config_hash           TEXT,
    strategy_hash         TEXT,
    metric_version        TEXT NOT NULL DEFAULT '1.0.0',

    -- Quality
    coverage_status       TEXT NOT NULL DEFAULT 'UNKNOWN',
    ambiguity_status      TEXT NOT NULL DEFAULT 'CLEAR',
    excluded_reasons      JSONB NOT NULL DEFAULT '[]',

    -- Versioning
    dataset_version       TEXT NOT NULL,
    published             BOOLEAN NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (run_id, trade_id)
);

COMMENT ON TABLE analytics.trade_fact
    IS 'Canonical trade fact — single source of truth for analytics consumers';

CREATE INDEX IF NOT EXISTS idx_trade_fact_symbol
    ON analytics.trade_fact (symbol);
CREATE INDEX IF NOT EXISTS idx_trade_fact_scanner
    ON analytics.trade_fact (scanner_name);
CREATE INDEX IF NOT EXISTS idx_trade_fact_direction
    ON analytics.trade_fact (direction);
CREATE INDEX IF NOT EXISTS idx_trade_fact_status
    ON analytics.trade_fact (status);
CREATE INDEX IF NOT EXISTS idx_trade_fact_entered
    ON analytics.trade_fact (entered_at);
CREATE INDEX IF NOT EXISTS idx_trade_fact_closed
    ON analytics.trade_fact (closed_at);
CREATE INDEX IF NOT EXISTS idx_trade_fact_published
    ON analytics.trade_fact (published, dataset_version);
CREATE INDEX IF NOT EXISTS idx_trade_fact_setup
    ON analytics.trade_fact (setup_id);
CREATE INDEX IF NOT EXISTS idx_trade_fact_run
    ON analytics.trade_fact (run_id);

-- ============================================================
-- 6. TRADE EVENT JOURNAL — append-only execution event log
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.trade_event (
    event_id          BIGSERIAL PRIMARY KEY,
    trade_id          BIGINT NOT NULL,
    setup_id          TEXT NOT NULL,
    event_type        TEXT NOT NULL CHECK (event_type IN (
        'SETUP_READY', 'ENTRY_ATTEMPTED', 'ENTRY_FILLED',
        'DCA_PLACED', 'DCA_FILLED', 'STOP_MOVED',
        'PARTIAL_EXIT', 'TRADE_CLOSED'
    )),
    event_at          TIMESTAMPTZ NOT NULL,
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    price             NUMERIC,
    quantity          NUMERIC,
    reason_code       TEXT,
    payload_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_event_key  TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Idempotency: each source event can only be recorded once
    UNIQUE (source_event_key)
);

COMMENT ON TABLE analytics.trade_event
    IS 'Append-only trade event journal — never UPDATEd, corrections via compensating events';
COMMENT ON COLUMN analytics.trade_event.source_event_key
    IS 'Idempotency key — prevents duplicate event recording';
COMMENT ON COLUMN analytics.trade_event.payload_json
    IS 'Event-specific payload (prices, quantities, reason details)';

CREATE INDEX IF NOT EXISTS idx_trade_event_trade
    ON analytics.trade_event (trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_event_setup
    ON analytics.trade_event (setup_id);
CREATE INDEX IF NOT EXISTS idx_trade_event_type
    ON analytics.trade_event (event_type);
CREATE INDEX IF NOT EXISTS idx_trade_event_at
    ON analytics.trade_event (event_at);
CREATE INDEX IF NOT EXISTS idx_trade_event_source_key
    ON analytics.trade_event (source_event_key);

-- ============================================================
-- 7. TRADE HORIZON METRIC — forward-looking metrics at fixed horizons
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.trade_horizon_metric (
    trade_id          BIGINT NOT NULL,
    anchor            TEXT NOT NULL CHECK (anchor IN ('signal', 'entry', 'dca_fill', 'exit')),
    horizon           TEXT NOT NULL,
    metric_version    TEXT NOT NULL DEFAULT '1.0.0',

    favorable_move_r       NUMERIC,
    adverse_move_r         NUMERIC,
    late_entry_loss_r      NUMERIC,
    post_exit_opportunity_r NUMERIC,
    continuation_r         NUMERIC,
    recovery_r             NUMERIC,

    coverage_status    TEXT NOT NULL DEFAULT 'COMPLETE',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (trade_id, anchor, horizon, metric_version)
);

COMMENT ON TABLE analytics.trade_horizon_metric
    IS 'Horizon-based forward metrics anchored to signal/entry/dca/exit timestamps';

CREATE INDEX IF NOT EXISTS idx_horizon_metric_trade
    ON analytics.trade_horizon_metric (trade_id);
CREATE INDEX IF NOT EXISTS idx_horizon_metric_anchor
    ON analytics.trade_horizon_metric (anchor);

-- ============================================================
-- 8. TRADE REPLAY METRIC — scenario-based simulation results
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.trade_replay_metric (
    trade_id              BIGINT NOT NULL,
    scenario              TEXT NOT NULL,
    metric_version        TEXT NOT NULL DEFAULT '1.0.0',

    simulated_pnl_r       NUMERIC,
    simulated_exit_price  NUMERIC,
    simulated_exit_at     TIMESTAMPTZ,
    simulated_exit_reason TEXT,

    scenario_family       TEXT NOT NULL,  -- entry, dca, exit, stop
    scenario_params       JSONB NOT NULL DEFAULT '{}'::jsonb,

    coverage_status       TEXT NOT NULL DEFAULT 'COMPLETE',
    ambiguity_status      TEXT NOT NULL DEFAULT 'CLEAR',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (trade_id, scenario, metric_version)
);

COMMENT ON TABLE analytics.trade_replay_metric
    IS 'Scenario replay results — ACTUAL, WHAT-IF, DIAGNOSTIC scenarios';

CREATE INDEX IF NOT EXISTS idx_replay_metric_trade
    ON analytics.trade_replay_metric (trade_id);
CREATE INDEX IF NOT EXISTS idx_replay_metric_family
    ON analytics.trade_replay_metric (scenario_family);

-- ============================================================
-- 9. SETUP COUNTERFACTUAL — what-if analysis for rejected/expired setups
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.setup_counterfactual (
    setup_id          TEXT NOT NULL,
    scenario          TEXT NOT NULL,
    metric_version    TEXT NOT NULL DEFAULT '1.0.0',

    would_have_saved_loss BOOLEAN,
    would_have_blocked_win BOOLEAN,
    classification    TEXT NOT NULL
        CHECK (classification IN ('FILTER_SAVED_LOSS', 'FILTER_BLOCKED_WINNER', 'NEUTRAL')),

    simulated_pnl_r   NUMERIC,
    simulated_exit_price NUMERIC,

    coverage_status   TEXT NOT NULL DEFAULT 'COMPLETE',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (setup_id, scenario, metric_version)
);

COMMENT ON TABLE analytics.setup_counterfactual
    IS 'Counterfactual analysis for rejected/expired setups — evaluates filter quality';

CREATE INDEX IF NOT EXISTS idx_counterfactual_setup
    ON analytics.setup_counterfactual (setup_id);
CREATE INDEX IF NOT EXISTS idx_counterfactual_class
    ON analytics.setup_counterfactual (classification);

-- ============================================================
-- 10. METRIC SNAPSHOT — period aggregates (24h/7d/30d)
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.metric_snapshot (
    run_id            UUID NOT NULL REFERENCES analytics.analysis_run(run_id),
    period            TEXT NOT NULL CHECK (period IN ('24h', '7d', '30d')),
    segment           TEXT NOT NULL,  -- e.g. 'scanner:TREND_PULLBACK_V2:LONG'
    metric_name       TEXT NOT NULL,
    metric_version    TEXT NOT NULL DEFAULT '1.0.0',

    metric_value      NUMERIC,
    sample_count      BIGINT,

    window_from       TIMESTAMPTZ NOT NULL,
    window_to         TIMESTAMPTZ NOT NULL,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (run_id, period, segment, metric_name, metric_version)
);

COMMENT ON TABLE analytics.metric_snapshot
    IS 'Period aggregates — semi-open windows [from, to) per segment per metric';
COMMENT ON COLUMN analytics.metric_snapshot.window_from
    IS 'Window start (inclusive)';
COMMENT ON COLUMN analytics.metric_snapshot.window_to
    IS 'Window end (exclusive) — [from, to)';

CREATE INDEX IF NOT EXISTS idx_metric_snapshot_run
    ON analytics.metric_snapshot (run_id);
CREATE INDEX IF NOT EXISTS idx_metric_snapshot_period
    ON analytics.metric_snapshot (period);
CREATE INDEX IF NOT EXISTS idx_metric_snapshot_segment
    ON analytics.metric_snapshot (segment);
CREATE INDEX IF NOT EXISTS idx_metric_snapshot_metric
    ON analytics.metric_snapshot (metric_name);

-- ============================================================
-- 11. METRIC REGISTRY — versioned metric definitions
-- ============================================================
CREATE TABLE IF NOT EXISTS analytics.metric_registry (
    metric_name       TEXT NOT NULL,
    metric_version    TEXT NOT NULL,
    formula_sql_or_code_ref TEXT NOT NULL,
    units             TEXT NOT NULL,
    grain             TEXT NOT NULL,
    direction_convention TEXT NOT NULL DEFAULT 'LONG=+1 SHORT=-1',
    fee_model_version TEXT NOT NULL DEFAULT '1.0.0',
    valid_from        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deprecated_at     TIMESTAMPTZ,

    PRIMARY KEY (metric_name, metric_version)
);

COMMENT ON TABLE analytics.metric_registry
    IS 'Versioned registry of all canonical metrics with formulas and conventions';

-- ============================================================
-- 12. HELPER: append-only enforcement for trade_event
-- ============================================================
CREATE OR REPLACE FUNCTION analytics.block_trade_event_update()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'trade_event is append-only — UPDATE is not permitted. '
                     'Use compensating events or new dataset version.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION analytics.block_trade_event_update()
    IS 'Blocks UPDATE on trade_event to enforce append-only invariant';

DROP TRIGGER IF EXISTS trg_trade_event_no_update ON analytics.trade_event;

CREATE TRIGGER trg_trade_event_no_update
    BEFORE UPDATE ON analytics.trade_event
    FOR EACH ROW
    EXECUTE FUNCTION analytics.block_trade_event_update();

-- ============================================================
-- Done
-- ============================================================
