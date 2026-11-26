# SHARED_FUNDING_SNAPSHOT_V1 — research contract only (Issue #304)

## Baselines observed on VPS (2026-11-26)

- V3 and OOS2 each track 12 OPEN positions, with 11 shared symbols.
- 15-minute successful attempts: V3 kline 696, funding 690; OOS2 kline 696, funding 696.
- Nearest completion-time gap between corresponding services (NOT the quote's observation age): kline median 6.133 s; funding median 6.1 s.
- Funding provider in both services calls `/v5/market/funding/history?category=linear&symbol=...&limit=1`, converts *fraction* to *percent*, preserves MISSING, INVALID and API_ERROR separately from confirmed zero; returns and cache freshness gate use 3600 s configuration.
- V3 charges backlog at the first observed rate (whole-position quantity). OOS2 additionally reconstructs historical quantities from durable close legs, holds dirty leg obligations and can defer terminal funding finalization.
- Funding regression executed in isolated VPS clone: 83 PASS. Selected OOS2 regressions: 287 PASS, 48 SKIPPED (not a clean integration-pass); existing real limiter 57 PASS; PR #319 15 PASS.

## PR scope

**Only a value object and parsing contract.** Nothing reads it from the running V3/OOS2 providers. Does not implement cache, writes, consumer admission, process coordinator, transport, trading, limiter, or scheduler. No feature flag that can be accidentally enabled.

- `FundingObservation` captures status, UTC observed_at, rate_percent (or None) and *optional* event timestamp when present in the raw Bybit row.
- Strict tri-state separation: genuine 0.0 is OK; empty history MISSING; malformed INVALID; transport error must be marked API_ERROR by caller, not falsely translated to MISSING.
- Missing funding event metadata is not invented. An OK rate with absent event timestamp is an observed value without a provable period identifier; **it is not yet reusable**.

## Blocking criteria before any shared cache / runtime integration

1. Replay both providers and their engines with the same timed event streams, including API outages, MISSING->OK transitions, multiple owed periods, confirmed 0.0, same-symbol partial closes, dirty leg storage failure, terminal STOP/TIMEOUT and restart.
2. Compare, per cycle and on durable rows: funding rates handed to engines, first-known time, `funding_periods_charged`, `funding_paid`, balances, close-leg facts, terminal obligations and PnL.
3. Fail-closed on missing/invalid event provenance, future observations, stale dates, clock rollback and any status mismatch. Verify behavior at funding settlement boundaries.
4. Resolve the 48 skipped OOS2 tests: identify Postgres-dependent cases and run them in authorized isolated integration environment; do not claim they passed.
5. Validate cross-process coordination and actual load reduction without changing safety-critical price/bar fetching. Prove by prospective soak and a separately approved release. Existing shared limiter 8 RPS / burst 12, breaker, trading policy and H2 HOLD remain untouched.

**Non-claim:** This PR does not yet improve Scanner coverage or reduce calls.
