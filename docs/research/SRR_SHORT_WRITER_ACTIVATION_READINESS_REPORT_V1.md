# SRR SHORT Writer Activation Readiness Report V1

**Experiment:** `SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1`
**Audit scope:** preparation, source audit, readiness, tests, review
**Production mutations performed:** none
**Production activation:** not performed and not approved

---

## 1. Verdict

### `BLOCKED_CODE_CHANGE_REQUIRED`

The persistence boundary, migration source, writer, and fail-closed guards are materially implemented and partially tested. However, the production runtime cannot enable the writer through configuration or operator SQL alone.

`evaluator_runner.py` hard-codes:

- `enable_writes=False`;
- `dry_run=True`;
- an assertion that the writer is `None`;
- no `activation_mode`;
- no `activation_ts`;
- no activation boundary for the SRR evaluator.

A narrowly scoped, separately reviewed SRR-specific implementation task is required before production activation.

This report is not `ACTIVATION_APPROVED` and not `ACTIVATION_COMPLETED`.

---

## 2. Baseline reviewed

The task-provided snapshot was treated as historical context, not as a fresh activation baseline.

The local branch source was reviewed at:

```text
HEAD: b642f12265d1d720d3591c5c406fe4d539078745
branch: feat/srr-short-writer-activation-boundary-v1
```

A fresh `git fetch origin` and merge-base comparison showed the reviewed branch was reachable from the then-current `origin/main`, with zero commits unique to that source branch. The documentation artifacts were subsequently created on a separate documentation branch; the pre-existing dirty files listed in the final worktree report were not staged, committed, reset, or removed.

A read-only local database probe confirmed:

- PostgreSQL was reachable;
- the configured local database contained no `research.srr_short_writer_activation`;
- the configured local database contained no dedicated SRR outcome table.

This probe was not used as a production baseline.

---

## 3. Audit findings

### 3.1 Runtime path

The audited path is:

```text
trad-bot-research-evaluator.service
  → evaluator_runner
    → prepare_srr_short_writer(enable_writes=False)
    → SrrShortExecutionRExpansionProspectiveEvaluator(dry_run=True)
    → ProspectiveOOSEvaluator(srr_policy_router=srr_eval)
      → SRR-specific router branch
```

### 3.2 Migration 062

Migration 062 creates an immutable, fail-closed activation record with:

- fixed experiment identity;
- fixed direction;
- fixed boundary version;
- immutable `activation_ts`;
- lifecycle `PENDING → ACTIVE → REVOKED`;
- terminal `REVOKED`;
- blocked `DELETE`;
- `updated_at` trigger;
- primary key on the single experiment row.

No PostgreSQL 17 incompatibility was identified from source review.

The migration is idempotent for object existence. It is not a substitute for runtime authorization.

### 3.3 Activation boundary

`SrrShortWriterActivationBoundary`:

- rejects missing timestamps;
- rejects naive datetimes;
- rejects invalid timezone offsets;
- normalizes to UTC;
- preserves nanosecond input from ISO text for exact runtime comparison;
- uses strict `signal_time > ACTIVATION_TS`;
- treats equality as ineligible;
- filters observations in SQL before candle retrieval;
- re-checks eligibility immediately before persistence.

### 3.4 Writer gate

`SrrShortWriterActivationGate`:

- requires explicit activation mode;
- requires a verified persisted record;
- rejects absent records;
- rejects non-`ACTIVE` records;
- rejects mismatched experiment, direction, boundary version, or timestamp;
- loads the authoritative record from PostgreSQL on every write;
- uses `FOR SHARE` inside the writer transaction;
- keeps the shared row lock through commit/rollback.

### 3.5 Persistence writer

`SrrOutcomeWriter`:

- rejects writes without a gate before any SQL;
- loads the authoritative activation record before outcome SQL;
- rejects pre-activation, wrong experiment, and non-ACTIVE records;
- performs transactional INSERT/UPDATE/COMMIT/ROLLBACK;
- makes non-final refresh idempotent;
- prevents final rows from being overwritten;
- rolls back on exceptions and releases the shared lock.

### 3.6 Connection isolation

`open_srr_outcome_writer` creates a separate PostgreSQL connection and refuses to use the reader connection. It rolls back and closes its own writer connection on exit.

---

## 4. Migration readiness

### 4.1 Dependencies

- `research` schema and `research.fn_set_updated_at()`: migration 035 and idempotent definition in 049.
- `research.prospective_experiment_direction_state`: migration 056.
- Dedicated SRR outcome table: migration 061.
- Prospective experiment and observation framework: prior research migrations.

### 4.2 Lock and service impact

Migration 062 does not scan or rewrite the outcome table. It creates a new table and trigger/function objects. Expected lock impact is catalog-level plus the new relation.

No production migration was executed.

### 4.3 Idempotency

