# LR LONG Entry Geometry Discovery V1 — 2026-10-01

**Task:** `LR_LONG_ENTRY_GEOMETRY_DISCOVERY_V1`  
**Safety:** READ-ONLY. No DB writes, no config changes, no service restarts, no gate changes.  
**Holdout rule:** `LR_LONG_GATE_V1` was NOT used for any performance metric selection. Only inventory/overlap.

---

## 1. DATASET ROLES

| Dataset | Role | Used for |
|---------|------|----------|
| `LR_GENERIC_V1` (LONG) | Discovery / hypothesis generation | Entry geometry counterfactual, MFE/MAE, cost-adjusted expectancy, subgroup analysis |
| `LR_LONG_GATE_V1` | Prospective holdout | Inventory, data-quality, overlap only — NO SL/TP/timeout/filter selection |

---

## 2. LINEAGE

### LR_GENERIC_V1 LONG (discovery)
| Metric | Value |
|--------|-------|
| First signal | 2026-09-25 22:28:53 UTC |
| Last signal | 2026-10-01 11:53:35 UTC |
| N (all) | 163 |
| Symbols | 56 |
| Source key | `research.research_signal.signal_id` |

### LR_LONG_GATE_V1 (holdout)
| Metric | Value |
|--------|-------|
| Status | RUNNING |
| started_at | 2026-09-30 14:36:46 UTC |
| First obs | 2026-09-30 15:37:49 UTC |
| Last obs | 2026-10-01 11:53:35 UTC |
| N | 27 |
| Symbols | 21 |
| Days | 2 |
| With outcome | 27 |

---

## 3. OVERLAP AUDIT

| Metric | Value |
|--------|-------|
| GENERIC_N | 163 |
| GATE_N | 27 |
| OVERLAP_N | 27 (by symbol + signal_time) |
| GENERIC_ONLY_N | 136 |
| GATE_ONLY_N | 0 |
| Overlap % of gate | 100.0% |

**Finding:** All 27 holdout observations are also present in the generic dataset. Holdout is a clean subset; excluded from discovery.

---

## 4. DISCOVERY CUTOFF

| Dataset | Definition | N | Symbols | Days |
|---------|------------|---|---------|------|
| **D1 STRICT_PRE_GATE** (primary) | generic LONG with signal_time < gate.started_at | 131 | 50 | 6 |
| D2 GENERIC_NON_OVERLAP | generic LONG excluding any holdout overlap | 136 | 51 | 7 |

**Main result reported on D1.** D2 used as sensitivity (5 extra observations after gate start, all wins under current geometry — noted, not used for selection).

---

## 5. DATA INTEGRITY

```
RAW_N = 163
  → HOLDOUT_EXCLUDED = -27
  → INVALID_EXCLUDED = 0 (all entry/stop valid, entry != stop)
  → VALID_DISCOVERY_N (D1) = 131
```

- Duplicates: none (all signal_ids unique)
- Missing outcomes: n/a (candle replay based)
- Invalid entry/stop: 0
- Candle coverage: 50/50 symbols fetched via Bybit 5m klines (min 300, max 1436 candles per symbol)
- Direction: all LONG (consistent)

---

## 6. CURRENT BASELINE (production geometry, D1)

**Current geometry (recovered from `app/scanners/liquidity_reversal.py`):**
- Entry: `reference_price` = swept_level (liquidity level)
- Stop: `invalidation_price` = recent_low × 0.998 (structural)
- TP: `target_1` = swept_level + ATR × 1.5
- TP2: `target_2` = swept_level + ATR × 3
- Timeout: 120 min (12 × 5m × TTL 2.0)
- Fees: taker 0.055% per side
- Slippage: 0.05% per side
- 1R = abs(entry − stop)

**CF-CURRENT on D1 (N=131):**

| Metric | Value |
|--------|-------|
| N | 131 |
| WR | 37.4% |
| Gross E[R] | +5.31 |
| Net E[R] (fee+slip) | +3.98 |
| PF | 8.85 |
| Median R | −0.23 |
| Timeouts | 91/131 (69%) |
| Max losing streak | 58 |

**⚠️ CRITICAL DATA INTEGRITY ISSUE — risk distance distribution:**

