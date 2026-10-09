# SRR SHORT Writer Activation Runbook V1

**Experiment:** `SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1`
**Boundary:** `SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1`
**Artifact status:** Prepared for operator review only
**Production activation:** **PROHIBITED by this runbook**

---

## 0. Executive safety statement

This runbook prepares a future activation; it does not authorize one.

Until a new, explicit operator approval is recorded, the following must remain unchanged:

- migration `062_srr_short_writer_activation_boundary.sql` is not applied to production;
- no production activation record is created or updated;
- no `ACTIVATION_TS` is selected;
- `dry_run=True` remains enabled;
- `enable_writes=False` remains enabled;
- the production evaluator is not restarted;
- no deploy, configuration change, historical backfill, or production data mutation occurs.

Permitted during preparation:

- local source audit;
- read-only production diagnostics;
- local unit tests;
- migration and integration tests against an isolated local PostgreSQL database only.

**Preparation is not approval.**

---

## 1. Scope and authoritative artifacts

This runbook covers:

1. `PHASE_1_MIGRATION_062`;
2. `PHASE_2_CREATE_PENDING_RECORD`;
3. `PHASE_3_ACTIVATION_TIMESTAMP_APPROVAL`;
4. `PHASE_4_ACTIVATE_RECORD`;
5. `PHASE_5_ENABLE_WRITER`;
6. `PHASE_6_POST_ACTIVATION_VERIFICATION`;
7. `PHASE_7_EMERGENCY_REVOKE`;
8. post-revocation verification and failure handling.

Authoritative source artifacts at audit time:

- `app/research/evaluator_runner.py`;
- `app/research/srr_short_runtime_wiring.py`;
- `app/research/srr_short_writer_activation_boundary_v1.py`;
- `app/research/srr_short_execution_outcome_persistence.py`;
- `app/research/srr_short_writer_connection_manager.py`;
- `app/research/srr_short_execution_r_expansion_prospective_evaluator.py`;
- `app/research/policies/srr_short_timeout_candle_policy_v1.py`;
- `sql/migrations/061_srr_short_execution_outcome_persistence.sql`;
- `sql/migrations/062_srr_short_writer_activation_boundary.sql`;
- `deploy/systemd/trad-bot-research-evaluator.service`.

---

## 2. Audited production path

### 2.1 Actual runtime path

```text
trad-bot-research-evaluator.service
  → app.research.evaluator_runner.main()
    → prepare_srr_short_writer(... enable_writes=False ...)
      → yield None
    → SrrShortExecutionRExpansionProspectiveEvaluator(... dry_run=True ...)
    → ProspectiveOOSEvaluator(... srr_policy_router=srr_eval ...)
      → SRR-specific run_evaluation_cycle()
        → SrrShortExecutionRExpansionProspectiveEvaluator.run_evaluation_cycle()
```

The production runner explicitly hard-codes:

- `enable_writes=False` in `evaluator_runner.py`;
- `dry_run=True` in `evaluator_runner.py`;
- an assertion that `srr_writer is None`.

The observe-only wiring yields no writer callback and opens no database connection.

### 2.2 Writer-enabled path required for future activation

A future writer-enabled path would be:

```text
evaluator_runner
  → prepare_srr_short_writer(
       enable_writes=True,
       activation_ts=<approved UTC value>,
       activation_mode=<explicitly authorized mode>
     )
  → SrrShortWriterActivationBoundary.create(activation_ts)
  → SrrShortWriterActivationGate
  → open_srr_outcome_writer()
  → separate PostgreSQL writer connection
  → SrrOutcomeWriter.write()
    → SELECT ... FROM research.srr_short_writer_activation FOR SHARE
    → verify ACTIVE + immutable metadata + exact runtime/record timestamp match
    → INSERT / UPDATE research.srr_short_execution_prospective_outcome
    → COMMIT or ROLLBACK
```

### 2.3 Audit conclusion: current code cannot be enabled by configuration alone

The currently deployed production runner remains observe-only. The local runtime-enablement implementation introduces SRR-specific configuration (`SRR_SHORT_WRITER_ENABLED`, `SRR_SHORT_WRITER_ACTIVATION_TS`, `SRR_SHORT_WRITER_APPROVAL_REFERENCE`), disabled by default. Configuration alone never authorizes persistence: an approved ACTIVE PostgreSQL activation record must also match. This implementation has not been deployed.

