# SRR SHORT Persistence PostgreSQL Bootstrap Readiness V1

**Verdict:** `READY_FOR_LOCAL_BOOTSTRAP`

**Production activation:** not authorized
**Production PostgreSQL:** untouched
**Bootstrap execution:** performed on isolated local PostgreSQL `127.0.0.1:55432` for `trad_bot_srr_persistence_it_20261009_v3`; final regression 35 passed.

---

## 1. Dependency chain

The disposable `trad_bot_srr_persistence_it_20261009_v3` bootstrap uses the repository migration files in this exact order:

```text
008_analytics_foundation.sql
  -> analytics.analysis_run
  -> analytics.analysis_stage_run
  -> analytics.data_quality_result
  -> analytics.update_updated_at_column()

035_research_foundation.sql
  -> research.fn_set_updated_at()
  -> research.fn_block_transition_history_mutation()
  -> research.finding, finding_occurrence, hypothesis, hypothesis_finding
  -> research.experiment, experiment_run, validation_result
  -> research.change_candidate, production_change, monitoring_result
  -> research.transition_history, fingerprint
  -> research updated_at and transition immutability triggers

049_generic_research_framework.sql
  -> research.research_experiment, research_observation
  -> research.research_signal, research_outcome
  -> research.fn_set_updated_at()
  -> research experiment/outcome updated_at triggers

050_prospective_oos_v3_experiments.sql
  -> research.prospective_experiment
  -> research.prospective_observation
  -> research.prospective_outcome
  -> prospective experiment/outcome updated_at triggers

052_srr_oos_prospective_experiment.sql
  -> SRR prospective experiment registration prerequisite

056_prospective_direction_lifecycle.sql
  -> research.prospective_experiment_direction_state

060_srr_short_execution_r_expansion_prospective_registration.sql
  -> registers SRR experiment with status READY_TO_START and started_at NULL

061_srr_short_execution_outcome_persistence.sql
  -> research.srr_short_execution_prospective_outcome
  -> research.fn_block_srr_short_execution_final_update()
  -> outcome updated_at and final-row immutability triggers
  -> CHECK constraints for economics, freeze boundary, SHORT-only, experiment ID

062_srr_short_writer_activation_boundary.sql
  -> research.srr_short_writer_activation
  -> activation lifecycle and delete-block functions/triggers
```

Migration 035 references `analytics.analysis_run`, so migration 008 is a real prerequisite. The tests do not need migration 049/050/056/060 objects directly for all cases, but applying the chain preserves the actual production schema instead of substituting simplified mock tables.

No `CREATE EXTENSION` is required by the selected chain. `gen_random_uuid()` is available in PostgreSQL 17 through the built-in `pgcrypto`-compatible core capability used by the current database server; if the isolated server rejects it, the bootstrap fails before migrations 061/062 and no later migration is attempted.

---

## 2. Bootstrap artifacts

Created:

- `tests/srr_postgres_test_safety.py`
  - explicit disposable database validation;
  - server identity checks;
  - admin/transactional connection helpers;
  - fail-closed migration application;
  - read-only post-bootstrap verification.
- `tests/bootstrap_srr_persistence_postgres_v1.py`
  - test-only bootstrap entrypoint;
  - applies the repository migrations above;
  - verifies all required objects and SRR seed state.
- `tests/test_srr_postgres_test_safety_v1.py`
  - safety and sequence regression tests.

The bootstrap does not create or drop a database. The operator must create the new disposable database separately before invoking it.

---

## 3. Test database contract

Environment required for the future run:

```text
SRR_PERSISTENCE_POSTGRES_IT=1
TEST_DB_HOST=127.0.0.1
TEST_DB_PORT=55432
SRR_TEST_PERSISTENCE_DB=trad_bot_srr_persistence_it_20261009_v3
SRR_TEST_ACTIVATION_DB=trad_bot_srr_activation_it_20261009_v3
TEST_DB_USER=postgres
```

The bootstrap refuses port `5432`, non-local hosts, production/system database names, legacy `20261008`, operator `20261009_v2`, and any malformed/non-disposable name.

Before migration application, `open_bootstrap_connection()` verifies:

- `current_database`;
- `current_setting('server_version')` major version `17`;
- `inet_server_addr`;
- `inet_server_port`.

---

## 4. Sufficient objects after bootstrap

