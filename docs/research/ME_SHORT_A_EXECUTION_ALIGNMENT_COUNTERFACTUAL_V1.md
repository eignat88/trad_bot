# Issue #206 — ME SHORT A executable-entry alignment evaluator V2

This tool is **offline and read-only**: it consumes CSV exports and produces a JSON report. It does not query or mutate the VPS, trade, change the frozen OOS cohort, or activate paper/live. Parent Phase-1 issue: #202. Corrective review of PR #211 (issues #206/#202/#208) implemented in protocol `ISSUE_206_ME_SHORT_A_V2`.

## Frozen vs executable
- Frozen: immediate SHORT at `reference_price` (recent_high of 5m bars); absolute `stop_price` and `target_price` unchanged.
- Executable: immediate SHORT at Phase-1 `estimated_fill` (modeled bid-side book walk); **same absolute** stop/target. Risk denominator is stop minus corresponding entry.
- Exit: first high touching stop or low touching target on fully closed 5-minute OHLC; both in same bar -> `AMBIGUOUS_STOP_FIRST` (STOP_FIRST pricing). Maximum 240m from precise signal timestamp.
- Entire 5m signal candle is excluded; OHLC cannot order trades earlier/later than intra-bar detection.
- The modeled price `estimated_fill` is **strictly separated** from real confirmed execution: offline data can never confirm a fill (`confirmed_execution=false` everywhere). See "Execution feasibility" below.

## Candle integrity (fail-closed)
Every candle row is validated before use and every `(symbol, open_time)` slot is checked:

- strict 5-minute grid (`open_time` aligned, tz-aware, no off-grid rows);
- OHLC bounds `low <= open/close <= high`, all prices positive and parseable;
- `is_closed` must be an explicit boolean; **unclosed bars are never treated as closed**;
- bars not yet closed at `--as-of` (export timestamp) are `FUTURE_OR_UNCLOSED` and excluded;
- duplicate rows with identical OHLC are collapsed and counted (`duplicate_identical_rows`);
- rows with conflicting OHLC for one slot are **never** resolved by "first record wins": a needed slot yields the dedicated `CONFLICTING_CANDLE` status;
- excluded (unclosed/future/corrupt/off-grid) rows are counted with reasons; the hole they leave is classified like any other missing data — nothing is imputed.

## Horizon / timeout statuses
| Status | Meaning |
|---|---|
| `FINAL` | Confirmed TP/SL on a complete closed-candle path, or timeout **correctly computed** from the close of the bar ending exactly at the cutoff (signal exactly on the 5m grid). |
| `MISSING_CANDLE` | Gap *inside* the provided history (`GAP_IN_HISTORY`, `expected_open` names the hole). |
| `SOURCE_INCOMPLETE_COVERAGE` | The export does not cover enough to evaluate: no candles (`NO_CANDLES`), tail truncation (`SOURCE_ENDS_BEFORE_HORIZON`) or an unclosed cutoff bar (`CUTOFF_CANDLE_UNCLOSED`). |
| `TIMEOUT_CLOSE_UNAVAILABLE` | History complete but the timeout mark falls mid-candle: 5m OHLC cannot resolve an exact hh:mm:ss timeout close. |
| `CONFLICTING_CANDLE` | A needed slot holds conflicting OHLC records (see above). |
| `INVALID_GEOMETRY` | `entry > target > 0 and stop > entry` violated. |
| `MISSING_ENTRY` | The arm's entry price is absent. |

A mid-candle cutoff is only `TIMEOUT_CLOSE_UNAVAILABLE` when the closed-candle history is complete through the straddling bar; otherwise the data problem (`MISSING_CANDLE` / `SOURCE_INCOMPLETE_COVERAGE`) takes precedence. No outcome is finalized on an incomplete path.

## Execution feasibility
`assess_execution_feasibility` re-checks the Phase-1 market-microstructure evidence exported with each signal and **recalculates** the modeled order (quantity-first: `requested_qty = notional/reference`, rounded down to `qtyStep` into `executable_qty`, book-walk fill):

- required evidence: `bid, ask, book_time_ms, depth_bid_10bps_usdt, requested_notional_usdt, requested_qty, executable_qty, min_order_qty, qty_step, min_notional` (plus `estimated_fill`);
- quote/order book timestamp and freshness: `signal_time - book_time + assumed_latency_ms <= 10s`, snapshot not in the future (>1s skew);
- spread sanity (`ask >= bid > 0`) and fill consistency (`0 < estimated_fill <= best bid`);
- notional/quantity/exchange minima: `executable_qty >= minOrderQty`, `order_value >= minNotionalValue`;
- available depth (`depth_bid_10bps_usdt >= order_value`).

| Status | Meaning |
|---|---|
| `EXECUTION_FEASIBILITY_MODELED` | All modeled checks pass — **model only**, never a confirmed fill. |
| `EXECUTION_FEASIBILITY_UNKNOWN` | Missing/stale/future/corrupt evidence (`MISSING_EXECUTION_EVIDENCE`, `STALE_BOOK_SNAPSHOT`, `FUTURE_BOOK_SNAPSHOT`, `FILL_INCONSISTENT_WITH_BOOK`, `*_INCONSISTENT`, `INVALID_QUOTE`, `INVALID_CONSTRAINTS`). Never treated as an executable trade. |
| `EXECUTION_INFEASIBLE` | Known constraint violation: `BELOW_EXCHANGE_MIN:*` or `INSUFFICIENT_DEPTH`. |

