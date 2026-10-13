-- ME Reverse Long V3: independent research-only execution outcomes.
-- Do not apply before explicit activation/deployment approval.

BEGIN;

CREATE TABLE IF NOT EXISTS research.me_reverse_long_v3_execution_outcome (
    observation_id BIGINT PRIMARY KEY
        REFERENCES research.prospective_observation(observation_id),

    experiment_id TEXT NOT NULL
        DEFAULT 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS'
        CHECK (
            experiment_id =
            'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS'
        ),

    status TEXT NOT NULL
        CHECK (status IN (
            'PENDING',
            'FINALIZED',
            'COVERAGE_INVALID',
            'TEMPORAL_UNVERIFIED',
            'INVALID_GEOMETRY'
        )),

    filter_group TEXT NOT NULL
        CHECK (filter_group IN (
            'PASS', 'REJECT', 'MISSING_FEATURE'
        )),

    execution_entry_at TIMESTAMPTZ,
    execution_entry_price NUMERIC,
    execution_stop_price NUMERIC,
    execution_target_price NUMERIC,
    exit_at TIMESTAMPTZ,
    exit_price NUMERIC,

    exit_reason TEXT,
    gross_r NUMERIC,
    net_r_normal NUMERIC,
    net_r_elevated NUMERIC,
    ambiguous BOOLEAN,

    coverage_reason TEXT,
    candle_count INTEGER,
    evaluated_at TIMESTAMPTZ NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT me_v3_finalized_integrity CHECK (
        status <> 'FINALIZED'
        OR (
            execution_entry_at IS NOT NULL
            AND execution_entry_price > 0
            AND execution_stop_price > 0
            AND execution_target_price > execution_entry_price
            AND execution_stop_price < execution_entry_price
            AND exit_at IS NOT NULL
            AND exit_price IS NOT NULL
            AND exit_reason IS NOT NULL
            AND gross_r IS NOT NULL
            AND net_r_normal IS NOT NULL
            AND net_r_elevated IS NOT NULL
            AND ambiguous IS NOT NULL
            AND coverage_reason IS NULL
            AND candle_count = 48
        )
    ),

    CONSTRAINT me_v3_nonfinal_no_economics CHECK (
        status = 'FINALIZED'
        OR (
            execution_entry_price IS NULL
            AND execution_stop_price IS NULL
            AND execution_target_price IS NULL
            AND exit_at IS NULL
            AND exit_price IS NULL
            AND exit_reason IS NULL
            AND gross_r IS NULL
            AND net_r_normal IS NULL
            AND net_r_elevated IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_me_v3_exec_status
    ON research.me_reverse_long_v3_execution_outcome
    (status, evaluated_at);

COMMIT;