Therefore, writer activation requires a reviewed production code change, preferably a narrowly scoped SRR-specific change in `evaluator_runner.py` or a dedicated SRR wiring module. A global `dry_run` or `enable_writes` switch must not be introduced.

This conclusion is based on:

- literal `enable_writes=False` in `evaluator_runner.py`;
- literal `dry_run=True` in `evaluator_runner.py`;
- no config lookup for either flag;
- `prepare_srr_short_writer` requiring an explicit `activation_mode.allows_writes=True` and explicit `activation_ts`;
- no production mechanism currently constructing `SrrShortWriterActivationMode(enabled=True)`.

---

## 3. Frozen identities and invariants

| Item | Required value |
|---|---|
| Experiment | `SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1` |
| Direction | `SHORT` |
| Boundary version | `SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1` |
| Activation record table | `research.srr_short_writer_activation` |
| Outcome table | `research.srr_short_execution_prospective_outcome` |
| Allowed transitions | `PENDING → ACTIVE → REVOKED`, `PENDING → REVOKED` |
| Eligibility rule | `signal_time > ACTIVATION_TS` |
| Equality rule | `signal_time == ACTIVATION_TS` is rejected |
| Naive datetime | rejected |
| DELETE activation record | prohibited |
| Re-activation after `REVOKED` | prohibited |

---

## 4. Approval checkpoints and stop conditions

Every checkpoint requires an affirmative written operator record.

| Checkpoint | Approval required before |
|---|---|
| CP-0 | Any production preparation, deploy, or migration action |
| CP-1 | Applying migration 062 to production |
| CP-2 | Creating the immutable `PENDING` record |
| CP-3 | Committing the selected `ACTIVATION_TS` |
| CP-4 | Applying the SRR-specific writer code change |
| CP-5 | `PENDING → ACTIVE` |
| CP-6 | Enabling the writer in runtime |
| CP-7 | Beginning post-activation monitoring |
| CP-8 | Emergency revoke execution |

Stop immediately and remain fail-closed when any of the following is observed:

- production Git HEAD is not the approved release;
- migration 062, its prerequisites, triggers, or grants differ from the approved manifest;
- the activation record is missing, duplicated, immutable-metadata-mismatched, or not in the expected lifecycle state;
- runtime and PostgreSQL `ACTIVATION_TS` differ at any precision;
- the timestamp is naive, inferred from runtime clocks, backdated, rounded, or selected after the record was created;
- scanner, paper, evaluator, or PostgreSQL health checks fail;
- PostgreSQL reports writer errors, lock waits, duplicate observations, wrong experiment, wrong direction, or historical outcomes;
- any destructive SQL is proposed or encountered;
- the operator has not explicitly authorized the next mutation.

---

## 5. Baseline requirements

The prior snapshot from 2026-10-09 is evidence only; it is not an activation baseline.

Before any future phase, capture a fresh read-only baseline:

```sql
SELECT
  current_database() AS database,
  current_user AS database_user,
  version() AS postgres_version,
  now() AS server_now;
```

```sql
SELECT experiment_id, status, started_at
FROM research.prospective_experiment
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1';
```

```sql
SELECT direction, status
FROM research.prospective_experiment_direction_state
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
ORDER BY direction, status;
```

```sql
SELECT
  count(*) AS outcome_rows,
  count(*) FILTER (WHERE is_final) AS final_rows,
  min(signal_time) AS min_signal_time,
  max(signal_time) AS max_signal_time
FROM research.srr_short_execution_prospective_outcome;
```

Also capture read-only system state:

```bash
cd /opt/trad_bot
git status -sb
git rev-parse HEAD
git rev-parse origin/main
```

```bash
systemctl is-active trad-bot-scanner
systemctl is-active trad-bot-paper
systemctl is-active trad-bot-research-evaluator
```

```bash
journalctl -u trad-bot-research-evaluator -n 200 --no-pager -l
```

Stop if any output contains an unexpected trace, a wrong database, a non-postgres version, stale Git state, or a service that is not `active`.

---

## 6. PHASE_1_MIGRATION_062

### 6.1 Preconditions

- CP-1 is recorded.
- The approved production release contains migration 062.
- The following dependencies are present:
  - migration 035 creates `research` and `research.fn_set_updated_at()`;
  - migration 049 repeats the idempotent `research.fn_set_updated_at()` definition;
  - migration 056 creates `research.prospective_experiment_direction_state`;
  - migration 061 creates the dedicated SRR outcome table.
