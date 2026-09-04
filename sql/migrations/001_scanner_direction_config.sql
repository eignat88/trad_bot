-- 001_scanner_direction_config.sql
-- Runtime snapshot of scanner×direction availability.
-- The scanner runner fully synchronizes this table from config.yaml at startup.

CREATE TABLE IF NOT EXISTS dds.scanner_direction_config (
    scanner_name     TEXT        NOT NULL,
    direction        TEXT        NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    enabled          BOOLEAN     NOT NULL DEFAULT TRUE,
    block_reason     TEXT,       -- config_block | regime_filter
    regime_whitelist TEXT[],
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scanner_name, direction)
);

COMMENT ON TABLE dds.scanner_direction_config IS
    'Complete runtime scanner×direction snapshot synchronized from config.yaml.';
COMMENT ON COLUMN dds.scanner_direction_config.block_reason IS
    'config_block takes precedence over regime_filter.';

-- Seed data: initial scanner×direction pairs.
-- The scanner runner fully synchronizes this table from config.yaml at startup.
INSERT INTO dds.scanner_direction_config (scanner_name, direction, enabled, block_reason) VALUES
    ('BREAKOUT_RETEST',           'LONG',  FALSE, 'config_block'),
    ('BREAKOUT_RETEST',           'SHORT', FALSE, 'config_block'),
    ('MOMENTUM_EXHAUSTION',       'LONG',  FALSE, 'config_block'),
    ('MOMENTUM_EXHAUSTION_R',     'LONG',  TRUE,  NULL),
    ('MOMENTUM_EXHAUSTION_R',     'SHORT', TRUE,  NULL),
    ('TREND_PULLBACK',            'SHORT', FALSE, 'regime_filter'),
    ('VOLATILITY_COMPRESSION',    'SHORT', FALSE, 'config_block')
ON CONFLICT (scanner_name, direction) DO NOTHING;
