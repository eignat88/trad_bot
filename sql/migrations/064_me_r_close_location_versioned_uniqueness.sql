-- ME Close Location: version-isolated signal uniqueness.
-- Coordinated deployment required: schema and writer must match.
-- Existing signals and outcomes are preserved.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- Fail closed on unexpected unique indexes or constraints.
-- Allowed: signal_id primary key, known legacy uniqueness, and
-- correctly defined versioned uniqueness on repeated execution.
DO $$
DECLARE
    idx record;
    expected_versioned text[] :=
        ARRAY['experiment_id', 'signal_version', 'symbol', 'signal_time'];
    expected_legacy text[] :=
        ARRAY['experiment_id', 'symbol', 'signal_time'];
    actual_columns text[];
BEGIN
    FOR idx IN
        SELECT
            i.relname AS index_name,
            ix.indexrelid,
            ix.indisunique,
            ix.indisvalid,
            ix.indisready,
            ix.indnkeyatts,
            ix.indnatts,
            ix.indpred,
            ix.indexprs,
            c.conname AS constraint_name,
            c.contype AS constraint_type
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        LEFT JOIN pg_constraint c
            ON c.conindid = ix.indexrelid
           AND c.contype IN ('p', 'u')
        WHERE ix.indrelid =
            'dds.me_r_long_close_location_oos_signal'::regclass
          AND ix.indisunique
    LOOP
        SELECT array_agg(a.attname::text ORDER BY k.ordinality)
        INTO actual_columns
        FROM unnest(
            (SELECT indkey FROM pg_index
             WHERE indexrelid = idx.indexrelid)
        ) WITH ORDINALITY AS k(attnum, ordinality)
        JOIN pg_attribute a
          ON a.attrelid =
              'dds.me_r_long_close_location_oos_signal'::regclass
         AND a.attnum = k.attnum;

        IF NOT idx.indisvalid
           OR NOT idx.indisready
           OR idx.indpred IS NOT NULL
           OR idx.indexprs IS NOT NULL
           OR idx.indnatts <> idx.indnkeyatts
        THEN
            RAISE EXCEPTION
                'Unexpected ME unique index structure: %', idx.index_name;
        END IF;

        IF idx.index_name = 'me_r_long_close_location_oos_signal_pkey'
           AND actual_columns = ARRAY['signal_id']
           AND idx.constraint_type = 'p'
        THEN
            CONTINUE;

        ELSIF idx.index_name =
            'me_r_long_close_location_oos__experiment_id_symbol_signal_t_key'
            AND actual_columns = expected_legacy
            AND idx.constraint_type = 'u'
        THEN
            CONTINUE;

        ELSIF idx.index_name =
            'uq_me_r_long_cl_oos_signal_experiment_symbol_time'
            AND actual_columns = expected_legacy
            AND idx.constraint_name IS NULL
        THEN
            CONTINUE;

        ELSIF idx.index_name =
            'uq_me_r_cl_oos_experiment_version_symbol_time'
            AND actual_columns = expected_versioned
            AND idx.constraint_name IS NULL
        THEN
            CONTINUE;

        ELSE
            RAISE EXCEPTION
                'Unexpected ME unique index: %, columns=%',
                idx.index_name, actual_columns;
        END IF;
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS
    uq_me_r_cl_oos_experiment_version_symbol_time
ON dds.me_r_long_close_location_oos_signal
    (experiment_id, signal_version, symbol, signal_time);

-- Drop the known UNIQUE CONSTRAINT, if present.
ALTER TABLE dds.me_r_long_close_location_oos_signal
    DROP CONSTRAINT IF EXISTS
    me_r_long_close_location_oos__experiment_id_symbol_signal_t_key;

-- Drop the standalone legacy unique index, if present.
DROP INDEX IF EXISTS
    dds.uq_me_r_long_cl_oos_signal_experiment_symbol_time;

COMMIT;
