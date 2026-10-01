# LIQUIDITY_REVERSAL LONG — OOS Execution Counterfactual Audit V1

**Date:** 2026-09-30 (executed 2026-10-01 UTC)
**Auditor:** MiMo-v2.5
**Scope:** `LR_GENERIC_V1` LONG, scanner `LIQUIDITY_REVERSAL`, generic research framework
**Safety:** READ-ONLY. No DB writes, no config changes, no service restarts.

---

## 0. POINT-IN-TIME / DATA SOURCE

| Field | Value |
|---|---|
| Audit timestamp UTC | 2026-10-01 12:11:57 |
| VPS HEAD | `04c5dc41358aa59b0e48406c9e8a74961f9cc563` |
| VPS branch | `main` |
| DB source | VPS PostgreSQL `trad_bot`: `research.research_observation`, `research.research_signal`, `research.research_outcome` |
| Experiment ID | `LR_GENERIC_V1` |
| Scanner | `LIQUIDITY_REVERSAL` v2.0.0 |
| Direction | LONG |
| Framework | generic research |
| Methodology source | `docs/research/LIQUIDITY_REVERSAL_SHORT_OOS_EXECUTION_COUNTERFACTUAL_2026-09-30.md`, `docs/research/LIQUIDITY_REVERSAL_SHORT_COUNTERFACTUAL_DATA_INTEGRITY_V2_2026-09-30.md`, `scripts/lr_v2_integrity_audit.py` |
| Analysis script | `scripts/analysis/lr_long_cf_fast.py` (v4, run 2026-10-01 10:54–12:00 UTC) |
| Process exit validation | stderr=0 Tracebacks, `DONE` marker present in stdout; wrapper printed `EXIT_CODE=True` which is a shell-mapping artifact — actual process return code validated as 0 via absence of Traceback + `DONE` marker + valid result JSON |
| Population cutoff | last signal 2026-10-01 02:58:45.882876+00:00 |

---

## 1. OOS SAMPLE

| Metric | Value |
|---|---:|
| N observations | 150 |
| Unique signal IDs | 150 |
| Unique observation IDs | 150 |
| Unique symbols | 52 |
| OOS days | 7 |
| First signal | 2026-09-25 22:28:53.315803+00:00 |
| Last signal | 2026-10-01 02:58:45.882876+00:00 |
| Observations before experiment created | 0 |
| Mature outcomes | 150/150 |

**Day distribution:** 2026-09-25: 1, 2026-09-26: 22, 2026-09-27: 30, 2026-09-28: 27, 2026-09-29: 27, 2026-09-30: 40, 2026-10-01: 3

---

## 2. OOS INTEGRITY

| Check | Result |
|---|---|
| N raw | 150 |
| N unique observations | 150 |
| N unique signals | 150 |
| N duplicates | 0 |
| N mature | 150 |
| N usable for execution CF | 65 |
| N excluded | 85 |
| Exclusion reasons | NO_PRODUCTION_ENTRY=48; INVALID_RISK=1; MISSING_CANDLES=36 |
| Duplicate source signals | 0 |
| Duplicate observations | 0 |
| Candles after signal time only | YES |
| Outcome linkage | 150/150 signals have outcomes |
| Look-ahead risk | LOW |

---

## 3. METHODOLOGY PARITY WITH LR_SHORT_GATE_V1

| Parameter | SHORT audit | LONG audit |
|---|---|---|
| Population | `research.prospective_observation` for `LR_SHORT_GATE_V1` | `research.research_observation`/`research_signal` for `LR_GENERIC_V1` LONG |
| Entry | market candle close in `[reference_price × 0.997, reference_price]` + 0.05% SHORT slippage | market candle close in `[reference_price, reference_price × 1.003]` + 0.05% LONG slippage |
| Stop | `invalidation_price = recent_high × 1.002` | `invalidation_price = recent_low × 0.998` |
| Target | `target_1 = reference_price − ATR × 1.5` | `target_1 = reference_price + ATR × 1.5` |
| Timeout | 120m | 120m |
| Intrabar ambiguity | conservative STOP_FIRST | conservative STOP_FIRST |
| Fees | taker 0.055% per side | taker 0.055% per side |
| Slippage | 0.05% per side | 0.05% per side |
| R denominator | `abs(entry − stop)` | `abs(entry − stop)` |
| Candle source | Bybit v5 linear perpetual 5m | Bybit v5 linear perpetual 5m |
| Maturity | 15/30/60/120/240m | 15/30/60/120/240m |

**Parity assessment:** Entry/stop/target rules are symmetric direction-specific production rules from scanner code. All other methodology components are 1-1.

---

## 4. CF-CURRENT (PRODUCTION EXIT LOGIC)

