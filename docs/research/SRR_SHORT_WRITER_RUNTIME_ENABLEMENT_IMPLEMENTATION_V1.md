# SRR SHORT Writer Runtime Enablement Implementation V1

**Experiment:** `SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1`
**Scope:** implementation only
**Writer enabled in production:** no
**Deployment:** not performed
**Activation:** not performed

---

## 1. Architecture before

```text
systemd: trad-bot-research-evaluator
  → evaluator_runner.main()
    → ScannerRepository(reader PostgreSQL connection)
    → prepare_srr_short_writer(
         enable_writes=False,
         activation_mode=None,
         activation_ts=None
       )
      → yields None; no writer connection
    → SrrShortExecutionRExpansionProspectiveEvaluator(dry_run=True)
    → ProspectiveOOSEvaluator(srr_policy_router=srr_eval)
```

The runner was intentionally fail-closed, but it had no SRR-specific runtime enablement path.

Relevant source locations at implementation time:

- `app/research/evaluator_runner.py:75-89` — writer wiring was hard-coded disabled.
- `app/research/evaluator_runner.py:91-95` — SRR evaluator was hard-coded `dry_run=True`.
- `app/research/prospective_evaluator.py:133-144` — generic evaluator accepts an SRR router but does not own `dry_run`.
- `app/research/prospective_evaluator.py:372-394` — only the SRR experiment delegates to the SRR router.
- `app/research/srr_short_runtime_wiring.py:24-95` — existing fail-closed gate.
- `app/research/srr_short_writer_activation_boundary_v1.py:379-569` — authoritative activation gate.
- `app/research/srr_short_execution_outcome_persistence.py:83-326` — transactional writer and boundary enforcement.
- `app/research/srr_short_writer_connection_manager.py:14-68` — separate writer connection.

---

## 2. Architecture after

```text
systemd: trad-bot-research-evaluator
  → evaluator_runner.main()
    → settings.srr_short_writer
       default: enabled=False
    → prepare_srr_short_writer(
         enable_writes=False,
         activation_mode=observe-only
       )
      → yields None; no writer connection
    → SrrShortExecutionRExpansionProspectiveEvaluator(dry_run=True)

Explicitly approved future runtime state:
  SRR_SHORT_WRITER_ENABLED=true
  SRR_SHORT_WRITER_ACTIVATION_TS=<canonical UTC>
  SRR_SHORT_WRITER_APPROVAL_REFERENCE=<approval id>
    → prepare_srr_short_writer(
         enable_writes=True,
         activation_mode=SrrShortWriterActivationMode(enabled=True),
         activation_ts=<canonical UTC>
       )
      → separate PostgreSQL writer connection
      → activation gate loads authoritative ACTIVE record with FOR SHARE
    → SrrShortExecutionRExpansionProspectiveEvaluator(
         dry_run=False,
         write_outcomes=srr_writer.write
       )
    → ProspectiveOOSEvaluator remains unchanged for every other experiment
```

---

## 3. Configuration contract

New settings:

```text
SRR_SHORT_WRITER_ENABLED=false
SRR_SHORT_WRITER_ACTIVATION_TS=
SRR_SHORT_WRITER_APPROVAL_REFERENCE=
```

Environment variables take precedence over the optional JSON object:

```json
"srr_short_writer": {
  "enabled": false,
  "activation_ts": "",
  "approval_reference": ""
}
```

Contract:

| Condition | Result |
|---|---|
| Flag absent | Disabled |
| Flag explicitly false | Disabled; no timestamp required |
| Invalid flag value | Startup fails closed |
| Enabled without timestamp | Startup fails closed |
| Enabled without approval reference | Startup fails closed |
| Naive, non-UTC, or noncanonical timestamp | Startup fails closed |
| Enabled with valid configuration | Runtime intent only; PostgreSQL remains authoritative |

The configuration never supplies or overrides the persisted activation record.

---

## 4. Code changes

### `app/config/settings.py`

Added `SrrShortWriterSettings`, default-disabled `Settings.srr_short_writer`, and strict environment/config parsing.

### `app/config/__init__.py`

Exports the new configuration type.

### `app/research/evaluator_runner.py`

- Reads only the scoped `settings.srr_short_writer`.
- Passes explicit `enable_writes`, `activation_ts`, and `SrrShortWriterActivationMode`.
- Changes only the SRR evaluator’s `dry_run`.
- Passes the writer callback only when the runtime gate returned a writer.
- Leaves `ProspectiveOOSEvaluator`, scanner, paper, and live execution unchanged.

