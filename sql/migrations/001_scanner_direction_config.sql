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
