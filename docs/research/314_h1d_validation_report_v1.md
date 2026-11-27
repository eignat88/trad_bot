# Issue #314 — H1-D validation report (`314_h1d_validation_report_v1.md`)

**Branch:** `research/h1d-historical-htf-fetch-v1` (Draft PR #315, base `main`).
**Scope:** RESEARCH_ONLY / READ_ONLY_SOURCE / NO_PRODUCTION_SIDE_EFFECTS /
NO_EXISTING_SERVICE_RESTART / NO_TRADING_LOGIC_CHANGE / NO_FREEZE_CHANGE /
NO_PROSPECTIVE_BACKFILL / NO_OOS_ACTIVATION / NO_DEPLOY.
**Date:** 2026-11-26.

## 1. Independent code review (gap audit) — outcome and fixes

An independent audit of the H1-D package against the Issue #314 substreams
and acceptance criteria found the substream implementation sound and raised
four review defects. All four are fixed on this branch:

| id | severity | defect | fix |
|---|---|---|---|
| D1 | Medium | no integration coverage of the fetch path against the REAL shared cross-process rate limiter / retry budget / circuit breaker (all error tests mocked `_public_get`) | new `tests/test_me_entry_quality_h1d_integration_v1.py`: `fetch_htf_history` driven through a REAL `BybitClient` + REAL `GlobalBybitRateLimiter` (durable state file) + REAL `RateLimitBreaker`/retry budget; only the transport is doubled and `urlopen` poisoned. 5 scenarios: one real grant per page request (P1), real SKIPPED denial → `HtfFetchLimiterDeniedError` with zero HTTP, real breaker deferral → `HtfFetchBreakerDeniedError` with zero HTTP, rate-limit retries bounded by `bybit_max_attempts` (exactly N HTTP attempts, adapter never retries), and 10006→OK recovery inside the budget (1 logical request / 3 HTTP attempts) |
| D2 | Low | `limiter_result="LIMITER_UNAVAILABLE"` (shared limiter ERROR, `bybit_client.py`) missing from `_LIMITER_RESULTS` → misclassified as `HtfFetchRateLimitError` | added to `_LIMITER_RESULTS` → `HtfFetchLimiterDeniedError`; parametrized test over all four gate results; additionally the process-local breaker deferral and retry-storm guard (which carry no structured flag) are now classified as `HtfFetchBreakerDeniedError` via the documented `cooldown open` / `storm guard` markers (tested) |
| D3 | Low | per-event report nested `regime_4h`/`bos_4h` under `result.htf_4h.*` instead of the spec-literal field names | `H1DClassification.to_dict()` now emits the Issue #314 §3.5 field names at top level: `source_event_key`, `signal_time`, `regime_4h`, `bos_4h`, `coverage_4h`, `regime_1d`, `bos_1d`, `coverage_1d`, `combined_state`, `eligible`, `eligibility_reason` (tested for both eligible and fail-closed outcomes) |
| D4 | Low | per-dataset snapshot fields `evaluation_asof_ms` / `source_kind` not literal per-dataset fields | snapshot schema `H1D_HTF_SNAPSHOT_2`: both fields are first-class per-timeframe dataset fields, `evaluation_asof_ms` is verified ≡ `signal_time_ms`, per-dataset `source_kind` is verified against the rebuilt H1-C identity; stripped/inconsistent datasets fail closed with `ProvenanceTamperError` (3 new tamper tests); schema-1 documents rejected explicitly |

Post-fix status: **all 6 substreams PASS, all 10 acceptance criteria PASS**
(§4). Constraints verified satisfied by the review (only new research files;
production client byte-identical; no wiring, no SQL, no deploy artifacts;
freeze untouched; no backfill/OOS code).

## 2. Test evidence

Environment: Python 3.13.17 / pytest 9.2.0, worktree root, ZERO network
(`urlopen` poisoned everywhere), no PostgreSQL.

```
393 passed in 6.67s
```

| suite | tests | covers |
|---|---|---|
| `test_me_entry_quality_h1d_coverage_v1.py` | 49 | 60/60 COMPLETE, 59/60 INCOMPLETE_COVERAGE (exact missing buckets), ASOF boundaries ±1ms/±1µs, future/unclosed exclusion (no look-ahead), gaps/duplicates/out-of-order/invalid OHLCV → INVALID_SOURCE, 1D grid |
| `test_me_entry_quality_h1d_fetch_v1.py` | 46 | interval `240`/`D`, ascending rows, pagination + hard cap, overlap dedup, row discipline (reject+count, never fabricate), `deadline_s` forwarding, 429 / `retCode=10006` / all limiter gate results / breaker + storm-guard markers / timeout / API errors fail-closed with `__cause__`, `get_klines_range` sentinel |
| `test_me_entry_quality_h1d_provenance_v1.py` | 30 | full provenance capture (H1-C `build_source_identity()` parity), canonical SHA-256 determinism, save/load round-trip, 13 tamper scenarios → `ProvenanceTamperError` (incl. defense-in-depth with recomputed document hash), byte-identical replay with transport poisoned |
| `test_me_entry_quality_h1d_classification_v1.py` | 18 | spec-literal report fields (D3), SOXSUSDT fixture (verbatim lineage, ASOF=signal_time µs ≠ `captured_at`), per-TF regime/BOS + combined state + coverage + eligibility + diagnostics, 59/60 / gap / invalid-OHLC / empty fail closed (never a market mode), H1-C lineage pass-through, determinism, replay=direct parity |
| `test_me_entry_quality_h1d_runner_v1.py` | 14 | replay default (zero HTTP), fetch refusal without flag / sentinel, guarded fetch happy path (`fixture_label=REAL_BYBIT_FETCH`, only `_public_get` with bounded params), limiter/breaker denial → exit 1 + no snapshot, tampered snapshot replay fails |
| `test_me_entry_quality_h1d_integration_v1.py` | 5 | D1: real `BybitClient` + real cross-process limiter + real breaker/retry budget (see §1) |
| `test_me_entry_quality_h1d_purity_v1.py` | 14 | AST import allowlist, forbidden tokens, clock ban in pure modules, native `D` only in `htf_fetch`, `get_klines_range` still rejects `D` and is never called, nothing outside the package references it, H1-C file set unmodified |
| **H1-D subtotal** | **176** | |
| `test_me_entry_quality_h1c_*` (4 files) | 126 | frozen H1-C contracts unweakened |
| `test_me_entry_quality_h1_{candles,structure,classifier}_v1.py` | 91 | frozen H1-A/B contracts unweakened |

## 3. Real fetch evidence (owner GO 2026-11-26)

Authorization: separate owner GO after code review; sentinel
`H1D_REAL_FETCH_APPROVED.sentinel` records the approval and scope; the
guarded runner (`--fetch --fetch-authorized`) executed exactly once.
The #305 event metadata (incl. the real `lineage_hash` and `captured_at`)
was read by a READ-ONLY SELECT from
`trad_bot.research_me_upstream_coverage.source_event` on the VPS.

**Event (verbatim #305 lineage):**

* `source_event_key=d2ce270406e63cde2e7546d74344dc7bd5d0f5109893631a2387a5e2cc6a5829`
* `source_identity=MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|SOXSUSDT|LONG|5m|1795612200000`
* `signal_time=2026-11-25T13:11:00.804844+00:00` → `evaluation_asof_ms=1795612260804` (µs ASOF `1795612260804844`)
* `captured_at=2026-11-25T13:11:01.262873+00:00` (never an ASOF)
* `lineage_hash=6eaa617857c3f319e97befb0c54a1dc91afda4872b93adafc10e4d6e2bdd2626`
* `activation_freeze_ts=2026-11-24T13:31:53.365952+00:00` → `1795527113365` (floor) — freeze UNTOUCHED

**Fetch:** Bybit V5 `/v5/market/kline`, category=linear, `SOXSUSDT`,
`interval=240` + native `interval=D`, canonical 60-candle windows ending at
the last candle closed at the ASOF, via `BybitClient._public_get()` (shared
retry budget / breaker / limiter preserved; `get_klines_range` untouched).
Result: 4H=60 rows, 1D=60 rows, 0 rejected, 0 deduplicated.

**Snapshot (`H1D_HTF_SNAPSHOT_2`, `fixture_label=REAL_BYBIT_FETCH`):**

| field | value |
|---|---|
| `snapshot_sha256` | `eca45f561a82884d0ced4cf43191e2c0b937b91d77c579739ce991be86d724ba` |
| `payload_4h.payload_sha256` | `2e343be9656b77d85ee45293e4d034139b43158c9dd6aa6e813ff4b79c5da021` |
| `payload_1d.payload_sha256` | `78ee0a9f0b5daaafc2845cbe239daa116d8c987cf5b99efe39ace7bc94b4e714` |
| `payload_4h` window | `1794744000000 … 1795593600000` (60 closed 4H candles) |
| `payload_1d` window | `1790380800000 … 1795478400000` (60 closed 1D candles) |
| `fetched_at_ms` | `1795769765346` |

**Classification (ASOF = `signal_time`, closed candles only):**

| field | value |
|---|---|
| `regime_4h` / `bos_4h` / `coverage_4h` | `BEARISH` / `BEARISH_BOS` / `COMPLETE_COVERAGE` 60/60 |
| `regime_1d` / `bos_1d` / `coverage_1d` | `NEUTRAL` / `BEARISH_BOS` / `COMPLETE_COVERAGE` 60/60 |
| `combined_state` | `BEARISH/NEUTRAL` |
| `eligible` / `eligibility_reason` | `True` / `ELIGIBLE` |
| H1-C outcome | `ok=True`, `combined_eligible=True`, `eligible=True` |

**Offline replay verification (criterion 6):** `runner --replay` on the
snapshot, ZERO HTTP, produced byte-identical output to the direct run —
SHA-256 `CA3988E1B3F7EF12BF17C4C4FAA48D47DB08531E42C7D257F94E81C28D436E6A`
for both files. Any tamper with the snapshot rows / lineage / ASOF / labels
raises `ProvenanceTamperError` (13 scenarios tested).

**Interpretation limits:** this is a research classification of 4H/1D
market structure at the signal instant for the H1 (Trend Alignment)
hypothesis. It is NOT a trading signal, does NOT authorize any position,
is NOT a prospective result, and is NOT written to any prospective ledger.
The historical fetch does NOT recover or backfill any missed prospective
event; the unverified capture interval stays unverified and the #305
`freeze_ts` is unchanged.

Evidence artifacts (workspace `reports/`, untracked): `h1d_real_source_event.json`,
`h1d_real_snapshot.json`, `h1d_real_classification.json`, `h1d_real_replay.json`.
The synthetic offline demonstration (`trackB_314_h1d_synthetic_*`, regenerated
under schema 2) remains machinery evidence only.

## 4. Acceptance criteria

| # | criterion | status | evidence |
|---|---|---|---|
| 1 | 4H(`240`)+1D(`D`) via Bybit in isolated research loop | PASS | `htf_fetch.py`; real fetch 2026-11-26 (§3) |
| 2 | no bypass of retry budget / circuit breaker / global rate limiter | PASS | `_public_get()` only; D1 integration suite drives the REAL limiter/breaker/budget |
| 3 | only candles closed at `source_event.signal_time` | PASS | `close_us <= asof_us` µs rule + boundary pins; ASOF=signal_time, `captured_at` proven inert |
| 4 | 60/60 valid history → correct H1-C result | PASS | real SOXSUSDT classification (§3) + 126 unweakened H1-C tests |
| 5 | insufficient/invalid history → explicit fail-closed | PASS | 59/60 / gap / invalid OHLCV / empty tests; runner writes NO snapshot on error |
| 6 | snapshot + SHA-256 reproduce result without re-fetch | PASS | byte-identical offline replay (§3); tamper detection tested |
| 7 | SOXSUSDT classified on real history (or documented reason) | PASS | real `REAL_BYBIT_FETCH` classification (§3) |
| 8 | offline + needed integration tests pass | PASS | 393 passed incl. real-governance integration (§2) |
| 9 | no production changes / restarts / backfill / OOS activation | PASS | diff = research package + its tests + docs only; `app/exchange/bybit_client.py` byte-identical; no service/db/deploy action performed |
| 10 | implementation / tests / review results formatted for PR #315 | PASS | this report + `314_h1d_implementation_notes_v1.md` + PR body/comment update in `reports/trackB_315_pr_body_update.md` |

## 5. Residual review notes

1. **ASOF close-boundary wording** (notes §2.1): implemented as
   `close_us <= asof_us` per the explicit `iff` rule, matching the frozen
   #297/#308 inclusive-at-close contract; pinned by ±1µs tests. A strict-`<`
   reading would be a one-line semantic change + re-run.
2. **Gap semantics** (notes §2.2): internal holes → `INVALID_SOURCE`
   (`CANDLE_GAP`); contiguous shortfall → `INCOMPLETE_COVERAGE`. Both fail
   closed; the distinction preserves the spec's "59/60 → INCOMPLETE_COVERAGE".
3. `runner.py`/`htf_fetch.py` import `app.config`/`app.exchange.bybit_client`
   for the shared client — within "the shared Bybit client" allowance,
   flagged in the purity allowlist.
4. Statuses: `H1D_SOURCE_READY`, `H1D_CLASSIFICATION_VALIDATED`,
   `H1D_REPLAY_VERIFIED`, `H1D_TESTS_PASS` — **`H1_OOS_HOLD`** remains in
   force: this completion does NOT start prospective accumulation, does NOT
   authorize trading, and needs a separate decision for any H1 OOS step.