### `app/research/srr_short_runtime_wiring.py`

Adds an optional connection factory for isolated tests. The activation gate logic is unchanged.

### `app/research/srr_short_writer_connection_manager.py`

Requires and verifies `autocommit=False`, preserving transactional writer semantics.

---

## 5. Writer gate contract

All existing gates remain authoritative:

1. explicit configuration intent;
2. explicit runtime activation mode;
3. canonical timezone-aware UTC timestamp;
4. exact runtime/persisted timestamp equality;
5. exact experiment and direction equality;
6. persisted status `ACTIVE`;
7. strict `signal_time > ACTIVATION_TS`;
8. SQL pre-filter before Bybit/candle retrieval;
9. defense-in-depth boundary check before persistence;
10. authoritative PostgreSQL read inside the writer transaction;
11. `FOR SHARE` held to commit/rollback;
12. rollback on every error.

No hidden bypass flag exists.

---

## 6. Timestamp precision contract

Configuration accepts only the canonical output of `normalize_activation_ts`:

```text
YYYY-MM-DDTHH:MM:SS.000000Z
```

The accepted form is exact and has PostgreSQL microsecond precision. Sub-microsecond timestamps are rejected at configuration startup, avoiding Python/PostgreSQL precision drift. No timestamp is rounded or moved backward.

The persisted record remains the source of truth; any mismatch still blocks writes.

---

## 7. Least-privilege grants

Reviewed artifact:

`sql/grants/srr_short_runtime_activation_acl_hardening_v1.sql`

The proposed role receives:

- `CONNECT`;
- `USAGE` on `research`;
- `SELECT` on the activation record;
- column-limited `UPDATE (notes)` on the activation record, required for `SELECT ... FOR SHARE`; runtime has no `UPDATE(status)` permission;
- `SELECT`, `INSERT`, `UPDATE` on the SRR outcome table;
- `SELECT` on prospective experiment metadata.

It receives no:

- `DELETE`;
- `TRUNCATE`;
- `ALTER`;
- `DROP`;
- activation immutable-column update rights;
- broad schema ownership.

The grant artifact was not applied to production.

---

## 8. Test evidence

Focused unit tests:

```text
python -m pytest -q tests/test_srr_short_writer_runtime_enablement_v1.py tests/test_srr_short_runtime_wiring_v1.py tests/test_srr_short_runtime_persistence_callback_v1.py
```

Existing activation and persistence tests remain covered by the prior focused suite. PostgreSQL integration tests must be executed only with:

```text
SRR_PERSISTENCE_POSTGRES_IT=1
TEST_DB_HOST=127.0.0.1
```

and a proven throwaway database. No production credentials or database may be used.

---

## 9. Remaining blockers

- Implementation PR requires review and merge.
- PostgreSQL grant artifact requires separate operational review.
- Migration 062 has not been applied to production.
- No activation record or `ACTIVATION_TS` exists for production.
- Fresh production baseline and operator checkpoints remain required.
- Local PostgreSQL integration/concurrency evidence must be recorded before deployment approval.

---

## 10. Production safety statement

This implementation does not enable the writer by default. It does not:

- connect to production for DDL/DML;
- apply migration 062;
- create/update activation records;
- set a production timestamp;
- deploy;
- restart services;
- change paper/live execution;
- modify frozen policy;
- run backfill.

**Implementation PR is not permission to deploy or activate.**
## 11. Local validation and ACL clarification — 2026-10-09

The current runtime connection uses the trad_bot database role,
not the proposed standalone srr_short_writer role.

The approved candidate for runtime ACL review is:

sql/grants/srr_short_runtime_activation_acl_hardening_v1.sql

Runtime activation rights:
- SELECT: allowed
- UPDATE(notes): allowed for SELECT FOR SHARE
- INSERT: denied
- UPDATE(status): denied
- table-wide UPDATE: denied

The earlier standalone-role grant proposal is excluded from
this runtime-enablement PR. It must not be applied as part
of this implementation.

Local verification:
- 35 PostgreSQL/bootstrap regression tests passed
- 131 runtime unit tests passed
- SELECT FOR SHARE passed with persisted ACL under trad_bot

These results supersede earlier statements that local PostgreSQL
integration evidence remains pending.

Production migration 062, ACL application, deployment and
activation remain separate operator-controlled decisions.
