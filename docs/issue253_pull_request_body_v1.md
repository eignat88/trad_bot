# PR body — Issue #253

**Title:** `fix: align shared Bybit rate limiter across all consumers (#253)`
**Branch:** `fix/issue253-bybit-shared-limiter-alignment` → `main`

> This file is the PR description of record for Issue #253 (kept in the
> repository so the reviewer sees the same text as the PR page).

---

## 1. Root cause

The shared `GlobalBybitRateLimiter` (Issue #231) is a correct, tested,
cross-process budget — but **which consumers actually use it is decided by
whichever config file each process happens to load**, and that activation is
not aligned:

* **Activation drift (confirmed):** the Scanner canary ran with
  `global_bybit_rate_limiter_enabled: true` while the Research evaluator,
  HTF Phase-2 V1 observer, SRR evaluator and Paper ran with it `false`.
  Every consumer therefore resolved a *different* effective policy for the
  *same* shared bucket.
* **Silent configuration bypasses (the hidden half of the root cause):**
  six code paths opened Bybit **outside** `BybitClient`'s config contract
  entirely, so no configuration could ever have aligned them:
  `BybitClient(Settings())` (built-in defaults → limiter permanently OFF
  regardless of any config file) in
  `app/research/me_r_long_early_mae_v3/isolated_oos.py` (×2, deployed on the
  5-minute `trad-bot-me-r-v3-oos.timer`),
  `tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py`,
  `tools/research/htf_keylevel_baseline_short_causal_replay_v1.py`,
  `tools/research/me_r_v3_universe_candidate_issue154.py`, and two raw
  `requests.get()` calls in `research_snapshot/htf_point_b_*.py`.
* **No configuration identity on the shared state:** two consumers that
  enabled the limiter from *different* config files with *different*
  `global_limiter_*` values would open the SAME state file with
  incompatible budgets and neither process would notice — the state file
  carried no fingerprint of the configuration that owns it.

**What the root cause does and does not explain.** The 30-minute VPS
baseline (2026-11-03) recorded **2539 `BYBIT_API_CALL` records and 5
`ret_code=10006`** (Scanner 1221/3, Research 1100/2, HTF V1 192/0, SRR 20/0,
Paper 6/0), all five at `attempt_index=1` on `/v5/market/kline`. The drift is
the **proven** alignment defect; it is **not proven to be the sole cause of
`10006`** (telemetry records are not unique wire calls, and a result record's
timestamp is not the send time). This PR therefore does not claim
`10006` root-caused — it makes the budget uniform and measurable so the
question becomes answerable.

## 2. Inventory

`docs/issue253_bybit_consumer_inventory_v1.md` — **41 consumers**, every
entrypoint with its `file:line`, config source and precedence, effective
activation, state-path source, endpoints, retry policy, bypass risk and
impact; plus a non-Bybit-HTTP exclusion list, the config-source matrix for
the five claimed services, runtime-vs-offline classification and an explicit
UNKNOWN list. It is deliberately **not** limited to the five main systemd
services: it covers timers, CLIs, research one-shots, shadow/v2d runners,
`scripts/`, `research_snapshot/` and the `deploy/systemd/*` units.

Key corrections the inventory established against the issue's starting
assumptions:

* **No config file in the repository enables the limiter** (`grep
  global_bybit_rate_limiter_enabled` over `*.yaml|*.yml|*.json` → 0 matches);
  the ENV factor is inert without `allow_env` in the config. Activation today
  is entirely config-file driven — hence the drift was possible at all.
* The Scanner's `SCANNER_CONFIG_PATH` mechanism is real (absolute path, no
  fallback), but **no `scanner_canary.json` exists in the repository and no
  deploy unit sets `SCANNER_CONFIG_PATH`** — whether production sets it is
  UNKNOWN from the repository; that must be confirmed on `trad-bot-01`
  before Stage 5 of the rollout.
