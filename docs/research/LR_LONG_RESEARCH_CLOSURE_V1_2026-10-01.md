# LIQUIDITY_REVERSAL LONG — Research Closure Record V1

**Date:** 2026-10-01
**Closure by:** MiMo-v2.5
**Type:** Closure / documentation task only
**Safety:** READ-ONLY for runtime/scanner/config/DB. Only research metadata documentation was created.

---

## 1. Closure Summary

| Field | Value |
|---|---|
| Scanner | `LIQUIDITY_REVERSAL` |
| Direction | `LONG` |
| Framework | generic research (`research.research_*`) |
| Experiment ID | `LR_GENERIC_V1` |
| Final research status | **`CLOSED_NEGATIVE`** |
| Current execution status | **`NO_EXECUTABLE_EDGE`** |
| Alternative path hypothesis | **`NO_PATH_EDGE`** |
| Prospective delayed-entry experiment | **`DO NOT CREATE`** |
| Paper/live enablement | **`DO NOT PROCEED`** |

---

## 2. Evidence Basis

### 2.1 LIQUIDITY_REVERSAL LONG — OOS Execution Counterfactual V1

**Report:** `docs/research/LIQUIDITY_REVERSAL_LONG_OOS_EXECUTION_COUNTERFACTUAL_2026-09-30.md`

**JSON artifact:** `audit_db/lr_long_cf_results.json`

**Key results (CF-CURRENT, production exit logic, 120m timeout, conservative STOP_FIRST):**

| Metric | Value |
|---|---:|
| N production entries | 65 |
| Net E[R] | −0.3243 |
| PF | 0.9744 |
| Median R | −0.6902 |
| Win rate | 40.0% |
| Total Net R | −21.08 |
| Bootstrap 95% CI | [−0.5634, −0.0744] |
| Bootstrap P(E[R]>0) | 1.8% |
| Day-cluster bootstrap P(E[R]>0) | 3.0% |
| Timeout sensitivity (60/120/240m) | All negative |

**Classification:** `CASE_A_NO_SIGNAL_EDGE`

**Conclusion:** Current production execution has no positive executable edge after realistic entry/SL/TP/timeout geometry and trading costs. Bootstrap CI does not contain zero.

---

### 2.2 LR_LONG_PRE_MFE_PATH_GEOMETRY_V1

**Report:** `docs/research/LR_LONG_PRE_MFE_PATH_GEOMETRY_V1_2026-10-01.md`

**JSON artifact:** `docs/research/lr_long_pre_mfe_path_geometry_v1_results.json`

**CSV artifact:** `docs/research/lr_long_pre_mfe_paths.csv`

**Key results (frozen population, 98 valid paths):**

| Metric | Value |
|---|---:|
| N raw signals | 150 |
| N valid paths | 98 |
| N excluded (MISSING_CANDLES_OR_INVALID_RISK) | 52 |
| MFE≥2R paths | 64 (65.3%) |
| MFE≥3R paths | 48 (49.0%) |
| Retest occurred (±0.5% tolerance) | 57/98 (58.2%) |
| Median time to retest | 33.2 min |
| Median MFE after retest | 1.39R |
| Reached 2R after retest | 22/57 |
| `close_above_reference` occurred | 95/98 (96.9%) |
| Median MFE after close_above_reference | 3.10R |
| `reclaim_after_retrace` occurred | 44/98 (44.9%) |
| Reached 2R after reclaim_after_retrace | 12/44 |
| Signal-time features separate winners/losers | NO |

**Classification:** `NO_PATH_EDGE`

**Conclusion:** High MFE exists (65.3% of paths reach +2R), but no repeatable online-detectable retrace/retest/reclaim pattern separates winners from losers. The `close_above_reference` event occurs in 96.9% of paths — too common to be a useful filter. `reclaim_after_retrace` occurs in 44.9% but has low predictive power (12/44 reach 2R after). Signal-time features (sweep_depth, rejection_strength, rr_ratio, stop_distance_atr) do NOT separate winners from losers.

---

## 3. Why Further OOS Accumulation Is Not Required

The current research status is determined by two independent findings:

1. **Execution counterfactual:** Current production entry/stop geometry produces negative executable Net E[R] (−0.324), PF < 1, bootstrap CI below zero. This is a **geometry problem**, not a signal-quality problem.

2. **Path-geometry diagnostic:** High MFE exists, but no online-detectable delayed-entry pattern improves the executable edge. The `close_above_reference` event is too common (96.9%), `reclaim_after_retrace` has low predictive power (12/44 reach 2R), and signal-time features do not separate winners from losers.

