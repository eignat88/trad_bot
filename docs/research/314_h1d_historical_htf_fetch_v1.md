# Issue #314 — H1-D Historical HTF Fetch & Real Event Classification V1

**Stage:** IMPLEMENTATION + REAL FETCH COMPLETE (this branch, pending review/merge) — real Bybit fetch executed 2026-11-26 under separate owner GO after code review; result recorded in `314_h1d_validation_report_v1.md`.  
**Parent:** #295. **Dependencies:** #297 / #308 / #305.  
**Scope:** RESEARCH_ONLY; NO_DEPLOY; NO_PRODUCTION_SIDE_EFFECTS.

## Why

On 2026-11-25, #305 upstream capture produced SOXSUSDT at 13:11:00.804844 UTC. Formal reconciliation reported COVERAGE_PASS, matched=1/1, lineage mismatches=0. This does **not** prove market-data coverage or classifier correctness.

H1-C classifier/adapter tests passed (217), but the VPS database has no persistent 4H/1D history. Existing `BybitClient.get_klines_range()` accepts numeric minute intervals (4H=`240`) and fails on native Bybit daily interval `D`.

## Implementation checklist

- [x] Research-only bounded historical Bybit V5 fetch for `240` and `D`; preserve existing client retry/deadline/circuit-breaker/global cross-process limiter. Do not use direct HTTP or alter production call paths. — `app/research/me_entry_quality_h1d_v1/htf_fetch.py` (`H1D_HTF_4H_FETCH_V1` / `H1D_HTF_1D_FETCH_V1`), `_public_get()` only, `get_klines_range()` untouched.
- [x] Exact event `signal_time` causal ASOF; 60 CLOSED candles for each HTF (UTC 4H and 1D grids) with strict gaps, duplicates and OHLCV checks. — `coverage.py` (`H1D_CAUSAL_ASOF_COVERAGE_V1`, µs precision).
- [x] No future/unclosed candle influences classification; no synthetic filling; `INCOMPLETE_COVERAGE` / `INVALID_SOURCE` fail closed.
- [x] Offline snapshot with source identity, original event key and lineage, raw payload, fetched-at, row count, SHA-256; replay with no HTTP and identical output. — `provenance.py` (`H1D_PROVENANCE_REPLAY_V1`, canonical SHA-256 + tamper detection).
- [x] Feed existing `build_source_identity` / `provide_htf_candles` / `run_h1_event`, without modifying the frozen H1 classifier. — `classification.py` (`H1D_REAL_EVENT_CLASSIFICATION_V1`); H1-C suite re-run unweakened (126 passed).
- [x] Report 4H + 1D regime, BOS, combined state, coverage, eligibility and diagnostics.
- [x] Offline tests: 60/60, 59/60, gap, duplicate, invalid OHLC, ASOF boundary, future append, pagination, transient API error, limiter/breaker, deterministic replay, isolation. — `tests/test_me_entry_quality_h1d_*_v1.py` (176 tests, all offline; incl. real-client governance integration `test_me_entry_quality_h1d_integration_v1.py`).
- [x] Real Bybit fetch only following separate approval; fail closed if rate limiter or source history unavailable. — enforcement in `runner.py` (`--fetch` requires `--fetch-authorized` AND owner-approval sentinel); REAL fetch executed 2026-11-26 under separate owner GO after code review: 4H=60/60, 1D=60/60 closed candles, `REAL_BYBIT_FETCH` snapshot with SHA-256 provenance, offline replay byte-identical. Fail-closed paths covered by tests (limiter / breaker / 429 / 10006 / empty history).

See `docs/research/314_h1d_implementation_notes_v1.md` for the implementation notes (semantics decisions, error taxonomy, UNKNOWN items).

## First event

- `source_event_key=d2ce270406e63cde2e7546d74344dc7bd5d0f5109893631a2387a5e2cc6a5829`
- `source_identity=MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|SOXSUSDT|LONG|5m|1795612200000`
- `signal_time=2026-11-25T13:11:00.804844+00:00`
- `captured_at=2026-11-25T13:11:01.262873+00:00` (real #305 value; never used as ASOF)
- `lineage_hash=6eaa617857c3f319e97befb0c54a1dc91afda4872b93adafc10e4d6e2bdd2626` (real #305 value, carried verbatim)
- `activation_freeze_ts=2026-11-24T13:31:53.365952+00:00`
- Unverified capture interval: `2026-11-24T13:31:53.365952+00:00` to `2026-11-25T07:14:41.230516+00:00`. Never backfill it or alter freeze.

**Real classification (2026-11-26, `REAL_BYBIT_FETCH` snapshot, ASOF = `signal_time`):**
`regime_4h=BEARISH` / `bos_4h=BEARISH_BOS` / `coverage_4h=60/60 COMPLETE`,
`regime_1d=NEUTRAL` / `bos_1d=BEARISH_BOS` / `coverage_1d=60/60 COMPLETE`,
`combined_state=BEARISH/NEUTRAL`, `eligible=True`, `eligibility_reason=ELIGIBLE`.
Snapshot SHA-256 `eca45f561a82884d0ced4cf43191e2c0b937b91d77c579739ce991be86d724ba`
(`H1D_HTF_SNAPSHOT_2`); offline replay byte-identical to the direct run.
This is a RESEARCH classification of HTF market structure — it is NOT a
trading signal and does NOT authorize any position.

## Hard gates

**NO_EXISTING_SERVICE_RESTART / ISOLATED_DEPLOYMENT / READ_ONLY_SOURCE / NO_PRODUCTION_SIDE_EFFECTS / NO_TRADING_LOGIC_CHANGE / NO_FREEZE_CHANGE / NO_PROSPECTIVE_BACKFILL / NO_OOS_ACTIVATION / NO_DEPLOY.**

## Completion evidence

Code + offline tests + independent review + documented safe read-only runner + source/provenance diagnostics required before a claim of H1-D readiness. This draft specification alone is not a working fetch adapter.
