# Issue #253 Stage A — Bybit API consumer / entrypoint inventory (v1)

**Scope:** every runtime entrypoint and consumer in this repository that can issue an
HTTP request to `https://api.bybit.com`, its configuration source and precedence, and
its effective shared-limiter (`GlobalBybitRateLimiter`) activation.

**Method:** grep-driven (`BybitClient(`, `api.bybit.com`, `requests.get`, `urlopen(`,
`get_klines`, `create_order(`, `load_settings(`, `"/v5/`) over `*.py` excluding
`tests/**`, plus `deploy/systemd/*`, `*.bat` and `app/scheduler/windows_tasks.py` for
deployment evidence. Every claim below carries a `file:line`. Anything I could not
verify is written as **UNKNOWN** together with what was checked.

---

## 0. Working-tree caveat (read this first)

The working tree is **not clean** and was being edited by another agent *while this
inventory was produced*. `git status --porcelain` at the time of writing:

```
 M app/config/settings.py                                    (+19: global_limiter_contract_path + apply_limiter_contract)
 M app/exchange/global_rate_limiter.py                       (+84: SHARED_SEMANTIC_FIELDS + LimiterConfig.fingerprint + config_fingerprint check)
 M app/research/me_r_long_early_mae_v3/isolated_oos.py       (BybitClient(Settings()) -> load_settings(repo config.yaml))
 M research_snapshot/htf_point_b_30d_fetch.py                (raw requests.get -> guarded_bybit_attempt wrapper)
 M research_snapshot/htf_point_b_warmup_fetch.py             (raw requests.get -> guarded_bybit_attempt wrapper)
 M tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py  (BybitClient(Settings()) -> load_settings(repo config.yaml))
 M tools/research/htf_keylevel_baseline_short_causal_replay_v1.py     (BybitClient(Settings()) -> load_settings(repo config.yaml))
 M tools/research/me_r_v3_universe_candidate_issue154.py     (BybitClient(Settings()) -> _settings() = load_settings(repo config.yaml))
?? app/config/limiter_contract.py                            (Issue #253 contract module, new)
?? app/exchange/offline_limiter.py                           (Issue #253 managed offline gate, new)
?? docs/issue253_bybit_api_quota_model_v1.md                 (new)
?? docs/issue253_oos_limiter_safety_analysis_v1.md           (new)
?? tests/test_issue253_shared_limiter_alignment_v1.py        (new)
```

