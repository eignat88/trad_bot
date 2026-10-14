-- ============================================================
-- Migration 068: ME_R_LONG_EARLY_MAE_EXIT_PROSPECTIVE_V3
-- Research-only; DO NOT APPLY until separately approved.
-- ============================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS research_me_early_mae_v3;

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.protocol (
    protocol_version TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    scanner_name TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    source_candle_version TEXT NOT NULL,
    direction TEXT NOT NULL,
    frozen_at TIMESTAMPTZ NOT NULL,
    activation_at TIMESTAMPTZ NOT NULL,
    protocol_json JSONB NOT NULL,
    protocol_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (direction = 'LONG'),
    CHECK (scanner_name = 'ME_R_LONG_CLOSED_V1'),
    CHECK (experiment_id = 'ME_R_LONG_EARLY_MAE_EXIT_OOS_V3'),
    CHECK (frozen_at < activation_at)
);

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.variant (
    protocol_version TEXT NOT NULL REFERENCES research_me_early_mae_v3.protocol(protocol_version),
    variant_id TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes > 0),
    threshold_mae_r NUMERIC NOT NULL CHECK (threshold_mae_r > 0),
    role TEXT NOT NULL CHECK (role IN ('PRIMARY','SECONDARY')),
    is_primary BOOLEAN NOT NULL,
    PRIMARY KEY (protocol_version, variant_id),
    CHECK ((role = 'PRIMARY') = is_primary),
    CONSTRAINT chk_me_mae_v3_exact_variant_definitions CHECK (
        (variant_id = 'MAE_10m_040' AND horizon_minutes = 10
         AND threshold_mae_r = 0.40 AND role = 'SECONDARY' AND NOT is_primary)
        OR
        (variant_id = 'MAE_10m_050' AND horizon_minutes = 10
         AND threshold_mae_r = 0.50 AND role = 'SECONDARY' AND NOT is_primary)
        OR
        (variant_id = 'MAE_15m_040' AND horizon_minutes = 15
         AND threshold_mae_r = 0.40 AND role = 'SECONDARY' AND NOT is_primary)
        OR
        (variant_id = 'MAE_15m_050' AND horizon_minutes = 15
         AND threshold_mae_r = 0.50 AND role = 'PRIMARY' AND is_primary)
        OR
        (variant_id = 'MAE_15m_060' AND horizon_minutes = 15
         AND threshold_mae_r = 0.60 AND role = 'SECONDARY' AND NOT is_primary)
        OR
        (variant_id = 'MAE_15m_075' AND horizon_minutes = 15
         AND threshold_mae_r = 0.75 AND role = 'SECONDARY' AND NOT is_primary)
        OR
        (variant_id = 'MAE_30m_050' AND horizon_minutes = 30
         AND threshold_mae_r = 0.50 AND role = 'SECONDARY' AND NOT is_primary)
    )
);

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.source_event (
    source_event_id BIGSERIAL PRIMARY KEY,
    protocol_version TEXT NOT NULL REFERENCES research_me_early_mae_v3.protocol(protocol_version),
    source_setup_id TEXT NOT NULL,
    instrument_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    scanner_name TEXT NOT NULL,
    scanner_version TEXT NOT NULL,
    source_candle_version TEXT NOT NULL,
    direction TEXT NOT NULL,
    source_candle_open_at TIMESTAMPTZ NOT NULL,
    source_candle_close_at TIMESTAMPTZ NOT NULL,
    source_fetch_started_at TIMESTAMPTZ NOT NULL,
    source_available_at TIMESTAMPTZ NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    entry_eligible_at TIMESTAMPTZ NOT NULL,
    entry_filled_at TIMESTAMPTZ,
    entry_price NUMERIC,
    stop_price NUMERIC,
    target_price NUMERIC,
    risk_distance NUMERIC,
    entry_zone_low NUMERIC,
    entry_zone_high NUMERIC,
    reference_price NUMERIC,
    baseline_deadline_at TIMESTAMPTZ,
    baseline_exit_at TIMESTAMPTZ,
    baseline_exit_price NUMERIC,
    baseline_exit_reason TEXT,
    baseline_gross_r NUMERIC,
    baseline_net_r_normal NUMERIC,
    baseline_net_r_elevated NUMERIC,
    source_features JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_status TEXT NOT NULL DEFAULT 'DETECTED'
        CHECK (source_status IN ('DETECTED','ENTRY_PENDING','ENTRY_FILLED','ENTRY_NOT_FILLED','INVALID','FINALIZED')),
    coverage_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (coverage_status IN ('PENDING','COMPLETE','INCOMPLETE')),
    invalid_reason TEXT,
    protocol_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (protocol_version, source_setup_id),
    UNIQUE (protocol_version, instrument_id, scanner_name, direction, source_candle_open_at),
    CHECK (source_candle_close_at <= detected_at),
    CHECK (source_available_at <= detected_at),
    CHECK (source_available_at <= source_candle_close_at + INTERVAL '300 seconds'),
    CHECK (entry_eligible_at >= source_available_at),
    CHECK (entry_filled_at IS NULL OR entry_filled_at >= entry_eligible_at),
    CHECK (coverage_status <> 'INCOMPLETE' OR source_status <> 'FINALIZED')
);