| Risk % (entry→stop) | N |
|---------------------|---|
| < 0.01% | 1 |
| 0.01–0.05% | 1 |
| 0.05–0.1% | 6 |
| 0.1–0.3% | 17 |
| 0.3–0.5% | 44 |
| ≥ 0.5% | 62 |

**Median risk = 0.45%.** 25/131 (19%) observations have risk < 0.3%. When risk is tiny (e.g., 0.002% for ONDOUSDT), R-multiples explode: ONDOUSDT contributed +202.9R alone. The Gross E[R] of +5.31 is **dominated by mathematically-correct but methodologically meaningless artifacts**. Median R of −0.23 is the representative statistic.

This matches the known LR SHORT LINKUSDT issue (risk 0.0116%, MFE 235R artifact).

---

## 7. COUNTERFACTUAL GRID (D1, all observations, production entry semantics)

**Grid:** SL ∈ {0.50, 0.75, 1.00, 1.25, 1.50}R × TP ∈ {1.0, 1.5, 2.0, 2.5, 3.0}R × Timeout ∈ {15, 30, 60, 120, 240}m  
**Costs:** taker 0.055% + slippage 0.05% per side  
**Ambiguity:** STOP_FIRST (conservative)

**Top 10 by Net E[R] (no risk gate):**

| SL | TP | Timeout | N | WR | PF | Net E[R] | Median R |
|----|----|---------|---|----|----|----------|----------|
| 0.50 | 3.0 | 120m | 131 | 55.7% | 1.06 | +0.064 | +0.79 |
| 0.50 | 3.0 | 15m | 131 | 67.9% | 1.06 | +0.049 | +0.63 |
| 0.75 | 3.0 | 120m | 131 | 60.3% | 1.04 | +0.040 | +0.81 |
| 0.75 | 3.0 | 15m | 131 | 68.7% | 1.04 | +0.036 | +0.63 |
| 0.50 | 3.0 | 60m | 131 | 58.0% | 1.03 | +0.032 | +0.55 |
| 1.00 | 3.0 | 120m | 131 | 60.3% | 1.02 | +0.020 | +0.81 |
| 0.75 | 3.0 | 60m | 131 | 61.8% | 1.02 | +0.020 | +0.66 |
| 1.00 | 3.0 | 15m | 131 | 68.7% | 1.02 | +0.020 | +0.63 |
| 1.25 | 3.0 | 15m | 131 | 68.7% | 1.01 | +0.007 | +0.63 |
| 1.00 | 3.0 | 60m | 131 | 62.6% | 1.00 | +0.004 | +0.66 |

**Robustness (top candidate SL=0.50 TP=3.0 T=120m, N=131):**

| Scenario | Net E[R] |
|----------|----------|
| Base | +0.064 |
| excl best day | −0.206 |
| excl worst day | +0.722 |
| excl best symbol | −0.015 |
| excl top-3 symbols | −0.119 |
| excl top winner | +0.043 |
| excl top-3 winners | −0.001 |
| Bootstrap 95% CI | [−1.46, +0.94] |

**Cost sensitivity (top candidate):**

| Scenario | Net E[R] |
|----------|----------|
| Gross (no cost) | +1.389 |
| Fee only | +0.695 |
| Fee + normal slippage | +0.064 |
| Fee + elevated slippage | −0.567 |

**Finding:** All candidates have Net E[R] ≈ 0, PF ≈ 1.0, bootstrap CI spanning zero, and edge disappears when best day/symbol/winner excluded. **No executable edge in the ungated discovery population.**

---

## 8. RISK-GATED COUNTERFACTUAL (key finding)

The risk-distance artifact (19% of observations with risk < 0.3%) inflates MFE/MAE but produces untradeable R-multiples. A risk floor gate is a **point-in-time observable** (stop is known at signal time), so it is a legitimate signal-quality filter — not a post-hoc outcome filter.

**CF: risk_dist / entry ≥ 0.3% (N drops 131 → 106)**

