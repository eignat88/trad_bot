# BYBIT_SCANNER_STARTUP_BREAKER_OPEN_RECOVERY_V1 — Scanner startup breaker recovery

**Parent issue:** #253 · **Branch:** `fix/issue253-scanner-startup-breaker-recovery-v1`
**Base:** `main` @ `54201c0` (`fix(bybit): classify clock steps by wall-vs-mono skew, not idle gap (#253) (#255)`)
**Status:** review only — no merge, no deploy.

## 1. Problem

Production evidence (2026-11-03, release `54201c0`, PR #255, rollback to `ce51f2a4`):

* `scanner_universe.mode=dynamic`, `top_n=50`.
* `main()` performs `get_scanner_universe()` **before** constructing
  `ScannerRepository` and before `acquire_runner_lock()`.
* The first request, `GET /v5/market/instruments-info`, is denied by the
  SHARED limiter:
  `BREAKER_OPEN: breaker_cooldown_exceeds_deadline` (reason
  `breaker_cooldown_exceeds_deadline` = acquire wait budget already spent
  against a still-armed cooldown).
* `BybitClient` fails closed and raises `BybitRateLimitError`; the exception
  escapes `main()` → the process exits non-zero →
  `deploy/systemd/trad-bot-scanner.service` (`Restart=on-failure`,
  `RestartSec=10`) restarts the Scanner in a loop — and each restart
  immediately re-denies, because the shared cooldown outlives the restart.

The fail-closed deny itself was **correct** — no request left the gate. The
defect was only that a *temporary* shared-limiter deferral at process
startup was treated as fatal.

## 2. Fix

`scanner_runner.initialize_scanner_universe(client, settings)` now wraps the
startup universe fetch (still the very first Bybit interaction, still before
the database and the runner lock):

```
attempt → get_scanner_universe()
  ├─ success                → return the DISCOVERED universe
  ├─ temporary limiter deny → log warning → bounded polled wait → retry
  │        (SIGTERM/SIGINT during the wait → return None → main() exits
  │         cleanly with NO database / OOS / trading side effect)
  └─ any other error        → re-raised unchanged after the first attempt
```

`main()` then does:

```python
universe = initialize_scanner_universe(client, settings)
if universe is None:            # shutdown while waiting
    logger.info("... before database initialization")
    return
symbols = [str(item["symbol"]) for item in universe]
repository = ScannerRepository(...)
```

### 2.1 Startup invariant (unchanged, now enforced by a test)

`universe recovery → ScannerRepository(...) → ping() → acquire_runner_lock()
→ observers/OOS/spool → run_scan_cycle loop` — the per-cycle refresh inside
the loop keeps its historical place (after the lock) and its
keep-last-good-universe guard. `tests/…::test_main_orders_universe_recovery_before_database_and_lock`
pins the order from `main()`'s source; the behavioural tests prove no
repository/observer is constructed while a deny is still being waited out.

### 2.2 Temporary vs. permanent classification

Temporary (retried) — only denials a bounded wait can legitimately outlive,
matched on the exact fail-closed wordings `BybitClient` emits today:

| marker | why it is temporary |
|---|---|
| `deferred by shared rate limiter (BREAKER_OPEN` | breaker cooldown (5/30/120 s) expires by construction — **the incident** |
| `deferred by shared rate limiter (SKIPPED` | healthy bucket/attention queue did not refill inside *this* acquire's deadline |
| `limiter grant revoked before send` | another process tripped the breaker (fresh epoch); the new cooldown expires |
| `Rate limited on ` | the client's own rate-limit budget (HTTP 429 / breaker gate) refills |

Permanent (never retried, propagated unchanged → process exits exactly as
before this PR):

* `BybitAuthError` (bad key / permission), any other `BybitError`;
* `BybitRateLimitError: … shared limiter unavailable (…)` — the limiter could
  not even open its shared state (corrupt state, config mismatch, write/lock
  failure, bad `global_limiter_*` config) — a configuration problem a wait
  cannot fix;
* `deferred … (STATE_UNAVAILABLE: …)` — same reasoning, fail-closed;
* `RuntimeError: Dynamic scanner universe is empty` (thresholds) — never
  replaced with fabricated/stale symbols;
* **unknown future wordings** — fail-closed default: not retried.

The classification lives in `is_temporary_startup_limiter_denial()`; the
unit test `test_temporary_denial_classification_contract` pins both lists,
and the two end-to-end tests drive the REAL client deny path, so a wording
change in `BybitClient` breaks them loudly.

### 2.3 Bounded, configurable schedule (no busy loop)

| config key | default | meaning |
|---|---|---|
| `scanner_universe.startup_retry_interval_s` | `10.0` | first wait; doubles after every retry |
| `scanner_universe.startup_retry_max_interval_s` | `60.0` | backoff cap (`>= interval`) |
| `scanner_universe.startup_retry_max_attempts` | `0` | total attempts; `0` = keep waiting until SIGTERM/SIGINT |

* Every wait is polled in `STARTUP_RETRY_POLL_S = 1.0 s` chunks
  (`wait_before_startup_retry()`), each chunk a **positive** sleep → no
  busy-spin, and shutdown is observed within one chunk.
* A hard floor `STARTUP_RETRY_MIN_INTERVAL_S = 0.5 s` applies even to a
  directly-constructed `Settings`, so the loop can never degenerate into a
  hot retry loop.
* Validation happens in `load_settings()` at settings-load time — before any
  `BybitClient` exists (fail-closed, zero HTTP): non-positive interval,
  `max_interval < interval`, negative attempts.

### 2.4 Fail-closed guarantees preserved

* No code path bypasses the limiter: a retried attempt goes through the same
  `BybitClient` → `acquire → prepare_send → HTTP → record_result` gate; a
  denied attempt sends **zero** HTTP (asserted by the session fake).
* Nothing is persisted before a successful universe: no symbols, no
  repository, no OOS/observer, no checkpoint, no paper/live behaviour.
* `app/exchange/global_rate_limiter.py` — including the PR #255 wall-vs-
  monotonic clock-skew classification — is **untouched**.

## 3. Scope of changes

| file | change |
|---|---|
| `app/config/settings.py` | 3 new `ScannerUniverseSettings` fields + `load_settings()` validation |
| `scanner_runner.py` | `initialize_scanner_universe()`, `wait_before_startup_retry()`, `_startup_sleep()`, `is_temporary_startup_limiter_denial()`, `main()` startup wiring |
| `tests/test_issue253_scanner_startup_breaker_recovery_v1.py` | new — 21 deterministic regressions |
| `docs/issue253_scanner_startup_breaker_recovery_v1.md` | this document |

Untouched: `app/exchange/global_rate_limiter.py`, `app/exchange/bybit_client.py`,
scoring, regime filters, trading gates, paper/live paths, OOS freeze
timestamps, checkpoints, capture lineage, systemd/deploy files.

## 4. Tests (requirement 10 mapping)

| requirement | test(s) |
|---|---|
| `BREAKER_OPEN → wait → RECOVERY → successful universe initialization` | `test_breaker_open_waits_then_recovers_and_initializes_universe` (REAL client + REAL limiter tripped via public API; deny sends no HTTP, waits are positive, exactly 2 requests after recovery, discovered symbols returned) and `test_main_recovers_from_breaker_open_then_initializes_database` (same sequence through `main()`, DB constructed only after both HTTP calls) |
| repeated temporary denials without busy looping | `test_repeated_temporary_denials_wait_with_bounded_backoff` (waits exactly `1, 2, 4, 4, 4` s on injected sleeps), `test_backoff_never_exceeds_configured_max_interval` (cap `2 s` held across 9 attempts), `test_recovery_wait_is_poll_chunked_and_shutdown_aware` |
| graceful shutdown while waiting | `test_shutdown_signal_during_wait_returns_none_after_one_poll`, `test_shutdown_before_first_attempt_does_no_work`, `test_main_shutdown_during_recovery_leaves_no_side_effects` (main returns `None`; zero HTTP, zero repository, zero observer/OOS init) |
| permanent startup failure | `test_permanent_startup_failure_is_never_retried[auth/api_error/limiter_unavailable/state_unavailable/empty_universe]`, `test_temporary_denial_classification_contract` |
| no DB/OOS side effects before successful initialization | `test_main_shutdown_during_recovery_leaves_no_side_effects`, `test_main_recovers_from_breaker_open_then_initializes_database`, `test_main_orders_universe_recovery_before_database_and_lock` |
| config contract | `test_startup_retry_knobs_default_and_load`, `test_invalid_startup_retry_knobs_fail_closed[…]`, `test_static_universe_passes_through_without_any_client_call` |

## 5. Validation results

* New suite: `pytest tests/test_issue253_scanner_startup_breaker_recovery_v1.py`
  → **21 passed** (6.6 s; the only real sleeps are the two end-to-end
  recovery tests waiting out a 2 s breaker cooldown).
* Issue #253 limiter safety suites
  (`test_issue253_limiter_deny_propagation_v1.py`,
  `test_issue253_limiter_idle_vs_clock_jump_v1.py`,
  `test_issue253_requests_http429_v1.py`,
  `test_issue253_shared_limiter_alignment_v1.py`,
  `test_issue253_shared_limiter_mp_v1.py`) → **56 passed**.
* Scanner / config / limiter contract suites (`test_scanner_runtime.py`,
  `test_issue231_scanner_config_path_v1.py`,
  `test_db_transaction_recovery.py`,
  `test_issue231_r{2,3}_*_v1.py`,
  `test_issue231_strict_limiter_flag_v1.py`,
  `test_issue231_r2_multi_user_persistence_v1.py`) → **253 passed, 14 skipped**.
* Shared-limiter core (`test_global_bybit_rate_limiter_{v1,integration_v1,mp_v1}.py`)
  → **67 passed, 3 skipped** (clock-skew logic from PR #255 verified green).
* Full suite, branch vs. base **in the same environment, same command**
  (`pytest --continue-on-collection-errors --tb=no -rf`):

  | ref | failed | passed | skipped | errors |
  |---|---|---|---|---|
  | base `54201c0` (worktree) | 71 | 5490 | 75 | 173 |
  | this branch `0abe781` | 74 | 5508 | 75 | 173 |

  * the 173 errors are identical in both runs (PostgreSQL/pg8000 fixtures —
    no database in this environment), as are the 71 pre-existing failures
    (DB-fixture / Windows-only / environment areas);
  * all **21 new tests pass** in the full run;
  * the only delta is **3 additional failures**, all *Phase-2 scope gates*
    that compare the working tree against `origin/main` and are by design
    red for any PR outside the HTF Phase-2 surface:
    `test_htf_baseline_short_phase2_isolation_v1.py::test_no_tracked_file_modified_against_base`,
    `…::test_new_files_are_confined_to_phase2_surface` (both routed out of
    CI for a `NO_PHASE2` PR by `main-pr-safety.yml` — it runs only
    `test_htf_baseline_short_phase2_scope_routing_v1.py`, which is green)
    and
    `test_htf_baseline_short_phase2_systemd_bybit_wiring_v1.py::test_scanner_paper_evaluator_settings_unchanged`
    (not referenced by any workflow).

## 6. Compatibility

* **Config:** all three keys are optional; absent → defaults
  (`10.0 / 60.0 / 0`). Existing `config.yaml` files load byte-identically
  (`test_default_config_matches_direct_load_settings` still green).
* **Static universe:** `initialize_scanner_universe()` short-circuits into the
  historical path — no client call, no wait, identical result.
* **Other consumers** (Paper, Research, HTF, Analytics, reconcile): untouched;
  they never call `get_scanner_universe()`.
* **Per-cycle refresh:** unchanged (keep-last-good-universe on failure).
* **Failure semantics for permanent errors:** identical to the pre-PR
  behaviour (propagate → non-zero exit → systemd restart).

## 7. Rollback

1. Revert the merge commit (or redeploy the previous artifact `54201c0`);
   no schema, config-file or state-file format change is involved —
   `global_limiter_state.json` is not touched by this PR at all.
2. The three new config keys are inert under the old code (unknown keys are
   ignored by `Settings.__dataclass_fields__` filtering), so a config written
   for the new build still loads on rollback.
3. No deploy/merge is part of this PR; rollback of a future deploy is the
   ordinary artifact rollback (`ce51f2a4`-style) with no data migration.

## 8. Operational notes

* With the default `startup_retry_max_attempts = 0`, a Scanner facing a
  permanently OPEN breaker **stays alive and waits** (warning every attempt:
  `scanner startup universe deferred by the shared Bybit limiter (attempt
  N): … — retrying in Xs`) instead of crash-looping. Set
  `startup_retry_max_attempts` to bound that if process-level restart is
  preferred.
* `systemctl stop` / SIGINT during recovery exits within ≤ 1 s, before the
  database is touched.
