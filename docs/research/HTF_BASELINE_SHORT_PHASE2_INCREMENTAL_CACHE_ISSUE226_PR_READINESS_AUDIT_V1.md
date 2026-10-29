# Issue #226 — Final Readiness Audit / PR Preparation (V1)

Scope: worktree `D:\py_pro\trad_bot_issue226`, branch `research/issue226-incremental-cache`.
Independent re-verification of the bounded-recovery / concurrency-locking /
causal-parity implementation. **No V2_GO declared. No activation, no deploy,
no merge. No production, VPS, frozen V1, scanner, paper/live or OOS registry
changes.** All numbers below come from deterministic local tests; live-system
telemetry was not collected (out of scope by design).

## 1. Overall verdict

| # | Audit item | Verdict |
|---|---|---|
| 1 | Joint regression re-run, no hidden env changes (`PYTHONPATH` empty) | **PASS** — 19/19 |
| 1b | Sandbox-shim dependency isolated | **PASS** — only 2 preserved items (listed below) |
| 2 | Extended cache window does not change IDs / levels / dedup / durable state | **PASS** |
| 3 | `provenance.candle_source` difference is diagnostic-only | **PASS** (2 documented caveats) |
| 4 | API budget vs Issue #225 authoritative contract | **API_BUDGET_UNVERIFIED** (reconciled vs #225 estimate; real telemetry pending) |
| 5 | Git hygiene: no temp artifacts, no accidental tracked changes | **PASS** |
| 6 | PR-ready package | **PREPARED — PR NOT OPENED** (blockers below) |

**Issue #226 functional criteria (locking, recovery fail-closed, parity,
restart, budget bounds): all PASS. One criterion remains UNVERIFIED and is
blocking for a merge-ready PR** → the PR is deliberately not opened.

## 2. Joint regression (item 1)

Shim-free suite — clean environment (`$env:PYTHONPATH = $null`), nothing
installed or patched for the run:

```
python -m pytest tests/test_htf_baseline_short_phase2_issue226_bounded_recovery_v1.py \
    tests/test_htf_baseline_short_phase2_issue226_concurrency_lock_v1.py \
    tests/test_htf_baseline_short_phase2_issue226_api_budget_v1.py \
    tests/test_htf_baseline_short_phase2_issue226_causal_parity_v1.py -q
# => 19 passed (7 + 7 + 2 + 3)
```

Sandbox-shim tests (kept separate from CI results, per issue instruction).
These are the two PRE-EXISTING items that rely on `tempfile.mkdtemp()` and hit
the local sandbox's 0700-directory quirk; they run with the gitignored shim
`.pytest_tmp/dsh_shim/sitecustomize.py` (adds `0o077` to `os.mkdir` mode):

```
$env:PYTHONPATH = '<repo>\.pytest_tmp\dsh_shim'
python -m pytest tests/test_htf_baseline_short_phase2_incremental_cache_v1.py -q          # 5 passed
$env:PYTHONPATH = '<repo>;<repo>\.pytest_tmp\dsh_shim'
python tests/test_htf_baseline_short_phase2_issue226_integration_smoke.py                 # PASS
```

No Issue #226 test (`tests/test_htf_baseline_short_phase2_issue226_*`) requires the shim.

## 3. Window-length invariance (item 2)

`test_cache_window_length_does_not_change_causal_outputs`
(`tests/test_htf_baseline_short_phase2_issue226_causal_parity_v1.py`): the identical scenario runs with
`max_cache_bars=800` (recovery trims leading history) and `max_cache_bars=2000`
(no trim); the exercise confirms genuinely different window lengths. Equal
across both runs: `source_event_id` sequence (and uniqueness), `episode_id`
sequence, `known_levels` + `known_level_ids`, cursors, duplicate-suppression
counters (also equal to the plain-source run), and the full durable checkpoint
state after restart + recovery. Result: **PASS**.

## 4. `provenance.candle_source` audit (item 3)