`assumed_latency_ms` is optional; when absent it defaults to 0 and is reported as `DEFAULT_ZERO` in the evidence.

## Costs: scenarios and decomposition (no double-counting)
- Two frozen cost scenarios are always reported per arm: `net_r_normal` (0.21% round-trip) and `net_r_elevated` (0.31%); `net_r` mirrors the normal scenario.
- Decomposition (per event, `cost_decomposition`):
  - `reference_to_bid_drift_bps` — **embedded in the executable entry price**, never added to `cost_rate`;
  - `market_impact_bps` (bid -> weighted fill) — **embedded in the executable entry price**, never added to `cost_rate`;
  - `spread_bps` — covered once inside `cost_rate`;
  - `fees_roundtrip_pct` (2 x 0.055%) — covered once inside `cost_rate`;
  - `exit_slippage` — `NOT_MODELED_UNKNOWN_NOT_ZERO` (elevated scenario only widens the allowance).
- Missing inputs are reported as `null` (unknown), never zero.

## Paired comparison, coverage, bootstrap
- One canonical `event_key` per event: `sha256(symbol|SHORT|<utc isoformat signal_time>|<ref>|<stop>|<target>)` with prices normalized to the frozen float-repr representation (`canonical_price`, the writer/reader identity contract fixed in #212), byte-identical to the Phase-1/lineage identity (migration 077; guarded by a parity regression test).
- `paired` compares frozen vs executable on that key. A pair is combined **only when both sides carry the required data** (resolved `FINAL` outcomes); anything else is excluded with an explicit reason and never merged.
- `coverage` reports per-symbol and per-UTC-day event counts, status counts, feasibility counts and comparable pairs, plus candle-integrity counts.
- `bootstrap` uses a **symbol-day cluster bootstrap** (`CLUSTER_BOOTSTRAP_SYMBOL_UTC_DAY`): repeated/overlapping signals of one symbol on one day stay in one cluster, preserving their correlation when clusters are resampled with replacement (seeded, deterministic). Series: frozen/executable net R (normal & elevated), executable net R restricted to `EXECUTION_FEASIBILITY_MODELED` events, and paired deltas over comparable pairs.

## Run regression and bundled example

```bash
python -m pytest -q tests/test_me_short_a_execution_alignment_counterfactual_v1.py
python tools/research/me_short_a_execution_alignment_counterfactual_v1.py --signals tests/fixtures/me_short_a_issue206_qusdt_signals.csv --candles tests/fixtures/me_short_a_issue206_qusdt_candles.csv --output /tmp/me_short_a_issue206_qusdt_report.json
```

Expected (for the provided 55 Bybit closed candle rows): QUSDT 2026-10-27 UTC: 02:37 TP at 06:30, 02:47 TP at 06:30, 02:57 SL at 03:25, modeled normal-cost sum Net R ~+3.26 (rounded); executable feasibility `EXECUTION_FEASIBILITY_UNKNOWN` (the fixture carries no order book evidence — modeled entries are **not** automatically executable). No actual trades were placed. Only the post-signal *fully closed* candle subsequence is used. Cannot assume the QUSDT results extend to the whole portfolio.

Fixture provenance: `tests/fixtures/me_short_a_issue206_qusdt_candles.csv` is a read-only Bybit QUSDT 5m export (27 Oct 2026). The corrective review added the real `open` column to the same snapshot (verified byte-for-byte against the Bybit public kline endpoint for high/low/close; all previously frozen values unchanged) so that the full `low <= open/close <= high` check can run.

## Full Phase-1 population input schema
Signals CSV: `symbol,signal_time,reference_price,estimated_fill,stop_price,target_price` plus execution evidence (`bid,ask,book_time_ms,depth_bid_10bps_usdt,requested_notional_usdt,requested_qty,executable_qty,min_order_qty,qty_step,min_notional`, optional `assumed_latency_ms`) (signal_time ISO8601 UTC).
Candles CSV: `symbol,open_time,open,high,low,close,is_closed` (5m candle open time ISO8601 UTC; unique per symbol/time; Bybit linear feed). Provide full continuous history from first complete post-signal bar to stop/target or 240m for every signal. Offline evaluation tolerates missing coverage by returning the explicit statuses above and never imputes outcomes.

## Limitations remaining for a full Phase-1 analysis
- Direct OOS outcome linkage (join to realized Phase-1/forward outcomes) is not implemented here.
- Exit fill/slippage and order book depth at exit are not modeled (`NOT_MODELED_UNKNOWN_NOT_ZERO`).
- Timeout closes remain unresolvable for mid-candle cutoffs from 5m OHLC by construction.
- Overlap/portfolio exposure caps, duplicate-entry rejection and actual order handling are not modeled (`overlap_portfolio: NOT_MODELED`); the QUSDT events overlap and are not independent.
- The bootstrap is correlation-aware but asymptotics over few clusters remain weak; report `n_clusters` alongside every CI.
- Frozen-vs-executable `delta_net_r` is a diagnostic only: the two arms have different risk denominators (absolute stop vs rebased entry), so R magnitudes are not directly comparable as performance.
- Results across all Phase-1 signals require providing candle CSV first; never infer missing market data.
