# ME_R_V3_RUNNER_ENABLEMENT_SAFETY_V1

## Scope

Issue #152 closes the safety gate for the standalone runner of
`ME_R_LONG_EARLY_MAE_EXIT_OOS_V3`. The work is confined to
`feature/issue-152-me-v3-runner-safety` and the isolated PostgreSQL contour
`trad_bot_runner_152_it`. Production deployment, OOS activation, scanner,
paper/live routing, systemd and production configuration were not changed.

## Implementation

- `app/research/me_r_long_early_mae_v3/runner.py`
  - Fail-closed default entrypoint; no mode starts implicitly.
  - `--preflight` validates only the explicit isolated contour and does not
    create pipeline rows.
  - Protected `--once` requires both `ME_EARLY_MAE_V3_ISOLATED_TEST_ONLY=true`
    and `ME_EARLY_MAE_V3_ISOLATED_ONCE=true`.
  - Rejects arbitrary DSN/database/user combinations and verifies the exact
    `trad_bot_runner_152_it` database, Unix socket, database/user/session
    identity, and dedicated Linux UID/EUID.
  - Verifies protocol version `3.0.0`, freeze/activation configuration and
    approved runtime fingerprint before execution.
  - Requires strict runtime activation ordering and the existing runtime guard.
  - Verifies all V3 tables, required constraints and critical triggers before
    repository use.
  - Performs live connection checks before repository construction and before
    evaluator execution.
  - Handles evaluator errors as runner failure, rolls back and closes the
    connection, and uses non-zero exits for failure.
  - Installs temporary SIGINT/SIGTERM handlers for graceful one-shot shutdown.

- `tests/test_me_r_early_mae_v3_runner_enablement_safety.py`
  - Expanded fail-closed unit coverage for CLI combinations, opt-ins,
    database identity, connection liveness, schema integrity, fingerprint,
    UID and evaluator error/success paths.

- `tests/test_me_r_early_mae_v3_pg_integration_issue152.py`
  - Real PostgreSQL integration test requires explicit opt-in.
  - Connects only to `trad_bot_runner_152_it_pg_test`.
  - Verifies the expected PostgreSQL role and Unix socket.
  - Requires empty disposable V3 tables and TEST_ONLY fixtures.
  - Uses 48 sequential five-minute candles for the 240-minute baseline.
  - Preserves immutable FINALIZED records.
  - Does not perform wildcard DELETE cleanup.
  - Disposable database teardown is a separate administrative operation.

## Test evidence

Commands and results on VPS:

```text
/opt/trad_bot/.venv/bin/python -m pytest -q tests/test_me_r_early_mae_v3_*.py
198 passed, 1 skipped

Targeted regression:
49 passed

Integration without explicit opt-in:
1 skipped

git diff --check:
PASS

PostgreSQL 17.11 isolated database:
trad_bot_runner_152_it, Unix socket peer authentication
schemas: dds, market, research_me_early_mae_v3
```

## PostgreSQL E2E verdict: PASS

The real PostgreSQL E2E passed in the isolated disposable database
`trad_bot_runner_152_it_pg_test`.

```text
PostgreSQL: 17.11
Database: trad_bot_runner_152_it_pg_test
DB role: trad_bot_runner_152_it
Connection: Unix socket / peer authentication
Pre-test: protocols=0, sources=0, outcomes=0
E2E: 1 passed in 0.26s
Full V3 regression: 198 passed, 1 skipped
git diff --check: PASS
The earlier FINALIZED_SOURCE_IMMUTABLE failure was caused by
the test connecting to trad_bot_runner_152_it, which already
contained a finalized ISSUE152-FINAL source.
The corrected test connects to the disposable database and validates
its identity. Its PostgreSQL fixture also uses the actual
dds.instrument columns and 48 contiguous five-minute candles.
The current E2E test validates registration, source insertion,
entry update, baseline evaluation, finalization and repeated evaluation.
It does not prove exhaustive concurrency, crash recovery or
continuous runner operation.
The disposable database must be empty before the test.
Finalized rows are never removed through wildcard DELETE operations.
Database teardown must be performed separately by an authorized
operator, exclusively for trad_bot_runner_152_it_pg_test.
The temporary HBA permission used for the earlier successful
integration run was removed after testing.
No approved runtime semantics, freeze/activation guards,
migrations or immutable triggers were changed.
```

## Safety confirmation

- `/opt/trad_bot` was not modified; its HEAD remains
  `5f7a99a6cef4512c0791bc73edd3eb606bc5f485`.
- Production PostgreSQL `trad_bot` was not modified.
- Production scanner, paper, evaluator services and systemd were not modified.
- No production runner, capture, OOS activation, paper/live routing,
  historical backfill or OOS dataset mutation was performed.
- Approved fingerprint and freeze/activation values were not changed.
- Production remains `NO_GO`.

## Verdict
READY_FOR_REVIEW
PostgreSQL integration test: PASS.
Full V3 regression: PASS with integration skipped unless opted in.
This is not production authorization.
Production activation: NO_GO.