Difference: `BybitPhase2CandleSource` vs `IncrementalPhase2CandleSource`
(`type(source).__name__`, `observer_runner_v1.py:336`).

Consumers checked across `app/` and `tools/`:
* Event identity: `episode_id_for` = sha256(experiment|symbol|direction|
  level_id|first_touch_open_ms); `source_event_id_for` = derived from episode
  id — **provenance not an input**.
* Dedup: runner keys on `source_event_id` only (`observer_runner_v1.py:687`);
  DB `insert_observation_raw` unique identity is `source_event_id` — provenance
  is stored inside the `features` JSON payload (`persistence_v1.py:208`).
* The repo's own equivalence checks (dry-run `recovery_status`) compare
  episode_id/first_touch_open_ms/known_at_ms/source_event_id — never provenance.

**Classification: diagnostic-only.** Two documented caveats:
1. `load_checkpoint` restores `self._provenance` from the saved payload
   (`observer_runner_v1.py:411`), so after swapping the source adapter on a
   restart the label keeps the ORIGINAL writer's class name. Label is not
   guaranteed to name the current adapter — it names the checkpoint author.
2. Any downstream consumer that compares or hashes FULL event payloads
   (including `features` JSON) byte-exactly across adapters will see the
   difference. No such consumer exists in this worktree today.

## 5. API budget vs Issue #225 (item 4)

Authoritative #225 contract (issue `eignat88/trad_bot#225`, task + comments):
* Five-symbol candidate roster: BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, NEARUSDT —
  **matches the #226 budget test roster exactly**.
* Uncached full-window risk estimate: `15 pages × 5 symbols × 12 cycles/h =
  900 kline requests/hour`, plus time calls, retries and catch-up — explicitly
  labelled "a risk estimate, **not a measured per-IP exchange limit**".
* Measured 60-min operator telemetry (context): 6145 calls total across
  scanner/paper/evaluator/observer, 3 rate-limit responses.
* Disposition: `SOURCE_5M_1H_COVERAGE_PASS / API_BUDGET_NOT_CLEARED /
  V2_START_NO_GO`; #226 was created to deliver an incremental cache **and
  measured before/after telemetry**.

#226 deterministic measurements (tests/test_htf_baseline_short_phase2_issue226_api_budget_v1.py):

| Phase | Per symbol | 5-symbol cycle | vs #225 estimate |
|---|---|---|---|
| cold (3000 bars / 200 per page) | 16 (1 time + 15 pages) | 80 | matches #225 "15 pages/symbol" |
| steady 5m cycle (1 new bar) | 2 (1 time + 1 page) | 10/cycle ≈ **120/h** | vs ~1200/h uncached (900 kline + 300 time) → **~10× fewer** |
| restart (no new bars) | 1 | 5 | cache reuse, 0 pages |
| recovery burst (250 bars) | 3 (1 time + 2 pages) | 15 | bounded, ≤ 79/load |
| transport retries (2 failures) | 4 (1 + 3 tries) | — | ≤ max_attempts 3/page |
| cold + hole | 17 (1 + 15 + 1 gap repair) | — | ≤ 16 gap-recovery bound |
| unrecoverable hole | 18 (bounded 2 tries) | — | fail-closed |

