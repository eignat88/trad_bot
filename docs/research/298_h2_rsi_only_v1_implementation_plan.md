# #298 — H2_RSI_ONLY implementation handoff (draft PR)

**Parent:** #295. **Scope:** isolated research-only implementation of H2 RSI confirmation; QRO/COMBINED deferred until formula/parity verification. This initial PR is SPEC-ONLY; it does not contain runtime implementation.

## Verified baseline / blockers (2026-11-25)
- `app/scanners/momentum_exhaustion_reverse_long_v2.py`: Wilder RSI(14) on closed 5m candles; eligible detector events require `rsi_delta_3 > 0`. Therefore `rsi_delta_3 < 0` on the **same** detector event is not a viable H2 filter. Confirm a turn **after** the signal.
- Independent upstream cohort: `research_me_upstream_coverage.source_event` (#305), with original `source_event_key`, `source_identity`, `signal_time`, `activation_freeze_ts`, `lineage_hash`, `features.rsi_14`, `features.rsi_delta_3` and `entry_confirmation_rsi_rising`.
- Reuse #308's event adapter, as-of timestamp policy and lineage contract where possible, without modifying H1. `signal_time` is original ASOF; `captured_at` is delivery, never signal-time ASOF. Preserve microsecond precision; derived ms view floors.
- Original source RSI may be from a still-forming candle and must remain a VERBATIM snapshot, distinct from H2's independently recomputed RSI on subsequently **closed** candles.
- VPS #305 activation RUNNING and runtime flag enabled, but at last audit `source_event=0`, `rejected_event=0`, `capture_health=0`; **no proven prospective cohort / source coverage yet**. Frozen #305 freeze_ts cannot be moved and no historical backfill is allowed.
- VPS does not have a usable deployed `market.candle` table. `BybitClient.get_klines_range` exists, but public API fetch requires separately reviewed bounded research budget and no contention with scanner. Pure H2 code should accept caller-supplied OHLCV, with no DB/network calls.

## Agent deliverables
Create `app/research/me_entry_quality_h2_v1/` with pure event adapter/reuse, 5m candle validator/provider, deterministic RSI confirmation, result model, pipeline, and separate tests. No migrations, systemd, deployment or changes to scanner, paper, evaluator, trading gates, existing source #305, H1, or other OOS.

### Frozen-candidate H2 branches (spec to freeze before capture)
- `H2_RSI_TURN_1`: first strictly decreasing RSI on a fully closed post-signal 5m candle relative to prior closed candle; no intra-candle signal.
- `H2_RSI_TURN_2`: two consecutive strictly decreasing closed RSI observations **after** the original signal. No retroactive confirmation based on pre-signal bars.
- `H2_RSI_PEAK_REVERSAL`: causal local-peak recognition with explicit lag and no future-centered pivots; if ambiguous, mark `NOT_READY` until prespecified.
- Candidate observation windows: 3 and 6 **post-signal** closed 5m candles, not approved defaults. Require explicit immutable/versioned policy, parameter fingerprint, and no post-hoc selection.
- Each original source event yields variant-specific `PENDING_CONFIRMATION / CONFIRMED_DELAYED / EXPIRED / INVALID_SOURCE / INCOMPLETE_COVERAGE`; original signal time/price, confirmation close time, independently specified executable entry time/price, expiry and coverage. Never use original entry price for delayed entry; never assume execution at an unfillable candle close.
- Pair by original event key against unchanged control. Do not label rejected or expired events with fictional realized 0R. Record missed winners, entry latency, coverage, MAE/MFE and eventual net E[R] under the separate frozen outcome policy.

### Causal/data-integrity requirements
- Closed candle iff `open_time + 300000 ms <= evaluation_asof_ms`. For post-signal observations require candle close **strictly later** than original signal instant, even at same-millisecond boundaries; tests must cover microseconds.
- 5m UTC grid, finite valid OHLCV, correct order, duplicate/conflicting candles rejected, contiguous RSI warmup history and full path through confirmation/expiry, no partial/future candle leakage; missing paths fail closed.
- Use same Wilder RSI implementation semantics as original `app/indicators/technical.py`, verifying parity on identical input close sequences; fail closed on mismatch.
- Preserve immutable source lineage, source snapshot, original `rsi_14`, hashes; provider provenance includes source kind/label, row count, payload hash, exact time window, coverage and fetched_at; bad/missing data => explicit reason codes.
- Pure package: no DB/network/filesystem/clock, no imports from production into H2 runtime path or reverse wiring; no trading behaviors.

### Test/acceptance
Unit tests: Wilder RSI parity, 5m boundary T-1ms/T/T+1ms and sub-ms timestamp, future-append invariance, signal on forming candle, zero/1/2 RSI declines, causal peak, delayed expiry, incomplete/missing candles, bad OHLC, out-of-order/duplicate candles, insufficient RSI seed, hash/source tampering, idempotency & lineage immutability. AST/grep purity and production isolation tests plus offline regression suite.
**DEPLOY HOLD**: no prospective activation until #305 independently demonstrates source coverage, bounded candle-fetch source is approved, result/outcome policy and parameters/freeze are fixed, and parity/ASOF/integrity tests pass. Do not stop/restart/reload any existing systemd services. No historical backfill. No QRO proxy.
