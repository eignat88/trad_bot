-- Multi-Setup Market Scanner schema for PostgreSQL
-- Version 2.0: scanner_run, lifecycle, market_signal, scanner_error

CREATE SCHEMA IF NOT EXISTS dds;

-- ============================================================
-- INSTRUMENT: справочник + ликвидность/universe
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.instrument (
    instrument_id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL DEFAULT 'USDT',
    category TEXT NOT NULL DEFAULT 'linear',
    status TEXT NOT NULL DEFAULT 'Trading',
    turnover_24h NUMERIC,
    volume_24h NUMERIC,
    last_price NUMERIC,
    universe_rank INTEGER,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- SCANNER_RUN: один полный проход по universe
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.scanner_run (
    run_id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    universe_mode TEXT NOT NULL DEFAULT 'exchange',
    symbols_total INTEGER NOT NULL DEFAULT 0,
    symbols_scanned INTEGER NOT NULL DEFAULT 0,
    symbols_failed INTEGER NOT NULL DEFAULT 0,
    setups_found INTEGER NOT NULL DEFAULT 0,
    duration_sec NUMERIC,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    error_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scanner_run_status_chk CHECK (
        status IN ('RUNNING', 'COMPLETED', 'FAILED', 'PARTIAL', 'ABORTED')
    )
);

CREATE INDEX IF NOT EXISTS idx_scanner_run_started ON dds.scanner_run (started_at DESC);

ALTER TABLE dds.scanner_run DROP CONSTRAINT IF EXISTS scanner_run_status_chk;
ALTER TABLE dds.scanner_run ADD CONSTRAINT scanner_run_status_chk CHECK (
    status IN ('RUNNING', 'COMPLETED', 'FAILED', 'PARTIAL', 'ABORTED')
);

CREATE TABLE IF NOT EXISTS dds.scanner_run_stat (
    run_id BIGINT NOT NULL REFERENCES dds.scanner_run(run_id) ON DELETE CASCADE,
    scanner_name TEXT NOT NULL,
    symbols_scanned INTEGER NOT NULL DEFAULT 0,
    candidates_found INTEGER NOT NULL DEFAULT 0,
    setups_saved INTEGER NOT NULL DEFAULT 0,
    errors_count INTEGER NOT NULL DEFAULT 0,
    duration_ms NUMERIC(12,3) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, scanner_name)
);

ALTER TABLE dds.scanner_run_stat
    ALTER COLUMN duration_ms TYPE NUMERIC(12,3) USING duration_ms::NUMERIC(12,3);

-- Immutable membership snapshot for each dynamic or static run universe.
CREATE TABLE IF NOT EXISTS dds.scanner_run_instrument (
    run_id BIGINT NOT NULL REFERENCES dds.scanner_run(run_id) ON DELETE CASCADE,
    instrument_id BIGINT NOT NULL REFERENCES dds.instrument(instrument_id),
    universe_rank INTEGER NOT NULL,
    turnover_24h NUMERIC,
    volume_24h NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, instrument_id),
    CONSTRAINT scanner_run_instrument_rank_chk CHECK (universe_rank > 0)
);

CREATE INDEX IF NOT EXISTS idx_scanner_run_instrument_instrument
ON dds.scanner_run_instrument (instrument_id, run_id DESC);

-- ============================================================
-- SCANNER_SETUP: сырые результаты каждого из 7 сканеров
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.scanner_setup (
    setup_id TEXT PRIMARY KEY,
    scanner_name TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    instrument_id BIGINT NOT NULL REFERENCES dds.instrument(instrument_id),
    run_id BIGINT REFERENCES dds.scanner_run(run_id),
    direction TEXT NOT NULL,
    htf_timeframe TEXT NOT NULL,
    setup_timeframe TEXT NOT NULL,
    entry_timeframe TEXT NOT NULL,
    setup_started_at TIMESTAMPTZ NOT NULL,
    signal_candle_open_time BIGINT NOT NULL DEFAULT 0,
    detected_at TIMESTAMPTZ NOT NULL,
    reference_price NUMERIC NOT NULL,
    entry_zone_low NUMERIC,
    entry_zone_high NUMERIC,
    invalidation_price NUMERIC,
    target_1 NUMERIC,
    target_2 NUMERIC,
    score NUMERIC NOT NULL,
    market_regime TEXT,
    status TEXT NOT NULL DEFAULT 'DETECTED',
    status_reason TEXT,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    features JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    invalidated_at TIMESTAMPTZ,
    expired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scanner_setup_direction_chk CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT scanner_setup_status_chk CHECK (
        status IN (
            'DETECTED', 'CONFIRMED', 'READY_TO_TRADE', 'CONFLICT',
            'EXECUTED', 'INVALIDATED', 'EXPIRED'
        )
    )
);

