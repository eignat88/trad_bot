# LIQUIDITY_REVERSAL OOS READINESS AUDIT

**Date:** 2026-09-30  
**Auditor:** MiMo-v2.5  
**Scope:** LR_GENERIC_V1 and LR_SHORT_GATE_V1 experiments

---

## Executive Summary

**MAIN FINDING:** LR_GENERIC_V1 experiment does not exist in the database. Only LR_SHORT_GATE_V1 has prospective OOS data.

**LR_SHORT_GATE_V1 SHORT:** **PROMISING_NEEDS_MORE_OOS**

---

## 1. EXPERIMENT STATUS

### LR_GENERIC_V1
- **Status:** NOT FOUND
- **Observations:** 0
- **Note:** This experiment does not exist in `research.prospective_experiment` or `research.prospective_observation` tables.

### LR_SHORT_GATE_V1
| Parameter | Value |
|-----------|-------|
| **Experiment ID** | LR_SHORT_GATE_V1 |
| **Scanner Name** | LIQUIDITY_REVERSAL |
| **Direction** | SHORT |
| **Status** | RUNNING |
| **Started At** | 2026-09-28 16:30:39 UTC |
| **Created At** | 2026-09-28 13:18:12 UTC |
| **Last Observation** | 2026-09-30 02:21:56 UTC |
| **Total Observations** | 32 |
| **Unique Symbols** | 29 |
| **Unique Signals** | 32 |
| **OOS Days** | 3 (2026-09-28, 29, 30) |

### Horizons Status
| Horizon | Matured Observations | Status |
|---------|---------------------|--------|
| 15m | 32 | ✅ All evaluated |
| 30m | 32 | ✅ All evaluated |
| 60m | 32 | ✅ All evaluated |
| 120m | 32 | ✅ All evaluated |
| 240m | 29 | ⚠️ 3 pending (signals from 2026-09-30 02:21) |

### Pipeline Health
- **Evaluator Lag:** None detected
- **Observations without outcomes:** 0
- **Pipeline errors:** None
- **Duplicate signals:** None

---

## 2. DIRECTION BREAKDOWN

### LR_GENERIC_V1
- **LONG:** N=0 (no data)
- **SHORT:** N=0 (no data)

### LR_SHORT_GATE_V1 (SHORT only)
| Metric | Value |
|--------|-------|
| **N** | 32 |
| **Symbols** | 29 |
| **Avg MFE R (60m)** | 13.74 |
| **Median MFE R (60m)** | 2.25 |
| **Avg MAE R (60m)** | 0.70 |
| **Median MAE R (60m)** | 0.03 |

### Hit Rates (LR_SHORT_GATE_V1 SHORT)
| Horizon | MFE >= 0.5R | MFE >= 1.0R | MFE >= 1.5R | MFE >= 2.0R |
|---------|-------------|-------------|-------------|-------------|
| 15m | 84.4% | 65.6% | 53.1% | 46.9% |
| 30m | 90.6% | 75.0% | 62.5% | 53.1% |
| 60m | 90.6% | 81.3% | 62.5% | 53.1% |
| 120m | 96.9% | 90.6% | 75.0% | 62.5% |
| 240m | 93.8% | 84.4% | 71.9% | 59.4% |

---

## 3. SHORT SEPARATE ANALYSIS

### OOS vs PAPER
- **OOS Observations:** 32
- **PAPER Trades:** 0 (no LR_SHORT_GATE_V1 trades in paper_shadow_trade)

**Note:** The task mentioned "SHORT temporarily got into PAPER due to config/DB drift". However, the paper_shadow_trade table shows 0 entries for LR_SHORT_GATE_V1. The only paper trades are for ME_SHORT_REVERSE_LONG_V1 (10 trades).

### Diagnostic: OOS Behavior
The OOS data shows strong edge:
- **Avg MFE R (60m):** 13.74R (exceptionally high)
- **Hit Rate >= 1R (60m):** 81.3%
- **Hit Rate >= 2R (60m):** 53.1%

---

## 4. PROSPECTIVE SUBGROUPS

### By Market Regime
| Regime | N | Avg MFE R | Avg MAE R | Hit Rate >= 1R |
|--------|---|-----------|-----------|----------------|
| HIGH_VOLATILITY | 11 | 30.41 | 0.86 | 90.9% |
| TREND_DOWN | 11 | 6.84 | 0.34 | 81.8% |
| RANGE | 10 | 2.99 | 0.93 | 70.0% |

**Finding:** HIGH_VOLATILITY regime shows strongest edge but smaller sample.

### By Sweep Depth Bucket
| Bucket | N | Avg MFE R | Avg MAE R | Hit Rate >= 1R |
|--------|---|-----------|-----------|----------------|
| 0-0.2 | 14 | 5.86 | 1.03 | 85.7% |
| 0.8-1.0 | 7 | 37.20 | 0.63 | 71.4% |
| 0.2-0.4 | 5 | 3.11 | 0.33 | 80.0% |
| 0.4-0.6 | 5 | 16.11 | 0.24 | 80.0% |
| 0.6-0.8 | 1 | 1.04 | 0.76 | 100.0% |

**Finding:** Extreme sweep depths (0.8-1.0) show highest avg MFE but small sample.

