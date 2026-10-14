-- ME_R_LONG_EARLY_MAE_EXIT_OOS_V3
-- DRAFT research-only. Requires migrations 068 and 069.
-- Does not activate capture, evaluator, paper or live trading.

BEGIN;

ALTER TABLE research_me_early_mae_v3.source_event
    ADD CONSTRAINT chk_me_mae_v3_finalized_baseline_unambiguous
    CHECK (
        source_status <> 'FINALIZED'
        OR (
            baseline_exit_reason IS NOT NULL
            AND baseline_exit_reason <> 'AMBIGUOUS_STOP_FIRST'
        )
    );

COMMIT;
