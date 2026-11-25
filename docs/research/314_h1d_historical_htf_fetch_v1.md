# Issue #314 — H1-D Historical HTF Fetch & Real Event Classification V1

**Stage:** DRAFT SPECIFICATION ONLY — implementation pending.  
**Parent:** #295. **Dependencies:** #297 / #308 / #305.  
**Scope:** RESEARCH_ONLY; NO_DEPLOY; NO_PRODUCTION_SIDE_EFFECTS.

## Why

On 2026-11-25, #305 upstream capture produced SOXSUSDT at 13:11:00.804844 UTC. Formal reconciliation reported COVERAGE_PASS, matched=1/1, lineage mismatches=0. This does **not** prove market-data coverage or classifier correctness.

H1-C classifier/adapter tests passed (217), but the VPS database has no persistent 4H/1D history. Existing `BybitClient.get_klines_range()` accepts numeric minute intervals (4H=`240`) and fails on native Bybit daily interval `D`.

## Implementation checklist

- [ ] Research-only bounded historical Bybit V5 fetch for `240` and `D`; preserve existing client retry/deadline/circuit-breaker/global cross-process limiter. Do not use direct HTTP or alter production call paths.
- [ ] Exact event `signal_time` causal ASOF; 60 CLOSED candles for each HTF (UTC 4H and 1D grids) with strict gaps, duplicates and OHLCV checks.
- [ ] No future/unclosed candle influences classification; no synthetic filling; `INCOMPLETE_COVERAGE` / `INVALID_SOURCE` fail closed.
- [ ] Offline snapshot with source identity, original event key and lineage, raw payload, fetched-at, row count, SHA-256; replay with no HTTP and identical output.
- [ ] Feed existing `build_source_identity` / `provide_htf_candles` / `run_h1_event`, without modifying the frozen H1 classifier.
- [ ] Report 4H + 1D regime, BOS, combined state, coverage, eligibility and diagnostics.
- [ ] Offline tests: 60/60, 59/60, gap, duplicate, invalid OHLC, ASOF boundary, future append, pagination, transient API error, limiter/breaker, deterministic replay, isolation.
- [ ] Real Bybit fetch only following separate approval; fail closed if rate limiter or source history unavailable.

## First event

- `source_event_key=d2ce270406e63cde2e7546d74344dc7bd5d0f5109893631a2387a5e2cc6a5829`
- `source_identity=MOMENTUM_EXHAUSTION_REVERSE_LONG_V2|SOXSUSDT|LONG|5m|1795612200000`
- `signal_time=2026-11-25T13:11:00.804844+00:00`
- `activation_freeze_ts=2026-11-24T13:31:53.365952+00:00`
- Unverified capture interval: `2026-11-24T13:31:53.365952+00:00` to `2026-11-25T07:14:41.230516+00:00`. Never backfill it or alter freeze.

## Hard gates

**NO_EXISTING_SERVICE_RESTART / ISOLATED_DEPLOYMENT / READ_ONLY_SOURCE / NO_PRODUCTION_SIDE_EFFECTS / NO_TRADING_LOGIC_CHANGE / NO_FREEZE_CHANGE / NO_PROSPECTIVE_BACKFILL / NO_OOS_ACTIVATION / NO_DEPLOY.**

## Completion evidence

Code + offline tests + independent review + documented safe read-only runner + source/provenance diagnostics required before a claim of H1-D readiness. This draft specification alone is not a working fetch adapter.
