-- 001_scanner_direction_config.sql
-- Таблица-справочник: статусы направлений (BLOCKED / ENABLED / REGIME)
-- Источник: config.yaml → blocked_scanner_directions
-- Используется: mart.scanner_direction_status

CREATE TABLE IF NOT EXISTS dds.scanner_direction_config (
    scanner_name   TEXT         NOT NULL,
    direction      TEXT         NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    enabled        BOOLEAN      NOT NULL DEFAULT TRUE,
    block_reason   TEXT,            -- 'config_block', 'regime_filter', 'expectancy_filter'
    regime_whitelist TEXT[],        -- e.g. ARRAY['TREND_UP'] для LONG
    updated_at     TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (scanner_name, direction)
);

COMMENT ON TABLE  dds.scanner_direction_config IS 'Справочник блокировок scanner×direction из config.yaml';
COMMENT ON COLUMN dds.scanner_direction_config.block_reason IS 'config_block | regime_filter | expectancy_filter';

-- Seed data из текущего config.yaml:
-- BREAKOUT_RETEST:      LONG=BLOCKED, SHORT=BLOCKED
-- MOMENTUM_EXHAUSTION:  LONG=BLOCKED
-- TREND_PULLBACK:       SHORT=BLOCKED (regime whitelist: LONG только в TREND_UP)
-- VOLATILITY_COMPRESSION: SHORT=BLOCKED
INSERT INTO dds.scanner_direction_config (scanner_name, direction, enabled, block_reason) VALUES
    ('BREAKOUT_RETEST',           'LONG',  FALSE, 'config_block'),
    ('BREAKOUT_RETEST',           'SHORT', FALSE, 'config_block'),
    ('MOMENTUM_EXHAUSTION',       'LONG',  FALSE, 'config_block'),
    ('TREND_PULLBACK',            'SHORT', FALSE, 'regime_filter'),
    ('VOLATILITY_COMPRESSION',    'SHORT', FALSE, 'config_block')
ON CONFLICT (scanner_name, direction) DO NOTHING;

-- Все остальные комбинации — ENABLED по умолчанию (не вставляем, NULL в JOIN = ENABLED)