- Read-only production diagnostics confirm migration 062 has not been applied.
- A local isolated PostgreSQL 17 test has passed migration 062 with correct constraints and triggers.
- The current writer user is audited for the least-privilege grants described below.

### 6.2 Migration structure

Migration 062 creates:

- schema if absent: `research`;
- table if absent: `research.srr_short_writer_activation`;
- primary key on `experiment_id`;
- CHECKs limiting:
  - `experiment_id` to the SRR experiment;
  - `direction` to `SHORT`;
  - `boundary_version` to `SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1`;
  - `status` to `PENDING`, `ACTIVE`, `REVOKED`;
- immutable-field guard:
  - `experiment_id`, `direction`, `boundary_version`, and `activation_ts` may never be changed;
- lifecycle guard:
  - `PENDING → ACTIVE`;
  - `PENDING → REVOKED`;
  - `ACTIVE → REVOKED`;
- delete guard:
  - `DELETE` always raises;
- `updated_at` trigger.

### 6.3 PostgreSQL 17 compatibility

The migration uses standard PostgreSQL syntax:

- `CREATE TABLE IF NOT EXISTS`;
- `TIMESTAMPTZ`;
- `CHECK` constraints;
- plpgsql trigger functions;
- `CREATE OR REPLACE FUNCTION`;
- `DO $$ ... $$` idempotency blocks;
- trigger existence checks through `pg_trigger`.

No PostgreSQL 17 incompatibility was identified in source review.

### 6.4 Lock impact

The migration creates one empty table and several triggers/functions. It does not alter or scan the existing outcome table. DDL lock impact is expected to be limited to the new object and catalog updates.

Because the research evaluator and scanner may be active, still require an approved maintenance window and verify no active writer transaction before applying the migration.

### 6.5 BEFORE migration checks

Run read-only:

```sql
SELECT to_regclass('research.srr_short_writer_activation');
```

Expected before first application: `NULL`.

```sql
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'research.srr_short_writer_activation'::regclass
ORDER BY conname;
```

```sql
SELECT tgname, tgtype
FROM pg_trigger
WHERE tgrelid = 'research.srr_short_writer_activation'::regclass
  AND NOT tgisinternal
ORDER BY tgname;
```

```sql
SELECT count(*)
FROM pg_stat_activity
WHERE datname = current_database()
  AND state <> 'idle'
  AND query ILIKE '%srr_short%';
```

Expected: zero or an explicitly reviewed set of benign queries.

### 6.6 Future production migration command

**WARNING: mutating command. Do not execute without CP-1.**

```bash
cd /opt/trad_bot
sudo -u postgres psql -d trad_bot -v ON_ERROR_STOP=1 -P pager=off \
  -f sql/migrations/062_srr_short_writer_activation_boundary.sql
```

The command must be run from the approved Git release and must be audited with `git diff` before execution.

### 6.7 AFTER migration checks

```sql
SELECT to_regclass('research.srr_short_writer_activation');
```

Expected: `research.srr_short_writer_activation`.

```sql
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'research'
  AND table_name = 'srr_short_writer_activation'
ORDER BY ordinal_position;
```

Expected columns include:

`experiment_id`, `boundary_version`, `activation_ts`, `direction`, `status`, `notes`, `created_at`, `updated_at`.

```sql
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'research.srr_short_writer_activation'::regclass
ORDER BY conname;
```

Expected constraints include:

- primary key on `experiment_id`;
- checks for experiment, direction, boundary version, and status.

```sql
SELECT tgname
FROM pg_trigger
WHERE tgrelid = 'research.srr_short_writer_activation'::regclass
  AND NOT tgisinternal
ORDER BY tgname;
```

Expected trigger names:

- `trg_srr_short_writer_activation_updated_at`;
- `trg_srr_short_writer_activation_block_change`;
- `trg_srr_short_writer_activation_block_delete`.

```sql
SELECT count(*) AS activation_rows
FROM research.srr_short_writer_activation;
```

Expected after migration before CP-2: `0`.

### 6.8 Idempotency and partial application

The migration is idempotent for table, trigger, and function existence. If the process stops between DDL objects, rerunning the same approved migration after fixing the underlying error is acceptable only with a new operator checkpoint and a read-only diff of the partially applied state.

