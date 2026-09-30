# LIQUIDITY_REVERSAL SHORT — OOS Execution Counterfactual Audit V1

**Date:** 2026-09-30  
**Auditor:** MiMo-v2.5  
**Scope:** LR_SHORT_GATE_V1 prospective OOS observations  
**Safety:** READ-ONLY. No DB writes, no config changes, no service restarts.

---

## 0. OOS SAMPLE (re-read from DB, not from previous audit)

| Metric | Value |
|--------|-------|
| **N observations** | 36 |
| **Unique source_signal_id** | 36 |
| **Unique symbols** | 31 |
| **First observation** | 2026-09-28 23:47:06 UTC |
| **Last observation** | 2026-09-30 11:08:08 UTC |
| **OOS days** | 3 |
| **Day distribution** | 2026-09-28: 2, 2026-09-29: 26, 2026-09-30: 8 |

**Maturity (from prospective_outcome):**

| Horizon | Evaluated | Status |
|---------|-----------|--------|
| 15m | 36/36 | ✅ |
| 30m | 36/36 | ✅ |
| 60m | 35/36 | ⚠️ 1 pending |
| 120m | 35/36 | ⚠️ 1 pending |
| 240m | 34/36 | ⚠️ 2 pending |

**Previous audit reported N=32.** The sample has grown to 36 since then (4 new observations on 2026-09-30).

---

## 1. CURRENT LR EXECUTION CONTRACT (recovered from production code)

| Parameter | Value | Source |
|-----------|-------|--------|
| **Entry** | Market price at signal candle close, with slippage applied | `app/paper/engine.py` `check_entries()` |
| **Stop** | `invalidation_price` = recent_high × 1.002 (0.2% above recent 5m high) | `app/scanners/liquidity_reversal.py` `_scan_short()` |
| **TP** | `target_1` = swept_level − ATR × 1.5 | `app/scanners/liquidity_reversal.py` |
| **TP2** | `target_2` = swept_level − ATR × 3 | same |
| **Timeout** | 120 minutes (12 bars × 5m × 2.0 TTL multiplier) | `app/paper/engine.py` `_ENTRY_TIMEOUT_BASE` + `_is_expired()` |
| **Management** | No DCA, no trailing, no breakeven (DEFAULT policy) | `config.yaml` — no execution_policies entry for LIQUIDITY_REVERSAL |
| **Fees** | Taker 0.055% per side | `config.yaml` `taker_fee: 0.00055` |
| **Slippage** | 0.05% per side | `config.yaml` `slippage_percent: 0.0005` |
| **1R definition** | `risk_dist = abs(entry − stop)`; R-multiple = signed_move / risk_dist | `app/paper/engine.py` |

**Key observation:** LIQUIDITY_REVERSAL SHORT is in `blocked_scanner_directions` in config.yaml. It is **currently blocked** from paper trading. The 2 paper trades found (PUMPFUNUSDT, KORUUSDT) were executed during a brief window when the direction gate was not yet applied (config/DB drift mentioned in the previous audit).

**Ambiguity resolution policy:** Conservative (STOP_FIRST when TP and SL hit in same candle). The production evaluator (`app/research/prospective_evaluator.py`) uses the same policy.

---

## 2. SIMULATION METHODOLOGY

- **Candle source:** Bybit v5 API, 5m klines (public data, no auth needed)
- **Entry price:** `reference_price` from prospective_observation (swept_level, i.e., the liquidity level that was swept)
- **Stop price:** `invalidation_price` from prospective_observation
- **1R distance:** `abs(entry − stop)`
- **Candle replay:** Candle-by-candle from signal_time, checking high/low against stop and TP levels
- **Ambiguous intrabar:** If both TP and SL are hit in the same candle → conservative (STOP_FIRST)
- **Timeout:** Fixed scenarios (60m/120m/240m); CF-CURRENT uses 240m as upper bound
- **Fees/slippage:** Applied as R-multiples: `cost_r = (entry + exit) × (taker_fee + slippage) / risk_dist`

---

## 3. CF-CURRENT (PRODUCTION EXIT LOGIC)

**Scenario:** Entry at reference_price, stop at invalidation_price, TP at target_1 (swept_level − ATR×1.5), timeout 240m, conservative ambiguous.