* **No production caller of `POST /v5/order/create` exists** —
  `app/execution/live_exchange.py` never submits and is constructed only by
  tests. Order-path (P0) production traffic is effectively zero today.
* Two systemd-scheduled consumers (`me_r_long_early_mae_v2/v3/runner.py`)
  and `scripts/verify_oos_deployment.py` issue **no** Bybit HTTP at all.
* Retry is single-layer: `BybitClient._budgeted_get` with
  `HARD_MAX_ATTEMPTS = 3` from `app/exchange/retry_budget.py`. The only
  consumer-level re-requests are HTF gap-recovery backoff (a data
  correctness loop, not a transport retry) and pagination loops. **No
  uncontrolled second retry layer was found.**
* Two pre-existing defects are recorded but **not** fixed here (out of
  scope, not caused by this issue): `tools/research/me_rl_v1_bybit_candle_download_v1.py`
  imports a module that does not exist (cannot run), and
  `app/research/candle_coverage_guard_v1.py` is an untracked orphan with
  zero callers.

## 3. Config contract

New `app/config/limiter_contract.py` + optional settings key
`global_limiter_contract_path` + shipped reference `limiter_contract.v1.json`:

* **One canonical parameter source.** Every consumer that points
  `global_limiter_contract_path` at the same contract resolves an identical
  `LimiterConfig` — identical state path, lock file, `G/B/max_wait`, weights
  and breaker cooldowns — regardless of its own config file or cwd.
* **Strict, fail-closed validation.** Unknown keys, forbidden keys, a missing
  `global_limiter_state_path`, an unreadable/malformed file or an
  out-of-range/type-invalid value all raise `LimiterContractError` (a
  `ValueError`) at `load_settings()` — **before** any `BybitClient` exists.
  There is no silent override and never a silent fallback to limiter-OFF.
* **Activation is never carried by the contract.** `global_bybit_rate_limiter_enabled`,
  `global_bybit_rate_limiter_allow_env` and `global_limiter_contract_path`
  itself are rejected if present: activation stays a per-consumer decision
  so the staged rollout (one service at a time, each behind an operator GO)
  is preserved.
* **Contract values win only where the config file is silent; an explicit
  disagreement fails startup** (e.g. config `g_rps: 6` vs contract `8.0`).
  Numerically equivalent values (`8` vs `8.0`) are accepted.
* **Second (cross-process) guard:** `GlobalBybitRateLimiter` now stores a
  `config_fingerprint` (sha256 over `SHARED_SEMANTIC_FIELDS`) in the shared
  state and fail-closes with `LimiterUnavailableError("config_mismatch")`
  when a process with different parameters touches the same state file. A
  state file written before this change has no fingerprint and is **adopted
  once** (only by a write-capable transaction, then re-fenced); read-only
  readers never mutate it.
* **The contract is optional.** Without `global_limiter_contract_path` the
  behaviour is byte-identical to Issue #231 (default-off regression tests
  untouched), so this PR is safe to merge before any activation GO.

**No silent bypass (verified, not assumed):**

* the contract can only *supply parameters*; `global_bybit_rate_limiter_enabled`
  and `global_bybit_rate_limiter_allow_env` are rejected if present, and the
  contract merge runs *after* the two-factor activation block, so activation
  still comes exclusively from config-file/ENV (`settings.py:868-901`);
* **parameters have no ENV path at all**: the only limiter ENV read in the
  codebase is `GLOBAL_BYBIT_RATE_LIMITER_ENABLED` (`settings.py:877`); a grep
  for `GLOBAL_LIMITER_*` returns nothing, so an environment cannot silently
  override a contract value;
* `app/analytics/cli.py:139-143` — the one entrypoint that *builds* its own
  `Settings` — explicitly copies every `global_limiter_*` /
  `global_bybit_rate_limiter_*` field from `load_settings()`, i.e. it joins
  the contract rather than bypassing it;