| Geometry | N | WR | PF | Net E[R] | Median R |
|----------|---|----|----|----------|----------|
| SL=0.50 TP=3.0 T=120m | 106 | 33.0% | 1.81 | **+0.292** | −0.245 |
| SL=0.50 TP=3.0 T=15m | 106 | 39.6% | 2.13 | +0.284 | −0.189 |
| SL=0.75 TP=3.0 T=15m | 106 | 40.6% | 2.08 | +0.281 | −0.179 |
| SL=0.50 TP=3.0 T=60m | 106 | 35.8% | 1.89 | +0.281 | −0.230 |
| SL=0.75 TP=3.0 T=60m | 106 | 37.7% | 1.84 | +0.277 | −0.221 |

**Cost sensitivity (risk-gated, SL=0.50 TP=3.0 T=120m):**

| Scenario | Net E[R] |
|----------|----------|
| Gross (no cost) | +0.684 |
| Fee only | +0.484 |
| Fee + normal slippage | **+0.302** |
| Fee + elevated slippage | +0.120 |

**Robustness (risk-gated, SL=0.50 TP=3.0 T=120m):**

| Scenario | Net E[R] | PF |
|----------|----------|----|
| Base | +0.292 | 1.81 |
| excl best day (2026-09-27) | +0.178 | 1.48 |
| excl worst day | +0.359 | 2.00 |
| excl best symbol (AEROUSDT) | +0.264 | 1.73 |
| excl top-3 symbols | +0.214 | 1.58 |
| excl top winner | +0.268 | 1.74 |
| excl top-3 winners | +0.218 | 1.59 |

**Bootstrap (risk-gated, SL=0.50 TP=3.0 T=120m, N=106, 5 days):**

| Method | Mean | 95% CI |
|--------|------|--------|
| Trade-level bootstrap | +0.302 | [+0.054, +0.541] |
| Day-block bootstrap | +0.304 | [+0.076, +0.568] |

**Day concentration:** top-3 days contribute +16.06, +8.43, +5.21 total R. Best day (2026-09-27) = +16.06 of ~31 total. Excluding it: Net E[R] still +0.178, PF 1.48 — positive but weaker.

**Symbol concentration:** top-3 symbols (AEROUSDT +4.03, RAREUSDT +2.83, UNIUSDT +2.76) contribute ~+9.6 of ~31 total R. Excluding top-3: Net E[R] +0.214, PF 1.58 — remains positive.

---

## 9. MFE/MAE BASELINE (risk-ungated, D1)

| Horizon | N | Avg MFE R | Median MFE R | Avg MAE R | Median MAE R | ≥1R | ≥1.5R | ≥2R | ≥3R |
|---------|---|-----------|--------------|-----------|--------------|-----|-------|-----|-----|
| 15m | 131 | 8.78 | 1.72 | −2.81 | −0.43 | 73.3% | 55.7% | 45.8% | 33.6% |
| 30m | 131 | 9.26 | 1.91 | −2.57 | −0.24 | 75.6% | 60.3% | 48.9% | 36.6% |
| 60m | 131 | 10.73 | 2.43 | −2.00 | −0.03 | 80.9% | 68.7% | 55.7% | 41.2% |
| 120m | 131 | 13.71 | 2.87 | −0.25 | 0.13 | 82.4% | 74.0% | 62.6% | 48.9% |
| 240m | 131 | 14.83 | 3.59 | 7.44 | 0.86 | 89.3% | 79.4% | 71.0% | 57.3% |

**Note:** Avg MFE is inflated by risk artifacts. Median MFE is the representative statistic. High raw MFE hit rates do NOT translate to executable edge (see §7, §8).

---

## 10. CANDIDATE SELECTION

### Primary candidate (risk-gated)

| Parameter | Value |
|-----------|-------|
| SL | 0.50R |
| TP | 3.0R |
| Timeout | 120 min |
| Risk gate | risk_dist / entry ≥ 0.3% |
| Entry | reference_price (swept_level), production semantics |
| Ambiguity rule | STOP_FIRST (conservative) |
| Fees | taker 0.055% per side |
| Slippage | 0.05% per side (normal), 0.10% (elevated sensitivity) |
| Discovery Net E[R] | +0.302 |
| Discovery PF | 1.81 |
| Discovery WR | 33.0% |
| Bootstrap CI (trade) | [+0.054, +0.541] |
| Bootstrap CI (day-block) | [+0.076, +0.568] |

### Backup candidate (risk-gated)

