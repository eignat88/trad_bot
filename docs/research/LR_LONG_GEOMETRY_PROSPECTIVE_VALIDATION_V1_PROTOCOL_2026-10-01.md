# LR_LONG_GEOMETRY_PROSPECTIVE_VALIDATION_V1 — Frozen Protocol

**Status:** FROZEN — parameters immutable from FREEZE_TS onward  
**Created:** 2026-10-01 13:48:27 UTC  
**Safety:** Research validation only. NO production changes. LONG remains BLOCKED for paper trading.

---

## 1. FREEZE METADATA

| Field | Value |
|-------|-------|
| **FREEZE_TS** | **2026-10-01 13:48:27 UTC** |
| **Git SHA** | **85cc87a21399c34424cc3ca5bbd513961c665f68** |
| VPS host | trad-bot-01 (91.99.60.150) |
| Experiment ID | LR_LONG_GEOMETRY_PROSPECTIVE_VALIDATION_V1 |

**After FREEZE_TS, all frozen hypothesis parameters are IMMUTABLE.**

---

## 2. FROZEN HYPOTHESIS

```text
experiment  = LR_LONG_GEOMETRY_PROSPECTIVE_VALIDATION_V1
direction   = LONG
scanner     = LIQUIDITY_REVERSAL

entry       = reference_price / swept_level (production semantics)
SL          = 0.50R
TP          = 3.0R
timeout     = 120 minutes
risk_gate   = abs(entry - original_structural_stop) / entry >= 0.003  (0.3%)
fees        = taker 0.055% per side
slippage    = 0.05% per side (normal); 0.10% per side (elevated sensitivity)
ambiguity   = STOP_FIRST (conservative, same-candle TP/SL)

primary_metric    = Net E[R] after fees + normal slippage
secondary_metrics = PF, WR, median R, day concentration, symbol concentration,
                    drawdown / losing streak
```

### CRITICAL IMMOVABILITY RULES

1. **risk_gate = 0.3%** — cannot be changed to 0.2%, 0.25%, 0.4%, or any other value after FREEZE_TS.
2. **SL = 0.50R, TP = 3.0R, timeout = 120m** — no alternatives allowed during validation.
3. **Fees and slippage** — normal and elevated are pre-declared; no other cost assumptions.
4. **No new filters** may be added after viewing any validation sample results.

---

## 3. SOURCE DISCOVERY REPORT

| Field | Value |
|-------|-------|
| Discovery report | `docs/research/LR_LONG_ENTRY_GEOMETRY_DISCOVERY_V1_2026-10-01.md` |
| Discovery grid | `docs/research/lr_long_entry_geometry_discovery_v1_grid.csv` |
| Discovery results | `docs/research/lr_long_entry_geometry_discovery_v1_results.json` |
| Discovery verdict | FRAGILE_DISCOVERY_EDGE |
| Discovery primary Net E[R] (risk-gated) | +0.302 |
| Discovery primary PF (risk-gated) | 1.81 |
| Discovery bootstrap CI (trade) | [+0.054, +0.541] |
| Discovery bootstrap CI (day-block) | [+0.076, +0.568] |

---

## 4. VALIDATION POPULATION RULE

```text
PRIMARY_VALIDATION = POST_FREEZE_ONLY
```

| Rule | Detail |
|------|--------|
| Inclusion | observations with `signal_time >= FREEZE_TS` (2026-10-01 13:48:27 UTC) |
| Source dataset | `LR_LONG_GATE_V1` prospective_observation |
| Pre-freeze observations | `PRE_FREEZE_BLIND_HOLDOUT` — NOT included in primary validation |
| Pre-freeze N at freeze time | 32 observations (all `signal_time < FREEZE_TS`) |
| Pre-freeze performance | NOT computed in this task; excluded to eliminate contamination debate |

---

## 5. MATURITY / CHECKPOINT RULE

```text
eligible POST_FREEZE N >= 50
AND
distinct UTC OOS days >= 7
```

**N definition:** only observations that pass the frozen risk gate (`risk_dist / entry >= 0.3%`).

**Example:** if raw post-freeze signals = 70 and gate-passed = 48 → validation N = 48 → checkpoint NOT reached.

---

## 6. METRICS POLICY

### ALLOWED BEFORE CHECKPOINT (operational only)

| Metric | Status |
|--------|--------|
| Post-freeze raw LONG signals | ✅ |
| Risk-gate eligible N | ✅ |
| Risk-gate rejected N | ✅ |
| Distinct symbols | ✅ |
| Distinct OOS days | ✅ |
| Outcomes available | ✅ |
| Finalized outcomes | ✅ |
| Last signal time | ✅ |
| Pipeline health | ✅ |

### FORBIDDEN BEFORE CHECKPOINT (interim peeking)

| Metric | Status |
|--------|--------|
| Net E[R] | ❌ |
| PF | ❌ |
| WR | ❌ |
| Candidate performance | ❌ |
| Day performance | ❌ |
| Symbol performance | ❌ |
| TP/SL performance | ❌ |

