-- Migration 040: Add MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 scanner
-- Blocks original ME SHORT and adds reverse LONG scanner

-- Block MOMENTUM_EXHAUSTION SHORT (for experiment)
INSERT INTO config.scanner_direction_gate
    (scanner_name, direction, status, allowed_regimes, reason, source)
VALUES
    ('MOMENTUM_EXHAUSTION', 'SHORT', 'BLOCKED', NULL, 'blocked for reverse long experiment', 'MIGRATION')
ON CONFLICT (scanner_name, direction) DO UPDATE SET
    status = 'BLOCKED',
    reason = 'blocked for reverse long experiment',
    source = 'MIGRATION',
    updated_at = now(),
    updated_by = 'migration';

-- Add MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG (BLOCKED by default)
INSERT INTO config.scanner_direction_gate
    (scanner_name, direction, status, allowed_regimes, reason, source)
VALUES
    ('MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', 'LONG', 'BLOCKED', NULL, 'new scanner, blocked by default', 'MIGRATION'),
    ('MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', 'SHORT', 'BLOCKED', NULL, 'reverse long scanner does not trade SHORT', 'MIGRATION')
ON CONFLICT (scanner_name, direction) DO NOTHING;