CREATE INDEX IF NOT EXISTS idx_me_mae_v3_pending_sources
    ON research_me_early_mae_v3.source_event (protocol_version, source_status)
    WHERE source_status IN ('DETECTED','ENTRY_PENDING','ENTRY_FILLED');

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.variant_outcome (
    protocol_version TEXT NOT NULL,
    source_event_id BIGINT NOT NULL REFERENCES research_me_early_mae_v3.source_event(source_event_id) ON DELETE CASCADE,
    variant_id TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL CHECK (horizon_minutes > 0),
    threshold_mae_r NUMERIC NOT NULL CHECK (threshold_mae_r > 0),
    role TEXT NOT NULL CHECK (role IN ('PRIMARY','SECONDARY')),
    is_primary BOOLEAN NOT NULL,
    candle_source_version TEXT NOT NULL,
    variant_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (variant_status IN ('PENDING','EVALUATED','BASELINE_EXITED_EARLY','AMBIGUOUS','INCOMPLETE_COVERAGE','FINALIZED')),
    trigger_result TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (trigger_result IN ('PENDING','TRIGGERED','NO_TRIGGER','BASELINE_EXITED_EARLY','BASELINE_SL_TP_PRECEDENCE','INCOMPLETE_COVERAGE')),
    mae_r NUMERIC,
    mfe_r NUMERIC,
    window_low NUMERIC,
    window_high NUMERIC,
    horizon_end_at TIMESTAMPTZ,
    variant_exit_at TIMESTAMPTZ,
    variant_exit_price NUMERIC,
    variant_exit_reason TEXT,
    variant_gross_r NUMERIC,
    net_r_normal NUMERIC,
    net_r_elevated NUMERIC,
    delta_net_r_normal NUMERIC,
    delta_net_r_elevated NUMERIC,
    ambiguous_ordering BOOLEAN NOT NULL DEFAULT FALSE,
    coverage_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (coverage_status IN ('PENDING','COMPLETE','INCOMPLETE')),
    coverage_reason TEXT,
    execution_price_semantics TEXT,
    evaluated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (protocol_version, source_event_id, variant_id),
    CHECK ((role = 'PRIMARY') = is_primary),
    CHECK (
        variant_status <> 'FINALIZED'
        OR (variant_exit_at IS NOT NULL AND variant_exit_price IS NOT NULL
            AND net_r_normal IS NOT NULL AND net_r_elevated IS NOT NULL
            AND delta_net_r_normal IS NOT NULL AND delta_net_r_elevated IS NOT NULL
            AND coverage_status = 'COMPLETE')
    ),
    CHECK (
        variant_status <> 'INCOMPLETE_COVERAGE'
        OR (variant_exit_at IS NULL AND net_r_normal IS NULL
            AND delta_net_r_normal IS NULL AND delta_net_r_elevated IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_me_mae_v3_variant_primary
    ON research_me_early_mae_v3.variant_outcome (variant_id, delta_net_r_normal NULLS LAST);

CREATE TABLE IF NOT EXISTS research_me_early_mae_v3.pipeline_run (
    pipeline_run_id BIGSERIAL PRIMARY KEY,
    protocol_version TEXT NOT NULL REFERENCES research_me_early_mae_v3.protocol(protocol_version),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'RUNNING'
        CHECK (status IN ('RUNNING','COMPLETED','FAILED')),
    sources_seen INTEGER NOT NULL DEFAULT 0,
    sources_inserted INTEGER NOT NULL DEFAULT 0,
    fills_created INTEGER NOT NULL DEFAULT 0,
    variants_written INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_block_finalized_source_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.source_status = 'FINALIZED' THEN
        RAISE EXCEPTION 'FINALIZED_SOURCE_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_block_finalized_source_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.source_status = 'FINALIZED' THEN
        RAISE EXCEPTION 'FINALIZED_SOURCE_IMMUTABLE';
    END IF;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_block_finalized_variant_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.variant_status = 'FINALIZED' THEN
        RAISE EXCEPTION 'FINALIZED_VARIANT_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_block_finalized_variant_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.variant_status = 'FINALIZED' THEN
        RAISE EXCEPTION 'FINALIZED_VARIANT_IMMUTABLE';
    END IF;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_validate_finalized_variant_batch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.variant_status <> 'FINALIZED' THEN
        RETURN NEW;
    END IF;

    IF NEW.coverage_status <> 'COMPLETE'
       OR NEW.variant_exit_at IS NULL
       OR NEW.variant_exit_price IS NULL
       OR NEW.net_r_normal IS NULL
       OR NEW.net_r_elevated IS NULL
       OR NEW.delta_net_r_normal IS NULL
       OR NEW.delta_net_r_elevated IS NULL
       OR NEW.execution_price_semantics IS NULL THEN
        RAISE EXCEPTION 'FINALIZED_VARIANT_INCOMPLETE';
    END IF;

    IF NEW.execution_price_semantics NOT IN (
        'RESEARCH_PRICE_PROXY_NOT_VERIFIED_EXECUTABLE',
        'BASELINE_EXECUTION_PROXY'
    ) THEN
        RAISE EXCEPTION 'UNAPPROVED_EXECUTION_PRICE_SEMANTICS';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_validate_source_finalization()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_count integer;
    p_count integer;
BEGIN
    IF NEW.source_status <> 'FINALIZED' THEN
        RETURN NEW;
    END IF;
    IF NEW.coverage_status <> 'COMPLETE'
       OR NEW.baseline_exit_at IS NULL
       OR NEW.baseline_exit_price IS NULL
       OR NEW.baseline_net_r_normal IS NULL
       OR NEW.baseline_net_r_elevated IS NULL THEN
        RAISE EXCEPTION 'FINALIZED_SOURCE_INCOMPLETE';
    END IF;
    SELECT count(*) INTO v_count
    FROM research_me_early_mae_v3.variant_outcome
    WHERE protocol_version = NEW.protocol_version
      AND source_event_id = NEW.source_event_id;
    SELECT count(*) INTO p_count
    FROM research_me_early_mae_v3.variant
    WHERE protocol_version = NEW.protocol_version AND is_primary;
    IF v_count <> 7 OR p_count <> 1 THEN
        RAISE EXCEPTION 'FINALIZED_SOURCE_VARIANT_TUPLE_INVALID';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_lock_variant_tuple()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_status = 'FINALIZED' THEN
        PERFORM 1 FROM research_me_early_mae_v3.variant_outcome
        WHERE protocol_version = NEW.protocol_version
          AND source_event_id = NEW.source_event_id
        FOR UPDATE;
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_source_immutable_update') THEN
  CREATE TRIGGER trg_me_mae_v3_source_immutable_update
  BEFORE UPDATE ON research_me_early_mae_v3.source_event
  FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v3.fn_block_finalized_source_update();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_source_immutable_delete') THEN
  CREATE TRIGGER trg_me_mae_v3_source_immutable_delete
  BEFORE DELETE ON research_me_early_mae_v3.source_event
  FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v3.fn_block_finalized_source_delete();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_variant_immutable_update') THEN
  CREATE TRIGGER trg_me_mae_v3_variant_immutable_update
  BEFORE UPDATE ON research_me_early_mae_v3.variant_outcome
  FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v3.fn_block_finalized_variant_update();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_variant_immutable_delete') THEN
  CREATE TRIGGER trg_me_mae_v3_variant_immutable_delete
  BEFORE DELETE ON research_me_early_mae_v3.variant_outcome
  FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v3.fn_block_finalized_variant_delete();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_variant_finalized_batch') THEN
  CREATE TRIGGER trg_me_mae_v3_variant_finalized_batch
  BEFORE UPDATE ON research_me_early_mae_v3.variant_outcome
  FOR EACH ROW WHEN (NEW.variant_status = 'FINALIZED')
  EXECUTE FUNCTION research_me_early_mae_v3.fn_validate_finalized_variant_batch();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_source_finalized') THEN
  CREATE TRIGGER trg_me_mae_v3_source_finalized
  BEFORE UPDATE ON research_me_early_mae_v3.source_event
  FOR EACH ROW WHEN (NEW.source_status = 'FINALIZED')
  EXECUTE FUNCTION research_me_early_mae_v3.fn_validate_source_finalization();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_source_lock_variant_tuple') THEN
  CREATE TRIGGER trg_me_mae_v3_source_lock_variant_tuple
  BEFORE UPDATE ON research_me_early_mae_v3.source_event
  FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v3.fn_lock_variant_tuple();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_source_updated_at') THEN
  CREATE TRIGGER trg_me_mae_v3_source_updated_at
  BEFORE UPDATE ON research_me_early_mae_v3.source_event
  FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v3.fn_set_updated_at();
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_me_mae_v3_variant_updated_at') THEN
  CREATE TRIGGER trg_me_mae_v3_variant_updated_at
  BEFORE UPDATE ON research_me_early_mae_v3.variant_outcome
  FOR EACH ROW EXECUTE FUNCTION research_me_early_mae_v3.fn_set_updated_at();
 END IF;
END $$;


-- Final state is checked at transaction end, not during each row update.
CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_validate_finalization_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_source_id BIGINT;
    v_protocol_version TEXT;
    v_status TEXT;
    v_total INTEGER;
    v_final INTEGER;
    v_primary INTEGER;
    v_invalid INTEGER;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_source_id := OLD.source_event_id;
        v_protocol_version := OLD.protocol_version;
    ELSE
        v_source_id := NEW.source_event_id;
        v_protocol_version := NEW.protocol_version;
    END IF;

    SELECT source_status INTO v_status
    FROM research_me_early_mae_v3.source_event
    WHERE source_event_id = v_source_id
      AND protocol_version = v_protocol_version;

    -- A deleted source has no remaining finalization contract here.
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT count(*),
           count(*) FILTER (WHERE variant_status = 'FINALIZED'),
           count(*) FILTER (WHERE is_primary),
           count(*) FILTER (
               WHERE coverage_status <> 'COMPLETE'
                  OR variant_id IS NULL
           )
    INTO v_total, v_final, v_primary, v_invalid
    FROM research_me_early_mae_v3.variant_outcome
    WHERE source_event_id = v_source_id
      AND protocol_version = v_protocol_version;

    IF v_status = 'FINALIZED' THEN
        IF v_total <> 7 OR v_final <> 7
           OR v_primary <> 1 OR v_invalid <> 0 THEN
            RAISE EXCEPTION 'FINALIZED_COMMIT_TUPLE_INVALID';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM research_me_early_mae_v3.variant_outcome o
            LEFT JOIN research_me_early_mae_v3.variant v
              ON v.protocol_version = o.protocol_version
             AND v.variant_id = o.variant_id
            WHERE o.source_event_id = v_source_id
              AND o.protocol_version = v_protocol_version
              AND (
                  v.variant_id IS NULL
                  OR v.role IS DISTINCT FROM o.role
                  OR v.is_primary IS DISTINCT FROM o.is_primary
                  OR v.horizon_minutes IS DISTINCT FROM o.horizon_minutes
                  OR v.threshold_mae_r IS DISTINCT FROM o.threshold_mae_r
              )
        ) THEN
            RAISE EXCEPTION 'FINALIZED_COMMIT_VARIANT_MISMATCH';
        END IF;
    ELSIF v_final > 0 THEN
        RAISE EXCEPTION 'FINALIZED_VARIANT_WITHOUT_FINALIZED_SOURCE';
    END IF;

    RETURN NULL;
END;
$$;


CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_validate_source_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_total INTEGER;
    v_final INTEGER;
    v_primary INTEGER;
    v_mismatch INTEGER;
BEGIN
    IF NEW.source_status <> 'FINALIZED' THEN
        RETURN NULL;
    END IF;

    SELECT count(*),
           count(*) FILTER (WHERE o.variant_status = 'FINALIZED'),
           count(*) FILTER (WHERE o.is_primary),
           count(*) FILTER (
               WHERE v.variant_id IS NULL
                  OR v.role IS DISTINCT FROM o.role
                  OR v.is_primary IS DISTINCT FROM o.is_primary
                  OR v.horizon_minutes IS DISTINCT FROM o.horizon_minutes
                  OR v.threshold_mae_r IS DISTINCT FROM o.threshold_mae_r
           )
    INTO v_total, v_final, v_primary, v_mismatch
    FROM research_me_early_mae_v3.variant_outcome o
    LEFT JOIN research_me_early_mae_v3.variant v
      ON v.protocol_version = o.protocol_version
     AND v.variant_id = o.variant_id
    WHERE o.protocol_version = NEW.protocol_version
      AND o.source_event_id = NEW.source_event_id;

    IF v_total <> 7 OR v_final <> 7 OR v_primary <> 1
       OR v_mismatch <> 0 THEN
        RAISE EXCEPTION 'FINALIZED_SOURCE_COMMIT_INVALID';
    END IF;

    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_me_mae_v3_variant_commit_check
AFTER INSERT OR UPDATE OR DELETE
ON research_me_early_mae_v3.variant_outcome
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION research_me_early_mae_v3.fn_validate_finalization_commit();

CREATE CONSTRAINT TRIGGER trg_me_mae_v3_source_commit_check
AFTER UPDATE OF source_status
ON research_me_early_mae_v3.source_event
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION research_me_early_mae_v3.fn_validate_source_commit();


-- Registered variant definitions are immutable.
-- Initial INSERT of the seven definitions remains permitted.
CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_block_variant_definition_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'REGISTERED_VARIANT_DEFINITION_IMMUTABLE';
END;
$$;

CREATE TRIGGER trg_me_mae_v3_variant_definition_immutable_update
BEFORE UPDATE ON research_me_early_mae_v3.variant
FOR EACH ROW
EXECUTE FUNCTION research_me_early_mae_v3.fn_block_variant_definition_mutation();

CREATE TRIGGER trg_me_mae_v3_variant_definition_immutable_delete
BEFORE DELETE ON research_me_early_mae_v3.variant
FOR EACH ROW
EXECUTE FUNCTION research_me_early_mae_v3.fn_block_variant_definition_mutation();


-- Source finalization must occur through a validated UPDATE.
-- Direct INSERT as FINALIZED would bypass transition checks.
CREATE OR REPLACE FUNCTION research_me_early_mae_v3.fn_block_direct_finalized_source_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_status = 'FINALIZED' THEN
        RAISE EXCEPTION 'DIRECT_FINALIZED_SOURCE_INSERT_FORBIDDEN';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_me_mae_v3_source_block_direct_finalized_insert
BEFORE INSERT ON research_me_early_mae_v3.source_event
FOR EACH ROW
EXECUTE FUNCTION research_me_early_mae_v3.fn_block_direct_finalized_source_insert();

COMMIT;