| Parameter | Value |
|-----------|-------|
| SL | 0.75R |
| TP | 3.0R |
| Timeout | 15 min |
| Risk gate | risk_dist / entry ≥ 0.3% |
| Discovery Net E[R] | +0.281 |
| Discovery PF | 2.08 |
| Discovery WR | 40.6% |

---

## 11. FROZEN HYPOTHESIS

```
LR_LONG_GEOMETRY_PROSPECTIVE_VALIDATION_V1

direction = LIQUIDITY_REVERSAL LONG
dataset_role = prospective holdout validation on LR_LONG_GATE_V1
               (observations after freeze timestamp ONLY)

entry = reference_price (swept_level) — production semantics
SL = entry − 0.50 × |entry − invalidation_price|
TP = entry + 3.0 × |entry − invalidation_price|
timeout = 120 minutes
risk_gate = |entry − invalidation_price| / entry ≥ 0.003  (0.3%)
fees = taker 0.055% per side
slippage = 0.05% per side (normal); 0.10% per side (elevated sensitivity)
same_candle_rule = STOP_FIRST (conservative)
primary_metric = Net E[R] after fee + normal slippage
secondary_metrics = PF, WR, median R, drawdown, day/symbol concentration

freeze_timestamp = 2026-10-01 14:00:00 UTC
minimum_sample = formal threshold not defined; target N ≥ 50, days ≥ 7
```

**Backup geometry (if primary fails validation):** SL=0.75R, TP=3.0R, timeout=15m, same risk gate/costs/ambiguity.

**DO NOT change these parameters after viewing LR_LONG_GATE_V1 results.**

---

## 12. DISCOVERY VERDICT

# **FRAGILE_DISCOVERY_EDGE**

**Rationale:**
- ✅ Risk-gated candidate shows positive Net E[R] (+0.30), PF 1.81, bootstrap CI excluding zero (both trade-level and day-block)
- ✅ Edge survives exclusion of best day, best symbol, top-3 symbols, top-3 winners (all remain positive)
- ✅ Cost-adjusted: still +0.12 even with elevated slippage
- ⚠️ Only 5 OOS days in discovery window — day concentration material (best day = ~50% of total R)
- ⚠️ Median R is negative (−0.24) — edge comes from tail winners, not typical trade
- ⚠️ Ungated population shows NO edge (Net E[R] ≈ 0) — the 0.3% risk gate is the critical discriminator
- ⚠️ Risk gate threshold (0.3%) chosen in discovery; must be frozen before holdout validation
- ⚠️ Sample N=106 (gated) over 5 days is modest

**Not ROBUST_OOS_EDGE_CANDIDATE** — frozen candidate has NOT been validated on LR_LONG_GATE_V1.

---

## 13. HOLDOUT STATUS (inventory only — NO performance)

| Metric | Value |
|--------|-------|
| LR_LONG_GATE_V1 N | 27 |
| Symbols | 21 |
| Days | 2 |
| started_at | 2026-09-30 14:36:46 UTC |
| Pipeline health | RUNNING, all 27 with outcomes |
| Overlap with discovery | 27/27 (100%) — holdout is a clean subset of generic; excluded from discovery |

**No SL/TP/timeout/filter performance was computed on the holdout.**

---

## 14. NEXT PHASE

If this verdict were to progress (separate task required):

**`LR_LONG_GEOMETRY_PROSPECTIVE_VALIDATION_V1`**
- Validate frozen primary geometry on LR_LONG_GATE_V1 observations collected AFTER freeze_timestamp
- No new optimizations
- One frozen geometry
- Pre-declared metrics
- Prospective observations only

---

## 15. ARTIFACTS

- `docs/research/LR_LONG_ENTRY_GEOMETRY_DISCOVERY_V1_2026-10-01.md` (this file)
- `docs/research/lr_long_entry_geometry_discovery_v1_results.json` (full grid + robustness + MFE/MAE)
- `docs/research/lr_long_entry_geometry_discovery_v1_grid.csv` (125-cell grid, no risk gate)

**Execution:** `scripts/lr_long_geometry_discovery.py` (uploaded to VPS as `/tmp/lr_long_geometry_discovery.py`, run against VPS DB, candle replay via Bybit v5 5m klines).

---

*End of Report*