* the remaining `Settings()` call inside `BybitClient` (`bybit_client.py:423`)
  feeds only the **process-local** `_RateLimiter(rps=bybit_rate_limit_rps)`,
  not the shared gate — it cannot bypass the shared budget;
* grep for `BybitClient(Settings())` in production code returns **0** matches
  after this PR;
* a missing/unreadable/unknown-key/forbidden-key contract fails
  `load_settings()` before any `BybitClient` exists, and a parameter mismatch
  against an existing state file fails closed with `config_mismatch`.

## 4. Changed files

**New**

| File | Purpose |
|---|---|
| `app/config/limiter_contract.py` | canonical contract load/validate/merge, `LimiterContractError` |
| `app/exchange/offline_limiter.py` | managed offline gate (`guarded_bybit_attempt`) for non-`BybitClient` HTTP |
| `limiter_contract.v1.json` | shipped, git-versioned reference contract (inert on its own) |
| `tests/test_issue253_shared_limiter_alignment_v1.py` | 33 deterministic tests |
| `tests/test_issue253_shared_limiter_mp_v1.py` | 3 multiprocess tests |
| `tests/test_issue253_limiter_deny_propagation_v1.py` | 10 end-to-end deny-propagation tests (5 real entrypoints) |
| `docs/issue253_bybit_consumer_inventory_v1.md` | Stage A inventory |
| `docs/issue253_oos_limiter_safety_analysis_v1.md` | Stage E OOS/latency safety |
| `docs/issue253_bybit_api_quota_model_v1.md` | Stage F quota model + observability |
| `docs/issue253_staged_rollout_rollback_runbook_v1.md` | staged rollout, rollback, ≥60 min runbook |
| `docs/issue253_followup_candle_coverage_issue_draft_v1.md` | draft of the SEPARATE coverage issue (not implemented here) |
| `docs/issue253_pull_request_body_v1.md` | this document |

**Modified**

| File | Change |
|---|---|
| `app/config/settings.py` | new optional `global_limiter_contract_path` field + contract merge in `load_settings()` |
| `app/exchange/global_rate_limiter.py` | `SHARED_SEMANTIC_FIELDS`, `LimiterConfig.fingerprint`, `config_fingerprint` in state, `_check_config_fingerprint()` fail-closed guard |
| `app/exchange/bybit_client.py` | GET-path `acquire` deadline bounded by `min(retry deadline, global_limiter_max_wait_s)` — the configured 30 s contract previously did **not** bound a BybitClient request (the 60 s retry deadline silently won) |
| `app/research/me_r_long_early_mae_v3/isolated_oos.py` | `BybitClient(Settings())` → `BybitClient(load_settings(repo config.yaml, repo .env))` (both call sites, `once()` and `market_preflight()`) |
| `tools/research/me_r_v3_universe_candidate_issue154.py` | same bypass removed |
| `tools/research/htf_keylevel_baseline_short_causal_replay_v1.py` | same bypass removed |
| `tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py` | same bypass removed |
| `research_snapshot/htf_point_b_warmup_fetch.py` | raw `requests.get` wrapped in `guarded_bybit_attempt` (one guarded block == one HTTP attempt) |
| `research_snapshot/htf_point_b_30d_fetch.py` | same |

**Not changed (explicitly):** no new limiter, no trading strategy, scoring,
signal or gate logic, no HTF V2 activation, no production config
(`config.yaml`, `scanner_canary.json`), no systemd/service, no database,
no freeze/checkpoint/registry/OOS artifact, no paper/live flag.

## 5. Test results

Environment: Python 3.13.2 (`.dsh-pytest-tmp/py/python.exe`), `pytest -q -p
no:cacheprovider`, Windows host.

**New Issue #253 tests**

```
pytest -q -p no:cacheprovider tests/test_issue253_shared_limiter_alignment_v1.py \
  tests/test_issue253_shared_limiter_mp_v1.py tests/test_issue253_limiter_deny_propagation_v1.py
→ 46 passed   (33 + 3 + 10)
```