-- Keep upgrades idempotent
ALTER TABLE dds.scanner_setup
    ADD COLUMN IF NOT EXISTS signal_candle_open_time BIGINT NOT NULL DEFAULT 0;
ALTER TABLE dds.scanner_setup
    ADD COLUMN IF NOT EXISTS run_id BIGINT REFERENCES dds.scanner_run(run_id);
ALTER TABLE dds.scanner_setup
    ADD COLUMN IF NOT EXISTS status_reason TEXT;
ALTER TABLE dds.scanner_setup
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE dds.scanner_setup
    ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;
ALTER TABLE dds.scanner_setup
    ADD COLUMN IF NOT EXISTS executed_at TIMESTAMPTZ;
ALTER TABLE dds.scanner_setup
    ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE dds.scanner_setup
    ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;

-- Migrate old statuses
ALTER TABLE dds.scanner_setup DROP CONSTRAINT IF EXISTS scanner_setup_status_chk;
UPDATE dds.scanner_setup SET status = 'DETECTED' WHERE status = 'CANDIDATE';
UPDATE dds.scanner_setup SET status = 'READY_TO_TRADE' WHERE status = 'READY';
UPDATE dds.scanner_setup SET status = 'EXECUTED' WHERE status = 'CONSUMED';
UPDATE dds.scanner_setup SET status = 'READY_TO_TRADE' WHERE status = 'SETUP_READY';
ALTER TABLE dds.scanner_setup ADD CONSTRAINT scanner_setup_status_chk CHECK (
    status IN (
        'DETECTED', 'CONFIRMED', 'READY_TO_TRADE', 'CONFLICT',
        'EXECUTED', 'INVALIDATED', 'EXPIRED'
    )
);

-- Legacy rows without a candle identity cannot be deduplicated reliably.
DELETE FROM dds.scanner_setup WHERE signal_candle_open_time = 0;

-- Unique index: один сигнал на одну свечу
CREATE UNIQUE INDEX IF NOT EXISTS uq_scanner_setup_signal_candle
ON dds.scanner_setup (
    instrument_id, scanner_name, direction, entry_timeframe,
    signal_candle_open_time
) WHERE signal_candle_open_time > 0;

CREATE INDEX IF NOT EXISTS idx_scanner_setup_symbol ON dds.scanner_setup (instrument_id);
CREATE INDEX IF NOT EXISTS idx_scanner_setup_status ON dds.scanner_setup (status);
CREATE INDEX IF NOT EXISTS idx_scanner_setup_detected ON dds.scanner_setup (detected_at);
CREATE INDEX IF NOT EXISTS idx_scanner_setup_scanner ON dds.scanner_setup (scanner_name);
CREATE INDEX IF NOT EXISTS idx_scanner_setup_score ON dds.scanner_setup (score DESC);
CREATE INDEX IF NOT EXISTS idx_scanner_setup_run ON dds.scanner_setup (run_id);

-- Partial index: активные сигналы
CREATE INDEX IF NOT EXISTS idx_scanner_setup_active
ON dds.scanner_setup (instrument_id, detected_at DESC)
WHERE status IN ('DETECTED', 'CONFIRMED', 'READY_TO_TRADE');