Never patch a partially applied migration by manually deleting or recreating objects without a new written protocol.

---

## 7. PHASE_2_CREATE_PENDING_RECORD

### 7.1 Ordering requirement

The immutable activation timestamp must be selected and approved **before** the record is inserted. The record cannot be created first and edited later: `activation_ts` is immutable even in `PENDING`.

The correct order is:

```text
approved CP-3 timestamp
  → immutable PENDING record
  → later PENDING → ACTIVE
```

Do not select a timestamp during this preparation task. The operator chooses it immediately before the approved activation window.

### 7.2 Operator SQL template

**WARNING: mutating command. Do not execute without CP-2 and CP-3.**

Replace only the two placeholders:

- `<ACTIVATION_TS_UTC_MICROSECONDS>`: explicit future UTC timestamp with exactly six fractional digits, for example `2027-01-01T00:00:00.000000Z`;
- `<APPROVAL_REFERENCE>`: immutable approval identifier.

```sql
BEGIN;

SELECT count(*) AS existing_records
FROM research.srr_short_writer_activation
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
FOR UPDATE;
-- Existing records must be 0. If not 0, ROLLBACK and investigate.

INSERT INTO research.srr_short_writer_activation (
    experiment_id,
    boundary_version,
    activation_ts,
    direction,
    status,
    notes
)
SELECT
    'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1',
    'SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1',
    '<ACTIVATION_TS_UTC_MICROSECONDS>'::timestamptz,
    'SHORT',
    'PENDING',
    '<APPROVAL_REFERENCE>'
WHERE NOT EXISTS (
    SELECT 1
    FROM research.srr_short_writer_activation
    WHERE experiment_id =
      'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
);

SELECT experiment_id, direction, boundary_version,
       activation_ts, status, notes
FROM research.srr_short_writer_activation
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1';

COMMIT;
```

If the `SELECT` count is not zero, or the inserted row does not show exactly one expected `PENDING` row with the approved timestamp, execute `ROLLBACK`.

### 7.3 Expected state

Exactly one record:

```text
experiment_id   = SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1
direction       = SHORT
boundary_version= SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1
status          = PENDING
activation_ts   = approved UTC value
```

---

## 8. PHASE_3_ACTIVATION_TIMESTAMP_APPROVAL

### 8.1 Required timestamp rules

- explicitly supplied by the operator;
- timezone-aware UTC;
- stored with microsecond precision (`TIMESTAMPTZ`);
- not obtained from `now()`;
- not obtained from a service startup clock;
- not backdated;
- fixed before the immutable `PENDING` record is created;
- chosen after the final baseline and after a quiet period in which no unintended observations are generated;
- chosen with enough lead time to complete CP-1 through CP-6 before it becomes future-relevant.

### 8.2 Selection procedure

1. Record the current UTC server time with a read-only query.
2. Confirm no pending or in-flight observation processing that could create unintended rows.
3. Choose a future timestamp with an approved lead-time margin.
4. Record the exact value in the approval artifact.
5. Verify it has six fractional digits and a `Z` or `+00:00` suffix.
6. Use the exact value in CP-3, the PENDING insert, the runtime configuration, and all verification queries.
7. Do not round, truncate, or reformat it after CP-3.

Example shape only, not a production selection:

```text
2030-01-01T00:00:00.000000Z
```

### 8.3 Precision verification

Runtime normalization preserves nanoseconds from an ISO string, but PostgreSQL `TIMESTAMPTZ` stores microseconds. Therefore the production operator must use a microsecond-precision value so that the canonical Python text and the PostgreSQL value round-trip exactly.

Before activation, run this local/read-only precision check against the approved timestamp:

```sql
SELECT
  '<APPROVED_TS>'::timestamptz AS pg_value,
  to_char('<APPROVED_TS>'::timestamptz, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS pg_canonical_text;
```

Expected `pg_canonical_text` equals the exact Python canonical text generated by `normalize_activation_ts`.

The runtime comparison uses normalized nanoseconds. It rejects:

- `signal_time < ACTIVATION_TS`;
- `signal_time == ACTIVATION_TS`;
- a runtime/record timestamp mismatch.

It permits only a strictly greater `signal_time`.

---

## 9. PHASE_4_ACTIVATE_RECORD

### 9.1 Preconditions

