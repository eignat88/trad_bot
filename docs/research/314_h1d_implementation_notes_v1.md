# Issue #314 — H1-D implementation notes (`314_h1d_implementation_notes_v1.md`)

**Branch:** `research/h1d-historical-htf-fetch-v1` (Draft PR #315).
**Scope:** RESEARCH_ONLY / READ_ONLY_SOURCE / NO_DEPLOY / NO_PRODUCTION_SIDE_EFFECTS.
**Status:** code + offline/integration tests implemented; real Bybit fetch
EXECUTED 2026-11-26 under separate owner GO after code review (§4).

## 1. Package layout (all-new files)

| file | substream | role |
|---|---|---|
| `app/research/me_entry_quality_h1d_v1/htf_fetch.py` | `H1D_HTF_4H_FETCH_V1` + `H1D_HTF_1D_FETCH_V1` | bounded research-only historical kline fetch (`interval=240` / native `interval=D`) via `BybitClient._public_get()` ONLY |
| `app/research/me_entry_quality_h1d_v1/coverage.py` | `H1D_CAUSAL_ASOF_COVERAGE_V1` | causal ASOF (`signal_time`, µs precision) canonical 60/60 coverage + exact diagnostics |
| `app/research/me_entry_quality_h1d_v1/provenance.py` | `H1D_PROVENANCE_REPLAY_V1` | immutable snapshot (canonical SHA-256, tamper detection) + offline byte-identical replay |
| `app/research/me_entry_quality_h1d_v1/classification.py` | `H1D_REAL_EVENT_CLASSIFICATION_V1` | #305 event → frozen H1-C pipeline + H1-D coverage/eligibility report |
| `app/research/me_entry_quality_h1d_v1/runner.py` | `H1D_VALIDATION_GOVERNANCE_V1` (part) | read-only CLI: `--replay` (offline, default) / `--fetch` (guarded) |
| `tests/test_me_entry_quality_h1d_*_v1.py` (6 files) | `H1D_VALIDATION_GOVERNANCE_V1` | 159 offline tests incl. isolation/purity |

Zero existing files modified (this doc + the #314 spec checklist are the only
edits to pre-existing files).

## 2. Semantics decisions (spec reconciliation)

1. **ASOF close-boundary wording.** The issue says the canonical window ends
   "at the last candle that closed strictly before the ASOF" and, in the same
   requirement, "a candle is admissible iff its close time <= ASOF at µs
   precision". H1-D implements the explicit `iff` rule — `close_us <= asof_us`
   — which matches the frozen Issue #297/#308 inclusive-at-close-boundary
   contract (`open_ms + tf_ms <= evaluation_asof_ms`) that must not be
   weakened. The boundary tests pin this at T-1ms / T / T+1ms and ±1µs
   (`test_me_entry_quality_h1d_coverage_v1.py::TestAsofBoundaries`).
2. **Gap vs insufficient history.** A hole strictly INSIDE the delivered
   closed series is source corruption → `INVALID_SOURCE` (`CANDLE_GAP`).
   A contiguous source that simply does not reach 60/60 (e.g. 59/60 — short
   history or stale tail) → `INCOMPLETE_COVERAGE` with the EXACT missing
   buckets. Neither is ever synthetic-filled.
3. **Empty payload.** Zero rows delivered → `INVALID_SOURCE`/`EMPTY_SERIES`
   (there is no source at all), consistent with the frozen #297 validator.
   Future/unclosed rows are excluded at µs precision BEFORE any OHLCV
   validation (ASOF isolation — corrupt future rows cannot invalidate or
   influence a historical classification).
4. **Fetch error taxonomy** (all fail-closed, `__cause__` preserved, no
   partial rows ever returned): `HtfFetchRateLimitError` (HTTP 429 /
   `retCode=10006`), `HtfFetchLimiterDeniedError` (shared limiter
   `SKIPPED`/`STATE_UNAVAILABLE`/`REVOKED`/`LIMITER_UNAVAILABLE` — the
   latter added by the audit fix D2 so a limiter ERROR is never
   misclassified as a Bybit rate limit), `HtfFetchBreakerDeniedError`
   (`BREAKER_OPEN`, and — via the documented message markers
   `cooldown open` / `storm guard` — the process-local breaker deferral
   and the retry-storm guard, which carry no structured flag),
   `HtfFetchTimeoutError`, `HtfFetchApiError`,
   `HtfFetchRequestCapExceeded` (hard pagination cap), and
   `HtfFetchRowConflictError` (same open time, different content — never
   resolved silently). Identical cross-page overlaps are deduplicated and
   counted; malformed/off-grid/out-of-bounds rows are rejected and counted,
   never fabricated.
5. **`D` interval isolation.** Native Bybit `interval=D` is accepted ONLY by
   the research adapter (`htf_fetch.py`). `BybitClient.get_klines_range()`
   is byte-for-byte untouched and still rejects non-minute intervals; the
   purity tests assert both (source scan + live `ValueError` check + a
   `get_klines_range` sentinel in every fetch stub).
6. **Snapshot identity.** `source_identity` inside each payload is minted by
   the frozen H1-C `build_source_identity()`; the snapshot additionally
   carries `payload_sha256` over a deterministic canonical row
   serialization and a `snapshot_sha256` over the whole document. Loading
   re-derives BOTH hashes and the H1-C identity — any tamper (rows, lineage,
   ASOF, counts, labels) raises `ProvenanceTamperError`. Schema
   `H1D_HTF_SNAPSHOT_2` (audit fix D4) carries the Issue #314 §3.4
   per-dataset field list literally — `source_kind` and `evaluation_asof_ms`
   are now first-class per-timeframe dataset fields (`evaluation_asof_ms`
   is verified to equal `signal_time_ms`; the per-dataset `source_kind` is
   verified against the rebuilt identity), alongside `source_event_key` /
   `source_identity` / `lineage_hash` / `signal_time` / `timeframe` /
   `source_label` / `fetched_at_ms` / `row_count` / `payload_hash` / raw
   rows. Schema-1 documents are rejected explicitly (`unknown snapshot
   schema`).
7. **Honest labeling.** `fixture_label` is a closed set:
   `SYNTHETIC_TEST_FIXTURE` or `REAL_BYBIT_FETCH`. Synthetic payloads can
   never be recorded as real; the SOXSUSDT demonstration snapshot is
   explicitly labeled `SYNTHETIC_TEST_FIXTURE`.

## 3. Reuse of frozen contracts (no weakening)

* `build_source_identity()`, `provide_htf_candles()`, `run_h1_event()` from
  `app/research/me_entry_quality_h1c_v1` are consumed AS-IS; no H1-C/H1-A
  file is modified (the 126 H1-C tests pass unchanged as the proof).
* H1-D eligibility is STRICTLY narrower than H1-C eligibility: it requires
  the H1-C eligible outcome AND 60/60 `COMPLETE_COVERAGE` on both timeframes.
  A failed source NEVER becomes a market mode.

## 4. First event (SOXSUSDT) — REAL fetch result (2026-11-26)

* `source_event_key=d2ce270406e63cde2e7546d74344dc7bd5d0f5109893631a2387a5e2cc6a5829`
* `source_identity=MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|SOXSUSDT|LONG|5m|1795612200000`
* `signal_time=2026-11-25T13:11:00.804844+00:00` → `signal_time_us=1795612260804844`
  (µs preserved; classification ASOF = `signal_time`, never `captured_at`)
* real #305 values carried verbatim (read-only SELECT from
  `research_me_upstream_coverage.source_event` on the VPS):
  `lineage_hash=6eaa617857c3f319e97befb0c54a1dc91afda4872b93adafc10e4d6e2bdd2626`,
  `captured_at=2026-11-25T13:11:01.262873+00:00` (never an ASOF),
  `comparable_lineage_hash=2adb36d537480fd276d54cd003e4bab0338561a8692b4d8b05926e5e59660d2c`.