CREATE INDEX IF NOT EXISTS idx_scanner_setup_symbol_direction
ON dds.scanner_setup (instrument_id, direction, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_scanner_setup_aggregation
ON dds.scanner_setup (
    instrument_id, direction, setup_timeframe, scanner_name, detected_at DESC
) WHERE status IN ('DETECTED', 'CONFIRMED', 'READY_TO_TRADE');

-- ============================================================
-- SCANNER_EVENT: история событий
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.scanner_event (
    event_id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    scanner_name TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    run_id BIGINT REFERENCES dds.scanner_run(run_id),
    timeframe TEXT,
    direction TEXT,
    score NUMERIC,
    detected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE dds.scanner_event
    ADD COLUMN IF NOT EXISTS run_id BIGINT REFERENCES dds.scanner_run(run_id);

CREATE INDEX IF NOT EXISTS idx_scanner_event_type ON dds.scanner_event (event_type);
CREATE INDEX IF NOT EXISTS idx_scanner_event_time ON dds.scanner_event (created_at);
CREATE INDEX IF NOT EXISTS idx_scanner_event_symbol_time ON dds.scanner_event (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scanner_event_run ON dds.scanner_event (run_id);

-- ============================================================
-- SCANNER_ERROR: timeout/API/retry ошибки
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.scanner_error (
    error_id BIGSERIAL PRIMARY KEY,
    run_id BIGINT REFERENCES dds.scanner_run(run_id),
    symbol TEXT,
    scanner_name TEXT,
    error_type TEXT NOT NULL,
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scanner_error_run ON dds.scanner_error (run_id);
CREATE INDEX IF NOT EXISTS idx_scanner_error_type ON dds.scanner_error (error_type);
CREATE INDEX IF NOT EXISTS idx_scanner_error_symbol ON dds.scanner_error (symbol);

-- ============================================================
-- MARKET_SIGNAL: объединённый торговый сигнал по монете
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.market_signal (
    signal_id BIGSERIAL PRIMARY KEY,
    instrument_id BIGINT NOT NULL REFERENCES dds.instrument(instrument_id),
    direction TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    scanner_count INTEGER NOT NULL DEFAULT 1,
    scanners JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_score NUMERIC NOT NULL,
    aggregate_score NUMERIC NOT NULL,
    first_detected_at TIMESTAMPTZ NOT NULL,
    last_detected_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    status_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT market_signal_direction_chk CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT market_signal_status_chk CHECK (
        status IN ('ACTIVE', 'EXECUTED', 'INVALIDATED', 'EXPIRED', 'SUPPRESSED')
    )
);

CREATE INDEX IF NOT EXISTS idx_market_signal_instrument ON dds.market_signal (instrument_id);
CREATE INDEX IF NOT EXISTS idx_market_signal_status ON dds.market_signal (status);
CREATE INDEX IF NOT EXISTS idx_market_signal_score ON dds.market_signal (aggregate_score DESC);
CREATE INDEX IF NOT EXISTS idx_market_signal_active ON dds.market_signal (aggregate_score DESC)
WHERE status = 'ACTIVE';

-- Repair historical duplicates before enforcing the active business key.
DELETE FROM dds.market_signal older
USING dds.market_signal newer
WHERE older.status = 'ACTIVE' AND newer.status = 'ACTIVE'
  AND older.instrument_id = newer.instrument_id
  AND older.direction = newer.direction
  AND older.timeframe = newer.timeframe
  AND (older.last_detected_at, older.signal_id) <
      (newer.last_detected_at, newer.signal_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_market_signal_active
ON dds.market_signal (instrument_id, direction, timeframe)
WHERE status = 'ACTIVE';

-- ============================================================
-- SIGNAL_OUTCOME: measured post-signal result for expectancy reports
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.signal_outcome (
    setup_id TEXT PRIMARY KEY REFERENCES dds.scanner_setup(setup_id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    scanner_name TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_touched BOOLEAN NOT NULL DEFAULT false,
    first_event TEXT NOT NULL,
    result_r NUMERIC NOT NULL DEFAULT 0,
    mfe_r NUMERIC NOT NULL DEFAULT 0,
    mae_r NUMERIC NOT NULL DEFAULT 0,
    bars_to_entry INTEGER,
    bars_to_exit INTEGER,
    entry_price NUMERIC,
    exit_price NUMERIC,
    fee_slippage_adjusted_result_r NUMERIC NOT NULL DEFAULT 0,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT signal_outcome_direction_chk CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT signal_outcome_first_event_chk CHECK (
        first_event IN ('NO_ENTRY', 'TP1', 'TP2', 'SL', 'EXPIRED', 'OPEN')
    )
);

CREATE INDEX IF NOT EXISTS idx_signal_outcome_scanner ON dds.signal_outcome (scanner_name);
CREATE INDEX IF NOT EXISTS idx_signal_outcome_symbol ON dds.signal_outcome (symbol);
CREATE INDEX IF NOT EXISTS idx_signal_outcome_event ON dds.signal_outcome (first_event);
CREATE INDEX IF NOT EXISTS idx_signal_outcome_result_r ON dds.signal_outcome (result_r DESC);

-- ============================================================
-- VIEWS
-- ============================================================
DROP VIEW IF EXISTS dds.scanner_stats CASCADE;
DROP VIEW IF EXISTS dds.run_history CASCADE;
DROP VIEW IF EXISTS dds.active_signals CASCADE;
DROP VIEW IF EXISTS dds.scanner_expectancy CASCADE;
DROP VIEW IF EXISTS dds.scanner_symbol_expectancy CASCADE;
DROP VIEW IF EXISTS dds.scanner_regime_expectancy CASCADE;
DROP VIEW IF EXISTS dds.score_bucket_expectancy CASCADE;
DROP VIEW IF EXISTS dds.score_calibration CASCADE;
DROP VIEW IF EXISTS dds.scanner_confluence_expectancy CASCADE;

CREATE OR REPLACE VIEW dds.scanner_stats AS
SELECT
    scanner_name,
    COUNT(*) AS total_setups,
    COUNT(*) FILTER (WHERE status = 'READY_TO_TRADE') AS ready_to_trade,
    COUNT(*) FILTER (WHERE status = 'DETECTED') AS detected,
    COUNT(*) FILTER (WHERE status = 'CONFLICT') AS conflict,
    COUNT(*) FILTER (WHERE status = 'INVALIDATED') AS invalidated,
    COUNT(*) FILTER (WHERE status = 'EXPIRED') AS expired,
    COUNT(*) FILTER (WHERE status = 'EXECUTED') AS executed,
    AVG(score) AS avg_score,
    MAX(detected_at) AS last_detected
FROM dds.scanner_setup
GROUP BY scanner_name;

CREATE OR REPLACE VIEW dds.run_history AS
SELECT
    run_id,
    started_at,
    finished_at,
    symbols_scanned || '/' || symbols_total AS universe,
    setups_found,
    error_count,
    ROUND(duration_sec::numeric, 1) AS duration_sec,
    status
FROM dds.scanner_run
ORDER BY started_at DESC
LIMIT 50;

CREATE OR REPLACE VIEW dds.active_signals AS
SELECT
    i.symbol,
    ms.direction,
    ms.scanner_count,
    ms.scanners,
    ms.max_score,
    ms.aggregate_score,
    ms.first_detected_at,
    ms.last_detected_at
FROM dds.market_signal ms
JOIN dds.instrument i ON i.instrument_id = ms.instrument_id
WHERE ms.status = 'ACTIVE'
ORDER BY ms.aggregate_score DESC;

CREATE OR REPLACE VIEW dds.scanner_expectancy AS
SELECT
    scanner_name,
    direction,
    COUNT(*) AS samples,
    COUNT(*) FILTER (WHERE entry_touched) AS entries,
    COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')) AS wins,
    COUNT(*) FILTER (WHERE first_event = 'SL') AS losses,
    ROUND(AVG(result_r), 4) AS avg_r,
    ROUND(AVG(fee_slippage_adjusted_result_r), 4) AS avg_r_after_costs,
    ROUND(AVG(mfe_r), 4) AS avg_mfe_r,
    ROUND(AVG(mae_r), 4) AS avg_mae_r,
    ROUND(
        (COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE entry_touched), 0),
        4
    ) AS win_rate_on_entries,
    ROUND(
        SUM(GREATEST(result_r, 0))
        / NULLIF(ABS(SUM(LEAST(result_r, 0))), 0),
        4
    ) AS profit_factor
FROM dds.signal_outcome
GROUP BY scanner_name, direction
ORDER BY avg_r_after_costs DESC NULLS LAST;

-- Enriched expectancy: join outcome with setup for regime/score context
-- Score buckets are wider (0-19, 20-39, 40-59, 60-79, 80-100) to better
-- reflect the new quality-based scoring where scores are more spread out.
CREATE OR REPLACE VIEW dds._outcome_enriched AS
SELECT
    o.*,
    s.market_regime,
    s.score AS setup_score,
    CASE
        WHEN s.score < 20 THEN '0-19'
        WHEN s.score < 40 THEN '20-39'
        WHEN s.score < 60 THEN '40-59'
        WHEN s.score < 80 THEN '60-79'
        ELSE '80-100'
    END AS score_bucket
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id;

CREATE OR REPLACE VIEW dds.scanner_symbol_expectancy AS
SELECT
    scanner_name,
    symbol,
    direction,
    COUNT(*) AS samples,
    COUNT(*) FILTER (WHERE entry_touched) AS entries,
    ROUND(AVG(result_r), 4) AS avg_r,
    ROUND(AVG(fee_slippage_adjusted_result_r), 4) AS avg_r_after_costs,
    ROUND(
        (COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE entry_touched), 0),
        4
    ) AS win_rate_on_entries
FROM dds.signal_outcome
GROUP BY scanner_name, symbol, direction
HAVING COUNT(*) >= 3
ORDER BY avg_r_after_costs DESC NULLS LAST;

CREATE OR REPLACE VIEW dds.scanner_regime_expectancy AS
SELECT
    scanner_name,
    direction,
    market_regime,
    COUNT(*) AS samples,
    COUNT(*) FILTER (WHERE entry_touched) AS entries,
    ROUND(AVG(result_r), 4) AS avg_r,
    ROUND(AVG(fee_slippage_adjusted_result_r), 4) AS avg_r_after_costs,
    ROUND(
        (COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE entry_touched), 0),
        4
    ) AS win_rate_on_entries
FROM dds._outcome_enriched
GROUP BY scanner_name, direction, market_regime
HAVING COUNT(*) >= 3
ORDER BY avg_r_after_costs DESC NULLS LAST;

CREATE OR REPLACE VIEW dds.score_bucket_expectancy AS
SELECT
    scanner_name,
    direction,
    score_bucket,
    COUNT(*) AS samples,
    COUNT(*) FILTER (WHERE entry_touched) AS entries,
    ROUND(AVG(result_r), 4) AS avg_r,
    ROUND(AVG(fee_slippage_adjusted_result_r), 4) AS avg_r_after_costs,
    ROUND(
        (COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE entry_touched), 0),
        4
    ) AS win_rate_on_entries
FROM dds._outcome_enriched
GROUP BY scanner_name, direction, score_bucket
HAVING COUNT(*) >= 3
ORDER BY score_bucket, avg_r_after_costs DESC NULLS LAST;

-- Score calibration: per-scanner expectancy by score bucket for tuning thresholds
CREATE OR REPLACE VIEW dds.score_calibration AS
SELECT
    s.scanner_name,
    s.direction,
    CASE
        WHEN s.score < 20 THEN '0-19'
        WHEN s.score < 40 THEN '20-39'
        WHEN s.score < 60 THEN '40-59'
        WHEN s.score < 80 THEN '60-79'
        ELSE '80-100'
    END AS score_bucket,
    COUNT(*) AS samples,
    COUNT(*) FILTER (WHERE o.entry_touched) AS entries,
    ROUND(AVG(o.result_r), 4) AS avg_r,
    ROUND(AVG(o.fee_slippage_adjusted_result_r), 4) AS avg_r_adjusted,
    ROUND(
        COUNT(*) FILTER (WHERE o.first_event IN ('TP1', 'TP2'))::numeric /
        NULLIF(COUNT(*) FILTER (WHERE o.entry_touched), 0), 4
    ) AS win_rate
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
WHERE o.entry_touched = true
GROUP BY s.scanner_name, s.direction, score_bucket
HAVING COUNT(*) >= 5
ORDER BY s.scanner_name, s.direction, score_bucket;

-- Scanner confluence is the number of distinct scanners that detected the
-- same symbol/direction within the runner's ten-minute conflict window.
CREATE OR REPLACE VIEW dds.scanner_confluence_expectancy AS
WITH enriched AS (
    SELECT
        o.scanner_name,
        o.direction,
        o.entry_touched,
        o.first_event,
        o.result_r,
        o.fee_slippage_adjusted_result_r,
        (
            SELECT COUNT(DISTINCT nearby.scanner_name)
            FROM dds.scanner_setup nearby
            JOIN dds.instrument nearby_i ON nearby_i.instrument_id = nearby.instrument_id
            WHERE nearby_i.symbol = o.symbol
              AND nearby.direction = o.direction
              AND nearby.detected_at BETWEEN s.detected_at - interval '10 minutes'
                                          AND s.detected_at + interval '10 minutes'
        ) AS confluence_count
    FROM dds.signal_outcome o
    JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
)
SELECT
    scanner_name,
    direction,
    confluence_count,
    COUNT(*) AS samples,
    COUNT(*) FILTER (WHERE entry_touched) AS entries,
    ROUND(AVG(result_r), 4) AS avg_r,
    ROUND(AVG(fee_slippage_adjusted_result_r), 4) AS avg_r_after_costs,
    ROUND(
        (COUNT(*) FILTER (WHERE first_event IN ('TP1', 'TP2')))::numeric
        / NULLIF(COUNT(*) FILTER (WHERE entry_touched), 0),
        4
    ) AS win_rate_on_entries
FROM enriched
GROUP BY scanner_name, direction, confluence_count
HAVING COUNT(*) >= 3
ORDER BY confluence_count DESC, avg_r_after_costs DESC NULLS LAST;

-- ============================================================
-- PAPER_TRADE: paper trading gate — simulated positions
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.paper_trade (
    trade_id        BIGSERIAL PRIMARY KEY,
    setup_id        TEXT NOT NULL REFERENCES dds.scanner_setup(setup_id),
    symbol          TEXT NOT NULL,
    scanner_name    TEXT NOT NULL,
    direction       TEXT NOT NULL,
    score           NUMERIC NOT NULL,
    -- entry / exit
    entry_price     NUMERIC NOT NULL,
    entry_fee       NUMERIC NOT NULL DEFAULT 0,
    stop_price      NUMERIC NOT NULL,
    target_1        NUMERIC,
    target_2        NUMERIC,
    entry_timeframe TEXT NOT NULL DEFAULT '5m',
    position_size   NUMERIC NOT NULL,
    risk_usdt       NUMERIC NOT NULL,
    -- exit (NULL → still open)
    exit_price      NUMERIC,
    exit_reason     TEXT,
    exit_fee        NUMERIC NOT NULL DEFAULT 0,
    -- P&L
    gross_pnl       NUMERIC NOT NULL DEFAULT 0,
    pnl_usdt        NUMERIC NOT NULL DEFAULT 0,
    pnl_r           NUMERIC NOT NULL DEFAULT 0,
    pnl_percent     NUMERIC NOT NULL DEFAULT 0,
    slippage        NUMERIC NOT NULL DEFAULT 0,
    entry_market_price NUMERIC,
    mfe             NUMERIC NOT NULL DEFAULT 0,
    mae             NUMERIC NOT NULL DEFAULT 0,
    mfe_r           NUMERIC NOT NULL DEFAULT 0,
    mae_r           NUMERIC NOT NULL DEFAULT 0,
    price_at_expiry NUMERIC,
    distance_to_tp  NUMERIC,
    distance_to_sl  NUMERIC,
    funding_paid    NUMERIC NOT NULL DEFAULT 0,
    funding_periods INTEGER NOT NULL DEFAULT 0,
    -- status & timestamps
    status          TEXT NOT NULL DEFAULT 'PENDING',
    entered_at      TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    duration_sec    NUMERIC,
    -- account snapshot
    balance_before  NUMERIC NOT NULL DEFAULT 0,
    balance_after   NUMERIC NOT NULL DEFAULT 0,
    -- metadata
    market_regime   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT paper_trade_direction_chk CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT paper_trade_status_chk CHECK (
        status IN ('PENDING', 'OPEN', 'CLOSED', 'EXPIRED', 'CANCELLED')
    ),
    CONSTRAINT paper_trade_exit_reason_chk CHECK (
        exit_reason IS NULL OR exit_reason IN (
            'TAKE_PROFIT_1', 'TAKE_PROFIT_2', 'TAKE_PROFIT_SLIPPAGE',
            'STOP_LOSS', 'STOP_LOSS_GAP', 'TRAILING_STOP',
            'EXPIRED', 'EXPIRED_PROFITABLE', 'TIMEOUT', 'MANUAL', 'RISK_LIMIT'
        )
    )
);

ALTER TABLE dds.paper_trade
    ADD COLUMN IF NOT EXISTS entry_timeframe TEXT NOT NULL DEFAULT '5m';
ALTER TABLE dds.paper_trade
    ADD COLUMN IF NOT EXISTS funding_paid NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE dds.paper_trade
    ADD COLUMN IF NOT EXISTS funding_periods INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS gross_pnl NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS entry_market_price NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS mfe NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS mae NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS mfe_r NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS mae_r NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS price_at_expiry NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS distance_to_tp NUMERIC;
ALTER TABLE dds.paper_trade ADD COLUMN IF NOT EXISTS distance_to_sl NUMERIC;
ALTER TABLE dds.paper_trade DROP CONSTRAINT IF EXISTS paper_trade_exit_reason_chk;
ALTER TABLE dds.paper_trade ADD CONSTRAINT paper_trade_exit_reason_chk CHECK (
    exit_reason IS NULL OR exit_reason IN (
        'TAKE_PROFIT_1', 'TAKE_PROFIT_2', 'TAKE_PROFIT_SLIPPAGE',
        'STOP_LOSS', 'STOP_LOSS_GAP', 'TRAILING_STOP',
        'EXPIRED', 'EXPIRED_PROFITABLE', 'TIMEOUT', 'MANUAL', 'RISK_LIMIT'
    )
);

CREATE INDEX IF NOT EXISTS idx_paper_trade_status ON dds.paper_trade (status);
CREATE INDEX IF NOT EXISTS idx_paper_trade_symbol ON dds.paper_trade (symbol, status);
CREATE INDEX IF NOT EXISTS idx_paper_trade_scanner ON dds.paper_trade (scanner_name);
CREATE INDEX IF NOT EXISTS idx_paper_trade_entered ON dds.paper_trade (entered_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_trade_setup ON dds.paper_trade (setup_id);

-- One scanner setup is executable exactly once, including after terminal states.
-- Existing duplicate historical data makes this statement fail explicitly: it is
-- deliberately not deleted or silently repaired during bootstrap.
DROP INDEX IF EXISTS dds.uq_paper_trade_active_per_setup;
CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_trade_setup
ON dds.paper_trade (setup_id);

-- ============================================================
-- PAPER_ACCOUNT: running account equity snapshots
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.paper_account (
    snapshot_id     BIGSERIAL PRIMARY KEY,
    balance         NUMERIC NOT NULL,
    equity          NUMERIC NOT NULL,
    open_positions  INTEGER NOT NULL DEFAULT 0,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    winning_trades  INTEGER NOT NULL DEFAULT 0,
    losing_trades   INTEGER NOT NULL DEFAULT 0,
    total_pnl       NUMERIC NOT NULL DEFAULT 0,
    max_drawdown    NUMERIC NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_paper_account_time ON dds.paper_account (created_at DESC);

-- Cooldown state for paper consecutive-loss gate
ALTER TABLE dds.paper_account
    ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMPTZ;

-- ============================================================
-- PAPER_SAFETY_GATE_STATE: durable runtime entry gate
-- ============================================================
-- A severe stop-loss gap must continue blocking entries across a runner restart.
-- This singleton is deliberately separate from periodic account snapshots so the
-- halt is persisted immediately by the position-monitor thread.
CREATE TABLE IF NOT EXISTS dds.paper_safety_gate_state (
    gate_id       SMALLINT PRIMARY KEY DEFAULT 1 CHECK (gate_id = 1),
    is_blocked    BOOLEAN NOT NULL DEFAULT FALSE,
    reason        TEXT,
    blocked_since     TIMESTAMPTZ,
    -- Runtime configuration last published by the currently running Paper Engine.
    -- NULL is valid until the first post-migration Paper Engine startup.
    safety_gate_mode  TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- PAPER_SAFETY_EVENT: append-only safety observation history
-- ============================================================
CREATE TABLE IF NOT EXISTS dds.paper_safety_event (
    event_id                 BIGSERIAL PRIMARY KEY,
    event_at                 TIMESTAMPTZ NOT NULL,
    event_type               TEXT NOT NULL,
    severity                 TEXT NOT NULL,
    safety_gate_mode         TEXT NOT NULL,
    symbol                   TEXT NOT NULL,
    scanner_name             TEXT NOT NULL,
    direction                TEXT NOT NULL,
    entry_price              NUMERIC,
    stop_price               NUMERIC,
    expected_exit            NUMERIC,
    observed_price           NUMERIC,
    raw_stop_r               NUMERIC,
    expected_stop_net_r      NUMERIC,
    actual_net_r             NUMERIC,
    gap_pct                  NUMERIC,
    gap_r                    NUMERIC,
    excess_execution_r       NUMERIC,
    position_size            NUMERIC,
    risk_usdt                NUMERIC,
    duration_sec             NUMERIC,
    would_block              BOOLEAN NOT NULL DEFAULT FALSE,
    gate_blocked             BOOLEAN NOT NULL DEFAULT FALSE,
    details_json             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT paper_safety_event_mode_chk CHECK (
        safety_gate_mode IN ('enforce', 'observe', 'disabled')
    )
);
CREATE INDEX IF NOT EXISTS idx_paper_safety_event_at
    ON dds.paper_safety_event (event_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_safety_event_type_at
    ON dds.paper_safety_event (event_type, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_paper_safety_event_scanner_at
    ON dds.paper_safety_event (scanner_name, event_at DESC);

-- ============================================================
-- PAPER_TRADE_STATS: aggregated performance by scanner
-- ============================================================
CREATE OR REPLACE VIEW dds.paper_trade_stats AS
SELECT
    scanner_name,
    direction,
    COUNT(*) AS total_trades,
    COUNT(*) FILTER (WHERE status = 'CLOSED') AS closed,
    COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_usdt > 0) AS wins,
    COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_usdt < 0) AS losses,
    ROUND(AVG(pnl_r) FILTER (WHERE status = 'CLOSED'), 4) AS avg_r,
    ROUND(AVG(pnl_usdt) FILTER (WHERE status = 'CLOSED'), 2) AS avg_pnl_usdt,
    ROUND(SUM(pnl_usdt) FILTER (WHERE status = 'CLOSED'), 2) AS total_pnl_usdt,
    ROUND(AVG(pnl_percent) FILTER (WHERE status = 'CLOSED'), 2) AS avg_pnl_pct,
    ROUND(AVG(duration_sec) FILTER (WHERE status = 'CLOSED'), 1) AS avg_duration_sec,
    ROUND(
        COUNT(*) FILTER (WHERE status = 'CLOSED' AND pnl_usdt > 0)::numeric
        / NULLIF(COUNT(*) FILTER (WHERE status = 'CLOSED'), 0),
        4
    ) AS win_rate,
    ROUND(
        SUM(GREATEST(pnl_usdt, 0)) FILTER (WHERE status = 'CLOSED')
        / NULLIF(
            ABS(SUM(LEAST(pnl_usdt, 0)) FILTER (WHERE status = 'CLOSED')),
            0
        ),
        4
    ) AS profit_factor
FROM dds.paper_trade
GROUP BY scanner_name, direction
ORDER BY total_pnl_usdt DESC;