`git log -1` = `ce51f2a perf(scanner): eliminate unused 4H kline fetch with parity guards (#251)`.
The tree kept changing while this document was being written: a later snapshot added
` M app/exchange/bybit_client.py` (+10: `deadline=min(deadline_at, t_start + shared.config.max_wait_s)`
in the shared-limiter acquire — Issue #253 `max_wait_s` contract), `?? limiter_contract.v1.json`
and `?? tests/test_issue253_shared_limiter_mp_v1.py`. **All `bybit_client.py` line numbers
in this document were re-verified against that final state.** Therefore:

* **Baseline** below = HEAD `ce51f2a` (the state the Stage-A question describes).
* **Current** = the working tree as of the last check in this document.
* Line numbers cited for *modified* files were re-verified after the last edit; line
  numbers for all other files are stable (they were not modified).

---

## 1. Shared-limiter mechanics (reference for the table)

| Fact | Evidence |
|---|---|
| Transport base URL | `app/exchange/bybit_client.py:403` (`BASE_URL = "https://api.bybit.com"`) |
| Default-off shared-limiter flag | `app/config/settings.py:134` (`global_bybit_rate_limiter_enabled: bool = False`) |
| Two-factor ENV gate (config file must grant `global_bybit_rate_limiter_allow_env`) | `app/config/settings.py:868-876` (parse), `:877-892` (ENV wins only when granted, otherwise warning + config value) |
| Gate applied per request inside the client | `app/exchange/bybit_client.py:437-459` (`_limiter_enabled`, strict bool), `:461-482` (`_shared_limiter`), `:663-825` (acquire `:665-677` → prepare_send → send → `record_result` in `finally` `:818-825`, fail-closed on deny) |
| State path source | `app/config/settings.py:154` (`global_limiter_state_path = "var/state/global_bybit_limiter_v1.json"`), anchored at the **repo root, never cwd** — `app/config/settings.py:688-703`; access-verified only when enabled — `:1032-1033` |
| Parameter contract (`LimiterConfig.from_settings`) | `app/exchange/global_rate_limiter.py:351` (strict parse before any client exists) |
| Retry owner | `app/exchange/bybit_client.py:540-585` (`_budgeted_get`), budget `:586-589`, `app/exchange/retry_budget.py:61` (`HARD_MAX_ATTEMPTS = 3`), "there is no independent retry layer above it — consumers must never wrap this call in their own retry loop" `bybit_client.py:549-550` |
| Process-local (NOT shared) limiter is always on | `bybit_client.py:863` (`limiter.acquire()` per attempt), rps read from a **built-in default** `Settings()` — `bybit_client.py:421-427`. This is the legacy per-process token bucket; it is *not* the Issue #231 shared limiter and its `Settings()` call is not a config bypass. |
| `POST /v5/order/create` | `bybit_client.py:1096-1101` (single non-retryable attempt) — **no production caller exists** (see §2 row "LiveExchange") |
| Config file that turns the flag on | **none in this repo**: grep `global_bybit_rate_limiter_enabled` over `*.{yaml,yml,json}` → no matches; `config.yaml` has no `global_*`/`limiter` key; `.env.example:29` has only a *commented* `GLOBAL_BYBIT_RATE_LIMITER_ENABLED=true`, and ENV is inert without `allow_env` in the config file |

---

## 2. Complete consumer / entrypoint table

One row per consumer/entrypoint. "Activation" is evaluated against the config source in
the previous column. Because no config file in this repository enables the flag
(§1 last row), **every config-sourced consumer is OFF today and only *config-dependent*
(ON-able by editing that specific file)**; only the two baseline raw-HTTP rows and the
four baseline `Settings()` rows are true BYPASS rows.

| # | Consumer / entrypoint | Exact entrypoint (file:line) | Config source + precedence | Effective limiter activation | state path source | Endpoints / methods used | Retry policy (layer + evidence) | Bypass risk | Impact if the request is rate-limited |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **Scanner runner** (service `trad-bot-scanner.service`, Windows task `BybitScanner`) | `scanner_runner.py:1462` `client = BybitClient(settings)` in `main()` | `_load_runner_settings()` `scanner_runner.py:189-230` → `resolve_scanner_config_path()` `:113-170`: `SCANNER_CONFIG_PATH` env (const `:54`, **absolute-only** `:138-143`, **no fallback** `:146-170`) else `<repo>/config.yaml` (`:31`,`:129`) → `load_settings(path=config_path, env_file=ENV_PATH)` `:201`; two-factor ENV gate inside `load_settings` `settings.py:868-892`. Deployed unit sets no `SCANNER_CONFIG_PATH` (`deploy/systemd/trad-bot-scanner.service`) | config-dependent — **OFF** today (no `global_*` key in `config.yaml`; canary file absent, see §4) | `settings.global_limiter_state_path` (`settings.py:154`), repo-root anchored `settings.py:688-703` | GET `/v5/market/kline` ×4 TFs (`app/scanners/context_builder.py:66-72`, submitted to a ThreadPool `scanner_runner.py:966`); GET `/v5/market/instruments-info` + `/v5/market/tickers` (universe `scanner_runner.py:250` → `bybit_client.py:1032,1045-1047,1065`); conditional GET `/v5/market/orderbook` + `/v5/market/instruments-info` (Phase-1 fill capture, env-gated `scanner_runner.py:438-454` → `app/research/me_short_a_fill_feasibility_v1.py:59-60,150,376`); GET `/v5/market/kline` via in-scanner OOS evaluator `scanner_runner.py:851-864` → `app/shadow/me_r_close_location_historical_source.py:60` | transport only: `_budgeted_get` `bybit_client.py:540`; no scanner-level retry loop; per-symbol exception caught + `save_error` `scanner_runner.py:1430-1438` | NO (client path; activation per config file) | That symbol's cycle error row + skipped prospective-OOS observation for the cycle (`scanner_runner.py:1401-1420`) → research observation gap; trading scan itself is fail-open |
| 2 | **Paper runner** (service `trad-bot-paper.service`, Windows task `BybitPaperRunner`) | `paper_runner.py:535` `client = BybitClient(settings)` | `_load_settings()` `paper_runner.py:243-244` = `<repo>/config.yaml` + `.env` (absolute, `:46-47`), **no env override** | config-dependent — **OFF** today | `settings.py:154` (repo-root anchored) | GET `/v5/market/tickers` `paper_runner.py:282`; GET `/v5/market/funding/history` `:299` — from both the slow cycle (`:603`) and the fast `PositionMonitor` thread (`:581-589`, 10 s default) | transport only; consumer catches and logs (`:278-291`, `:294-302`), no retry loop | NO | Empty/partial price dict → entry/exit checks skipped for that cycle (`:377`, `:263`); paper state is DB-driven, no corruption |
| 3 | **Research evaluator** (service `trad-bot-research-evaluator.service`) | `app/research/evaluator_runner.py:79` `client = BybitClient(settings)` | `--config` argparse default `"config.yaml"` `:47` → `load_settings(args.config)` `:69` (cwd-relative default); deployed unit passes no `--config`, `WorkingDirectory=/opt/trad_bot` (`deploy/systemd/trad-bot-research-evaluator.service`) | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/kline` 5 m: `app/research/evaluator.py:268`, `app/research/prospective_evaluator.py:207`; SRR historical route paginated kline `app/research/srr_short_execution_r_expansion_prospective_evaluator.py:416`; V3 source `app/research/me_reverse_long_v3/historical_source.py:55` | transport only; evaluators swallow fetch errors → `[]` (`evaluator.py:270-272`, `prospective_evaluator.py:209-211`), no consumer retry | NO | That signal/observation not evaluated this 300 s cycle; cycle is idempotent and re-run (`evaluator_runner.py:290-338`) → horizon maturity delayed, no outcome loss |
| 4 | **HTF Phase-2 V1 observer** (service `trad-bot-htf-baseline-short-observer.service`, flag-file gated) | `app/research/htf_baseline_short_phase2_observer_runner_v1.py:1122-1123` `BybitClient(load_settings())` | Only on `--candle-source bybit` (`:1107`), CLI default `csv` `:1145`; systemd passes `--candle-source bybit` and no `--config` → `load_settings()` **cwd-relative default** `settings.py:770` resolved via `WorkingDirectory=/opt/trad_bot`; daemon loop `run_loop` `:774-785` (`--poll-seconds 300`) | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/time` + GET `/v5/market/kline` paged: `app/research/htf_baseline_short_phase2_bybit_candle_source_v1.py:60-61,414,458` | transport budget per logical request ("no source-level retry loop" `:407-410`, `:445-446`) **plus** a bounded gap-recovery *re-request* loop with exponential backoff `:610-642` (budget = `max_gap_windows × gap_recovery_attempts`) — the only consumer-level re-request layer found anywhere; it re-fetches missing ranges, it is not a transport retry | NO | Writes checkpoint `/opt/trad_bot/var/htf_phase2/state.json` (systemd `ExecStart`); a fail-closed rate-limit becomes `BybitCandleSourceError` REASON_RATE_LIMITED quarantine (`:425-433`) → cycle not advanced, checkpoint stays resumable (research state preserved) |
| 5 | **ME SHORT A forward outcome runtime** (service `trad-bot-me-short-a-forward-outcome.service`, flag-file gated) | `app/research/me_short_a_forward_outcome_runtime_runner_v1.py:170` `BybitClient(load_settings())` via `_build_bybit_client()` `:157-170`, used at `:195-197` | `--candle-source bybit` is the CLI default `:265`; systemd passes it explicitly, no `--config` → `load_settings()` cwd-relative default `settings.py:770`; loop `:342-351` (`--poll-seconds 30`) | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/kline` (closed candles only) via `BybitClosedCandleSource` → `get_klines_range` `app/research/me_short_a_forward_outcome_runtime_sources_v1.py:391` → `bybit_client.py:917,966` | transport only; per-candidate/per-cycle exceptions logged and the same page is retried **next cycle** (`app/research/me_short_a_forward_outcome_runtime_v1.py:1206-1208,1273-1276,1292-1296`); no HTTP retry loop beyond `_budgeted_get` | NO | A denied fetch = `failed_symbols` for that cycle; the runtime is **strictly forward-only** (`me_short_a_forward_outcome_runtime_runner_v1.py:3`) with no backfill, so a missed candle window is a permanent gap in the forward-outcome dataset (systemd unit comment: "CLOSED 5m klines only") |
| 6 | **ME_R V3 isolated OOS collector** (timer `trad-bot-me-r-v3-oos.timer`, every 5 min, oneshot) | Baseline `app/research/me_r_long_early_mae_v3/isolated_oos.py:153` and `:220` `BybitClient(Settings())`; **current tree** `:162` and `:235` `BybitClient(load_settings(path=root / "config.yaml", env_file=root / ".env"))` (root = release root `parents[3]`) | Baseline: **built-in defaults, no config file ever read**. Current: release-root `config.yaml` + `.env`, two-factor gate applies | Baseline: **BYPASS — permanently OFF regardless of config**. Current: config-dependent — **OFF** today | `settings.py:154` (only reachable when enabled) | GET `/v5/market/kline` ×3 TFs `:95` (`collect_context`) | transport only; per-symbol exception → counted `failed_symbols`, loop continues `:196-204`; no retry loop | **YES at baseline** (hidden bypass, §3); NO after the working-tree edit | Writes the isolated OOS DB; the module states it "Does not backfill missing OOS candles after downtime: incompleteness remains visible" (`:4`) → a rate-limited window is a **permanent, visible gap** in the OOS dataset |
| 7 | **Outcome backfill CLI** (timer `trad-bot-outcome.timer` every 15 min; Windows task `BybitScannerOutcomeBackfill` via `run_outcome_backfill.bat:14`) | `app/scanners/outcome_cli.py:209` `client = BybitClient(settings)` | `load_settings()` `:199` (no `--config` arg; argparse `:186-196`) → cwd `config.yaml`; systemd/`bat` pass no config, `WorkingDirectory=/opt/trad_bot` | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/kline` `:58` (`fetch_outcome_candles`) | transport only; per-setup failure logged and counted `:174-182`; next timer run retries the setup | NO | Outcome rows for that setup stay pending → evaluated on the next 15-min run (no loss) |
| 8 | **Shadow scanner runner** (service `trad-bot-shadow-atr-wick.service`) | `app/shadow/runner.py:449` | `--config` default `"config.yaml"` `:407-411` → `load_settings(args.config)` `:429`; unit passes no `--config` | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/kline` 200 bars `:68` (`SHADOW_KLINE_LIMIT` `:32`) | transport only; `BybitRateLimitError` counted as a drop, not retried (`:49`, `:257`, `:271`; comment `:37`) | NO | That symbol's shadow signal row missing for the cycle (research only, DB row) |
| 9 | **Shadow evaluator** (timer `trad-bot-shadow-evaluator.timer`, 5 min, `--once`) | `app/shadow/evaluator.py:387` | `--config` default `"config.yaml"` `:366` → `load_settings(args.config)` `:376` | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/kline` `:186` | transport only; cycle errors counted `:390-402` | NO | Horizon evaluation for those signals deferred to the next timer run |
| 10 | **Shadow backfill** (manual one-shot) | `app/shadow/backfill.py:460` | `--config` default `"config.yaml"` `:401-406` → `load_settings(args.config)` `:440` (required `--start` `:378-383`) | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/kline` `:269` | transport only; no consumer loop | NO | Backfill window partially uncollected → re-run with `--start` (research data, rerunnable) |
| 11 | **V2D scanner runner** (service `trad-bot-v2d-scanner.service`) | `app/shadow/v2d_runner.py:412` | `--config` default `"config.yaml"` `:385` → `load_settings(args.config)` `:396` | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/kline` `:64` | transport only; rate-limit counted drop (`:48-50`, `:264-267`), no retry | NO | Prospective shadow cycle gap for that symbol (research only) |
| 12 | **V2D evaluator** (timer `trad-bot-v2d-evaluator.timer`, 5 min, `--once`) | `app/shadow/v2d_evaluator.py:295` | `--config` default `"config.yaml"` `:274` → `load_settings(args.config)` `:284` | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/kline` `:128` | transport only | NO | Outcome evaluation deferred to the next timer run |
| 13 | **SRR research evaluator** (timer `trad-bot-srr-evaluator.timer`, every 300 s, `--once`) | `app/shadow/srr_research_evaluator.py:455` | `--config` default `"config.yaml"` `:431` → `load_settings(args.config)` `:445`; deployed unit passes `--config /opt/trad_bot/config.yaml` **explicitly** (`deploy/systemd/trad-bot-srr-evaluator.service:8`) | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/kline` `:138` | transport only; daemon loop `:405-411` re-runs the cycle | NO | SRR outcome rows (research state) stay pending until the next 300 s run |
| 14 | **Analytics CLI** (timer `trad-bot-analytics.timer` 06:00 + `trad-bot-analytics-finalize.timer` 10:05) | `app/analytics/cli.py:253` `bybit_client = BybitClient(settings)` | Env-owned `Settings` dict `:64-126` **plus** limiter-contract fields copied from `load_settings()` `:128-143` → activation/state/G-B come from cwd `config.yaml` (unit `WorkingDirectory=/opt/trad_bot`); everything else from `/etc/trad-bot/analytics.env` (`deploy/systemd/trad-bot-analytics.service`) | config-dependent — **OFF** today (activation can *never* come from analytics' own env, only from `config.yaml` — `cli.py:140-143`) | `settings.py:154` (overlaid, `cli.py:140-143`) | GET `/v5/market/kline` at priority **P3** `app/analytics/candle_sync.py:252-261` | transport only — "this wrapper never retries" `candle_sync.py:233-242`; batch loop advances and records `failed_ranges` `:187-199` | NO (limiter fields overlay the shared contract) | Failed candle ranges recorded; the next analytics run reconciles gaps (`candle_sync.py:366-448`) → no permanent loss |
| 15 | **ME early-MAE V2 runner** (service `trad-bot-me-mae-v2-evaluator.service`) | no `BybitClient` — `app/research/me_r_long_early_mae_v2/runner.py:42` builds `MeEarlyMaeV2Evaluator(v2_repository)` | `--config` default `"config.yaml"` `:24` → `load_settings(args.config)` `:31` (DB only) | n/a (no HTTP) — would be config-dependent OFF if it ever fetched | n/a | **none** (grep for `bybit` / `Bybit` / `get_klines` / `requests` over the package → no matches; `repository.py` matches are only the `candle_source_version` column) | n/a | NO | n/a — DB-only evaluator |
| 16 | **ME_R V3 evaluator** (service `trad-bot-me-mae-v3-evaluator.service`, `--once`, condition-gated) | no `BybitClient` in `app/research/me_r_long_early_mae_v3/runner.py` (grep over the package: only `isolated_oos.py` constructs one) | UNKNOWN for config (module not read in full); no HTTP either way | n/a (no HTTP) | n/a | **none** | n/a | NO | n/a |
| 17 | **bot.py → app/main.py** (manual, one-shot) | `bot.py:6,10` → `app/main.py:18` `client = BybitClient(settings)` | `load_settings()` `:15` (`run_once`) and `:41` (`main`) → **cwd-relative** default `settings.py:770`; argparse has only `--check-config` `:39` | config-dependent — **OFF** today (and silently *built-in defaults* if cwd has no `config.yaml`, `settings.py:774-777`) | `settings.py:154` | GET `/v5/market/kline`, `/v5/market/open-interest`, `/v5/market/funding/history` via `MarketDataService` `app/market_data/market_data_service.py:22-24` (constructed `app/main.py:19`) | transport only; one pass over `settings.symbols`, no loop (`app/main.py:22-34`) | NO (but cwd-dependent config availability — see §3 residual risk) | That symbol's snapshot missing → printed as skipped/no signal; writes only local JSONL (`app/main.py:20`) |
| 18 | **Scanner CLI** (manual) | `app/scanners/cli.py:52` | `load_settings()` `:51` (no `--config`; argparse `:44-49`) → cwd `config.yaml` | config-dependent — **OFF** today (cwd-dependent) | `settings.py:154` | GET `/v5/market/tickers` + `/v5/market/instruments-info` for a dynamic universe `:57-62`; GET `/v5/market/kline` ×4 via `build_market_context` `:91` → `context_builder.py:66-72` | transport only; per-symbol `except` prints ERROR `:104-108` | NO (cwd-dependent) | That symbol's setups not saved for this manual run (rerunnable) |
| 19 | **Paper CLI** (manual: `status/trades/stats/run-once`) | `app/paper/cli.py:254` (`cmd_run_once`) | `_load_settings()` `:31-34` = **repo-root** `config.yaml` + `.env` (`Path(__file__).resolve().parent.parent.parent`) — absolute, no env override | config-dependent — **OFF** today | `settings.py:154` | GET `/v5/market/tickers` `:68`, GET `/v5/market/funding/history` `:82` (`build_market_context` is imported `:23` but never called — grep shows only the import) | transport only; per-call `except` `:83-84` | NO | `run-once` skips exit/entry checks for that invocation |
| 20 | **Order-submit reconcile tool** (manual) | `tools/bybit_order_submit_reconcile_v1.py:125` `BybitClient(settings).reconcile_order_submissions()` | `--config` default `"config.yaml"` `:86`; `--reconcile` **requires** the config and never falls back to `Settings()` (`:44-64`, `:115`, message `:60-63`); offline modes load it only when present | config-dependent — **OFF** today | `settings.py:154` | **Signed private** GET `/v5/order/realtime` then `/v5/order/history` `bybit_client.py:1524-1537` (fn `:1568`) | transport only (`_private_get` → `_budgeted_get`), no tool-level retry; auth errors fail closed (`BybitAuthError` `bybit_client.py:32-40`) | NO | An unresolved submission stays blocked → exit code 2 (fail-closed by design; a timer may never substitute for verification — `:22-24`) |
| 21 | **HTF Phase-2 Bybit live dry-run** (manual research) | baseline `tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py:395` `CountingClient(BybitClient(Settings()))`; current `:400` `CountingClient(BybitClient(load_settings(path=_root/"config.yaml", env_file=_root/".env")))` (uncommitted Stage-B edit) | Baseline: **built-in defaults** (argparse has no `--config`, `:348-378`). Current: repo-root `config.yaml` + `.env` | Baseline: **BYPASS — permanently OFF**. Current: config-dependent — **OFF** | `settings.py:154` | GET `/v5/market/time` `:439` + paged GET `/v5/market/kline` via `BybitPhase2CandleSource` `:440-448` | transport budget + source gap-recovery loop (`app/research/htf_baseline_short_phase2_bybit_candle_source_v1.py:610-642`) | **YES at baseline**; NO after edit | Writes a throwaway checkpoint + report JSON (`:415-416`, `:376`) — research artifact only, rerunnable |
| 22 | **HTF key-level causal replay** (manual research) | baseline `tools/research/htf_keylevel_baseline_short_causal_replay_v1.py:165` `BybitHistoricalCandleSource(BybitClient(Settings()))`; current `:169` with `load_settings(path=_root/"config.yaml", ...)` (uncommitted) | Baseline: **built-in defaults** (argparse `:149-154` has no `--config`). Current: repo-root `config.yaml` | Baseline: **BYPASS — permanently OFF**. Current: config-dependent — **OFF** | `settings.py:154` | paged GET `/v5/market/kline` via `BybitHistoricalCandleSource` (`srr_short_execution_r_expansion_prospective_evaluator.py:416`) | transport budget + bounded pagination loop `:414-445` (not a retry) | **YES at baseline**; NO after edit | Run aborts (`FETCH_BLOCKED` `:167-168`); output restricted to `/tmp/` `:173-175` — no persistent research state |
| 23 | **ME_R V3 universe candidate snapshot** (manual research) | baseline `tools/research/me_r_v3_universe_candidate_issue154.py:25`; current `:35` `BybitClient(_settings())` with `_settings()` `:31` (uncommitted) | Baseline: **built-in defaults**. Current: repo-root `config.yaml` + `.env` (`_ROOT = parents[2]` `:26`) | Baseline: **BYPASS — permanently OFF**. Current: config-dependent — **OFF** | `settings.py:154` | GET `/v5/market/instruments-info` + `/v5/market/tickers` (via `get_liquid_instruments` `bybit_client.py:1032,1045-1047,1065`) | transport budget only | **YES at baseline**; NO after edit | `CANDIDATE_UNIVERSE_NOT_COMPLETE` abort `:31-32`; stdout JSON only, no persisted state |
| 24 | **ME_RL V1 bybit candle download** (manual research) | `tools/research/me_rl_v1_bybit_candle_download_v1.py:85` calls `bybit_historical_klines(...)` | imports it from `tools.research.srr_short_r_expansion_candle_path_integrity_audit_v1` `:11-13` | **that module does not exist in this checkout** (`Test-Path` → `False`; `tools/research` contains 49 files, none of that name; only `tests/test_srr_short_r_expansion_candle_path_integrity_audit_v1.py` exists) → **import fails, no HTTP can be issued** | UNKNOWN (module missing — checked glob + `Test-Path`) | would be `/v5/market/kline` if the module existed | UNKNOWN (implementation not present) | NO (cannot run) | n/a today; if restored on another branch, treat as offline bulk fetch → managed-path candidate |
| 25 | **research_snapshot/htf_point_b_30d_fetch.py** (manual one-shot) | baseline raw `research_snapshot/htf_point_b_30d_fetch.py:76` `requests.get(URL, ...)`; current `:86-87` wrapped in `guarded_bybit_attempt("/v5/market/kline")` (uncommitted) | Baseline: **no config at all**. Current: `app/exchange/offline_limiter.py:136-140` → `load_settings()` (**cwd-relative** default) | Baseline: **BYPASS — raw HTTP, no limiter, no client**. Current: config-dependent — **OFF** today; managed path P3 (`offline_limiter.py:40`) | Baseline: none. Current: `settings.py:154` via `load_settings()` | GET `https://api.bybit.com/v5/market/kline` `:25` (public, no auth) | **no retry**: one attempt per page, `raise_for_status`, `retCode` → `RuntimeError`; per-symbol checkpoint `next_end_ms`/`completed` `:26-38` resumes the dataset | **YES at baseline** (raw HTTP bypass); NO after edit (managed `guarded_bybit_attempt`) | Per-symbol `FAILED` checkpoint keeps the historical dataset resumable; no partial page marked complete (see the Issue-#253 comment block now at `:14-21`) |
| 26 | **research_snapshot/htf_point_b_warmup_fetch.py** (manual one-shot) | baseline raw `:44` `requests.get(URL, ...)`; current `:54-55` wrapped in `guarded_bybit_attempt` (uncommitted) | same as row 25 (offline_limiter `load_settings()` cwd default) | Baseline: **BYPASS — raw HTTP**. Current: config-dependent — **OFF**, P3 managed | Baseline none / current `settings.py:154` | GET `https://api.bybit.com/v5/market/kline` `:23` | **no retry**; page loop with `sleep(0.05)`; run raises `WARMUP_INVALID` if incomplete (baseline `:174-175`) | **YES at baseline**; NO after edit | Incomplete warmup aborts the run (hardcoded `OUT` under `D:\py_pro\trad_bot` `:11` — portability UNKNOWN outside that machine) |
| 27 | **scripts/verify_oos_deployment.py** (manual verification) | **no entrypoint** — `load_settings()` `scripts/verify_oos_deployment.py:134` only | cwd default `config.yaml` (assertions on `blocked_scanner_directions`, execution policies) | n/a — **not a consumer** (grep for `BybitClient` / `get_klines` / `_public_get` / `client` over the file → no matches) | n/a | **none** | n/a | NO | n/a |
| 28 | **app/research/htf_point_b_observation_recovery.py** (manual recovery) | no `BybitClient`; DB-only `_load_connection()` `:339-342` uses `load_settings(path=root/"config.yaml", env_file=root/".env")` (`root = parents[2]`) | repo-root `config.yaml` (only when `--host` is not given `:323-335`) | n/a — **not a consumer** (grep for `Bybit` / `bybit` / `get_klines` / `requests` → no matches) | n/a | **none** | n/a | NO | n/a |
| 29 | **app/scanners/context_builder.py** (library) | no construction; takes `client: BybitClient` `:60-63` | injected — callers decide: `scanner_runner.py:966`, `app/scanners/cli.py:91` | inherits caller's activation (OFF today) | caller's `settings.py:154` | GET `/v5/market/kline` ×4 (`:66-72`; the 4H fetch was deliberately removed, `:69-71`) | transport only | NO (no own config) | n/a (raises → caller handles) |
| 30 | **app/market_data/market_data_service.py** (library) | `:12` `def __init__(self, client: BybitClient, ...)`; only caller `app/main.py:19` | injected | inherits caller (OFF today) | caller's | GET kline `:22`, `/v5/market/open-interest` `:23`, `/v5/market/funding/history` `:24` | transport only | NO | n/a |
| 31 | **app/research/evaluator.py** (library) | `:252` `def __init__(self, conn, client: BybitClient)`; constructed `evaluator_runner.py:80` | injected (evaluator_runner's config) | inherits `evaluator_runner` (OFF today) | `settings.py:154` | GET `/v5/market/kline` `:268` | transport only; fetch errors swallowed → `[]` `:270-272` | NO | evaluation deferred a cycle |
| 32 | **app/research/prospective_evaluator.py** (library) | `:136` ctor; constructed `evaluator_runner.py:131-134` | injected | inherits `evaluator_runner` (OFF today) | `settings.py:154` | GET `/v5/market/kline` `:207` | transport only; swallowed `:209-211` | NO | evaluation deferred a cycle |
| 33 | **SRR prospective evaluator + `BybitHistoricalCandleSource`** (library) | `srr_short_execution_r_expansion_prospective_evaluator.py:380,416`; constructed `evaluator_runner.py:123-129` | injected | inherits `evaluator_runner` (OFF today) | `settings.py:154` | paged GET `/v5/market/kline` `:416-423` | transport budget + pagination loop `:414-445` (capped 100 pages, not a retry); source errors → `SOURCE_FETCH_ERROR` `:446-452` | NO | SRR route reports fetch error for that symbol, evaluation skipped that cycle |
| 34 | **HTF Phase-2 candle source** (library) | `app/research/htf_baseline_short_phase2_bybit_candle_source_v1.py:414,458`; constructed at `observer_runner_v1.py:1122` and `tools/.../dry_run_v1.py:440` | injected | inherits (OFF today) | `settings.py:154` | GET `/v5/market/time` `:414`, GET `/v5/market/kline` `:458` | "no source-level retry loop" `:407-410`,`:445-446` + bounded gap-recovery re-requests `:610-642` | NO | quarantine + checkpoint not advanced (row 4) |
| 35 | **ME SHORT A `BybitClosedCandleSource`** (library) | `app/research/me_short_a_forward_outcome_runtime_sources_v1.py:391`; constructed `runtime_runner_v1.py:196-197` | injected | inherits (OFF today) | `settings.py:154` | GET `/v5/market/kline` via `get_klines_range` | transport only | NO | forward-only gap (row 5) |
| 36 | **ME SHORT A fill-feasibility capture** (library, env-gated) | `app/research/me_short_a_fill_feasibility_v1.py:150,376`; called from `scanner_runner.py:443-454` **only when** `ME_SHORT_A_FILL_CAPTURE_ENABLED=1` | injected (scanner's settings) | inherits scanner (OFF today) | `settings.py:154` | GET `/v5/market/instruments-info` `:150` (constants `:59-60`), GET `/v5/market/orderbook` `:376` | transport only; both wrapped in `try/except` (fail-open for the scanner) `:168-169`, `:382-395` | NO | Phase-1 feasibility row records `ERROR` for that symbol (research row, no scanner impact) |
| 37 | **`app/research/candle_coverage_guard_v1.py`** (library, unwired) | `:518` (`get_klines_range`), `:554` (`get_klines`) inside `fetch_candles_for_evaluation` `:443` | no callers: grep `candle_coverage_guard_v1` across the repo → **no matches**; the file is **untracked** | UNKNOWN (no entrypoint exists) | UNKNOWN | GET `/v5/market/kline` (both call forms) | docstring says any exception (incl. "retry-budget exhaustion, shared limiter deny") is fail-closed `:464` | UNKNOWN — cannot be a bypass while nothing imports it | n/a until wired |
| 38 | **Shadow close-location OOS evaluator** (library) | `app/shadow/me_r_long_close_location_oos_evaluator.py:35-41` (takes `client`); only construction `scanner_runner.py:861` | injected (scanner's settings) | inherits scanner (OFF today) | `settings.py:154` | GET `/v5/market/kline` via `me_r_close_location_historical_source.py:60` | transport budget + **pagination loop** `:54-68` (not a retry) | NO | OOS evaluation for those pending signals skipped this scanner cycle |
| 39 | **`app/execution/live_exchange.py`** (exists; never wired) | `:27-33` takes an injected `BybitClient`; constructed **only in tests** (`tests/test_live_gate.py:38,44`) | n/a — no production construction site (grep `LiveExchange(` → tests only) | n/a | n/a | **none**: `submit()` always calls `self._block(...)` `:62-77` and never reaches `create_order`; grep `create_order(` outside `bybit_client.py` → **tests only** | n/a | NO | **No production consumer issues `POST /v5/order/create` today.** |
| 40 | **`bot.py`** (manual launcher) | `bot.py:6,10` → `app.main.main()` | see row 17 | see row 17 | see row 17 | see row 17 | see row 17 | NO | see row 17 |
| 41 | **`app/analytics/candle_sync.py`** (library) | `:107` ctor, `:252` `_public_get` call; constructed `analytics/cli.py:253` | injected | inherits analytics (OFF today) | `settings.py:154` | GET `/v5/market/kline` P3 `:252-261` | "this wrapper never retries" `:233-242`; failures → `failed_ranges` `:187-199` | NO | gap recorded, reconciled next run (row 14) |

### 2b. Non-Bybit HTTP checked and excluded

| File | Evidence | Verdict |
|---|---|---|
| `app/notifications/telegram.py` | `:16` `urlopen(Request(f"https://api.telegram.org/..."))` | Telegram, not Bybit — excluded |
| `app/analytics/agents/llm_provider.py` | `:3,:100-101` `httpx` → OpenAI-compatible endpoint | LLM API, not Bybit — excluded |
| `app/backtest/*` | grep for `Bybit`, `get_klines`, `client` → no matches | offline, no Bybit |
| `app/scanners/expectancy_report.py:38`, `app/scanners/fvg_parity_audit.py:284` | `load_settings()` only, no `BybitClient` | config checks only |
| `tools/research/htf_point_b_paired_report_v1.py:166`, `.../htf_point_b_final_paired_snapshot_and_report_v1.py:450` | `load_settings(...)` used only to build `ScannerRepository` (`:167`, `:452-453`) | DB only, no Bybit |
| `tests/**` | excluded by task scope | — |

---

## 3. Hidden bypasses

### 3a. Baseline (HEAD `ce51f2a`) — every bypass found

1. **`app/research/me_r_long_early_mae_v3/isolated_oos.py:153`** — `client = BybitClient(Settings())`
   (and the preflight twin at **`:220`**). Built-in defaults ⇒ `global_bybit_rate_limiter_enabled`
   is the dataclass default `False` (`settings.py:134`), so the shared limiter is
   **permanently OFF regardless of `config.yaml`**, and no config file is ever read.
   This one matters most: the module is deployed on a **5-minute timer**
   (`deploy/systemd/trad-bot-me-r-v3-oos.timer` → `isolated_oos --once`).
2. **`tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py:395`** —
   `CountingClient(BybitClient(Settings()))` (default `client_factory`, `:390-395`).
3. **`tools/research/htf_keylevel_baseline_short_causal_replay_v1.py:165`** —
   `BybitHistoricalCandleSource(BybitClient(Settings()))`.
4. **`tools/research/me_r_v3_universe_candidate_issue154.py:25`** —
   `BybitClient(Settings()).get_liquid_instruments(...)`.
5. **`research_snapshot/htf_point_b_30d_fetch.py:76`** — raw `requests.get("https://api.bybit.com/v5/market/kline", ...)`
   with no client, no config, no limiter and no retry budget.
6. **`research_snapshot/htf_point_b_warmup_fetch.py:44`** — same raw `requests.get`.

Verification: `grep BybitClient\(Settings\(\)\)` over `*.py` (excluding tests) returned
exactly those four construction sites at baseline; `grep api\.bybit\.com` returned
exactly those two scripts plus `bybit_client.py:403`; `grep requests\.(get|post)` returned
exactly those two scripts (plus `bybit_client.py`'s own `urlopen` transport
`:389-399`).

**Current working-tree status:** all six have already been rewritten by an in-flight
Stage-B edit while this inventory was being written (uncommitted diffs; see §0). After
those edits `grep BybitClient\(Settings\(\)\)` → **0 matches**. Whether those edits are
the intended Stage-B deliverable is for the parent to confirm; this document records
both states.

### 3b. Residual bypass risks that remain even after those edits

* **`app/exchange/offline_limiter.py:136-140`** — when no `settings` is passed it calls
  `load_settings()` with the **cwd-relative** default `config.yaml`. If the utility is
  started from a directory without `config.yaml`, `load_settings` silently proceeds with
  **built-in defaults** (`settings.py:774-777` — `raw = {}` when the path does not exist)
  ⇒ limiter OFF + empty API keys, with no error. This is the same silent-fallback class
  that Issue 231 R3 removed from `tools/bybit_order_submit_reconcile_v1.py:44-64`
  (which now fails closed with `FileNotFoundError`). Same caveat applies to every
  cwd-relative `load_settings()` caller: `app/main.py:15,41`, `app/scanners/cli.py:51`,
  `app/scanners/outcome_cli.py:199`, `app/research/evaluator_runner.py:69`,
  `app/research/htf_baseline_short_phase2_observer_runner_v1.py:1123`,
  `app/research/me_short_a_forward_outcome_runtime_runner_v1.py:170`,
  `app/shadow/{runner,evaluator,backfill,v2d_runner,v2d_evaluator,srr_research_evaluator}.py`
  (`--config` default `"config.yaml"`), `app/analytics/cli.py:139`,
  `scripts/verify_oos_deployment.py:134`, and the two `research_snapshot` scripts via
  `offline_limiter`.
* **`research_snapshot/htf_point_b_30d_fetch.py:11`** still hard-codes
  `sys.path.insert(0, r"D:\py_pro\trad_bot")` (and `:14` a `D:\...` output dir), so on any
  other host the script cannot even import `app.*` — portability **UNKNOWN**.
* **`bybit_client.py:421-427`** builds the *process-local* `_RateLimiter` from a
  bare `Settings()` — that is only the legacy 10 rps in-process bucket (`:853`), not the
  shared limiter; documented here so it is not mistaken for a bypass.
* `app/config/limiter_contract.py` (new, untracked) explicitly forbids the contract from
  carrying activation keys (`FORBIDDEN_CONTRACT_KEYS` `:73-77`), so parameter alignment
  can never silently switch a consumer on — activation stays per-config-file.

---

## 4. Config source matrix — the five claimed production services

Claim under test: *Scanner (scanner_canary.json via `SCANNER_CONFIG_PATH`), Research
evaluator (config.yaml), HTF Phase-2 V1 observer (`load_settings()`), SRR evaluator
(config.yaml), Paper (config.yaml).*

| Service | Claimed config source | Verdict | Evidence |
|---|---|---|---|
| **Scanner** | `scanner_canary.json` via `SCANNER_CONFIG_PATH` | **Mechanism CONFIRMED, deployment claim CORRECTED** | Mechanism: env const `scanner_runner.py:54`; resolution + absolute-only + no-fallback `:113-170`; consumed by `_load_runner_settings()` `:189-230` → `load_settings(path=config_path, env_file=ENV_PATH)` `:201`. **Correction:** no `scanner_canary.json` exists in this repo (glob `**/*canary*` → only `docs/issue231_scanner_isolated_canary_config_v1.md` and tests), and the deployed unit `deploy/systemd/trad-bot-scanner.service` sets **no** `SCANNER_CONFIG_PATH`, so the effective source today is `<repo>/config.yaml` (`scanner_runner.py:129`). The canary is documented as an *operator* step (`docs/issue231_scanner_isolated_canary_config_v1.md:123,148`) — whether production sets it is **UNKNOWN** (not in this repo). |
| **Research evaluator** | `config.yaml` | **CONFIRMED** | `--config` default `"config.yaml"` `app/research/evaluator_runner.py:47` → `load_settings(args.config)` `:69` → `BybitClient(settings)` `:79`; deployed unit passes no `--config` and runs with `WorkingDirectory=/opt/trad_bot` (`deploy/systemd/trad-bot-research-evaluator.service`). Note: the default is **cwd-relative**, not repo-anchored. |
| **HTF Phase-2 V1 observer** | `load_settings()` | **CONFIRMED (with two clarifications)** | `BybitClient(load_settings())` `app/research/htf_baseline_short_phase2_observer_runner_v1.py:1122-1123`. (1) It is reached **only** on `--candle-source bybit` (`:1107`), whose CLI default is `csv` `:1145` — the systemd unit passes `--candle-source bybit` explicitly. (2) `load_settings()` uses the **cwd-relative** default `config.yaml` (`settings.py:770`); the unit's `WorkingDirectory=/opt/trad_bot` is what makes it resolve `/opt/trad_bot/config.yaml`. |
| **SRR evaluator** | `config.yaml` | **CONFIRMED (twice over)** | `app/shadow/srr_research_evaluator.py:431` (`--config` default) → `:445` → `BybitClient` `:455`; the deployed unit passes `--config /opt/trad_bot/config.yaml` **explicitly** (`deploy/systemd/trad-bot-srr-evaluator.service:8`). A second SRR route (the SRR prospective evaluator inside the research evaluator) shares the research evaluator's config: `evaluator_runner.py:69,123-129`. |
| **Paper** | `config.yaml` | **CONFIRMED** | `paper_runner.py:46-47` (`CONFIG_PATH = PROJECT_ROOT / "config.yaml"`, absolute) → `_load_settings()` `:243-244` → `BybitClient` `:535`. The manual `app/paper/cli.py:31-34` resolves the same repo-root `config.yaml` independently. |

**Cross-cutting correction:** *no service is currently ON.* Activation for all five is
"config-dependent, **OFF today**", because (a) no config file in this repository carries
`global_bybit_rate_limiter_enabled` (grep over `*.{yaml,yml,json}` → no matches) and
(b) the ENV factor is inert without `global_bybit_rate_limiter_allow_env: true` in the
config file (`settings.py:868-892`; `.env.example:29` ships the ENV var commented out).
The `limiter_contract.py` module docstring separately asserts that activation drift was
"confirmed on `trad-bot-01`" (`app/config/limiter_contract.py:14-15`) — that host-level
state is **UNKNOWN** from this checkout.

---

## 5. Runtime vs offline classification

### 5a. RUNTIME — long-running services / scheduled timers (must be aligned)

| Consumer | Deployment evidence | Bybit HTTP? |
|---|---|---|
| `scanner_runner.py` | `deploy/systemd/trad-bot-scanner.service`; Windows `BybitScanner` (`app/scheduler/windows_tasks.py:7`), loop `scanner_runner.py:1568` | YES (kline, tickers, instruments, orderbook) |
| `paper_runner.py` | `deploy/systemd/trad-bot-paper.service`; Windows `BybitPaperRunner` (`windows_tasks.py:8`), loop `paper_runner.py:603` + monitor thread `:582-589` | YES (tickers, funding) |
| `app.research.evaluator_runner` | `trad-bot-research-evaluator.service`, daemon loop `evaluator_runner.py:290-338` | YES (kline, via 4 injected sources) |
| `app.research.htf_baseline_short_phase2_observer_runner_v1` | `trad-bot-htf-baseline-short-observer.service` (flag-file gated), loop `:774-785` | YES only with `--candle-source bybit` (as deployed) |
| `app.research.me_short_a_forward_outcome_runtime_runner_v1` | `trad-bot-me-short-a-forward-outcome.service` (flag-file gated), loop `:342-351` | YES (kline) |
| `app.research.me_r_long_early_mae_v3.isolated_oos` | **`trad-bot-me-r-v3-oos.timer` every 5 min** (oneshot per firing) | YES (kline ×3) — was a `Settings()` bypass at baseline |
| `app.scanners.outcome_cli` | `trad-bot-outcome.timer` every 15 min; Windows `BybitScannerOutcomeBackfill` via `run_outcome_backfill.bat:14` | YES (kline) |
| `app.shadow.runner` | `trad-bot-shadow-atr-wick.service`, loop `runner.py:357,377` | YES (kline) |
| `app.shadow.evaluator` | `trad-bot-shadow-evaluator.timer` (5 min, `--once`) | YES (kline) |
| `app.shadow.v2d_runner` | `trad-bot-v2d-scanner.service`, loop `v2d_runner.py:366` | YES (kline) |
| `app.shadow.v2d_evaluator` | `trad-bot-v2d-evaluator.timer` (5 min, `--once`) | YES (kline) |
| `app.shadow.srr_research_evaluator` | `trad-bot-srr-evaluator.timer` (300 s, `--once --config /opt/trad_bot/config.yaml`) | YES (kline) |
| `app.analytics.cli` | `trad-bot-analytics.timer` 06:00 + `trad-bot-analytics-finalize.timer` 10:05 (oneshot per firing) | YES (kline, P3) |
| `app.research.me_r_long_early_mae_v2.runner` | `trad-bot-me-mae-v2-evaluator.service`, loop `runner.py:44-49` | **NO** (DB-only) |
| `app.research.me_r_long_early_mae_v3.runner` | `trad-bot-me-mae-v3-evaluator.service` (`--once`, condition-gated) | **NO** (DB-only) |

### 5b. OFFLINE / one-shot / manual (may receive a documented managed path)

| Consumer | Invocation evidence | Bybit HTTP? | Managed path status |
|---|---|---|---|
| `bot.py` → `app/main.py` | manual; not in any unit/task | YES (kline, OI, funding) | none — client path (config-dependent OFF) |
| `app.scanners.cli` | manual (`python -m app.scanners.cli`) | YES | none |
| `app.paper.cli` | manual (`python -m app.paper.cli …`, docstring `:3-7`) | YES (`run-once`) | none |
| `app.shadow.backfill` | manual (`--start` required `:378`) | YES (kline) | none |
| `tools/bybit_order_submit_reconcile_v1.py` | manual (`--list/--reconcile/--manual-resolution` `:89-97`) | YES (signed order queries, `--reconcile` only) | n/a — already config-required fail-closed `:44-64` |
| `tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py` | manual | YES | **converted in working tree** (was `Settings()` bypass) |
| `tools/research/htf_keylevel_baseline_short_causal_replay_v1.py` | manual | YES | **converted in working tree** |
| `tools/research/me_r_v3_universe_candidate_issue154.py` | manual | YES | **converted in working tree** |
| `tools/research/me_rl_v1_bybit_candle_download_v1.py` | manual; **cannot run** (missing import, row 24) | would be YES | UNKNOWN (module absent) |
| `research_snapshot/htf_point_b_30d_fetch.py` | manual one-shot, per-symbol checkpoint | YES (raw) | **converted in working tree** → `guarded_bybit_attempt` (managed, P3) |
| `research_snapshot/htf_point_b_warmup_fetch.py` | manual one-shot | YES (raw) | **converted in working tree** → `guarded_bybit_attempt` |
| `scripts/verify_oos_deployment.py` | manual verification script | NO | n/a |
| `app.research.htf_point_b_observation_recovery.py` | manual (`--dry-run`/`--apply` `:361-362`) | NO | n/a |

**Interpretation for Stage B:** everything in 5a must end up on the shared limiter via its
own config source (all already construct `BybitClient` with `load_settings()`-resolved
settings — no structural change needed, only activation). Everything in 5b either already
has a fail-closed config contract, has just received the managed `guarded_bybit_attempt`
path (the two `research_snapshot` scripts), or — for the three `tools/research` tools —
was just moved off built-in defaults onto `load_settings(repo config.yaml)`.

---

## 6. UNKNOWNs (checked, not guessable from this checkout)

1. **Production config contents.** Whether the deployed `/opt/trad_bot/config.yaml` /
   `scanner_canary.json` enable the limiter — not verifiable here; the repo copy has no
   `global_*` keys. Checked: grep over all `*.{yaml,yml,json}`.
2. **Whether `SCANNER_CONFIG_PATH` is set in production.** Not present in
   `deploy/systemd/*` or any `*.bat`; only the docs show an example
   (`docs/issue231_scanner_isolated_canary_config_v1.md:148`).
3. **`tools/research/srr_short_r_expansion_candle_path_integrity_audit_v1.py`** — absent
   from this checkout (49 files in `tools/research`, none of that name); only its test
   exists. `bybit_historical_klines` therefore has **no implementation here**; whether it
   exists on another branch is UNKNOWN.
4. **`app/research/candle_coverage_guard_v1.py`** — untracked and referenced by nobody
   (grep for its module name → 0 matches). Provenance/wiring UNKNOWN.
5. **`.baseline-main/` and `.dsh-pytest-tmp/`** untracked directories — not inspected for
   Python entrypoints (outside `app/`, `tools/`, `scripts/`, `research_snapshot/`, root).
   If a copy of the repo lives there it could contain parallel entrypoints; **UNKNOWN**.
6. **`ForwardOutcomeRuntime` internals** beyond `run_cycle` — I verified the cycle-level
   error handling (`me_short_a_forward_outcome_runtime_v1.py:1206-1208,1273-1296`) but did
   not audit every method for a hidden HTTP retry loop; no `get_klines`/`_public_get`
   appears in that module (grep), so any retry would have to be in the injected source
   (row 35, transport-only).
7. **Live order submission.** No production `create_order(` caller exists today
   (row 39); if `LiveExchange` is wired later, `POST /v5/order/create` becomes a P0
   consumer of the same limiter (`retry_budget.py:95-96` maps `/v5/order` → P0).
