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
  - Uses only the dedicated `trad_bot_runner_152_it` contour and temporary
    fixture rows with cleanup in `finally`.
  - Covers protocol idempotency, seven approved variants, pre-activation
    rejection, missing measured provenance rejection, source capture,
    entry-fill persistence, evaluator lifecycle, seven-outcome finalization
    and restart retry behavior.

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

The skipped integration test is intentional fail-closed behavior without
`ME_EARLY_MAE_V3_PG_INTEGRATION=true`.

## PostgreSQL E2E status: BLOCKED

The explicit integration test was run under the dedicated
`trad_bot_runner_152_it` role against a disposable
`trad_bot_runner_152_it_pg_test` database with migrations 068-070. Protocol
registration and variant counts passed, but the deterministic synthetic candle
fixture cannot produce a finalized timeout outcome with the approved runtime
semantics:

```text
approved BASELINE_MAX_HOLD_MINUTES = 240
240 x 5m candles = 14400 seconds
repository fetch uses close_time > entry_at
expected baseline window = 240 candles
retrieved deterministic window = 236 candles
result = INCOMPLETE_BASELINE_COVERAGE
```

The blocker is intentionally not worked around by changing approved runtime
semantics, migrations, freeze/activation values or guards. The failing
integration test is preserved for reproducible follow-up. Existing unit and
mock lifecycle tests remain green.

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

`READY_FOR_REVIEW` with PostgreSQL E2E explicitly marked `BLOCKED` by the
documented deterministic baseline-window mismatch. Regression and diff checks
are `PASS`.
