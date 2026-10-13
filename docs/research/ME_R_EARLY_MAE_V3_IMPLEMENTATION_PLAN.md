# ME_R_EARLY_MAE_V3 Implementation Plan

**Status:** DRAFT — the design is operator-approved for implementation planning, but implementation is not yet authorized.

## Modules to create

- `app/research/me_r_long_early_mae_v3/protocol.py`
- `app/research/me_r_long_early_mae_v3/closed_context.py`
- `app/research/me_r_long_early_mae_v3/detector.py`
- `app/research/me_r_long_early_mae_v3/temporal.py`
- `app/research/me_r_long_early_mae_v3/identity.py`
- `app/research/me_r_long_early_mae_v3/entry_policy.py`
- `app/research/me_r_long_early_mae_v3/candles.py`
- `app/research/me_r_long_early_mae_v3/outcomes.py`
- `app/research/me_r_long_early_mae_v3/variants.py`
- `app/research/me_r_long_early_mae_v3/repository.py`
- `app/research/me_r_long_early_mae_v3/lifecycle.py`
- `app/research/me_r_long_early_mae_v3/capture.py`
- `app/research/me_r_long_early_mae_v3/evaluator.py`
- `app/research/me_r_long_early_mae_v3/runner.py`
- `app/research/me_r_long_early_mae_v3/pipeline.py`
- `app/research/me_r_long_early_mae_v3/guards.py`

## Migration to create after approval

`sql/migrations/0XX_me_r_early_mae_v3_prospective.sql`

The migration must create only `research_me_early_mae_v3` objects:

- `protocol`
- `variant`
- `source_event`
- `variant_outcome`
- `pipeline_run`
- protocol/source lifecycle views
- indexes for source retrieval, terminal-state scanning, and variant summaries
- uniqueness and check constraints documented in the protocol design

No existing migration may be modified.

## Runner and service

- `deploy/systemd/trad-bot-me-mae-v3-evaluator.service`
- Dedicated `SyslogIdentifier=trad-bot-me-mae-v3-evaluator`
- Explicit opt-in flag required at startup
- No changes to `trad-bot-scanner`, `trad-bot-paper`, or the existing research evaluator
- Service remains disabled until operator-approved freeze and activation

## Tests to create

- `tests/test_me_r_early_mae_v3_closed_context.py`
- `tests/test_me_r_early_mae_v3_detector_parity.py`
- `tests/test_me_r_early_mae_v3_temporal_contract.py`
- `tests/test_me_r_early_mae_v3_identity_deduplication.py`
- `tests/test_me_r_early_mae_v3_entry_next_open.py`
- `tests/test_me_r_early_mae_v3_entry_coverage.py`
- `tests/test_me_r_early_mae_v3_baseline_outcomes.py`
- `tests/test_me_r_early_mae_v3_variant_semantics.py`
- `tests/test_me_r_early_mae_v3_repository_atomicity.py`
- `tests/test_me_r_early_mae_v3_activation_fingerprint_guards.py`
- `tests/test_me_r_early_mae_v3_paper_live_isolation.py`
- `tests/test_me_r_early_mae_v3_existing_v2_invariance.py`
- `tests/test_me_r_early_mae_v3_existing_me_v3_isolation.py`

## Files to leave unchanged

- `app/scanners/momentum_exhaustion_reverse_long_v1.py`
- `app/scanners/momentum_exhaustion_reverse_long_v2.py`
- `app/research/me_r_long_early_mae_v2/**`
- `research_me_reverse_long_v2_entry_geometry/**`
- production `config.yaml`
- paper and live engines
- existing systemd units
- frozen V2 protocol and database schema

## Implementation order

1. Create the isolated closed-candle V1 detector and parity tests.
2. Create temporal and identity modules.
3. Create the independent schema migration.
4. Implement repository and atomic persistence.
5. Implement `NEXT_OPEN_5M` entry, the 300-second maximum source-delay rejection, and baseline outcomes.
6. Implement the seven paired variants, exactly one `MAE_15m_050` PRIMARY, horizon-end conditional MAE semantics, and ambiguity handling.
7. Implement the opt-in evaluator runner and service.
8. Run deterministic tests and read-only schema checks.
9. Stop before deployment, freeze, activation, or source publication.

## Explicitly prohibited during implementation

- Applying the migration to production.
- Starting or enabling a service.
- Inserting historical source events.
- Changing production scanner gates.
- Modifying frozen V2.
- Merging with the existing ME Reverse Long V3 experiment.
- Enabling paper/live trading.
- Assigning freeze or activation timestamps.

Before implementation begins, pending open decisions must be resolved or explicitly deferred by the operator: sample-size checkpoints, statistical stopping rules, multiple-comparison adjustment methodology, final identity/collision audit, causal execution limitations, deployment approval, and freeze/activation timestamps.