- CP-5 is recorded.
- Exactly one immutable record exists.
- The record is `PENDING`.
- The record’s experiment, direction, boundary version, and timestamp match CP-3.
- The runtime code change and configuration change are prepared but not yet enabled.
- A maintenance window and rollback plan are approved.

### 9.2 Activation SQL

**WARNING: mutating command. Do not execute without CP-5.**

```sql
BEGIN;

SELECT experiment_id, direction, boundary_version, activation_ts, status
FROM research.srr_short_writer_activation
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
FOR UPDATE;

-- Verify exactly one PENDING row with the approved immutable metadata.

UPDATE research.srr_short_writer_activation
SET status = 'ACTIVE'
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
  AND status = 'PENDING'
  AND direction = 'SHORT'
  AND boundary_version =
    'SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1'
  AND activation_ts = '<APPROVED_TS>'::timestamptz;

-- Verify row count = 1. If not, ROLLBACK.

SELECT experiment_id, direction, boundary_version, activation_ts, status
FROM research.srr_short_writer_activation
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1';

COMMIT;
```

Expected final state:

```text
status = ACTIVE
activation_ts unchanged
immutable metadata unchanged
```

### 9.3 Ordering relative to runtime enablement

The safe order is:

```text
1. ACTIVE transition committed
2. runtime code/config assembled and reviewed
3. writer-enabled evaluator restart
4. post-activation verification
```

Never enable the runtime writer before the `ACTIVE` record is committed. This avoids an uncontrolled persistence window. Never leave a partially enabled writer after a failed restart: revert to observe-only and investigate.

If an error occurs between phases:

- before `ACTIVE`: keep writer disabled and rollback the record transaction;
- after `ACTIVE` but before restart: keep runtime writer disabled;
- during restart: immediately disable runtime writer, inspect logs, and revoke if an unauthorized write is possible;
- after restart: run PHASE_6 immediately and revoke under PHASE_7 if any invariant fails.

---

## 10. PHASE_5_ENABLE_WRITER

### 10.1 Required gates

All gates must be true:

- the production code change implements SRR-specific writer mode;
- operator approval CP-6 exists;
- `enable_writes=True` is passed only in the approved SRR path;
- `activation_mode.allows_writes=True`;
- explicit `activation_ts` equals the PostgreSQL record;
- PostgreSQL record is `ACTIVE`;
- writer owns a separate connection;
- the writer loads the authoritative record inside its own transaction with `FOR SHARE`;
- `FOR SHARE` is held until commit/rollback;
- every read, verification, or write error is fail-closed;
- no other experiment receives a writer-enabled route.

### 10.2 Required production code change

Current `evaluator_runner.py` cannot satisfy these gates without a code change because:

- it hard-codes `enable_writes=False`;
- it hard-codes `dry_run=True`;
- it asserts that no writer exists;
- it passes no `activation_mode`;
- it passes no `activation_ts`;
- it does not pass a boundary into the SRR evaluator.

A future implementation task must add an explicitly approved, SRR-specific path that:

1. reads an approved activation timestamp from a narrowly scoped configuration source;
2. constructs `SrrShortWriterActivationMode(enabled=True)` only under an explicit operator-controlled condition;
3. calls `prepare_srr_short_writer` with the same timestamp;
4. constructs `SrrShortExecutionRExpansionProspectiveEvaluator` with:
   - `dry_run=False`;
   - `write_outcomes=<writer-bound callback>`;
   - `activation_boundary=<approved boundary>`;
5. routes the writer-enabled evaluator through the SRR-specific route in `ProspectiveOOSEvaluator`;
6. leaves generic prospective evaluators untouched;
7. fails startup if the approved timestamp or activation mode is absent or malformed.

A global `dry_run` or `enable_writes` setting is prohibited.

### 10.3 Dry-run isolation analysis

`dry_run=False` is local to `SrrShortExecutionRExpansionProspectiveEvaluator`. The generic prospective evaluator is constructed separately and does not receive the SRR writer. The SRR-specific route in `ProspectiveOOSEvaluator.run_evaluation_cycle` delegates only the SRR experiment to the SRR router.

No source evidence was found that changing this SRR evaluator’s `dry_run` affects other prospective evaluators. However, the future implementation task must include a regression test proving this isolation.

### 10.4 Restart command

**WARNING: mutating command. Do not execute without CP-6.**