Covered: contract alignment across 3 independent configs; config/state-path
disagreement fails closed; contract rejects activation keys, missing state
path, unknown keys, missing file, invalid parameter and wrong type; legacy
default-off; `limiter_param_keys()` vocabulary completeness; cwd-independent
relative contract path; `config_mismatch` fail-closed with byte-identical
state; BybitClient-level mismatch → no HTTP + `BYBIT_API_SKIP reason=config_mismatch`;
legacy state fingerprint adoption; default-off zero I/O; fingerprint
equality rules; **3-consumer shared budget** (exactly `burst` grants then
denial); burst-ceiling invariant `total_grants ≤ burst + refill_total`; GET
retry = one grant per attempt; POST = single P0 grant; REVOKED path; offline
gate no-op / outcome mapping / deny-before-send / fail-closed / repo-anchored
settings / **caller-set `RATE_LIMIT` outcome is never clobbered**; zero
`bybit_retry_deadline_s` still bounds the acquire by `max_wait_s`; source-level
`if not candles` guard in all five generic evaluators; shipped reference
contract validity; multiprocess shared state identity, single accounting
point, bucket invariant, drifted-config fail-closed with byte-identical
state, no second state/lock file; and the **10 end-to-end deny-propagation
tests** in §7 (deny → no HTTP → no outcome/checkpoint/trade advancement →
recovery) on all five real entrypoints.

**Review findings fixed in this PR (2 × Codex P2, both real):**

1. `min(deadline_at, t_start + max_wait_s)` raised `TypeError` when
   `bybit_retry_deadline_s = 0` (a supported setting — `settings.py:944`
   only forbids *negative*), which the gate converted into
   `BybitRateLimitError` and would have failed **every** budgeted GET.
   Fixed: with a disabled retry deadline the configured
   `global_limiter_max_wait_s` alone bounds the acquire.
   Regression: `test_zero_retry_deadline_still_bounds_acquire_by_max_wait`.
2. `guarded_bybit_attempt` overwrote an **explicit** caller classification
   (`attempt.outcome = "RATE_LIMIT"` before raising on Bybit `retCode 10006`)
   with the generic mapping of the wrapper's own `RuntimeError`, recording
   provider throttling as a plain `ERROR` so the shared breaker would never
   trip. Fixed: only an unclassified (`"OK"`) attempt is mapped.
   Regression: `test_offline_gate_preserves_explicit_caller_rate_limit`.

**Regression suites (Stage G)**

```
pytest -q -p no:cacheprovider tests/test_issue231_*.py tests/test_global_bybit_*.py \
  tests/test_issue253_*.py tests/test_bybit_*.py tests/test_scanner_*.py tests/test_paper_*.py \
  tests/test_htf_baseline_short_phase2_*.py tests/test_prospective_*.py tests/test_shadow_cli.py \
  tests/test_srr_*.py
→ 42 failed, 1950 passed, 65 skipped, 22 errors   (96 files)
```

Identical failure/error set to the pre-review run (**64 entries, byte-for-byte
the same list**) — the review fixes and the 13 added tests introduced **zero**
new failures.

**Baseline for comparison** (pristine `origin/main` @ `ce51f2a` in a
separate worktree, same interpreter, same flags):

```
pytest -q -p no:cacheprovider --tb=no --continue-on-collection-errors
→ 71 failed, 5434 passed, 75 skipped, 173 errors
```

Diff of the Stage-G batch against that baseline: **64 failure/error entries,
of which 62 are byte-for-byte pre-existing baseline failures** (PostgreSQL
unavailable, `pwd`/`psycopg2`/`pg8000` missing, one missing
`tools.research.*` module) and **2 are new**, both being Phase-2 *branch
scope* guardrails:

