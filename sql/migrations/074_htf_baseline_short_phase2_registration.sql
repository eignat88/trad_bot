-- 074_htf_baseline_short_phase2_registration.sql
--
-- Issue #176 Phase-2 dedicated prospective persistence (Blocker B6).
--
-- Creates an ISOLATED storage for the new observe-only experiment:
--   HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1
--
-- Safety contract of this migration:
--   * it does NOT activate the experiment (status stays PENDING);
--   * it does NOT create a freeze_ts (freeze_ts stays NULL);
--   * it does NOT enable the observer (observer_enabled stays FALSE);
--   * it does NOT modify any existing table, experiment, trigger or function
--     (in particular the frozen Point-B prospective stack is untouched);
--   * observations are append-only immutable with durable dedup on
--     (experiment_id, source_event_id);
--   * freeze enforcement (strict signal_time > freeze_ts AND
--     first_touch_time > freeze_ts) is enforced at the database boundary by
--     research.htf_baseline_short_phase2_insert_observation().
--
-- Activation (status PENDING -> RUNNING with a freeze timestamp and protocol
-- fingerprint) is only possible through
-- research.htf_baseline_short_phase2_activate(), which is intentionally never
-- called by this migration or by any automatic process.

BEGIN;

CREATE SCHEMA IF NOT EXISTS research;

-- ---------------------------------------------------------------- experiment
CREATE TABLE IF NOT EXISTS research.htf_baseline_short_phase2_experiment (
    experiment_id        TEXT PRIMARY KEY,
    direction            TEXT NOT NULL DEFAULT 'SHORT' CHECK (direction = 'SHORT'),
    protocol_fingerprint TEXT CHECK (protocol_fingerprint IS NULL OR length(protocol_fingerprint) = 64),
    status               TEXT NOT NULL DEFAULT 'PENDING'
                         CHECK (status IN ('PENDING', 'READY_TO_START', 'RUNNING', 'CANCELLED', 'COMPLETED')),
    freeze_ts            TIMESTAMPTZ,
    observer_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'RUNNING' OR (freeze_ts IS NOT NULL AND protocol_fingerprint IS NOT NULL))
);

CREATE OR REPLACE FUNCTION research.htf_baseline_short_phase2_experiment_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'PENDING' OR NEW.freeze_ts IS NOT NULL OR NEW.observer_enabled IS NOT FALSE THEN
            RAISE EXCEPTION 'P2_REGISTRATION_MUST_START_PENDING_UNFROZEN_DISABLED';
        END IF;
        RETURN NEW;
    END IF;
    -- UPDATE rules ---------------------------------------------------------
    IF NEW.experiment_id <> OLD.experiment_id OR NEW.direction <> OLD.direction THEN
        RAISE EXCEPTION 'P2_EXPERIMENT_IDENTITY_IMMUTABLE';
    END IF;
    IF OLD.protocol_fingerprint IS NOT NULL
       AND NEW.protocol_fingerprint IS DISTINCT FROM OLD.protocol_fingerprint THEN
        RAISE EXCEPTION 'P2_PROTOCOL_FINGERPRINT_SET_ONCE';
    END IF;
    IF OLD.freeze_ts IS NOT NULL AND NEW.freeze_ts IS DISTINCT FROM OLD.freeze_ts THEN
        RAISE EXCEPTION 'P2_FREEZE_TS_SET_ONCE';
    END IF;
    IF OLD.status IN ('CANCELLED', 'COMPLETED') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'P2_TERMINAL_STATUS_IMMUTABLE';
    END IF;
    IF OLD.status = 'PENDING' AND NEW.status NOT IN ('PENDING', 'READY_TO_START', 'RUNNING', 'CANCELLED') THEN
        RAISE EXCEPTION 'P2_ILLEGAL_STATUS_TRANSITION';
    END IF;
    IF OLD.status = 'READY_TO_START' AND NEW.status NOT IN ('READY_TO_START', 'RUNNING', 'CANCELLED') THEN
        RAISE EXCEPTION 'P2_ILLEGAL_STATUS_TRANSITION';
    END IF;
    IF OLD.status = 'RUNNING' AND NEW.status NOT IN ('RUNNING', 'CANCELLED', 'COMPLETED') THEN
        RAISE EXCEPTION 'P2_ILLEGAL_STATUS_TRANSITION';
    END IF;
    IF NEW.status = 'RUNNING' AND (NEW.freeze_ts IS NULL OR NEW.protocol_fingerprint IS NULL) THEN
        RAISE EXCEPTION 'P2_RUNNING_REQUIRES_FREEZE_AND_FINGERPRINT';
    END IF;
    IF OLD.observer_enabled IS TRUE AND NEW.observer_enabled IS FALSE THEN
        NEW.updated_at := now();
        RETURN NEW; -- emergency stop is always allowed
    END IF;
    IF NEW.observer_enabled IS DISTINCT FROM OLD.observer_enabled AND NEW.status <> 'RUNNING' THEN
        RAISE EXCEPTION 'P2_OBSERVER_ENABLE_REQUIRES_RUNNING';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_htf_baseline_short_phase2_experiment_guard
    ON research.htf_baseline_short_phase2_experiment;
