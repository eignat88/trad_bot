-- Migration 041: Add ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 scanner
-- Blocks MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG for new entries
-- Enables new OOS validation scanner with close_location >= 0.70 filter

-- Block ME_R_LONG V1 LONG — paused for OOS validation
INSERT INTO config.scanner_direction_gate
    (scanner_name, direction, status, allowed_regimes, reason, source)
VALUES
    ('MOMENTUM_EXHAUSTION_REVERSE_LONG_V1', 'LONG', 'BLOCKED',
     NULL,
     'paused for ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 OOS validation',
     'MANUAL')
ON CONFLICT (scanner_name, direction) DO UPDATE SET
    status = 'BLOCKED',
    reason = 'paused for ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 OOS validation',
    source = 'MANUAL',
    updated_at = now(),
    updated_by = 'migration_041';

-- Enable new OOS validation scanner LONG
INSERT INTO config.scanner_direction_gate
    (scanner_name, direction, status, allowed_regimes, reason, source)
VALUES
    ('ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1', 'LONG', 'ENABLED',
     NULL,
     'OOS validation: close_location >= 0.70 adverse filter on ME_R_LONG',
     'MANUAL'),
    ('ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1', 'SHORT', 'BLOCKED',
     NULL,
     'reverse long scanner does not trade SHORT',
     'MANUAL')
ON CONFLICT (scanner_name, direction) DO UPDATE SET
    status = EXCLUDED.status,
    reason = EXCLUDED.reason,
    source = 'MANUAL',
    updated_at = now(),
    updated_by = 'migration_041';