The migration uses `IF NOT EXISTS` and trigger/constraint existence checks. A partial application should be stopped and diagnosed, not silently patched.

---

## 5. Test evidence

Focused local tests were executed against the current source tree.

Command:

```text
python -m pytest -q tests/test_srr_short_writer_activation_boundary_v1.py tests/test_srr_short_writer_activation_migration_062_it_v1.py tests/test_srr_short_activation_concurrency_it_v1.py tests/test_srr_short_real_concurrency_it_v1.py tests/test_srr_short_execution_outcome_persistence_v1.py tests/test_srr_short_execution_postgres_integration_v1.py tests/test_srr_short_runtime_wiring_v1.py tests/test_srr_short_runtime_persistence_callback_v1.py tests/test_srr_short_timeout_callback_postgres_e2e_v1.py
```

Observed result:

```text
109 passed, 21 skipped
```

The skipped tests include PostgreSQL integration/concurrency tests that require the explicit local opt-in:

```text
SRR_PERSISTENCE_POSTGRES_IT=1
```

Therefore:

- unit and mocked persistence tests passed;
- local PostgreSQL integration tests were not executed in this run;
- no production database tests were run;
- no migration 062 was applied by this task;
- no writer was enabled by this task.

---

## 6. Identified issues

### Issue 1 — production runtime hard-blocked

**Severity:** blocker
**Impact:** production writer cannot be enabled by configuration or database state alone.

Required resolution:

- create a separate implementation task;
- implement only an SRR-specific writer-enabled path;
- preserve `dry_run=True` and `enable_writes=False` for all non-approved paths;
- add regression tests for:
  - missing activation mode;
  - missing timestamp;
  - naive timestamp;
  - runtime/database mismatch;
  - non-ACTIVE record;
  - generic evaluator isolation.

### Issue 2 — integration/concurrency evidence requires explicit local PostgreSQL opt-in

**Severity:** test evidence gap
**Impact:** the focused test run passed, but local PostgreSQL integration and concurrency cases were skipped.

Required resolution before activation:

- run the integration and concurrency suites on an isolated local PostgreSQL 17 instance with `SRR_PERSISTENCE_POSTGRES_IT=1`;
- record pass/fail output;
- verify no production DB host, database, or credentials are used;
- do not substitute production execution for skipped local tests.

### Issue 3 — database-role grant model is not defined in the repository

**Severity:** operational readiness gap
**Impact:** the writer database user’s exact INSERT/UPDATE/SELECT/FOR SHARE grant set was not auditable from production in this task.

Required resolution:

- define least-privilege grants for the writer connection;
- grant access only to:
  - the activation record;
  - the dedicated SRR outcome table;
  - required prospective experiment/observation reads;
- prohibit broad `TRUNCATE`, `DELETE`, and unrelated research-write privileges;
- verify grants on the isolated test database before production approval.

### Issue 4 — timestamp precision must be controlled by the operator protocol

**Severity:** safety-critical
**Impact:** PostgreSQL stores microseconds; Python boundary logic preserves nanoseconds from text input. An uncontrolled rounding or string rewrite could create a mismatch.

Required resolution:

- choose an explicit microsecond timestamp;
- store the exact canonical value in CP-3 and the runtime source;
- verify canonical equality before CP-6;
- never use rounded wall-clock timestamps.

---

## 7. Production changes required before activation

1. Implement SRR-specific writer enablement in a separate PR.
2. Add explicit approved configuration only for the SRR path.
3. Add regression tests for runtime configuration isolation.
4. Run all PostgreSQL integration and concurrency tests locally.
5. Define and verify least-privilege writer database grants.
6. Capture a fresh production baseline immediately before activation.
7. Execute PHASE_1 through PHASE_7 only under the runbook’s checkpoints.

---

## 8. Allowed next independent stage

The next allowed independent stage is:

```text
SRR_SHORT_WRITER_RUNTIME_ENABLEMENT_IMPLEMENTATION_TASK
```

That task may design and implement the SRR-specific code path and its tests. It must not:

- apply migration 062 to production;
- create or update an activation record;
- select `ACTIVATION_TS`;
- restart production services;
- deploy without a new operator instruction;
- enable `dry_run=False` globally;
- enable `enable_writes=True` globally.

After that implementation task is reviewed and merged, a new activation-readiness audit may be requested.

---

## 9. Production mutation confirmation

This task performed no production mutations.

No production mutation is authorized by this report.
## Local validation addendum — 2026-10-09

The earlier readiness assessment describes the state at
the time of that audit.

Subsequent isolated local validation completed successfully:
- 35 PostgreSQL/bootstrap regression tests passed
- 131 runtime unit tests passed
- persistent ACL SELECT FOR SHARE passed under trad_bot

The local runtime feature branch remains default-disabled.
Production migration 062 has not been applied.
Production activation remains unauthorized.

The older statement that PostgreSQL integration tests were
not executed is historical and no longer describes current
local test evidence.