CREATE TRIGGER trg_htf_baseline_short_phase2_experiment_guard
    BEFORE INSERT OR UPDATE ON research.htf_baseline_short_phase2_experiment
    FOR EACH ROW EXECUTE FUNCTION research.htf_baseline_short_phase2_experiment_guard();

-- -------------------------------------------------------------- observations
CREATE TABLE IF NOT EXISTS research.htf_baseline_short_phase2_observation (
    observation_id       BIGSERIAL PRIMARY KEY,
    experiment_id        TEXT NOT NULL
                         REFERENCES research.htf_baseline_short_phase2_experiment (experiment_id),
    source_event_id      TEXT NOT NULL,
    episode_id           TEXT NOT NULL,
    level_id             TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    direction            TEXT NOT NULL DEFAULT 'SHORT' CHECK (direction = 'SHORT'),
    signal_time          TIMESTAMPTZ NOT NULL,
    first_touch_time     TIMESTAMPTZ NOT NULL,
    known_at             TIMESTAMPTZ NOT NULL,
    level_price          NUMERIC,
    entry_reference_price NUMERIC,
    features             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (experiment_id, source_event_id),
    CHECK (signal_time > first_touch_time),
    CHECK (known_at = signal_time),
    CHECK (source_event_id <> '')
);

CREATE INDEX IF NOT EXISTS idx_htf_p2_obs_experiment_time
    ON research.htf_baseline_short_phase2_observation (experiment_id, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_htf_p2_obs_episode
    ON research.htf_baseline_short_phase2_observation (experiment_id, episode_id);

CREATE OR REPLACE FUNCTION research.htf_baseline_short_phase2_observation_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'P2_OBSERVATIONS_ARE_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_htf_baseline_short_phase2_observation_guard
    ON research.htf_baseline_short_phase2_observation;
CREATE TRIGGER trg_htf_baseline_short_phase2_observation_guard
    BEFORE UPDATE OR DELETE ON research.htf_baseline_short_phase2_observation
    FOR EACH ROW EXECUTE FUNCTION research.htf_baseline_short_phase2_observation_guard();

-- --------------------------------------------------------------- insert path
CREATE OR REPLACE FUNCTION research.htf_baseline_short_phase2_insert_observation(
    _experiment_id         TEXT,
    _source_event_id       TEXT,
    _episode_id            TEXT,
    _level_id              TEXT,
    _symbol                TEXT,
    _signal_time           TIMESTAMPTZ,
    _first_touch_time      TIMESTAMPTZ,
    _known_at              TIMESTAMPTZ,
    _level_price           NUMERIC,
    _entry_reference_price NUMERIC,
    _features              JSONB DEFAULT '{}'::jsonb
) RETURNS BIGINT AS $$
DECLARE
    _exp research.htf_baseline_short_phase2_experiment%ROWTYPE;
    _id  BIGINT;
BEGIN
    SELECT * INTO _exp
      FROM research.htf_baseline_short_phase2_experiment
     WHERE experiment_id = _experiment_id
       FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'P2_EXPERIMENT_NOT_REGISTERED';
    END IF;
    IF _exp.freeze_ts IS NULL THEN
        RAISE EXCEPTION 'P2_FREEZE_NOT_SET';
    END IF;
    IF _exp.status <> 'RUNNING' THEN
        RAISE EXCEPTION 'P2_EXPERIMENT_NOT_RUNNING';
    END IF;
    IF _exp.protocol_fingerprint IS NULL THEN
        RAISE EXCEPTION 'P2_PROTOCOL_FINGERPRINT_MISSING';
    END IF;
    IF NOT _exp.observer_enabled THEN
        RAISE EXCEPTION 'P2_OBSERVER_NOT_ENABLED';
    END IF;
    IF _signal_time <= _exp.freeze_ts THEN
        RAISE EXCEPTION 'P2_SIGNAL_NOT_AFTER_FREEZE';
    END IF;
    IF _first_touch_time <= _exp.freeze_ts THEN
        RAISE EXCEPTION 'P2_FIRST_TOUCH_NOT_AFTER_FREEZE';
    END IF;
    INSERT INTO research.htf_baseline_short_phase2_observation (
        experiment_id, source_event_id, episode_id, level_id, symbol, direction,
        signal_time, first_touch_time, known_at, level_price,
        entry_reference_price, features
    ) VALUES (
        _experiment_id, _source_event_id, _episode_id, _level_id, _symbol, 'SHORT',
        _signal_time, _first_touch_time, _known_at, _level_price,
        _entry_reference_price, COALESCE(_features, '{}'::jsonb)
    )
    ON CONFLICT (experiment_id, source_event_id) DO NOTHING
    RETURNING observation_id INTO _id;
    RETURN _id; -- NULL on duplicate: idempotent, never an error
END;
$$ LANGUAGE plpgsql;

-- --------------------------------------------------------------- activation
CREATE OR REPLACE FUNCTION research.htf_baseline_short_phase2_activate(
    _experiment_id TEXT,
    _freeze_ts     TIMESTAMPTZ,
    _fingerprint   TEXT
) RETURNS VOID AS $$
DECLARE
    _exp research.htf_baseline_short_phase2_experiment%ROWTYPE;
BEGIN
    IF _fingerprint IS NULL OR length(_fingerprint) <> 64 THEN
        RAISE EXCEPTION 'P2_FINGERPRINT_REQUIRED';
    END IF;
    IF _freeze_ts IS NULL THEN
        RAISE EXCEPTION 'P2_FREEZE_TS_REQUIRED';
    END IF;
    IF _freeze_ts > now() + interval '5 minutes' THEN
        RAISE EXCEPTION 'P2_FREEZE_TS_MUST_NOT_BE_FAR_FUTURE';
    END IF;
    SELECT * INTO _exp
      FROM research.htf_baseline_short_phase2_experiment
     WHERE experiment_id = _experiment_id
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'P2_EXPERIMENT_NOT_REGISTERED';
    END IF;
    IF _exp.status NOT IN ('PENDING', 'READY_TO_START') THEN
        RAISE EXCEPTION 'P2_ACTIVATION_REQUIRES_PENDING_STATUS';
    END IF;
    IF _exp.freeze_ts IS NOT NULL THEN
        RAISE EXCEPTION 'P2_FREEZE_TS_SET_ONCE';
    END IF;
    UPDATE research.htf_baseline_short_phase2_experiment
       SET status = 'RUNNING',
           freeze_ts = _freeze_ts,
           protocol_fingerprint = _fingerprint
     WHERE experiment_id = _experiment_id;
END;
$$ LANGUAGE plpgsql;

-- ------------------------------------------------- registration (NOT activation)
INSERT INTO research.htf_baseline_short_phase2_experiment (
    experiment_id, direction, status, freeze_ts, observer_enabled
) VALUES (
    'HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1', 'SHORT', 'PENDING', NULL, FALSE
) ON CONFLICT (experiment_id) DO NOTHING;

COMMIT;
