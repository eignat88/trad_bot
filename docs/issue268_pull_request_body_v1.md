# PR: Issue #268 — isolated production runtime for `ME_LONG_V2_INVERSE_SHORT_V1`

**Branch:** `feat/issue268-me-inverse-short-production-runtime`
**Base PR:** #267 (merge `537ad743a68a93fa7493c1483fec27e862bb7ee4`)
**Definition of done:** Production-ready, activation OFF, no backfill, no
trading side effects.

---

## 0. Review response (PR #269 review `5825354280`)

### Codex re-review round — three findings: **all FIXED (red→green)**

* **P1 — straddle candle could close the SHORT.** `_merge_bars` now
  requires the candle's WHOLE interval after the entry
  (`open_ms >= max(entered_at)` on the symbol): a candle that closed
  after the entry but OPENED before it (or carries no known `open_ms`) is
  inadmissible and withheld from ALL positions on that symbol — the
  engine falls back to the observed price only (conservative delay,
  no lookahead, no pre-entry extremes, no invented fills). Red→green:
  `test_straddling_candle_high_cannot_stop_out_the_short`,
  `test_straddling_candle_low_cannot_take_profit_the_short`,
  `test_unknown_bar_provenance_is_never_used_for_exits` (missing
  `open_ms`), `test_merge_admits_only_whole_intervals_after_the_entry`,
  `test_multiple_positions_symbol_clips_to_the_latest_entry`,
  `test_restart_does_not_change_bar_admissibility` — every one of them
  failed before the fix and passes after it.
* **P1 — quote stale after slow API requests.** `MarketDataProvider.
  snapshot()` now runs a RETURN-TIME freshness gate immediately before
  returning: every usable price/bar/funding observation is re-checked
  against the current UTC clock and excluded as `STALE` when it aged out
  during the cycle's later OHLC/funding requests (with
  `PRICE_MAX_AGE_S=30`, a quote followed by a 60 s kline request can
  never reach the engine as `OK`). Red→green: `TestReturnTimeFreshnessGate`
  (slow kline / slow funding / per-symbol observation times) and the
  runtime-level `TestSlowApiStalenessRuntime` (an aged quote blocks the
  new entry **without losing the pending event** and enters exactly once
  after recovery; an aged quote never fabricates a TP — management
  resumes on the first healthy cycle).
* **P2 — empty funding history must not become `0.0`.** The provider
  fetches funding through the SAME Bybit API layer (`_public_get` —
  budgeted, retried, limiter- and telemetry-aware) with the tri-state
  preserved: a row → `OK` (a genuine `0.0` included), an EMPTY list →
  `MISSING` (never a zero), a malformed row → `INVALID`, a failure →
  `API_ERROR`. `BybitClient.get_funding_rate()` is NOT modified (its
  historical contract still serves other callers). Red→green:
  `TestFundingRealClientContract` — against the REAL `BybitClient` the
  test proves `get_funding_rate() == 0.0` on an empty history while the
  provider reports `MISSING` and ships no rate — plus the end-to-end
  `TestFundingEmptyHistoryRuntime`: `funding_periods_charged` stays `0`
  while the history is empty, settles exactly once at the first real
  rate, and stays unchanged across a restart.

### R1 (P1) — pre-entry candle could close the SHORT: **CONFIRMED → FIXED (red→green)**

* **Defect (confirmed):** `_merge_bars` handed the last CLOSED bar to
  `check_exits` without entry-time clipping. A candle whose window ENDED
  before `entered_at` (11:55–12:00 candle, entry at 12:00:30) whose high
  breached the SHORT stop produced a **false `STOP_LOSS`** on the next
  cycle while the live quote was below the stop; a pre-entry low below the
  target produced a **false `TAKE_PROFIT_1`**.