| Test | Verdict | Attribution |
|---|---|---|
| `test_htf_baseline_short_phase2_isolation_v1::test_no_tracked_file_modified_against_base` | FAIL (new) | Its allowlist is the **HTF Phase-2 surface**; Issue #253 legitimately modifies 9 files outside it (`settings.py`, `bybit_client.py`, `global_rate_limiter.py`, `isolated_oos.py`, 2× `research_snapshot`, 3× `tools/research`). It diffs `merge-base(HEAD, origin/main)` → red for **any** non-Phase-2 branch and self-heals on merge. Broadening that allowlist would permanently whitelist `app/config/settings.py` for the Phase-2 workstream — the control itself says *"do not broaden the allowlist"* — and Issue #253 has no authority to weaken an HTF Phase-2 isolation control. **Left untouched deliberately.** |
| `test_htf_baseline_short_phase2_systemd_bybit_wiring_v1::test_scanner_paper_evaluator_settings_unchanged` | FAIL (new) | Same branch-scope guard: forbids any changed path matching `scanner\|paper\|live\|evaluator\|config.yaml\|app/trading\|app/execution`. The single offender is `tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py`, matched only because its filename contains `live`; the edit is the required bypass removal. Same precedent and same deliberate non-change. |

**Why CI is green on these two:** `.github/workflows/main-pr-safety.yml`
classifies every PR three ways (`NO_PHASE2` / `MIXED_WITH_PHASE2` /
`PHASE2_ONLY`) against the frozen Phase-2 governed surface. This PR touches
`app/exchange/*`, `app/config/*`, `docs/*`, `tests/*` (not governed) **and**
`tools/research/htf_baseline_short_phase2_bybit_live_dry_run_v1.py`
(governed) → it classifies as **`MIXED_WITH_PHASE2`**, where the workflow
explicitly `--deselect`s exactly those two confinement tests and instead runs
the frozen-invariant + HTF causal/parity/closed-bar regression. That routing
itself is verified on every run by
`tests/test_htf_baseline_short_phase2_scope_routing_v1.py` (wired
unconditionally), which also fails if the workflow's governed-surface list
drifts from the isolation test's lists. Observed on PR #254: **Main PR
Safety = success**, **Bybit retry budget regression (offline) = success**,
**Offline V3 tests and frozen universe = success**.

Full-suite run on this branch (same flags as the baseline, re-run **after**
the review fixes):

```
pytest -q -p no:cacheprovider --tb=no --continue-on-collection-errors
→ 71 failed, 5480 passed, 75 skipped, 1 warning, 173 errors
```

Full-suite diff against the pristine baseline (`71 failed, 5434 passed, 75
skipped, 173 errors`) — **244 failing/erroring entries on both sides**:

* **+46 passed** = exactly the new Issue #253 tests (33 + 3 + 10);
* **2 new failures** = the two Phase-2 branch-scope guardrails above;
* **2 baseline failures now pass** (they failed on the pristine tree too and
  are order/cwd-sensitive):
  `test_bybit_order_submit_guard_multiproc_v1::test_multiproc_concurrent_reconciliation_and_create`
  and
  `test_issue231_r2_config_contract_v1::test_relative_state_path_anchors_to_repo_root_not_cwd`;
* **failed (71) and error (173) counts are unchanged**; every remaining
  failure/error is present identically on pristine `origin/main`.

`compileall` over `app tests tools scanner_runner.py paper_runner.py bot.py
research_snapshot` → exit 0.

**Status:** `IMPLEMENTED / TESTED / DEPLOY_PENDING`. No production audit has
been run; nothing here may be read as `PRODUCTION_VALIDATED`, and no
synthetic test is used to claim the API budget is cleared
(`#225: API_BUDGET_NOT_CLEARED` stands).

## 6. Quota assumptions and risks

From `docs/issue253_bybit_api_quota_model_v1.md`:

* Bybit documents **per-UID rolling windows per second**, **600 req / 5 s
  per IP**, `POST /v5/order/create` and cancel at **10/s**, and
  `order/realtime`·`order/history`·`position/list`·`wallet/balance` at
  **50/s**. **Market endpoints have no documented RPS.**
