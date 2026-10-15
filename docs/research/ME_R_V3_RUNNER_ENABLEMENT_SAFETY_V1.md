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
  - Real `pg8000` integration test remains explicit opt-in via
    `ME_EARLY_MAE_V3_PG_INTEGRATION=true`.
  - Uses only the dedicated `trad_bot_runner_152_it` contour and disposable
    fixture rows with cleanup in `finally`.
  - TEST_ONLY synthetic baseline now uses exactly 48 sequential 5-minute
    candles: `open_at = entry + 5*i minutes`,
    `close_at = open_at + 5 minutes`.
  - Cleanup deletes only exact TEST_ONLY rows identified by
    `source_setup_id LIKE 'ISSUE152-%'` through the source FK,
    `source='ISSUE152_TEST_ONLY'`, and `symbol='ISSUE152TEST'`.
    Immutable triggers remain authoritative and are not bypassed.

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

## PostgreSQL E2E verdict: FAIL / BLOCKED

The final reproducible E2E run used a strictly identity-checked disposable
database:

```text
identity=trad_bot_runner_152_it_pg_test|1|0|0
rc_identity=0
```

The disposable database was rebuilt from migrations 068-070. The E2E failed
immediately after protocol registration:

```text
PROTOCOL_COUNT_BEFORE [1]
SOURCES_BEFORE ([39, 'ISSUE152-FINAL', 'FINALIZED'],)
ValueError: FINALIZED_SOURCE_IMMUTABLE
```

This is not the previously documented synthetic-candle mismatch. The repository
`register_protocol()` path leaves an already-finalized `ISSUE152-FINAL` source
in a clean isolated database before the test calls `update_source_entry()`.
The immutable trigger/repository lifecycle therefore prevents the intended
`DETECTED -> ENTRY_FILLED` path in the current implementation.

No approved protocol/runtime semantics, migrations, freeze guards or
immutable triggers were changed to force a PASS. The minimal failing evidence
is preserved by the committed integration test and final E2E output.

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

`READY_FOR_REVIEW` is allowed only with the open PostgreSQL E2E blocker.
PostgreSQL E2E verdict: `FAIL / BLOCKED`. Regression and diff checks are
`PASS`. This is not a production-ready status.