* **RED before the fix** (`TestPreEntryBarClipping`, run against the
  pre-fix code):
  * `test_pre_entry_candle_high_cannot_stop_out_the_short` →
    `exit_reasons == ['STOP_LOSS']` (expected `[]`)
  * `test_pre_entry_candle_low_cannot_take_profit_the_short` →
    `['TAKE_PROFIT_1']` (expected `[]`)
  * `test_unknown_bar_provenance_is_never_used_for_exits` →
    `['STOP_LOSS']` (expected `[]`)
  * `test_merge_clips_pre_entry_and_unknown_provenance_bars` →
    `TypeError` (new explicit 3-arg clipping contract)
* **Fix:** bars now carry their provenance (`open_ms`/`close_ms` emitted by
  `MarketDataProvider`) and `ProductionRuntime._merge_bars(snapshot, prices,
  open_positions)` supplies a bar per symbol ONLY when
  `close_ms > max(entered_at of the open positions on that symbol)`;
  an unknown close time is treated as inadmissible (fail-safe). The engine
  then falls back to the observed price only — a conservative delay, never
  a fabricated fill, never a pre-entry extreme in the exit decision.
* **GREEN after the fix:** all 6 tests pass, including
  `test_post_entry_candle_still_stops_out` (protective STOP still works)
  and `test_reverted_forming_bar_touch_is_detected_when_bar_closes`
  (an intrabar touch inside the still-FORMING bar that already reverted
  is NOT reconstructed from the last quote — no lookahead, no invented
  fill — and fires when that candle closes: bounded ≤ 1-bar delay).

### R2 — PG integration executed on a real isolated database: **3 passed**

* Server: PostgreSQL **16.15** portable binaries (zonky
  `embedded-postgres-binaries-windows-amd64:16.15.0` from Maven Central),
  data dir `.tmp/pgdist/data2`, TCP `127.0.0.1:5433`, trust auth, started
  for this run only (no service installed, no production server touched).
* Exact commands:
  ```powershell
  # one-time provisioning (workspace-local, ephemeral)
  .tmp\pgdist\bin\initdb.exe -D .tmp\pgdist\data2 -U postgres -A trust -E UTF8 --locale=C
  .tmp\pgdist\bin\postgres.exe -D .tmp\pgdist\data2 -p 5433 -c listen_addresses=127.0.0.1 -c unix_socket_directories=
  # test database (created fresh; production name guarded)
  #   CREATE DATABASE trad_bot_migration_test
  $env:TEST_DB_HOST='127.0.0.1'; $env:TEST_DB_PORT='5433'
  $env:TEST_DB_NAME='trad_bot_migration_test'; $env:TEST_DB_USER='postgres'; $env:TEST_DB_PASSWORD=''
  .venv\Scripts\python.exe -m pytest tests/test_me_long_v2_inverse_short_production_runtime_pg_it_v1.py -v
  # → test_disabled_runtime_writes_nothing PASSED
  #   test_full_cycle_restart_and_exit_on_real_postgres PASSED
  #   test_duplicate_event_cannot_open_a_second_row PASSED          (3 passed in 1.21s)
  ```
* **Isolation proof:** `current_database()` = `trad_bot_migration_test`
  ≠ production (`trad_bot`); the suite's `_guard_not_production` skips if
  the database name ever equals `PRODUCTION_DB_NAME` (default `trad_bot`);
  migrations 085/086 are applied to the TEST database only, through the
  test connection itself (no psql binary required); teardown restores the
  activation row snapshot and deletes every row the suite created.
  The production database was never contacted by any test.

### R3 — matched baseline (same environment, `origin/main` = `537ad74`)

* **Method:** fresh shallow clone of `origin/main`, unshallowed to full
  history (a shallow clone made6 git-history-dependent tests fail — fixed
  before the final run), same venv, same pytest command (same 6 `--ignore`
  files for pre-existing Windows-only import errors), runs executed
  sequentially in their own trees.
* **Baseline (`537ad74`):** **72 failed, 6145 passed, 78 skipped,
  168 errors**.