* `retCode 10006` = rate limit; HTTP `429` = system frequency protection;
  `X-Bapi-Limit*` headers carry the remaining quota.
* **Unresolved risk (documented, not silently accepted):** the configured
  `G = 8 rps / burst = 12` is **not provably safe** against the documented
  caps — a burst of 12 could exceed the documented **10/s order**
  per-UID limit, and the market-endpoint cap is unknown. This PR does
  **not** change the limiter algorithm to paper over it; a per-endpoint
  order sub-limiter is the recommended follow-up and requires its own
  issue + operator decision. Mitigating fact from the inventory: **no
  production `POST /v5/order/create` caller exists today**, so the order-path
  exposure is currently theoretical.
* The pre-deploy baseline shows a **peak of 16 records in one calendar
  second**, above both `G = 8` and `B = 12`. Enabling the limiter **will**
  defer/deny at peaks; that is intended, but throughput at peak seconds will
  be lower than today's unregulated baseline and must be judged on
  `BYBIT_API_SKIP`, queue wait and consumer backlog — never on "no errors".
* Telemetry records ≠ unique wire calls; a result record's timestamp ≠ send
  time. All before/after comparisons are like-for-like telemetry.

## 7. OOS safety

From `docs/issue253_oos_limiter_safety_analysis_v1.md`:

* **Ruled out:** the literal `deny → [] → finalize` chain. All five generic
  evaluators (`app/research/evaluator.py`, `app/research/prospective_evaluator.py`,
  `app/shadow/evaluator.py`, `app/shadow/srr_research_evaluator.py`,
  `app/shadow/v2d_evaluator.py`) guard `if not candles: … continue` before
  any finalization; this is now asserted by a source-level test.
* **End-to-end deny propagation, proven on the real entrypoints**
  (`tests/test_issue253_limiter_deny_propagation_v1.py`, 10 tests, all green,
  deterministic — real `BybitClient` + real `GlobalBybitRateLimiter` whose
  breaker is genuinely tripped through `acquire`/`record_result`, and a
  session that FAILS the test if any HTTP attempt escapes):

  | Entry | Deny behaviour (file:line) | Advancement on deny |
  |---|---|---|
  | Scanner | `get_scanner_universe` unwrapped → aborts before any run (`scanner_runner.py:250`, `:1463`); per-symbol `build_market_context` deny caught at `:971-983` → `save_error(MARKET_DATA_ERROR)` → `continue` | no `save_setup`, no signal, no outcome; run finishes `PARTIAL` (`:1652-1659`) |
  | Research (prospective) | `_get_candles` swallows → `[]` (`prospective_evaluator.py:209-211`) → guard `:484-486` | `finalized = 0`, **zero** outcome SQL (asserted) |
  | Research (generic) | `_get_candles_for_symbol` swallows → `[]` (`evaluator.py:270-272`) → guard `:421-423` | no outcome write |
  | HTF V1 observer | deny **translated** to `BybitCandleSourceError(RATE_LIMITED)` (`bybit_candle_source_v1.py:425-433`) → `SOURCE_BLOCKED` + `continue` (`observer_runner_v1.py:723-736`) | `_advance_cursor` is only callable from `process_symbol_bars`, which only runs after the handler (asserted) |
  | SRR evaluator | `_get_candles_for_symbol` swallows → `[]` (`srr_research_evaluator.py:140-142`) → guard `:370-372` | `_save_outcome_partial` never called (asserted by a spy that fails the test) |
  | Paper | never fetches candles; `_get_prices` swallows → `{}` (`paper_runner.py:289-291`) | `check_entries` skips `price is None` (`engine.py:292-294`) → **no trade recorded** (asserted) |

  Recovery is asserted too: after the breaker cooldown the *same*
  entrypoint sends exactly one HTTP request again.
