-- Multi-Setup Market Scanner schema for PostgreSQL

CREATE SCHEMA IF NOT EXISTS dds;

CREATE TABLE IF NOT EXISTS dds.instrument (
    instrument_id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL DEFAULT 'USDT',
    category TEXT NOT NULL DEFAULT 'linear',
    status TEXT NOT NULL DEFAULT 'Trading',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dds.scanner_setup (
    setup_id TEXT PRIMARY KEY,
    scanner_name TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    instrument_id BIGINT NOT NULL REFERENCES dds.instrument(instrument_id),
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
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    features JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scanner_setup_direction_chk CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT scanner_setup_status_chk CHECK (
        status IN (
            'DETECTED', 'CONFIRMED', 'READY_TO_TRADE', 'CONFLICT',
            'EXECUTED', 'INVALIDATED', 'EXPIRED'
        )
    )
);

-- Keep upgrades idempotent for databases created before candle-level
-- deduplication was introduced.
ALTER TABLE dds.scanner_setup
    ADD COLUMN IF NOT EXISTS signal_candle_open_time BIGINT NOT NULL DEFAULT 0;

ALTER TABLE dds.scanner_setup DROP CONSTRAINT IF EXISTS scanner_setup_status_chk;
UPDATE dds.scanner_setup SET status = 'DETECTED' WHERE status = 'CANDIDATE';
UPDATE dds.scanner_setup SET status = 'READY_TO_TRADE' WHERE status = 'READY';
UPDATE dds.scanner_setup SET status = 'EXECUTED' WHERE status = 'CONSUMED';
ALTER TABLE dds.scanner_setup ADD CONSTRAINT scanner_setup_status_chk CHECK (
    status IN (
        'DETECTED', 'CONFIRMED', 'READY_TO_TRADE', 'CONFLICT',
        'EXECUTED', 'INVALIDATED', 'EXPIRED'
    )
);

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

CREATE TABLE IF NOT EXISTS dds.scanner_event (
    event_id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    scanner_name TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT,
    direction TEXT,
    score NUMERIC,
    detected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_scanner_event_type ON dds.scanner_event (event_type);
CREATE INDEX IF NOT EXISTS idx_scanner_event_time ON dds.scanner_event (created_at);

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
