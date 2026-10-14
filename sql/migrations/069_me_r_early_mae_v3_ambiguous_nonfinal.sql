-- ME_R_LONG_EARLY_MAE_EXIT_OOS_V3
-- DRAFT research-only lifecycle extension.
-- Requires migration 068. Does not activate capture or evaluator.

BEGIN;

ALTER TABLE research_me_early_mae_v3.source_event
    DROP CONSTRAINT IF EXISTS source_event_source_status_check;

ALTER TABLE research_me_early_mae_v3.source_event
    ADD CONSTRAINT source_event_source_status_check
    CHECK (
        source_status IN (
            'DETECTED',
            'ENTRY_PENDING',
            'ENTRY_FILLED',
            'ENTRY_NOT_FILLED',
            'INVALID',
            'FINALIZED',
            'AMBIGUOUS_NONFINAL'
        )
    );

-- A finalized variant must never contain ambiguous ordering.
ALTER TABLE research_me_early_mae_v3.variant_outcome
    ADD CONSTRAINT chk_me_mae_v3_finalized_not_ambiguous
    CHECK (
        variant_status <> 'FINALIZED'
        OR ambiguous_ordering = FALSE
    );

CREATE OR REPLACE FUNCTION
research_me_early_mae_v3.fn_block_direct_ambiguous_source_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_status = 'AMBIGUOUS_NONFINAL' THEN
        RAISE EXCEPTION 'DIRECT_AMBIGUOUS_NONFINAL_SOURCE_INSERT_FORBIDDEN';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_me_mae_v3_block_direct_ambiguous_insert
BEFORE INSERT ON research_me_early_mae_v3.source_event
FOR EACH ROW EXECUTE FUNCTION
research_me_early_mae_v3.fn_block_direct_ambiguous_source_insert();

CREATE OR REPLACE FUNCTION
research_me_early_mae_v3.fn_block_ambiguous_source_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.source_status = 'AMBIGUOUS_NONFINAL' THEN
        RAISE EXCEPTION 'AMBIGUOUS_NONFINAL_SOURCE_IMMUTABLE';
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$$;

CREATE TRIGGER trg_me_mae_v3_ambiguous_source_update
BEFORE UPDATE OR DELETE
ON research_me_early_mae_v3.source_event
FOR EACH ROW
EXECUTE FUNCTION
research_me_early_mae_v3.fn_block_ambiguous_source_mutation();

CREATE OR REPLACE FUNCTION
research_me_early_mae_v3.fn_block_ambiguous_variant_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_status TEXT;
BEGIN
    SELECT source_status INTO parent_status
    FROM research_me_early_mae_v3.source_event
    WHERE source_event_id = NEW.source_event_id
    FOR UPDATE;

    IF parent_status = 'AMBIGUOUS_NONFINAL' THEN
        RAISE EXCEPTION 'AMBIGUOUS_NONFINAL_VARIANT_IMMUTABLE';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_me_mae_v3_ambiguous_variant_insert
BEFORE INSERT ON research_me_early_mae_v3.variant_outcome
FOR EACH ROW EXECUTE FUNCTION
research_me_early_mae_v3.fn_block_ambiguous_variant_insert();

CREATE OR REPLACE FUNCTION
research_me_early_mae_v3.fn_block_ambiguous_variant_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_status TEXT;
BEGIN
    SELECT source_status INTO parent_status
    FROM research_me_early_mae_v3.source_event
    WHERE source_event_id = OLD.source_event_id
    FOR UPDATE;

    IF parent_status = 'AMBIGUOUS_NONFINAL' THEN
        RAISE EXCEPTION 'AMBIGUOUS_NONFINAL_VARIANT_IMMUTABLE';
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$$;

CREATE TRIGGER trg_me_mae_v3_ambiguous_variant_update
BEFORE UPDATE OR DELETE
ON research_me_early_mae_v3.variant_outcome
FOR EACH ROW
EXECUTE FUNCTION
research_me_early_mae_v3.fn_block_ambiguous_variant_mutation();

-- Validate the complete seven-variant diagnostic tuple at COMMIT.
ALTER TABLE research_me_early_mae_v3.variant_outcome
    ADD CONSTRAINT chk_me_mae_v3_ambiguous_status_consistency
    CHECK (
        (variant_status = 'AMBIGUOUS') = ambiguous_ordering
    );

CREATE OR REPLACE FUNCTION
research_me_early_mae_v3.fn_validate_ambiguous_nonfinal_commit()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
    v_total INTEGER;
    v_ambiguous INTEGER;
    v_invalid INTEGER;
    v_finalized INTEGER;
    v_primary INTEGER;
BEGIN
    SELECT source_status INTO v_status
    FROM research_me_early_mae_v3.source_event
    WHERE source_event_id = NEW.source_event_id
      AND protocol_version = NEW.protocol_version;

    IF v_status IS DISTINCT FROM 'AMBIGUOUS_NONFINAL' THEN
        RETURN NULL;
    END IF;

    SELECT
        count(*),
        count(*) FILTER (
            WHERE ambiguous_ordering = TRUE
               OR variant_status = 'AMBIGUOUS'
        ),
        count(*) FILTER (
            WHERE coverage_status <> 'COMPLETE'
               OR coverage_reason IS NOT NULL
               OR variant_status NOT IN ('EVALUATED', 'AMBIGUOUS', 'BASELINE_EXITED_EARLY')
               OR variant_exit_at IS NULL
               OR variant_exit_price IS NULL
               OR net_r_normal IS NULL
               OR net_r_elevated IS NULL
               OR delta_net_r_normal IS NULL
               OR delta_net_r_elevated IS NULL
               OR execution_price_semantics IS NULL
        ),
        count(*) FILTER (WHERE variant_status = 'FINALIZED'),
        count(*) FILTER (WHERE is_primary)
    INTO
        v_total,
        v_ambiguous,
        v_invalid,
        v_finalized,
        v_primary
    FROM research_me_early_mae_v3.variant_outcome
    WHERE source_event_id = NEW.source_event_id
      AND protocol_version = NEW.protocol_version;

    IF v_total <> 7
       OR v_ambiguous < 1
       OR v_invalid <> 0
       OR v_finalized <> 0
       OR v_primary <> 1
       OR EXISTS (
           SELECT 1
           FROM research_me_early_mae_v3.variant_outcome o
           LEFT JOIN research_me_early_mae_v3.variant v
             ON v.protocol_version = o.protocol_version
            AND v.variant_id = o.variant_id
           WHERE o.source_event_id = NEW.source_event_id
             AND o.protocol_version = NEW.protocol_version
             AND (
                 v.variant_id IS NULL
                 OR v.role IS DISTINCT FROM o.role
                 OR v.is_primary IS DISTINCT FROM o.is_primary
                 OR v.horizon_minutes IS DISTINCT FROM o.horizon_minutes
                 OR v.threshold_mae_r IS DISTINCT FROM o.threshold_mae_r
           )
       ) THEN
        RAISE EXCEPTION 'AMBIGUOUS_NONFINAL_TUPLE_INVALID';
    END IF;

    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_me_mae_v3_ambiguous_commit_check
AFTER UPDATE
ON research_me_early_mae_v3.source_event
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
WHEN (NEW.source_status = 'AMBIGUOUS_NONFINAL')
EXECUTE FUNCTION
research_me_early_mae_v3.fn_validate_ambiguous_nonfinal_commit();

COMMIT;