* **Pre-existing risk (not introduced by this issue, not fixed here):** a
  non-empty but non-covering latest-N window (~1000 bars ≈ 3.47 days) can
  still finalize `is_final=True` with all-false TP/SL defaults, and observers
  drop events on deny with only an in-memory `rate_limit_drops` counter.
  **Raised as a SEPARATE issue** — draft in
  `docs/issue253_followup_candle_coverage_issue_draft_v1.md` — because
  fixing it changes OOS finalization/coverage policy and needs its own
  operator decision. It is byte-identical on `main` and on this branch
  (no dependency, no regression), but it means "no new denials" must never
  be read as OOS-coverage proof.
* Forward-only, gap-permanent consumers are exactly the OOS-sensitive ones:
  `isolated_oos.py` and the ME SHORT A forward runtime.
* **Deliberately not wired:** `app/research/candle_coverage_guard_v1.py` is
  dead code; wiring it would be an eligibility/coverage change requiring an
  operator decision.
* **Untouched:** freeze timestamps, checkpoints, eligibility rules,
  registries, OOS rows, backfill/replay, and `global_limiter_max_wait_s`
  (the only `max_wait_s` change is bounding the BybitClient GET path *down*
  to the already-configured 30 s — a convergence towards the documented
  contract, never an extension of it).

## 8. Staged rollout

Full plan in `docs/issue253_staged_rollout_rollback_runbook_v1.md`
(**prepared, NOT executed**). Activation is per-consumer and ordered by
blast radius, then load: **0** pre-flight (no activation) → **1** Paper
(6 records/30 min) → **2** SRR evaluator (20) → **3** HTF Phase-2 V1
observer (192) → **4** Research evaluator (1100) → **5** Scanner (1221) →
**6** remaining timers/CLIs. Each stage has an entry checklist (effective
config read back, service states, identical `state_path`, zero SKIP/breaker,
unchanged source freshness and OOS checkpoint/coverage) and a ≥60-minute
exit checklist with explicit rollback triggers.

## 9. Rollback

Per stage, newest first, one consumer at a time: restore the pre-stage
config copy (`global_bybit_rate_limiter_enabled: false` / contract path
removed) → operator performs the service restart → verify cycle times and
telemetry return to the pre-stage envelope and that OOS checkpoint/coverage
rows are unchanged. Code rollback is a reviewed `git revert`/redeploy of the
recorded revision.

**Hard prohibitions during any rollout or rollback:** never manually edit,
truncate or delete an active limiter state file or its lock (corruption
fail-closes; the fix is a config/code rollback, not state surgery); never
change a research freeze; never touch production PostgreSQL; never
backfill/replay; never flip paper/live, scoring or gates; never enable two
consumers with different `global_limiter_*` parameters on one state path
(the `config_fingerprint` guard fail-closes the second one — fix the config
instead of bypassing it). **No automatic restarts, no systemd edits, no DB
changes are performed by this issue.**

## 10. HTF V2 — NO_GO

**HTF V2 stays `NO_GO`.** Completing Issue #253 is not an activation signal
for HTF V2; its own gate (Issue #225, `API_BUDGET_NOT_CLEARED`) still
applies, and this PR neither enables nor configures it. The ~120 req/hour
assessment procedure (burst shape, warmup/catch-up, retry multiplier, reserve
for existing consumers at their observed 16/s peak, provider-side
per-endpoint limits, and a ≥60-minute measured soak) is specified in
`docs/issue253_staged_rollout_rollback_runbook_v1.md` §4 — **procedure only,
not run**. A mathematical average headroom alone is explicitly rejected as
evidence.

---

**Statuses preserved:** `#253`: IMPLEMENTATION IN PROGRESS → **IMPLEMENTED /
TESTED / DEPLOY_PENDING** · `#225`: **API_BUDGET_NOT_CLEARED** ·
`PR #227`: **DRAFT / DO NOT MERGE** (untouched, not merged) · `HTF V2`:
**NO_GO**.