**Therefore:** Continuing to accumulate OOS data within the same hypothesis (`LR_GENERIC_V1 LONG` with current production entry/stop geometry) will not change the verdict. The problem is not sample size — it is that the current execution geometry cannot capture the available MFE, and no online-detectable delayed-entry pattern exists to improve the situation.

**Additional OOS accumulation would only be justified if:**
- A new, independent hypothesis were formulated (e.g., a different entry geometry, a different stop placement, or a different signal filter)
- That hypothesis were frozen and tested prospectively on new data
- The new hypothesis were **not** a post-hoc optimization of the current LR LONG execution

---

## 4. Conditions for Reopening

`LIQUIDITY_REVERSAL LONG` research can only be reopened under the following conditions:

1. **New independent hypothesis required.** A new hypothesis must be formulated that is **not** a post-hoc optimization of the current LR LONG execution geometry. Examples:
   - A different entry geometry (e.g., different entry zone, different confirmation logic)
   - A different stop placement (e.g., structural stop, ATR-based stop)
   - A different signal filter (e.g., sweep_depth threshold, regime filter)
   - A different exit logic (e.g., different timeout, different TP placement)

2. **Frozen before testing.** The new hypothesis must be frozen (parameters, entry/stop/TP/timeout, acceptance criteria) before any OOS data is collected.

3. **Prospective validation.** The new hypothesis must be tested on prospective OOS data, not on the existing `LR_GENERIC_V1 LONG` dataset.

4. **No retrospective optimization.** The new hypothesis must not be derived by optimizing parameters on the existing `LR_GENERIC_V1 LONG` dataset.

**If these conditions are not met:** `NO_FURTHER_ACTION`.

---

## 5. Runtime / Scanner / Config / DB Confirmation

| Component | Changed? | Details |
|---|---|---|
| Scanner behavior | NO | No scanner gate changes, no direction ENABLE/BLOCK changes |
| Runtime config | NO | No `config.yaml` or `settings.py` changes |
| Production DB | NO | No DB writes, no schema changes, no experiment status changes |
| Execution logic | NO | No paper/live execution logic changes |
| Services | NO | No restarts, no systemd changes |
| Deploy | NO | No deployment performed |
| Paper/live | NO | Not enabled |

**This closure task created only:**
- `docs/research/LR_LONG_RESEARCH_CLOSURE_V1_2026-10-01.md` (this document)

No other files were modified.

---

## 6. Linked Artifacts

| Artifact | Path |
|---|---|
| Execution counterfactual report | `docs/research/LIQUIDITY_REVERSAL_LONG_OOS_EXECUTION_COUNTERFACTUAL_2026-09-30.md` |
| Execution counterfactual JSON | `audit_db/lr_long_cf_results.json` |
| Path geometry report | `docs/research/LR_LONG_PRE_MFE_PATH_GEOMETRY_V1_2026-10-01.md` |
| Path geometry JSON | `docs/research/lr_long_pre_mfe_path_geometry_v1_results.json` |
| Path geometry CSV | `docs/research/lr_long_pre_mfe_paths.csv` |
| SHORT execution counterfactual (methodology source) | `docs/research/LIQUIDITY_REVERSAL_SHORT_OOS_EXECUTION_COUNTERFACTUAL_2026-09-30.md` |
| SHORT data integrity V2 (methodology source) | `docs/research/LIQUIDITY_REVERSAL_SHORT_COUNTERFACTUAL_DATA_INTEGRITY_V2_2026-09-30.md` |
| SHORT closure record | `docs/research/LR_SHORT_GATE_V1_CLOSURE_2026-09-30.md` |
| LR LONG enablement analysis | `docs/audit/LIQUIDITY_REVERSAL_LONG_OOS_ENABLEMENT_2026-09-30.md` |
| OOS accumulation status audit | `docs/audit/OOS_ACCUMULATION_STATUS_2026-09-30.md` |

---

## 7. Final Status

```
LIQUIDITY_REVERSAL LONG RESEARCH STATUS = CLOSED_NEGATIVE

Current execution:     NO_EXECUTABLE_EDGE
Path hypothesis:       NO_PATH_EDGE
Delayed-entry:         DO NOT CREATE
Paper/live:            DO NOT PROCEED
Further OOS (same):    NOT REQUIRED

Reopen condition:      New independent hypothesis + frozen parameters + prospective validation
```

---

*End of Closure Record*