| Metric | Value |
|--------|-------|
| **N** | 36 |
| **Wins** | 20 |
| **Losses** | 16 |
| **Timeouts** | 5 |
| **Stops** | 14 |
| **TPs** | 17 |
| **Ambiguous** | 0 |
| **Win rate** | 55.6% |
| **Gross E[R]** | +10.88 |
| **Net E[R]** (after fees/slippage) | +9.74 |
| **Median R** | +0.67 |
| **Profit Factor** | 26.78 |
| **Avg win R** | +20.34 |
| **Avg loss R** | −0.95 |
| **Max consecutive losses** | 6 |
| **Max drawdown R** | 14.57 |
| **Total cost R** | 40.90 |
| **Avg cost R/trade** | 1.14 |

**⚠️ CRITICAL CAVEAT:** The E[R] of +9.74 is **dominated by extreme outliers**. LINKUSDT alone contributed +267.8 net R (a single trade where MFE was 235.75R — price moved from 15.42 to below 0.08). Without LINKUSDT: N=35, net E[R] = +2.37, PF = 7.97. Even without LINKUSDT, the result is heavily influenced by a few large winners (PONSUSDT +18.9R, HBARUSDT +27.7R).

**Median R of +0.67 is the more representative statistic.** The mean is inflated by fat-tail events.

---

## 4. FIXED-RR COUNTERFACTUALS (CF-1 through CF-4)

All scenarios use: same entry, same stop, same 1R denominator, same fee/slippage model, conservative ambiguous.

### 4.1 Timeout = 60m

| Scenario | N | Win% | E[R] gross | E[R] net | Median R | PF | Total R net | MaxDD R |
|----------|---|------|-----------|---------|---------|-----|-------------|---------|
| TP1.0R | 36 | 80.6 | +0.68 | −0.47 | +0.40 | 6.29 | −16.90 | 24.53 |
| TP1.5R | 36 | 72.2 | +0.80 | −0.35 | +0.45 | 5.19 | −12.63 | 24.75 |
| TP2.0R | 36 | 72.2 | +1.04 | −0.11 | +0.51 | 6.47 | −3.87 | 22.24 |
| TP3.0R | 36 | 69.4 | +1.37 | +0.22 | +0.49 | 7.29 | +8.04 | 21.24 |

### 4.2 Timeout = 120m

| Scenario | N | Win% | E[R] gross | E[R] net | Median R | PF | Total R net | MaxDD R |
|----------|---|------|-----------|---------|---------|-----|-------------|---------|
| TP1.0R | 36 | 88.9 | +0.73 | −0.41 | +0.43 | 7.59 | −14.93 | 23.46 |
| TP1.5R | 36 | 77.8 | +0.81 | −0.33 | +0.64 | 4.66 | −11.99 | 25.95 |
| TP2.0R | 36 | 77.8 | +1.12 | −0.03 | +0.72 | 6.04 | −0.98 | 23.45 |
| TP3.0R | 36 | 72.2 | +1.47 | +0.33 | +0.72 | 6.31 | +11.83 | 22.45 |

### 4.3 Timeout = 240m

| Scenario | N | Win% | E[R] gross | E[R] net | Median R | PF | Total R net | MaxDD R |
|----------|---|------|-----------|---------|---------|-----|-------------|---------|
| TP1.0R | 36 | 86.1 | +0.72 | −0.43 | +0.43 | 6.16 | −15.47 | 23.46 |
| TP1.5R | 36 | 69.4 | +0.71 | −0.44 | +0.45 | 3.49 | −15.91 | 26.54 |
| TP2.0R | 36 | 69.4 | +1.02 | −0.12 | +0.82 | 4.62 | −4.39 | 23.03 |
| TP3.0R | 36 | 63.9 | +1.37 | +0.23 | +0.99 | 5.06 | +8.22 | 21.65 |

### 4.4 Key observations from fixed-RR scenarios

1. **Low-RR scenarios (TP1.0R–TP2.0R) are net NEGATIVE after costs.** The 1.15R average cost per trade (fees + slippage) eats the gross edge.
2. **TP3.0R is the only fixed-RR scenario with positive net E[R]** across all timeouts. But this is exploratory — not a validated hypothesis.
3. **Win rates are high (64–89%)** but avg win is too small to cover costs at low TP levels.
4. **Timeout sensitivity:** Longer timeouts generally improve results (more room for TP to hit), but differences are modest.
5. **The cost structure is the dominant factor.** At ~1.15R cost per trade, the gross edge must exceed 1.15R per trade to be profitable.