```bash
sudo systemctl restart trad-bot-research-evaluator
```

Then:

```bash
systemctl is-active trad-bot-research-evaluator
journalctl -u trad-bot-research-evaluator -n 100 --no-pager -l
```

---

## 11. PHASE_6_POST_ACTIVATION_VERIFICATION

Run all checks read-only. Do not infer success from service status alone.

### 11.1 Git and services

```bash
cd /opt/trad_bot
git rev-parse HEAD
git status -sb
systemctl is-active trad-bot-scanner
systemctl is-active trad-bot-paper
systemctl is-active trad-bot-research-evaluator
```

### 11.2 Activation record

```sql
SELECT experiment_id, direction, boundary_version, activation_ts, status
FROM research.srr_short_writer_activation
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1';
```

Expected exactly one row with:

- `status = 'ACTIVE'`;
- approved immutable metadata.

### 11.3 Runtime timestamp match

Verify the runtime configuration and the database record show the same canonical timestamp. The runtime value must be supplied by the approved SRR-specific path and must not be computed at service startup.

### 11.4 No historical or boundary outcomes

```sql
SELECT count(*) AS invalid_historical_rows
FROM research.srr_short_execution_prospective_outcome
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
  AND signal_time <= (
    SELECT activation_ts
    FROM research.srr_short_writer_activation
    WHERE experiment_id =
      'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
  );
```

Expected: `0`.

### 11.5 No LONG outcomes in the SRR SHORT table

```sql
SELECT count(*) AS long_rows
FROM research.srr_short_execution_prospective_outcome
WHERE direction <> 'SHORT';
```

Expected: `0`.

### 11.6 No other experiment outcomes

```sql
SELECT experiment_id, count(*)
FROM research.srr_short_execution_prospective_outcome
GROUP BY experiment_id
ORDER BY experiment_id;
```

Expected only the SRR experiment.

### 11.7 Duplicate observation IDs

```sql
SELECT observation_id, count(*)
FROM research.srr_short_execution_prospective_outcome
GROUP BY observation_id
HAVING count(*) > 1;
```

Expected: no rows.

### 11.8 Prospective cohort validity

```sql
SELECT o.observation_id, o.experiment_id, o.direction,
       o.signal_time, e.started_at
FROM research.srr_short_execution_prospective_outcome o
JOIN research.prospective_experiment e
  ON e.experiment_id = o.experiment_id
WHERE o.experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
  AND (
    o.experiment_id <> e.experiment_id
    OR o.direction <> 'SHORT'
    OR o.signal_time < e.started_at
    OR o.signal_time <= (
      SELECT activation_ts
      FROM research.srr_short_writer_activation
      WHERE experiment_id =
        'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
    )
  );
```

Expected: no rows.

### 11.9 Evaluator logs and cycle completion

Inspect logs for the SRR experiment, errors, and persistence actions:

```bash
journalctl -u trad-bot-research-evaluator --since '<ACTIVATION_UTC>' --no-pager -l
```

Confirm:

- no tracebacks;
- no unexpected `REJECTED_*` actions;
- one successful SRR cycle;
- no duplicate observation errors;
- no database connection errors.

Because the frozen evaluator may require a full 120-minute horizon before a path is final, an absence of newly finalized outcomes immediately after activation is not, by itself, an error. Verify non-final diagnostics and absence of errors instead.

---

## 12. PHASE_7_EMERGENCY_REVOKE

### 12.1 Objective

Revoke authorization for future writer commits without deleting existing outcomes.

### 12.2 Incident sequence

1. Record incident timestamp in UTC.
2. Immediately revert runtime to observe-only or stop the research evaluator if needed:
   ```bash
   sudo systemctl stop trad-bot-research-evaluator
   ```
3. Record any in-flight writer transaction from `pg_stat_activity` and `pg_locks`.
4. Execute the activation-record revoke:
   **WARNING: mutating command. Do not execute without CP-8.**
   ```sql
   BEGIN;
   UPDATE research.srr_short_writer_activation
   SET status = 'REVOKED'
   WHERE experiment_id =
     'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
     AND status = 'ACTIVE';
   -- Verify exactly one row updated.
   COMMIT;
   ```