**Verdict: `API_BUDGET_UNVERIFIED`.** The directional reconciliation against
the #225 estimate is favourable (~10× steady-state reduction), but #225's own
acceptance requires *measured* request telemetry, observed retry/rate-limit
evidence and a safe-capacity proposal from the real runtime ("not a measured
per-IP exchange limit", `GLOBAL_API_BUDGET_PENDING`). Collecting that
telemetry requires the live services this worktree must not touch. Recorded
as UNVERIFIED, not PASS, per audit instruction.

## 6. Git hygiene (item 5)

* `git status --short`: 8 untracked deliverable files only (list below).
  `git diff --stat`: empty — **zero tracked files modified** (incl.
  `tests/conftest.py`, frozen V1 code, scanners, persistence).
* No stray artifacts in tracked space (worker scripts / result files live
  only under gitignored `tests/.tmp/`; `.gitignore` covers `.pytest_tmp/`
  (line 5) and `tests/.tmp/` (line 6)).
* Sandbox residue (poisoned 0700 dirs in `.pytest_tmp/`, `tests/.tmp/`) is
  ignored by git and regenerated per test run; normal CI is unaffected.

Deliverables (all new/untracked, ready to commit):
* `app/research/htf_baseline_short_phase2_incremental_cache_v1.py` — cache + bounded recovery + per-symbol inter-process lock
* `tests/test_htf_baseline_short_phase2_issue226_bounded_recovery_v1.py` (7), `tests/test_htf_baseline_short_phase2_issue226_concurrency_lock_v1.py` (7),
  `tests/test_htf_baseline_short_phase2_issue226_api_budget_v1.py` (2), `tests/test_htf_baseline_short_phase2_issue226_causal_parity_v1.py` (3)
* `docs/research/HTF_BASELINE_SHORT_PHASE2_INCREMENTAL_CACHE_ISSUE226_RECOVERY_RUNBOOK_V1.md`
* Preserved WIP baseline suites (renamed into the Phase-2 surface, mechanical
  import-path updates only): `tests/test_htf_baseline_short_phase2_incremental_cache_v1.py`,
  `tests/test_htf_baseline_short_phase2_issue226_integration_smoke.py`

## 7. Remaining risks / blockers

1. **BLOCKING — `API_BUDGET_UNVERIFIED`:** measured before/after API telemetry
   and rate-limit headroom for the real services are not collected (#225:
   `API_BUDGET_NOT_CLEARED / GLOBAL_API_BUDGET_PENDING`). Requires a bounded
   operator VPS run — outside this worktree's mandate.
2. Provenance label survives adapter swaps via checkpoints (diagnostic caveat,
   §4.1) — recommend normalizing or documenting at the #225 revisit.
3. Deterministic fakes only: no live-exchange behaviour (rate limits, partial
   outages) was exercised; the fail-closed bounds are code-verified, not
   exchange-verified.
4. Activation gates (#225 universe/freeze approvals, #224 compatibility,
   #181 maturity) are untouched and unresolved by design.

## 8. PR package (prepared, NOT opened)

Because blocker (1) is unresolved, the PR is **not opened as ready to merge**
per audit instruction. UPDATE (draft stage): a **Draft** PR is opened with
explicit HOLD gates (`DO NOT MERGE`, `API_BUDGET_UNVERIFIED`, `V2_NO_GO`) and
must not be merged until blocker (1) clears. Materials for a human to open after telemetry lands:

* Branch: `research/issue226-incremental-cache`
* Suggested title: `HTF Phase-2 incremental 5m candle cache: bounded recovery, per-symbol locking, causal parity (#226)`
* Suggested body: this report + §2 test commands + §5 budget table;
  label `DO NOT MERGE` until `API_BUDGET_UNVERIFIED` is cleared by the #225
  operator telemetry run.
* Nothing to merge or deploy until then; V1/V2 activation remains NO_GO.

## 9. Exact test commands

```
# shim-free (CI-equivalent), clean env
$env:PYTHONPATH = $null
python -m pytest tests/test_htf_baseline_short_phase2_issue226_bounded_recovery_v1.py tests/test_htf_baseline_short_phase2_issue226_concurrency_lock_v1.py tests/test_htf_baseline_short_phase2_issue226_api_budget_v1.py tests/test_htf_baseline_short_phase2_issue226_causal_parity_v1.py -q

# sandbox-shim only (local DSH runner quirk; separate from CI results)
$env:PYTHONPATH = '<repo>\.pytest_tmp\dsh_shim'
python -m pytest tests/test_htf_baseline_short_phase2_incremental_cache_v1.py -q
$env:PYTHONPATH = '<repo>;<repo>\.pytest_tmp\dsh_shim'
python tests/test_htf_baseline_short_phase2_issue226_integration_smoke.py
```