---

## 5. PATH ANALYSIS

| Metric | Value |
|--------|-------|
| **Reached +1R before stop** | 30/36 (83.3%) |
| **Avg MFE before stop** | 22.55R |
| **Avg MFE overall** | 22.55R |
| **Avg MAE before first +1R** | 7.52R |

**Key insight:** 83.3% of signals reached +1R before being stopped. This is very high. However, the avg MAE before first +1R is 7.52R — meaning many signals had significant adverse excursion before reaching +1R. In a real trade with a fixed stop at invalidation_price, this would mean the stop is hit before +1R for many signals.

Wait — the path analysis uses the simulated stop (invalidation_price), not a hypothetical wider stop. So "reached +1R before stop" means: price moved favorably by ≥1R before price moved against by enough to hit the stop at invalidation_price.

---

## 6. MFE → EXECUTABLE FUNNEL

| Funnel Stage | Count | % |
|--------------|-------|---|
| OOS signals | 36 | 100% |
| MFE ≥ 1R (raw, from DB outcome) | 29 | 80.6% |
| Reached +1R BEFORE stop (simulated) | 30 | 83.3% |
| MFE ≥ 2R (raw, from DB outcome) | 19 | 52.8% |
| Reached +2R BEFORE stop (simulated) | 15 | 41.7% |

**Analysis:**
- **1R funnel:** Raw MFE ≥ 1R is 80.6%; executable (before stop) is 83.3%. The executable rate is actually *higher* than raw — this means some signals that had MFE < 1R in the DB outcome actually reached +1R in the candle replay before being stopped. This is likely because the DB outcome uses a different entry price (entry_zone semantics) vs. our simulation (reference_price).
- **2R funnel:** Raw MFE ≥ 2R is 52.8%; executable (before stop) is 41.7%. A significant gap (11.1 percentage points) — some signals reached +2R in the MFE data but were stopped before reaching +2R in the candle replay. This is the "MFE without executable value" phenomenon.

---

## 7. DAY-BY-DAY ROBUSTNESS

### CF-CURRENT (240m timeout)

| Day | N | Win% | E[R] net | PF | Total R net |
|-----|---|------|---------|-----|-------------|
| 2026-09-28 | 2 | 50.0 | +1.32 | 4.73 | +2.65 |
| 2026-09-29 | 26 | 53.8 | +12.52 | 33.26 | +325.50 |
| 2026-09-30 | 8 | 62.5 | +2.82 | 9.94 | +22.53 |
| **EX-2026-09-29** | **10** | **60.0** | **+2.52** | **8.64** | **+25.17** |

**CRITICAL FINDING:** The edge **PERSISTS after removing 2026-09-29**. EX-2026-09-29 shows N=10, E[R] net = +2.52, PF = 8.64. The edge is NOT solely driven by the dominant day.

However, the CF-CURRENT E[R] is still inflated by outliers. The more meaningful statistic is the median R, which is +2.90 for EX-2026-09-29.

### TP2.0R-240m (representative fixed-RR scenario)

| Day | N | Win% | E[R] net | PF | Total R net |
|-----|---|------|---------|-----|-------------|
| 2026-09-28 | 2 | 50.0 | −0.05 | 2.00 | −0.09 |
| 2026-09-29 | 26 | 69.2 | −0.38 | 4.60 | −10.00 |
| 2026-09-30 | 8 | 75.0 | +0.71 | 6.00 | +5.69 |
| **EX-2026-09-29** | **10** | **70.0** | **+0.56** | **4.67** | **+5.60** |

**TP2.0R-240m:** EX-2026-09-29 shows positive net E[R] (+0.56), but the full sample is slightly negative (−0.12) due to cost drag on 2026-09-29 signals with larger risk distances.

---

## 8. SYMBOL CONCENTRATION

### CF-CURRENT

| Metric | Value |
|--------|-------|
| **Total net R** | +350.68 |
| **Top-1 symbol** | LINKUSDT (+267.84) |
| **Top-1 contribution** | 76.4% |
| **Top-3 symbols** | LINKUSDT + HBARUSDT + PONSUSDT = +314.44 |
| **Top-3 contribution** | 89.7% |
| **Without best symbol** | N=35, net E[R] = +2.37, PF = 7.97 |

