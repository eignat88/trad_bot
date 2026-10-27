# Issue #206 — ME SHORT A executable-entry alignment evaluator V1

This tool is **offline and read-only**: it consumes CSV exports and produces a JSON report. It does not query or mutate the VPS, trade, change the frozen OOS cohort, or activate paper/live. Parent Phase-1 issue: #202.

## Frozen vs executable
- Frozen: immediate SHORT at `reference_price` (recent_high of 5m bars); absolute `stop_price` and `target_price` unchanged.
- Executable: immediate SHORT at Phase-1 `estimated_fill` (modeled bid-side book walk); **same absolute** stop/target. Risk denominator is stop minus corresponding entry.
- Exit: first high touching stop or low touching target on fully closed 5-minute OHLC; both in same bar -> STOP_FIRST. Maximum 240m from precise signal timestamp.
- Entire 5m signal candle is excluded; OHLC cannot order trades earlier/later than intra-bar detection.
- Gaps -> MISSING_CANDLE, invalid geometry -> INVALID_GEOMETRY, no resolved hit -> TIMEOUT_CLOSE_UNAVAILABLE (5m OHLC cannot resolve an exact hh:mm:ss timeout mark).
- Cost: frozen baseline 0.21% round-trip of entry notional, not realized fees or exchange exit slippage. `reference_price - bid` is **not separately subtracted** after rebasing at bid-fill. No double-count.
- The 3 QUSDT events overlap and are NOT statistically independent. Fixed-notional summation is hypothetical and does not model portfolio exposure caps, duplicate entry rejection or actual orders.

## Run regression and bundled example

```bash
python -m pytest -q tests/test_me_short_a_execution_alignment_counterfactual_v1.py
python tools/research/me_short_a_execution_alignment_counterfactual_v1.py --signals tests/fixtures/me_short_a_issue206_qusdt_signals.csv --candles tests/fixtures/me_short_a_issue206_qusdt_candles.csv --output /tmp/me_short_a_issue206_qusdt_report.json
```

Expected (for the provided 55 Bybit closed candle rows): QUSDT 2026-10-27 UTC: 02:37 TP at 06:30, 02:47 TP at 06:30, 02:57 SL at 03:25, modeled normal-cost sum Net R ~+3.26 (rounded). No actual trades were placed. Only the post-signal *fully closed* candle subsequence is used. Cannot assume the QUSDT results extend to the whole portfolio.

## Full Phase-1 population input schema
Signals CSV: `symbol,signal_time,reference_price,estimated_fill,stop_price,target_price` (signal_time ISO8601 UTC).
Candles CSV: `symbol,open_time,high,low,close,is_closed` (5m candle open time ISO8601 UTC; unique per symbol/time; Bybit linear feed). Provide full continuous history from first complete post-signal bar to stop/target or 240m for every signal. Offline test tolerates missing coverage by returning INCONCLUSIVE-style statuses and never imputing outcomes.

**Not implemented in this V1:** completeness audit across all symbols/days; deduping duplicate bars; source observation matching (source_observation_ids are null in known QUSDT rows); verified timeout close at 240m; actual exit fill/slippage and orderbook depth at exit; statistically valid bootstrap over correlated signals; trading/execution rollout. Results across all Phase-1 signals require providing candle CSV first; never infer missing market data.
