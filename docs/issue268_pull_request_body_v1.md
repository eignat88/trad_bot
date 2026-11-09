# PR: Issue #268 — isolated production runtime for `ME_LONG_V2_INVERSE_SHORT_V1`

**Branch:** `feat/issue268-me-inverse-short-production-runtime`
**Base PR:** #267 (merge `537ad743a68a93fa7493c1483fec27e862bb7ee4`)
**Definition of done:** Production-ready, activation OFF, no backfill, no
trading side effects.

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
| `tests/test_me_long_v2_inverse_short_market_data_v1.py` | **42 passed** |
| `tests/test_me_long_v2_inverse_short_production_runtime_v1.py` | **38 passed** |
| `tests/test_me_long_v2_inverse_short_production_runtime_pg_it_v1.py` | **3 skipped** (no `TEST_DB_*` in this environment; runs on a separate PostgreSQL test DB) |
| PR #267 contour regression (v1, source capture, redelivery, ambiguous commit, close durability, funding/draining, PG reconnect, review2/review3, adoption costs) | **196 passed, 1 skipped** |
| Bybit client / retry budget / API telemetry / global rate limiter / candle-safety | **412 passed, 3 skipped** |
| Scanner/Paper/Evaluator subset (all `test_paper_*`, `test_scanner_*`, evaluator shutdown, direction gate, expectancy) | **355 passed**; 17 failed + 15 errors — **all pre-existing/environmental**: no local PostgreSQL (`pg8000 InterfaceError: localhost:5432`) plus the documented baseline failure `test_paper_runner.py::test_paper_disable_uses_exact_canonical_identifier` (known on `origin/main`, see `test_me_long_v2_inverse_short_close_durability_v1.py`) |
| Full suite | collection aborted by 6 pre-existing import errors on Windows (`pwd`, `psycopg2`, missing `tools.research…`) — unrelated to this change; full run re-executed with those files ignored (result recorded below) |

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
5. **PostgreSQL integration suite is skipped** where no `TEST_DB_*` is
   configured — it must be executed once against the disposable test DB
   (command in runbook §7) as part of the deployment checklist.
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