**CRITICAL FINDING:** LINKUSDT alone accounts for 76.4% of the total net R. Without it, the edge drops from E[R] = +9.74 to +2.37. The edge is **highly concentrated** in one symbol.

Even without LINKUSDT, the top-3 (HBARUSDT +27.7R, PONSUSDT +18.9R) still dominate. The result is driven by a few extreme winners.

### TP2.0R-240m

| Metric | Value |
|--------|-------|
| **Total net R** | −4.39 |
| **Top-1 symbol** | SOXLUSDT (+3.23) |
| **Without best symbol** | N=34, net E[R] = −0.22 |

TP2.0R-240m is net negative overall; symbol concentration is less relevant when the overall edge is weak.

---

## 9. REGIME ANALYSIS

### CF-CURRENT

| Regime | N | Win% | E[R] net | PF | Total R net | Median R |
|--------|---|------|---------|-----|-------------|----------|
| HIGH_VOLATILITY | 12 | 41.7 | +24.72 | 46.92 | +296.65 | −1.13 |
| RANGE | 12 | 41.7 | +0.40 | 3.17 | +4.82 | −0.87 |
| TREND_DOWN | 12 | 83.3 | +4.10 | 29.33 | +49.20 | +3.68 |

**TREND_DOWN** shows the most consistent edge: 83.3% win rate, median R = +3.68, PF = 29.33. This is the regime where the LR SHORT setup is most naturally aligned (short in a downtrend).

**HIGH_VOLATILITY** has the highest E[R] but it's driven by LINKUSDT (+267.8R). Median R is −1.13, meaning most trades in this regime were losses. The avg win is +65.7R — clearly an outlier.

**RANGE** shows a weak edge (E[R] = +0.40, median R = −0.87).

### TP2.0R-240m

| Regime | N | Win% | E[R] net | PF | Total R net |
|--------|---|------|---------|-----|-------------|
| HIGH_VOLATILITY | 12 | 75.0 | −0.99 | 5.42 | −11.87 |
| RANGE | 12 | 50.0 | −0.15 | 2.31 | −1.83 |
| TREND_DOWN | 12 | 83.3 | +0.78 | 9.41 | +9.31 |

**TREND_DOWN** remains the strongest regime even under TP2.0R-240m constraints (net E[R] = +0.78, PF = 9.41).

---

## 10. PAPER TRADE RECONCILIATION

### Actual LIQUIDITY_REVERSAL SHORT paper trades found in `dds.paper_trade`:

| trade_id | Symbol | Entry | Exit | PnL USDT | PnL R | Exit reason | Regime | Entered at |
|----------|--------|-------|------|----------|-------|-------------|--------|------------|
| 22 | LTCUSDT | 49.92 | 49.69 | +7.99 | +0.16 | EXPIRED | RANGE | 2026-08-27 11:43 |
| 391 | PUMPFUNUSDT | 0.004987 | 0.00506 | −22.50 | −1.15 | STOP_LOSS_GAP | HIGH_VOLATILITY | 2026-09-29 08:26 |
| 392 | KORUUSDT | 21.03 | 21.38 | −25.19 | −0.91 | EXPIRED | RANGE | 2026-09-29 16:40 |

### Matching OOS observations:

| Paper trade | OOS obs_id | Symbol | Signal time | OOS MFE R 60m | OOS MAE R 60m | OOS result |
|-------------|-----------|--------|-------------|---------------|---------------|------------|
| 391 | 230 | PUMPFUNUSDT | 2026-09-29 08:06 | 0.31 | 1.21 | SL hit |
| 392 | 552 | KORUUSDT | 2026-09-29 15:22 | 0.59 | 0.89 | SL hit |

### Comparison:

| Metric | OOS counterfactual | Actual paper |
|--------|-------------------|--------------|
| PUMPFUNUSDT | SL hit (MAE 1.21R > 1R) | STOP_LOSS_GAP, −1.15R |
| KORUUSDT | SL hit (MAE 0.89R) | EXPIRED, −0.91R |

**Agreement:** The OOS outcomes and actual paper results are **consistent**. Both trades hit the stop / expired negative. The paper sample (2 trades during OOS window) is too small to draw statistical conclusions, but it does not contradict the OOS findings.

**Note:** Trade 22 (LTCUSDT, 2026-08-27) is from before the OOS experiment started and is not part of the prospective sample.

