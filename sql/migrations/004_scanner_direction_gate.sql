-- 004_scanner_direction_gate.sql
-- Runtime source of truth for scanner/direction trade eligibility.
CREATE SCHEMA IF NOT EXISTS config;

CREATE TABLE IF NOT EXISTS config.scanner_direction_gate (
    scanner_name TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    allowed_regimes TEXT[],
    reason TEXT,
    source TEXT NOT NULL DEFAULT 'MANUAL',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT,
    PRIMARY KEY (scanner_name, direction),
    CONSTRAINT scanner_direction_gate_direction_chk CHECK (direction IN ('LONG', 'SHORT')),
    CONSTRAINT scanner_direction_gate_status_chk CHECK (status IN ('ENABLED', 'BLOCKED', 'REGIME'))
);

CREATE TABLE IF NOT EXISTS config.scanner_direction_gate_history (
    history_id BIGSERIAL PRIMARY KEY,
    scanner_name TEXT NOT NULL, direction TEXT NOT NULL,
    old_status TEXT, new_status TEXT NOT NULL,
    old_allowed_regimes TEXT[], new_allowed_regimes TEXT[],
    reason TEXT, changed_at TIMESTAMPTZ NOT NULL DEFAULT now(), changed_by TEXT
);

CREATE OR REPLACE FUNCTION config.record_scanner_direction_gate_history()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO config.scanner_direction_gate_history
            (scanner_name, direction, new_status, new_allowed_regimes, reason, changed_at, changed_by)
        VALUES (NEW.scanner_name, NEW.direction, NEW.status, NEW.allowed_regimes,
                NEW.reason, NEW.updated_at, NEW.updated_by);
    ELSIF (OLD.status, OLD.allowed_regimes, OLD.reason, OLD.source)
          IS DISTINCT FROM (NEW.status, NEW.allowed_regimes, NEW.reason, NEW.source) THEN
        INSERT INTO config.scanner_direction_gate_history
            (scanner_name, direction, old_status, new_status, old_allowed_regimes,
             new_allowed_regimes, reason, changed_at, changed_by)
        VALUES (NEW.scanner_name, NEW.direction, OLD.status, NEW.status,
                OLD.allowed_regimes, NEW.allowed_regimes, NEW.reason,
                NEW.updated_at, NEW.updated_by);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS scanner_direction_gate_history_trg ON config.scanner_direction_gate;
CREATE TRIGGER scanner_direction_gate_history_trg
AFTER INSERT OR UPDATE ON config.scanner_direction_gate
FOR EACH ROW EXECUTE FUNCTION config.record_scanner_direction_gate_history();

-- ON CONFLICT protects SQL changes made by operators after rollout.
INSERT INTO config.scanner_direction_gate
    (scanner_name, direction, status, allowed_regimes, reason, source)
VALUES
 ('LIQUIDITY_SWEEP_CHOCH_OB','LONG','ENABLED',NULL,'migration default','MIGRATION'),
 ('LIQUIDITY_SWEEP_CHOCH_OB','SHORT','ENABLED',NULL,'migration default','MIGRATION'),
 ('BREAKOUT_RETEST','LONG','BLOCKED',NULL,'static safety blocklist','MIGRATION'),
 ('BREAKOUT_RETEST','SHORT','BLOCKED',NULL,'static safety blocklist','MIGRATION'),
 ('LIQUIDITY_REVERSAL','LONG','ENABLED',NULL,'migration default','MIGRATION'),
 ('LIQUIDITY_REVERSAL','SHORT','BLOCKED',NULL,'static safety blocklist','MIGRATION'),
 ('TREND_PULLBACK_V2','LONG','REGIME',ARRAY['TREND_UP'],'TREND_UP only','MIGRATION'),
 ('TREND_PULLBACK_V2','SHORT','BLOCKED',NULL,'static safety blocklist','MIGRATION'),
 ('VOLATILITY_COMPRESSION','LONG','BLOCKED',NULL,'static safety blocklist','MIGRATION'),
 ('VOLATILITY_COMPRESSION','SHORT','BLOCKED',NULL,'static safety blocklist','MIGRATION'),
 ('SUPPORT_RESISTANCE_REACTION','LONG','BLOCKED',NULL,'static safety blocklist','MIGRATION'),
 ('SUPPORT_RESISTANCE_REACTION','SHORT','BLOCKED',NULL,'static safety blocklist','MIGRATION'),
 ('MOMENTUM_EXHAUSTION','LONG','BLOCKED',NULL,'static safety blocklist','MIGRATION'),
 ('MOMENTUM_EXHAUSTION','SHORT','ENABLED',NULL,'migration default','MIGRATION')
ON CONFLICT (scanner_name, direction) DO NOTHING;