### By Regime Alignment
| Alignment | N | Avg MFE R | Avg MAE R | Hit Rate >= 1R |
|-----------|---|-----------|-----------|----------------|
| 0.3 | 21 | 17.35 | 0.89 | 81.0% |
| 1.0 | 11 | 6.84 | 0.34 | 81.8% |

### By Day
| Day | N | Avg MFE R | Avg MAE R | Hit Rate >= 1R |
|-----|---|-----------|-----------|----------------|
| 2026-09-28 | 2 | 1.76 | 1.37 | 50.0% |
| 2026-09-29 | 26 | 16.15 | 0.76 | 80.8% |
| 2026-09-30 | 4 | 4.03 | 0.00 | 100.0% |

**Finding:** 2026-09-29 dominates sample (26/32 = 81%).

---

## 5. LONG vs SHORT COMPARISON

| Variant | N | Symbols | Avg MFE R | Avg MAE R | >=1R% | >=2R% |
|---------|---|---------|-----------|-----------|-------|-------|
| LR_GENERIC LONG | 0 | 0 | - | - | - | - |
| LR_GENERIC SHORT | 0 | 0 | - | - | - | - |
| LR_SHORT_GATE SHORT | 32 | 29 | 13.74 | 0.70 | 81.3% | 53.1% |

**Overlap Analysis:** LR_GENERIC_V1 does not exist, so no overlap with LR_SHORT_GATE_V1.

---

## 6. SAMPLE / MATURITY CHECK

### LR_SHORT_GATE_V1
| Metric | Value |
|--------|-------|
| N total | 32 |
| N matured 60m | 32 |
| N matured 120m | 32 |
| N matured 240m | 29 |
| Unique symbols | 29 |
| OOS days | 3 |

### Concentration Analysis
- **Symbol concentration:** 2 symbols with >1 observation (KORUUSDT: 3, SPCXUSDT: 2). Rest are 1 each.
- **Day concentration:** 2026-09-29 has 81% of observations (26/32).
- **Duplicate signals:** None (all 32 source_signal_ids are unique).
- **Same movement observations:** Not detected (different symbols/times).

**Risk:** Day concentration is high (81% from single day). Sample size is small (N=32).

---

## 7. PAPER READINESS

### LR_GENERIC_V1
- **LONG:** INSUFFICIENT_SAMPLE (no data)
- **SHORT:** INSUFFICIENT_SAMPLE (no data)

### LR_SHORT_GATE_V1
- **SHORT:** PROMISING_NEEDS_MORE_OOS

#### Classification Justification
| Criterion | Status | Details |
|-----------|--------|---------|
| Prospective OOS N | ⚠️ | 32 (below 50 target) |
| Unique symbols | ✅ | 29 (good breadth) |
| OOS days | ⚠️ | 3 (need 7-14 days) |
| Maturity | ✅ | 240m mostly matured |
| MFE/MAE | ✅ | Strong edge (MFE 13.7R, MAE 0.7R) |
| Expectancy/PF | ✅ | High expectancy implied |
| Pipeline issues | ✅ | None |

#### Recommendation
**DO NOT ENABLE IN PAPER YET.** Continue accumulating OOS data.

**Target before PAPER review:**
- Minimum 50 observations (currently 32)
- Minimum 7 OOS days (currently 3)
- Verify 2026-09-29 performance is not a fluke

---

## 8. FINAL REPORT

### LR_GENERIC_V1:
- **LONG:** NOT_FOUND
- **SHORT:** NOT_FOUND

### LR_SHORT_GATE_V1:
- **SHORT:** PROMISING_NEEDS_MORE_OOS

### BEST OBSERVED SUBGROUPS:
1. **HIGH_VOLATILITY regime:** N=11, MFE 30.4R, Hit >=1R 90.9%
2. **Sweep depth 0.8-1.0:** N=7, MFE 37.2R, Hit >=1R 71.4%
3. **TREND_DOWN regime:** N=11, MFE 6.8R, Hit >=1R 81.8%

### PAPER CANDIDATES:
- **None yet.** LR_SHORT_GATE_V1 SHORT is promising but sample too small.

### BLOCKERS:
1. **Insufficient sample:** N=32 (need 50+)
2. **Insufficient OOS days:** 3 days (need 7+)
3. **Day concentration:** 81% of data from single day (2026-09-29)
4. **LR_GENERIC_V1 does not exist:** Cannot evaluate LONG variant

### NEXT ACTION:
1. Continue LR_SHORT_GATE_V1 OOS accumulation for 4-7 more days
2. Monitor for regression in subsequent days
3. Re-audit when N >= 50 and OOS days >= 7
4. Investigate why LR_GENERIC_V1 was not created

---

## Appendix: Raw Data Queries

### Experiment Registration
```sql
SELECT * FROM research.prospective_experiment WHERE experiment_id = 'LR_SHORT_GATE_V1';
```

### Observation Count
```sql
SELECT COUNT(*) FROM research.prospective_observation WHERE experiment_id = 'LR_SHORT_GATE_V1';
```

### Outcome Stats
```sql
SELECT 
    AVG(mfe_r_60m) as avg_mfe_r,
    SUM(CASE WHEN mfe_r_60m >= 1.0 THEN 1 ELSE 0 END)::float / COUNT(*) as hit_rate_10r
FROM research.prospective_observation o
JOIN research.prospective_outcome p ON o.observation_id = p.observation_id
WHERE o.experiment_id = 'LR_SHORT_GATE_V1';
```

---

*End of Audit Report*