* **Head (`170046c`, this PR):** **74 failed, 6232 passed, 81 skipped,
  168 errors**.
* **Failure-set diff:** baseline-only failures = **0**; head-only
  failures = **exactly 2**, both git-based branch-hygiene guards of the
  HTF Phase-2 line:
  * `test_htf_baseline_short_phase2_isolation_v1.py::test_new_files_are_confined_to_phase2_surface`
    — every NEW file vs merge-base must live under the HTF Phase-2
    surface;
  * `test_htf_baseline_short_phase2_systemd_bybit_wiring_v1.py::test_scanner_paper_evaluator_settings_unchanged`
    — no changed path may match `scanner|paper|live|evaluator|...`
    (this branch necessarily adds `app/paper/me_long_v2_inverse_short_v1/**`).
* **Control experiment:** both tests were re-run on the PR **#267**
  branch state (`c1e560a`, pre-Issue-#268, simulated pre-merge
  `origin/main`) in the same environment — **both FAIL there too**, with
  PR #267's own files as offenders (`app/paper/**`, `scanner_runner.py`,
  …). They are merge-base surface guards that fail on ANY feature branch
  touching those paths; they assert nothing about runtime behaviour and
  pass on a clean `main` (as the baseline run shows).
* Counts reconcile exactly: `6145 − 2 (hygiene flipped) + 89 (my tests:
  92 collected − 3 PG skips without TEST_DB) = 6232` and
  `78 + 3 = 81` skips; errors identical (168).
* Per-failure classification of the common72: `pg8000 InterfaceError:
  localhost:5432` (DB-dependent suites — no server in this environment),
  registry-content assertions on files untouched by this branch
  (`test_prospective_oos_v3`), `sql_quality_gate` internal failures
  (`test_analysis_runner`), and the documented baseline
  `test_paper_runner::test_paper_disable_uses_exact_canonical_identifier`.
  None of them imports or reads a file changed here.