---

## 7. VALIDATION EXECUTION (after checkpoint only)

When `eligible N >= 50 AND OOS days >= 7` simultaneously:

For **each** eligible observation apply **exactly**:

```text
SL       = 0.50R
TP       = 3.0R
timeout  = 120 min
STOP_FIRST (conservative)
fees     = 0.055% per side
slippage = 0.05% per side
```

- ❌ No grid search
- ❌ No alternative SL/TP/timeouts
- ❌ No new filters after seeing results

### Primary result to compute

N, symbols, OOS days, wins, losses, timeouts, ambiguous, WR, PF, Gross E[R], **Net E[R]** (primary), median R, average win, average loss, max losing streak.

### Predeclared robustness (only these)

**Costs:**
- normal slippage 0.05%/side
- elevated slippage 0.10%/side

**Concentration:**
- exclude best day
- exclude top symbol
- exclude top-3 symbols
- exclude largest winner
- exclude top-3 winners

**Bootstrap:**
- trade-level 95% CI
- day-block 95% CI

---

## 8. VALIDATION VERDICT RULES

| Verdict | Condition |
|---------|-----------|
| **VALIDATION_FAILED** | Frozen hypothesis does not show robust positive edge after costs |
| **VALIDATION_INCONCLUSIVE** | Net E[R] ≈ 0; CI spans zero; edge depends entirely on one day/symbol; sample not robust enough |
| **VALIDATION_PASSED** | ALL of: Net E[R] > 0; PF > 1; survives normal trading costs; does not disappear excluding best day; does not disappear excluding top symbol; bootstrap evidence consistent with positive expectancy |

**VALIDATION_PASSED does NOT mean automatic live trading.**

---

## 9. NO RETRO-OPTIMIZATION RULE

If validation fails:
- ❌ Do NOT return to the same dataset and pick SL=0.75, TP=2.5, timeout=15m, risk_gate=0.4%, etc.
- Any new geometry = **new hypothesis** = separate research cycle with new freeze.

---

## 10. PIPELINE VERIFICATION AT FREEZE

### Experiment status

| Experiment | Status | started_at |
|------------|--------|------------|
| LR_LONG_GATE_V1 | RUNNING | 2026-09-30 14:36:46 UTC |

### Direction gate

| Scanner | Direction | Status |
|---------|-----------|--------|
| LIQUIDITY_REVERSAL | LONG | **BLOCKED** |
| LIQUIDITY_REVERSAL | SHORT | BLOCKED |

### Capture pipeline (at FREEZE_TS)

| Check | Result |
|-------|--------|
| Total observations | 32 |
| Direction = LONG | 32/32 ✅ |
| Distinct symbols | 24 |
| Distinct days | 2 |
| Duplicate signals | 0 ✅ |
| Outcomes available | 28 |
| Finalized outcomes | 19 |
| Last signal | 2026-10-01 13:44:03 UTC |
| Paper trades (LR_LONG_GATE_V1) | 0 ✅ |
| Paper trades (LIQUIDITY_REVERSAL LONG, post-freeze) | 0 ✅ |
| Paper trades (LIQUIDITY_REVERSAL LONG, since experiment start) | 0 ✅ |

### Post-freeze counters at FREEZE_TS

| Metric | Value |
|--------|-------|
| Post-freeze raw LONG signals | 0 |
| Risk-gate eligible | 0 |
| Risk-gate rejected | 0 |
| Eligible symbols | 0 |
| OOS days | 0 |
| Outcomes available | 0 |
| Finalized | 0 |
| Last signal | pre-freeze (13:44:03 UTC) |
| Pipeline status | RUNNING, healthy |

**No natural LONG signal has occurred yet after FREEZE_TS. Pipeline is armed and waiting.**

---

## 11. SAFETY INVARIANTS CONFIRMED

| Invariant | Status |
|-----------|--------|
| LIQUIDITY_REVERSAL LONG PAPER gate = BLOCKED | ✅ |
| No new paper trades since FREEZE_TS | ✅ |
| No new paper trades since experiment start | ✅ |
| Research capture independent of paper gate | ✅ |
| No duplicate observations | ✅ |
| LR_LONG_GATE_V1 semantics unchanged | ✅ |
| No production config changes | ✅ |
| No scanner changes | ✅ |
| No service restarts | ✅ |

---

## 12. NEXT ACTION

1. Wait for natural LIQUIDITY_REVERSAL LONG signals to accumulate as `LR_LONG_GATE_V1` post-freeze observations.
2. Monitor operational counters ONLY (raw signals, gate-eligible N, symbols, days, outcomes maturity, pipeline health).
3. Do NOT compute interim performance.
4. When `eligible N >= 50 AND OOS days >= 7` simultaneously → execute frozen validation per Section 7.
5. Apply verdict rules per Section 8.

---

*Protocol frozen 2026-10-01 13:48:27 UTC. Parameters immutable.*