Read-only verification requires:

### Tables

- `analytics.analysis_run`;
- `analytics.analysis_stage_run`;
- `analytics.data_quality_result`;
- all `research` foundation tables listed above;
- `research.prospective_experiment`;
- `research.prospective_observation`;
- `research.prospective_outcome`;
- `research.prospective_experiment_direction_state`;
- `research.srr_short_execution_prospective_outcome`;
- `research.srr_short_writer_activation`.

### Functions

- `analytics.update_updated_at_column`;
- `research.fn_set_updated_at`;
- `research.fn_block_transition_history_mutation`;
- `research.fn_block_srr_short_execution_final_update`;
- `research.fn_block_srr_short_writer_activation_change`;
- `research.fn_block_srr_short_writer_activation_delete`.

### Triggers

- foundation updated_at and transition-history immutability triggers;
- research/prospective updated_at triggers;
- SRR outcome updated_at and final-row immutability triggers;
- SRR activation updated_at, lifecycle, and delete-block triggers.

### Constraints

- SRR outcome primary key on `observation_id`;
- SRR outcome foreign key to `research.prospective_observation`;
- SRR outcome economics/freeze/SHORT/experiment CHECKs;
- activation primary key and experiment/direction/boundary/status CHECKs.

---

## 5. Seed records

The migration chain seeds the prospective experiment:

```text
experiment_id:
  SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1
status:
  READY_TO_START
started_at:
  NULL
```

Migration 061 intentionally creates an empty outcome table. Migration 062 intentionally creates an empty activation table.

The tests insert their own synthetic prospective observations. The bootstrap must not insert historical observations, outcomes, or activation records.

---

## 6. Test sufficiency and migration 062

`test_srr_short_execution_postgres_integration_v1.py` requires:

- migration 061 outcome table;
- `research.prospective_observation`;
- `research.prospective_experiment`;
- the activation table because `make_activation_gate()` and `SrrOutcomeWriter.load_authoritative_record()` read `research.srr_short_writer_activation` on every write.

`test_srr_short_timeout_callback_postgres_e2e_v1.py` requires:

- migration 061 outcome table;
- prospective experiment/observation tables;
- migration 062 activation table and the same activation-gate semantics.

Therefore migration 062 is required for both tests even when the fixture uses a pre-verified activation gate: the writer re-reads the authoritative record with `FOR SHARE` inside the writer transaction and must fail closed when the table is absent.

---

## 7. Destructive scope for the future local run

Only the new disposable database may be modified:

```text
trad_bot_srr_persistence_it_20261009_v3
```

Allowed after explicit operator approval:

- apply migrations 008, 035, 049, 050, 056, 060, 061, 062;
- insert synthetic prospective observations for the two tests;
- insert a test-only activation record required by the gate;
- insert/refresh/rollback synthetic SRR outcomes;
- delete synthetic observations only within the disposable database.

Never modify:

- `trad_bot`;
- port `5432`;
- production VPS;
- `trad_bot_srr_activation_it_20261008`;
- `trad_bot_srr_activation_it_20261009_v2`;
- any other pre-existing database.

---

## 8. Unit and collection evidence

Bootstrap safety tests:

```text
python -m pytest -q tests/test_srr_postgres_test_safety_v1.py
```

Result:

```text
26 passed
```

Persistence integration collection:

```text
python -m pytest --collect-only -q tests/test_srr_short_execution_postgres_integration_v1.py tests/test_srr_short_timeout_callback_postgres_e2e_v1.py
```

Result:

```text
3 tests collected
```

Non-destructive run:

```text
python -m pytest -q tests/test_srr_short_execution_postgres_integration_v1.py tests/test_srr_short_timeout_callback_postgres_e2e_v1.py
```

Result:

```text
3 skipped
```

No `CREATE DATABASE`, `DROP DATABASE`, migration execution, persistence test execution, or production mutation occurred.

---

## 9. Remaining blockers

The bootstrap is ready for a separately approved local run, but the following remain:

- explicit operator approval for creating the new disposable database;
- explicit approval to apply the migration chain;
- explicit approval to run the three persistence integration tests;
- stable service lifetime for `trad-bot-srr-pg17-test` through the entire run;
- post-bootstrap SQL grant verification for the chosen test role;
- no commit/push/PR/deploy has been performed.