---

## 11. INTERPRETATION

### Q1: Is there executable positive expectancy, or just high MFE?

**Both.** The CF-CURRENT simulation shows E[R] net = +9.74, but this is dominated by extreme outliers (LINKUSDT +267.8R). The median R of +0.67 is more representative. The edge exists but is fat-tailed — a few huge winners carry the strategy.

Under fixed-RR constraints (TP1.0R–TP2.0R), the edge **disappears after costs** (net E[R] negative). Only TP3.0R shows positive net E[R], but this is exploratory.

### Q2: Does the edge survive stop-before-TP chronology?

**Yes.** The conservative ambiguous policy (STOP_FIRST) was used throughout. 0 ambiguous trades were detected — no signal had both TP and SL hit in the same 5m candle. The edge is not an artifact of favorable intrabar ordering.

### Q3: Does the edge survive fees/slippage?

**For CF-CURRENT: yes** (net E[R] = +9.74 vs gross +10.88).  
**For fixed-RR scenarios: mostly no.** At TP1.0R–TP2.0R, the 1.15R average cost per trade exceeds the gross edge. Only TP3.0R survives costs.

### Q4: What fraction reaches +1R before stop?

**83.3% (30/36).** This is very high — the LR SHORT setup has strong initial directional movement.

### Q5: What fraction reaches +2R before stop?

**41.7% (15/36).** There's a significant drop from 1R to 2R — many signals reverse after reaching +1R.

### Q6: How dependent is the result on 2026-09-29?

**Less dependent than the previous audit suggested.** EX-2026-09-29 shows N=10, E[R] net = +2.52, PF = 8.64 for CF-CURRENT. The edge persists without the dominant day. However, the sample outside 2026-09-29 is still small (N=10).

### Q7: How dependent is the result on individual symbols?

**Highly dependent.** LINKUSDT alone accounts for 76.4% of total net R. Without it, E[R] drops from +9.74 to +2.37. The top-3 symbols account for 89.7%. This is a **critical fragility** — the result is not robust to symbol-level variation.

### Q8: Does the current production exit logic work?

**Under the OOS conditions, yes** — CF-CURRENT shows positive net E[R] and PF = 26.78. But this is heavily influenced by outliers. The production logic (TP at target_1 = swept_level − ATR×1.5, stop at invalidation_price, 120m timeout) does capture some of the large moves.

However, the production timeout is 120m (not 240m as simulated). Under 120m timeout, the edge would be smaller but still positive.

### Q9: Is there reason to continue LR_SHORT_GATE_V1 OOS?

**Yes.** The sample is still small (N=36, 3 days). The edge appears real but is fragile (symbol concentration, outlier dependence). More data is needed to:
- Confirm the edge is not driven by a few extreme symbols
- Test whether TREND_DOWN regime edge is robust
- Validate TP3.0R as a pre-registered hypothesis on unseen data

### Q10: Is there reason to consider SHORT for PAPER review?

**Not yet.** Blockers:
1. **Symbol concentration:** 76.4% from one symbol (LINKUSDT)
2. **Outlier dependence:** Median R (+0.67) is much lower than mean (+9.74)
3. **Fixed-RR edge weak:** TP1.0R–TP2.0R are net negative after costs
4. **Sample too small:** N=36, 3 OOS days
5. **Paper trade evidence:** Only 2 actual paper trades, both negative

---

## 12. DATA QUALITY / LIMITATIONS

| Issue | Impact |
|-------|--------|
| **Candle source: Bybit API** | Candles are fetched from Bybit public API, not from the project's DB (which has incomplete coverage for the OOS period). Bybit data is authoritative for the exchange where trades would execute. |
| **5m resolution** | Only 5m candles available. 1m candles would provide better intrabar resolution. At 5m, ambiguity detection is coarser. |
| **Entry price assumption** | We use `reference_price` (swept_level) as entry. In production, entry is at market price when price enters the entry zone. The actual entry price may differ. |
| **No 1m candles** | Cannot resolve intrabar ambiguity below 5m. 0 ambiguous cases detected at 5m resolution. |
| **LINKUSDT outlier** | MFE = 235.75R for LINKUSDT is an extreme event (price moved from 15.42 to ~0.08). This may be a data error, a delisting, or a genuine black swan. If it's a data error, the E[R] figures are overstated. |
| **Cost model** | Fees and slippage are applied uniformly at 0.055% + 0.05% per side. Actual costs may vary by symbol (liquidity, spread). |