5. If the revoke waits on `FOR SHARE`, identify the holding writer transaction. Do not delete it; wait for its bounded completion/rollback or terminate only under a separately approved incident procedure.
6. Confirm `REVOKED` is committed.
7. Restart runtime only in observe-only mode; never restart a writer-enabled evaluator against `REVOKED`.
8. Verify frozen policy and immutable metadata are unchanged.
9. Preserve evidence: incident timestamp, SQL output, `pg_stat_activity`, logs, Git HEAD, and activation record snapshot.

### 12.3 Ordering semantics

- `REVOKE` start time and `REVOKE` commit time are different.
- A writer transaction already holding `FOR SHARE` may commit before the revoke update.
- After revoke commits, new writer transactions load `REVOKED` and reject persistence.
- No absolute “zero commits after revoke command starts” claim is made.
- The expected guarantee is strict: after revoke commit, no new writer transaction can pass the activation gate.

### 12.4 Terminal-state warning

`REVOKED` is terminal. Normal restart or re-enable is impossible. Reactivation requires a new, separately approved protocol and cannot be achieved by creating a second activation record under the same experiment primary key. Deleting or altering the record is prohibited by database triggers.

---

## 13. Post-revocation verification

```sql
SELECT experiment_id, direction, boundary_version, activation_ts, status
FROM research.srr_short_writer_activation
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1';
```

Expected: `status = 'REVOKED'`, immutable metadata unchanged.

```sql
SELECT count(*) AS post_revoke_rows
FROM research.srr_short_execution_prospective_outcome
WHERE experiment_id =
  'SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1'
  AND created_at > '<INCIDENT_REVOKE_COMMIT_UTC>';
```

Expected: `0` after all pre-revoke in-flight transactions have settled.

Confirm no frozen-policy changes:

```bash
git status -sb
git diff -- app/research/policies/srr_short_timeout_candle_policy_v1.py
```

Expected: no unexpected diff against the approved release.

Confirm no outcome deletion, truncation, or rollback of stored outcomes occurred.

---

## 14. Recovery and stop-condition handling

| Situation | Action |
|---|---|
| Any gate fails before CP-5 | Rollback activation transaction; keep writer disabled |
| Runtime starts without exact timestamp | Stop/revert runtime; fail closed |
| PostgreSQL unavailable | Stop writer-enabled path; do not retry writes |
| Writer transaction error | Rollback outcome transaction; investigate; do not overwrite finalized rows |
| Unexpected outcome row | Revoke activation; preserve evidence; do not delete |
| Revoke blocks on `FOR SHARE` | Inspect lock holder; wait/terminate only under approved incident procedure |
| Any immutable metadata drift | Revoke and open a new protocol; never repair in place |

---

## 15. Explicit separation of preview and execution

This document contains future execution commands only as controlled documentation.

It does **not** authorize:

- applying migration 062;
- creating or updating an activation record;
- selecting `ACTIVATION_TS`;
- changing `dry_run` or `enable_writes`;
- restarting services;
- deploying code;
- running backfill;
- modifying outcomes.

Each mutating command is marked `WARNING`. Each requires its named CP checkpoint and a new operator instruction.
## 2.4 Runtime enablement implementation update — 2026-10-09

The earlier audit in sections 2.1-2.3 describes the deployed
observe-only baseline, not the current local feature branch.

The local branch introduces SRR-specific configuration:
- SRR_SHORT_WRITER_ENABLED (default false)
- SRR_SHORT_WRITER_ACTIVATION_TS
- SRR_SHORT_WRITER_APPROVAL_REFERENCE

The runtime passes these controls to prepare_srr_short_writer.
The SRR evaluator remains dry-run unless explicitly enabled.

Configuration alone cannot authorize persistence. Every write
requires an authoritative ACTIVE activation record, matching
the runtime timestamp and frozen experiment identity.

The runtime PostgreSQL role is trad_bot. After migration 062,
apply the separately reviewed ACL hardening artifact:

sql/grants/srr_short_runtime_activation_acl_hardening_v1.sql

The resulting activation permissions are SELECT and
UPDATE(notes), with no INSERT or UPDATE(status).
UPDATE(notes) is required for SELECT FOR SHARE.

Emergency revoke is an operator action using a separately
authorized administrative connection. It is not performed
by the runtime writer.

Local evidence:
- 35 PostgreSQL/bootstrap regression tests passed
- 131 runtime unit tests passed
- persistent ACL SELECT FOR SHARE verified under trad_bot

No production migration 062, deployment or activation
has been authorized.