* `activation_freeze_ts=2026-11-24T13:31:53.365952+00:00` → `1795527113365` ms
  (floor policy) — freeze UNTOUCHED, no backfill of the unverified interval.
* **REAL fetch (owner GO 2026-11-26, guarded runner):** Bybit V5 klines
  `interval=240` + native `interval=D`, canonical windows, 4H=60/60 and
  1D=60/60 closed candles; snapshot `H1D_HTF_SNAPSHOT_2`,
  `fixture_label=REAL_BYBIT_FETCH`,
  `snapshot_sha256=eca45f561a82884d0ced4cf43191e2c0b937b91d77c579739ce991be86d724ba`,
  `payload_sha256` 4H=`2e343be9…a021`, 1D=`78ee0a9f…e714` (full values in
  `docs/research/314_h1d_validation_report_v1.md`).
* **REAL classification:** `regime_4h=BEARISH` / `bos_4h=BEARISH_BOS`,
  `regime_1d=NEUTRAL` / `bos_1d=BEARISH_BOS`, `combined_state=BEARISH/NEUTRAL`,
  both coverages `COMPLETE_COVERAGE` 60/60, `eligible=True`,
  `eligibility_reason=ELIGIBLE`. Offline replay from the snapshot is
  BYTE-IDENTICAL to the direct output (SHA-256 `CA3988E1…36E6A`).
* The earlier offline demonstration (synthetic rows, `BULLISH/BULLISH`,
  explicitly labeled `SYNTHETIC_TEST_FIXTURE`) remains in `reports/` as
  machinery evidence only; it is NEVER a claim of market coverage.

## 5. Runner authorization gate (real fetch)

`python -m app.research.me_entry_quality_h1d_v1.runner --fetch` REFUSES
(exit code 2) unless BOTH:

1. the explicit flag `--fetch-authorized` is passed, AND
2. the owner-approval sentinel file exists (`--sentinel`, default
   `./H1D_REAL_FETCH_APPROVED.sentinel`).

A real Bybit fetch requires **separate owner approval after review** of this
code — offline tests and the #305 `COVERAGE_PASS` do NOT authorize it. Even
in fetch mode every limiter / breaker / 429 / 10006 denial fails closed
(explicit error, non-zero exit, NO snapshot written, rows never fabricated).

Owner GO granted 2026-11-26 after the independent code review (sentinel
`H1D_REAL_FETCH_APPROVED.sentinel` records the approval and its scope); the
guarded `--fetch --fetch-authorized` run executed exactly once for the
SOXSUSDT event (§4).

## 6. Test evidence

`393 passed in 6.67s` — 176 H1-D tests (159 offline + 5 real-client
governance integration + 12 added for the audit fixes D1–D4) + 126
pre-existing H1-C tests + 91 H1-A/B tests (contract-reuse proof).  No
PostgreSQL needed; every client is mocked or stubbed; replay tests assert
the transport is never touched; the integration suite drives
`fetch_htf_history` through the REAL `BybitClient` / `GlobalBybitRateLimiter`
/ retry budget / breaker with only the transport doubled and `urlopen`
poisoned.  Full offline-suite evidence: `reports/trackB_tests_h1d.txt` /
`reports/trackB_tests_summary.txt`; review + real-fetch evidence:
`docs/research/314_h1d_validation_report_v1.md`.