| Metric | Value |
|---|---:|
| N production entries | 65 |
| Wins | 26 |
| Losses | 39 |
| TPs | 6 |
| SLs | 26 |
| Timeouts | 33 |
| Ambiguous | 0 |
| Win rate | 40.0% |
| Gross E[R] | -0.0119 |
| **Net E[R]** | **-0.3243** |
| Median R | -0.6902 |
| Profit Factor | 0.9744 |
| Total Gross R | -0.7763 |
| Total Costs R | 20.3049 |
| **Total Net R** | **-21.0812** |
| Avg cost R/trade | 0.3124 |
| Avg winner R | 1.1367 |
| Avg loser R | -0.7777 |
| Max win R | 4.0422 |
| Max loss R | -1.9826 |
| Max drawdown R | 24.1196 |
| Max loss streak | 8 |

---

## 5. INTRABAR AMBIGUITY

- Ambiguous N: 0
- Ambiguous %: 0.0%
- Policy: conservative STOP_FIRST, identical to SHORT audit.
- Optimistic/exclude sensitivity not material — zero or rare ambiguous cases.

---

## 6. COSTS

| Component | Value |
|---|---:|
| Taker fee | 0.055% per side |
| Slippage | 0.05% per side |
| Round-trip cost | 0.21% |
| Gross E[R] | -0.0119 |
| Net E[R] | -0.3243 |
| Avg cost R/trade | 0.3124 |

---

## 7. DISTRIBUTION

| Metric | Value |
|---|---:|
| P25 R | -1.2715 |
| P50 R | -0.6902 |
| P75 R | 0.3364 |
| P90 R | 1.2672 |
| P95 R | 2.1486 |
| Mean R | -0.3243 |

---

## 8. MFE / MAE

| Horizon | Avg MFE R | Median MFE R | Avg MAE R | Median MAE R | P(MFE≥0.5R) | P(MFE≥1R) | P(MFE≥1.5R) | P(MFE≥2R) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 15m | 1.7351 | 1.2287 | 0.3302 | 0.0434 | 73.8% | 60.0% | 33.8% | 21.5% |
| 30m | 2.0596 | 1.3316 | 0.4479 | 0.1700 | 84.6% | 64.6% | 40.0% | 24.6% |
| 60m | 2.4195 | 1.6132 | 0.9281 | 0.4400 | 89.2% | 73.8% | 52.3% | 32.3% |
| 120m | 3.7246 | 1.8013 | 1.4974 | 0.7519 | 93.8% | 76.9% | 60.0% | 43.1% |
| 240m | 4.1454 | 2.1414 | 3.9295 | 1.2302 | 95.4% | 86.2% | 67.7% | 55.4% |

---

## 9. EXIT EFFICIENCY

| Metric | Value |
|---|---:|
| Avg MFE→Net R giveback | 4.4698 |
| Median MFE→Net R giveback | 3.0215 |
| P(MFE≥1R) with realized Net≤0 | 64.3% |
| P(MFE≥2R) with realized Net≤0 | 55.6% |

MFE→executable funnel: raw MFE≥1R = 48/65; reached +1R before stop = 19/65; raw MFE≥2R = 21/65; reached +2R before stop = 1/65.

---

## 10. TIMEOUT SENSITIVITY

| Timeout | N | Win% | Net E[R] | Median R | Total Net R | PF |
|---|---:|---:|---:|---:|---:|---:|
| 60m | 65 | 47.7 | -0.3386 | -0.3842 | -22.006 | 0.9289 |
| 120m | 65 | 40.0 | -0.3243 | -0.6902 | -21.0812 | 0.9744 |
| 240m | 65 | 40.0 | -0.341 | -0.8969 | -22.165 | 0.9453 |

---

## 11. SYMBOL CONCENTRATION

Total symbols with production entries: 35.

| Symbol | N | Net E[R] | PF | Win% |
|---|---:|---:|---:|---:|
| ENAUSDT | 2 | 1.9568 | inf | 100.0 |
| ARBUSDT | 1 | 2.574 | inf | 100.0 |
| 1000PEPEUSDT | 1 | 1.865 | inf | 100.0 |
| DASHUSDT | 1 | 1.5912 | inf | 100.0 |
| AEROUSDT | 2 | 0.6131 | 2.5982 | 50.0 |

### Leave-top-symbols-out

| Scenario | N | Net E[R] | PF | Win% |
|---|---:|---:|---:|---:|
| Full | 65 | -0.3243 | 0.9744 | 40.0 |
| Without top-1 | 63 | -0.3967 | 0.829 | 38.1 |
| Without top-3 | 61 | -0.4825 | 0.6666 | 36.1 |
| Without top-5 | 58 | -0.5561 | 0.5334 | 34.5 |

---

## 12. DAY CONCENTRATION

| Date | N | Win% | Net E[R] | PF | Total Net R |
|---|---:|---:|---:|---:|---:|
| 2026-09-26 | 13 | 46.2 | -0.0487 | 1.6773 | -0.633 |
| 2026-09-27 | 15 | 40.0 | -0.4776 | 0.8339 | -7.1638 |
| 2026-09-28 | 14 | 14.3 | -0.7207 | 0.2436 | -10.0898 |
| 2026-09-29 | 8 | 62.5 | 0.3976 | 2.6213 | 3.1807 |
| 2026-09-30 | 14 | 42.9 | -0.4478 | 0.703 | -6.269 |
| 2026-10-01 | 1 | 100.0 | -0.1062 | inf | -0.1062 |

