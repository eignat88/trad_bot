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
-- VIEWS
-- ============================================================
DROP VIEW IF EXISTS dds.scanner_stats CASCADE;
DROP VIEW IF EXISTS dds.run_history CASCADE;
DROP VIEW IF EXISTS dds.active_signals CASCADE;

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