* This branch's diff vs `origin/main` is **new files only for the first
  commit + the review-fix commit** (base tree hash `720726c…` ==
  `origin/main` tree hash; `git diff c1e560a HEAD --name-only` lists
  exactly the Issue #268 files; frozen PR #267 files untouched).

### R4 — BybitClient / limiter / config / events: **covered by tests**

* `TestBybitClientContract::test_shared_limiter_follows_the_resolved_settings_flag`
  — default-off builds NO limiter; resolved flag ON builds the
  process-shared limiter exactly once (same object on every call); the
  provider performs no HTTP of its own (AST test).
* `TestBybitClientContract::test_runtime_cycle_never_logs_credentials` —
  sentinel `db_password`/`bybit_api_secret` never appear in logs or in
  the telemetry payload.
* `TestApiOutage::test_source_event_delivered_during_outage_is_kept_and_enters_later`
  — source capture keeps working while the API is down; the event enters
  only after the feed recovers (fail-closed meanwhile).
* DRAINING restart, SIGTERM cycle completion and store-outage
  recovery/re-entry are covered by `TestDraining`,
  `TestGracefulShutdown`, `TestStoreOutage` (all re-run green).

### R5 — systemd unit / Python env / credentials

* The runbook §3 step 2 now states explicitly that the unit's
  `ExecStart` interpreter IS the release-local venv created there
  (`/opt/trad_bot_me_inverse_short_release/.venv/bin/python` +
  `requirements.txt`), so the unit and the documented install match.
* `DB_PASSWORD=CHANGE_ME` is labelled a PLACEHOLDER in the env example and
  the runbook (mode 0640, real value only on the server, never committed,
  never logged — asserted by test). The unit and env example keep
  `ME_INVERSE_SHORT_V1_ENABLED=0`.
* Python environment: CPython 3.13.17 venv; unit contract test verifies
  `User=tradbot`, separate release path, mandatory `EnvironmentFile`,
  `Restart=on-failure`, `RestartPreventExitStatus=3`, graceful
  `KillSignal=SIGTERM`, `ProtectSystem=strict` + `ReadWritePaths`.

---

## 1. Architecture (what was built)

A standalone, systemd-managed service around the EXISTING frozen PR #267
contour — no second Paper engine, no changes to any trading logic:

```
trad-bot-me-inverse-short-runtime.service  (systemd, User=tradbot, separate release)
  └── python -m app.paper.me_long_v2_inverse_short_v1.production_runtime
        ├── RuntimeInstanceLock          OS lock → exactly ONE process (exit code 3)
        ├── ensure_writable(log/var/lock) fail-fast (PermissionError lesson)
        ├── load_settings + MarketDataConfig.from_env()   fail-fast on bad values
        ├── connect_pg_store()           DEDICATED PostgreSQL connection (bounded retry)
        ├── BybitClient (retry budget + ApiTelemetry + GLOBAL rate limiter)
        │     └── MarketDataProvider     freshness / no-lookahead / honest triage
        └── ProductionRuntime.run()       loop + SIGTERM/SIGINT + telemetry + heartbeat
              └── InverseShortContour.run_cycle(prices, funding, bars)   ← PR #267, untouched
                    refresh → exits/outbox/funding → entries (FULL only) → MTM
```

**Per-cycle order (frozen, unchanged):** activation refresh →
recovery/reconciliation (engine recovery at start; `flush_pending_closes` +
dirty-funding retry first inside `check_exits`) → open-position exits →
funding settlement → prospective entries only in FULL mode → persistence →
telemetry/heartbeat.

**Event source:** only `dds.me_long_v2_inverse_short_v1_event` via the
existing independent pre-filter source capture in `scanner_runner.py`
(original `MOMENTUM_EXHAUSTION_REVERSE_LONG_V2` LONG signals → research
SHORT). The runtime never writes events, never reads `dds.scanner_setup`,
never backfills (`signal_time > freeze_ts` gate + migration-085 CHECK).

**Market data policy**

| Stream | Source (via `BybitClient`) | Max age (env) | On violation |
|---|---|---|---|
| Price | `lastPrice` from `/v5/market/tickers` | `…PRICE_MAX_AGE_S` = 30 s | excluded → entries fail closed, exits HOLD |
| OHLC | last **CLOSED** `/v5/market/kline` bar (5 m) | `…BAR_MAX_AGE_S` = 600 s | no bar → intrabar check uses only the observed price |
| Funding | `/v5/market/funding/history` (percent) | `…FUNDING_MAX_AGE_S` = 3600 s | rate UNKNOWN → obligation preserved, nothing booked |

* No lookahead: `open_ms + interval_ms <= now_ms` only; off-grid/malformed
  bars rejected; the bar range handed to the engine is the closed bar
  **widened by the live quote** (both observable now — a forming-bar touch
  is never missed, a future value is never used).
* Entry-time clipping (review P1): a candle that closed at/before a
  position's `entered_at` is never handed to `check_exits` (see §0 R1) —
  pre-entry extremes can neither stop nor take-profit a position, and a
  bar of unknown provenance is treated as inadmissible.
* Honest triage per (symbol, stream): `OK / DELAYED / STALE / MISSING /
  API_ERROR / INVALID` — stale/missing/invalid values are never substituted.
* One UTC clock; a backwards clock is never "fresh".
* No limiter bypass: the provider performs no HTTP itself.

**Exit codes:** `0` clean stop · `1` fatal (restart) · `2` usage ·
`3` another instance holds the lock (`RestartPreventExitStatus=3`).

## 2. Changed files (all NEW — no existing file modified)

| File | Purpose |
|---|---|
| `app/paper/me_long_v2_inverse_short_v1/production_runtime.py` | CLI entrypoint, instance lock, loop, signals, telemetry, heartbeat |
| `app/paper/me_long_v2_inverse_short_v1/market_data.py` | freshness/no-lookahead market data provider over `BybitClient` |
| `deploy/systemd/trad-bot-me-inverse-short-runtime.service` | unit template (User=tradbot, separate release, graceful stop) |
| `deploy/env/me-inverse-short-runtime.env.example` | predictable env file (`ME_INVERSE_SHORT_V1_ENABLED=0`) |
| `docs/me_inverse_short_v1_production_runtime_v1.md` | install / verify / stop / rollback / future-activation runbook |
| `tests/test_me_long_v2_inverse_short_market_data_v1.py` | provider contract (37+ tests) |
| `tests/test_me_long_v2_inverse_short_production_runtime_v1.py` | runtime contract + systemd unit contract |
| `tests/test_me_long_v2_inverse_short_production_runtime_pg_it_v1.py` | real-PostgreSQL integration (TEST_DB only, skip-guarded) |

**Reused, untouched:** `runner.py` (`InverseShortContour.run_cycle`),
`engine.py`, `store.py` (`connect_pg_store`), `activation.py`,
`capture.py`, `protocol.py`, `scanner_runner.py` capture path,
`BybitClient`/retry budget/API telemetry/global rate limiter, `Settings`.

## 3. Test results

| Suite | Result |
|---|---|
| `tests/test_me_long_v2_inverse_short_market_data_v1.py` | **51 passed** (incl. `TestReturnTimeFreshnessGate` + `TestFundingRealClientContract` red→green, Codex R2) |
| `tests/test_me_long_v2_inverse_short_production_runtime_v1.py` | **54 passed** (incl. `TestPreEntryBarClipping` straddle/restart/multi-position red→green, `TestSlowApiStalenessRuntime`, `TestFundingEmptyHistoryRuntime`, `TestBybitClientContract`, outage-capture) |
| `tests/test_me_long_v2_inverse_short_production_runtime_pg_it_v1.py` | **3 passed on a real isolated PostgreSQL 16.15** (`TEST_DB_*` → `trad_bot_migration_test`; commands + isolation proof in §0 R2) — NOT skipped, NOT mocked |
| PR #267 contour regression (v1, source capture, redelivery, ambiguous commit, close durability, funding/draining, PG reconnect, review2/review3, adoption costs) | **196 passed, 1 skipped** |
| Bybit client / retry budget / API telemetry / global rate limiter (R2 rerun: 224 passed) + candle-safety (8 pre-existing fixture failures, identical in baseline) | **412 passed, 3 skipped** (full-suite equivalent) |
| Scanner / Paper / Evaluator critical subset (R2 rerun) | head **16 failed, 306 passed, 15 errors** — byte-identical counts on the baseline clone (**16 / 306 / 15**): no-local-PostgreSQL + documented `test_paper_disable_uses_exact_canonical_identifier` |
| Full suite vs matched `origin/main` baseline (same env, full history, same 6 `--ignore` files) | baseline `537ad74`: **72 failed, 6145 passed, 78 skipped, 168 errors**; head (Codex R2 fixes): **74 failed, 6248 passed, 81 skipped, 168 errors** — baseline-only failures **0**, head-only = **2 HTF branch-hygiene git guards** (pre-existing on any feature branch incl. PR #267 — control experiment in §0 R3); the +103 passing tests are this PR's new suites (87 pre-R2 + 16 Codex-R2 red→green) |

Mandatory §5 coverage map (18 items) is documented in the docstring of
`tests/test_me_long_v2_inverse_short_production_runtime_v1.py`; items 11–13
are covered by the existing PR #267 fault-injection suites (re-run above).

## 4. Review checklist (Phase 4)

- [x] No lookahead — closed-bar filter, live-price widening, single UTC clock
- [x] No backfill — no history endpoints, capture gate + DB CHECK untouched
- [x] No Engine duplication — runtime only wraps `InverseShortContour` (static AST test)
- [x] No trading-gate changes — zero existing files modified (`git status`: new files only)
- [x] Fail-closed — two-factor activation, stale/missing excluded, DB errors block entries
- [x] Funding/accounting invariants — engine untouched; UNKNOWN rate preserves the obligation
- [x] Independent from production Paper — dedicated store, no `dds.paper_trade` writes (AST test + PG IT assertion)
- [x] Single instance — OS lock + `RestartPreventExitStatus=3`
- [x] Graceful SIGTERM — current cycle finishes, committed state kept (test)
- [x] Observability — all §3.7 fields per cycle + heartbeat file + refusal reasons

## 5. Risks and limitations

1. **No real-market run yet** — the provider is proven against scripted
   clients; first live cycles should be watched (`journalctl` telemetry)
   before trusting long unattended runs.
2. **Local-clock dependency** — freshness and closed-bar filters compare
   against the host UTC clock; a badly wrong clock delays/holds exits
   (fail-safe direction: no bogus fills, but protective exits may lag).
3. **`dds.me_long_v2_inverse_short_v1_event` still depends on the running
   Scanner** for new captures (existing design of the capture route). The
   runtime itself consumes only that table.
4. **Funding rate source** is the last settled rate without a per-row
   timestamp (client API shape); freshness therefore uses observation age.
5. **PostgreSQL integration** was executed on a workspace-local, ephemeral
   PostgreSQL 16.15 (§0 R2). The production server's version may differ —
   migrations 085/086 are idempotent and were applied only to the test
   database; re-run the suite against the deployment's test DB as part of
   the deployment checklist (command in runbook §7).
6. Synthetic dry-run PASS is **not** evidence of profitability (unchanged).

## 6. Deployment plan (no activation)

Detailed commands: `docs/me_inverse_short_v1_production_runtime_v1.md` §3–4.

1. Dedicated release checkout `/opt/trad_bot_me_inverse_short_release` + venv.
2. Create/verify writable dirs `logs/` and `var/` (owner `tradbot`, 0750)
   **before** first start (the runtime also probes them and refuses to start).
3. Install `/etc/trad-bot/me-inverse-short-runtime.env` with
   `ME_INVERSE_SHORT_V1_ENABLED=0` and DB credentials (mode 0640).
4. Confirm migrations 084/085/086 applied and the activation row is
   `READY_TO_START` (fingerprint/freeze NULL).
5. Install the unit, `daemon-reload`, `enable --now`; verify telemetry:
   `activation_mode=NONE`, `entries=0`, refusals `ACTIVATION:DISABLED_FLAG`,
   heartbeat file updating, zero rows in the contour tables.
6. **No systemd production changes are performed by this PR.**

## 7. Rollback plan

`systemctl disable --now trad-bot-me-inverse-short-runtime.service`, remove
the unit + `daemon-reload`; optionally remove release/env/runtime dirs.
State lives in PostgreSQL and is never deleted by rollback; the Scanner,
Paper and Evaluator services are untouched (separate units/processes).

## 8. Activation-off confirmation

* Production activation row **not** modified (stays `READY_TO_START`,
  `protocol_fingerprint = NULL`, `freeze_ts = NULL`).
* `ME_INVERSE_SHORT_V1_ENABLED` **not** set to `1` anywhere (unit and env
  example both keep `0`; asserted by tests).
* No freeze timestamp, no fingerprint write, no production systemd change,
  no real orders, no backfill, no writes to `dds.paper_trade`.

## 9. What a future clean prospective OOS start needs (separate approval)

1. Deploy this service with the flag at `0` and verify §6.
2. `scripts/me_inverse_short_v1_activate.py --activate --freeze-ts <UTC>`
   (fixes the CURRENT fingerprint + freeze atomically).
3. Set `ME_INVERSE_SHORT_V1_ENABLED=1` in the env file.
4. `systemctl restart trad-bot-me-inverse-short-runtime.service`.
5. Verify `activation_mode=FULL` and `signal_time > freeze_ts` on every event.
6. Later: `--drain` for RUNNING → DRAINING (exit-only, identity preserved).