### Leave-one-day-out

| Excluded day | N | Net E[R] | PF | Win% |
|---|---:|---:|---:|---:|
| ex_2026-09-26 | 52 | -0.3932 | 0.8234 | 38.5 |
| ex_2026-09-27 | 50 | -0.2783 | 1.0294 | 40.0 |
| ex_2026-09-28 | 51 | -0.2155 | 1.2739 | 47.1 |
| ex_2026-09-29 | 57 | -0.4256 | 0.7936 | 36.8 |
| ex_2026-09-30 | 51 | -0.2904 | 1.0232 | 39.2 |
| ex_2026-10-01 | 64 | -0.3277 | 0.9683 | 39.1 |

---

## 13. REGIME ANALYSIS

| Regime | N | Win% | Net E[R] | PF | Median R |
|---|---:|---:|---:|---:|---:|
| HIGH_VOLATILITY | 22 | 31.8 | -0.2954 | 0.8892 | -0.8843 |
| RANGE | 25 | 44.0 | -0.2114 | 1.2984 | -0.2746 |
| TREND_DOWN | 14 | 50.0 | -0.3703 | 0.8987 | -0.3077 |
| TREND_UP | 4 | 25.0 | -1.0287 | 0.3246 | -1.3881 |

---

## 14. LONG vs SHORT COMPARISON

| Metric | LR LONG | LR SHORT (V2) |
|---|---:|---:|
| N | 65 | 21 |
| OOS days | 7 | 3 |
| Symbols | 52 | 21 |
| Net E[R] | -0.3243 | -0.344 |
| Median R | -0.6902 | -1.0984 |
| Total Net R | -21.0812 | -7.2243 |
| PF | 0.9744 | 0.9915 |
| Win% | 40.0 | 47.6 |
| Costs R/trade | 0.3124 | 0.3395 |

SHORT sample was a 3-day prospective OOS with 21 production entries. LONG sample is a 7-day generic OOS. Sample composition differences must be considered before direct comparison.

---

## 15. BOOTSTRAP / UNCERTAINTY

| Stat | Value |
|---|---:|
| Point estimate Net E[R] | -0.3243 |
| Bootstrap median | -0.3285 |
| 5th percentile | -0.5634 |
| 95th percentile | -0.0744 |
| Share E[R] > 0 | 1.8% |
| Day-cluster bootstrap median | -0.3265 |
| Day-cluster 5th percentile | -0.5376 |
| Day-cluster 95th percentile | -0.0375 |
| Day-cluster share > 0 | 3.0% |

---

## 16. FAILURE-MODE CLASSIFICATION

### CASE_A_NO_SIGNAL_EDGE

MFE is strong (avg 2.4R at 60m, avg 4.1R at 240m), but executable Net E[R] is negative after costs; alternative exits do not materially improve the result.

This classification is not an automatic decision for paper/live trading.

---

## 17. DECISION MATRIX

| Check | Result |
|---|---|
| OOS integrity | PASS |
| Execution simulation valid | PASS |
| Net E[R] > 0 | NO |
| PF > 1 | NO |
| Median R > 0 | NO |
| Costs survivable | NO |
| Symbol concentration acceptable | UNCERTAIN |
| Day concentration acceptable | UNCERTAIN |
| Timeout sensitivity stable | YES |
| Bootstrap supports positive expectancy | NO |
| Enough OOS days | YES |
| More OOS required | YES |

**Formal thresholds not defined** for bootstrap, concentration, or OOS-day minimums beyond registry minimum_n.

---

## 18. FINAL ANSWERS

1. **Актуальный sample:** N=65 production entries, 52 symbols, 7 OOS days.
2. **CF-CURRENT Net E[R] после costs:** -0.3243.
3. **PF:** 0.9744.
4. **Median R:** -0.6902.
5. **Win rate:** 40.0%.
6. **Symbol dependence:** see Section 11.
7. **Day dependence:** see Section 12.
8. **Timeout sensitivity:** see Section 10.
9. **MFE/MAE:** see Section 8.
10. **Classification:** CASE_A_NO_SIGNAL_EDGE.
11. **LONG vs SHORT:** see Section 14 under identical methodology.
12. **What remains before next review:** additional OOS days, execution counterfactual on the new dedicated `LR_LONG_GATE_V1` prospective experiment, regime/symbol concentration review, and entry-zone/stop validation.

---

## 19. ARTIFACTS

- Report: `docs/research/LIQUIDITY_REVERSAL_LONG_OOS_EXECUTION_COUNTERFACTUAL_2026-09-30.md`
- Analysis script: `scripts/analysis/lr_long_cf_fast.py`
- Raw result JSON: `audit_db/lr_long_cf_results.json`
- VPS collection evidence: `audit_db/lr_long_cf_vps/`

*End of Report*