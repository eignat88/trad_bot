# Issue #304 — SAFE_REQUEST_COORDINATION model (offline-only)

**Status:** Draft research artifact. **Not connected to any production runtime.**
No service receives a new priority; no feature flag is read by running services.

## Verified wiring (2026-11-26 VPS read-only audit)

- Scanner context builder: per symbol kline 5/15/60/1D (limits 200/200/200/8).
- V3 and OOS2 runtime gather `open_symbols` and prospective `entry_symbols`.
- `snapshot(price_symbols=open_symbols | entry_symbols, bar_symbols=open_symbols, funding_symbols=open_symbols)`.
- V3/OOS2 tickers is one *whole-market* call, not one per symbol.
- Kline and funding in these runtimes service **open positions**, including protective exits / funding obligations.
- Existing shared limiter is global; market kline and tickers default P1, funding P2; token accounting and breaker are safety-critical. Changing a process-local priority is not equivalent to reserving Scanner capacity.

## Conservative evidence labels

1. PROTECTIVE: explicit open position bar/funding, and mixed whole-market tickers when open positions exist.
2. SCANNER: context-building kline request from scanner service.
3. ENTRY_ONLY: reserved for a future *demonstrably separable* source. **No deployed V3/OOS2 requests currently qualify**.
4. UNKNOWN: missing or conflicting evidence. Never auto-downgrade.

## Pure simulation

`simulate_slots()` is a deterministic counterexample/test harness. Its candidate orders PROTECTIVE/UNKNOWN ahead of SCANNER ahead of ENTRY_ONLY. Its default-off reference keeps original order. It explicitly accounts for unsent work and hard deadlines. It deliberately does **not** model the actual Bybit breaker, rate limiter token/credit state, HTTP attempts, process-local deadlines, concurrent services, OOS freshness or shared persistence. Therefore **even passing tests cannot prove production improvement** or no additional protective latency.

Illustrative overload: 2 protective + 4 Scanner + 4 discretionary requests with capacity 8. A candidate can serve protective 2 and Scanner 4 but **not all 10**. With capacity 3 it serves protective 2 and Scanner 1; a 50/50 guarantee is impossible without demonstrated sufficient remaining capacity.

## Mandatory follow-up before a runtime PR

- Find actually deferrable traffic (if any), with proof that it cannot block an open-position SL/TP/TIMEOUT/funding cycle.
- Validate actual budget/arrival/rate-limit/breaker dynamics from captured telemetry (completion time is not HTTP dispatch time).
- Inject contention into the **real** shared limiter with a fake clock and stubbed Bybit client. Assert protective latency, Scanner deadline coverage, no bypass, exactly-once grant accounting, breaker fail-closed, no OOS freeze or trading changes.
- Separate rollout approval, default-off flag, prospective soak with rollback. H2 stays inactive.

**Do not** merge as a coverage fix, activate on VPS, change weights/G-RPS/burst or restart any service based on this modeling PR.