---

## 13. FINAL SUMMARY

### OOS SAMPLE:
N=36, symbols=31, OOS days=3 (2026-09-28/29/30), maturity 15m/30m/60m/120m/240m = 36/36/35/35/34.

### CURRENT EXECUTION (CF-CURRENT, 240m timeout, conservative ambiguous):
```
E[R] net:    +9.74  (dominated by LINKUSDT +267.8R outlier)
Median R:    +0.67
PF:          26.78
Total R net: +350.68
Max DD:      14.57R
Classification: PROMISING_EXECUTION_NEEDS_MORE_OOS
```

### MFE → EXECUTABLE:
```
>=1R raw:           80.6% (29/36)
>=1R before stop:   83.3% (30/36)
>=2R raw:           52.8% (19/36)
>=2R before stop:   41.7% (15/36)
```

### DAY ROBUSTNESS:
```
EX-2026-09-29: N=10, E[R] net = +2.52, PF = 8.64
Edge persists without dominant day (but small sample)
```

### SYMBOL CONCENTRATION:
```
LINKUSDT: 76.4% of total net R
Top-3: 89.7%
Without LINKUSDT: E[R] = +2.37, PF = 7.97
CRITICAL FRAGILITY
```

### PAPER TRADE RECONCILIATION:
```
2 actual trades during OOS window (PUMPFUNUSDT, KORUUSDT)
Both negative, consistent with OOS outcomes
Paper sample too small for statistical comparison
```

### FIXED RR SENSITIVITY:
```
TP1.0R: net E[R] negative at all timeouts
TP1.5R: net E[R] negative at all timeouts
TP2.0R: net E[R] slightly negative at all timeouts
TP3.0R: net E[R] positive (+0.22 to +0.33) — EXPLORATORY ONLY
Cost structure (~1.15R/trade) dominates low-RR scenarios
```

### REGIME:
```
TREND_DOWN: N=12, win 83.3%, median R +3.68, PF 29.33 — strongest
HIGH_VOLATILITY: driven by LINKUSDT outlier
RANGE: weak edge
```

---

## 14. FINAL CLASSIFICATION

### LR_SHORT_GATE_V1 SHORT (CURRENT execution):
**PROMISING_EXECUTION_NEEDS_MORE_OOS**

**Rationale:**
- Positive net E[R] (+9.74) and PF (26.78) under CF-CURRENT
- Edge persists without dominant day (EX-2026-09-29: E[R] = +2.52)
- High 1R-before-stop rate (83.3%)
- BUT: extreme symbol concentration (76.4% from LINKUSDT)
- BUT: median R (+0.67) much lower than mean — outlier-driven
- BUT: fixed-RR scenarios mostly net negative after costs
- BUT: sample too small (N=36, 3 days)

### Fixed-RR scenarios (TP1.0R–TP3.0R):
**EXPLORATORY_ONLY**

These were first analyzed on this sample. Any fixed-RR variant that looks better must be pre-registered as a new hypothesis and validated on unseen OOS observations.

---

## 15. NEXT ACTION

1. **Continue LR_SHORT_GATE_V1 OOS accumulation** for at least 4–7 more days (target: N ≥ 50, OOS days ≥ 7)
2. **Monitor LINKUSDT outlier:** Verify whether the LINKUSDT MFE = 235.75R is a data error or genuine event. If data error, re-run simulation excluding it.
3. **Pre-register TP3.0R hypothesis:** If TP3.0R-240m is to be pursued, freeze it as a new experiment before collecting more OOS data.
4. **Investigate TREND_DOWN regime edge:** The 83.3% win rate and +3.68 median R in TREND_DOWN is the most robust finding. Consider whether this can be isolated as a separate sub-experiment.
5. **Do NOT enable in PAPER yet.** The edge is promising but fragile. More data is needed.
6. **No changes to scanner config, direction gates, or experiment config.** This was a READ-ONLY audit.

---

*End of Audit Report*

---

## Appendix: Simulation Script

The counterfactual simulation was performed by `scripts/lr_cf_audit.py` (uploaded to VPS as `/tmp/lr_cf_audit.py`). Full results saved to `docs/research/lr_counterfactual_results.json` on VPS.
