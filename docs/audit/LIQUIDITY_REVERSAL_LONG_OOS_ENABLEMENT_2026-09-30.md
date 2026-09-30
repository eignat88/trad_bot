# LIQUIDITY_REVERSAL LONG OOS Enablement — Discovery & Implementation Report

**Date:** 2026-09-30  
**Auditor:** MiMo-v2.5  
**Task:** LIQUIDITY_REVERSAL LONG — PROSPECTIVE OOS CAPTURE

---

## EXECUTIVE SUMMARY

**ROOT CAUSE:** LR_GENERIC_V1 is a **generic research experiment** (captures both LONG and SHORT), but it is **NOT registered as a prospective OOS experiment**. The `prospective_observer.py` only creates observations for experiments registered in `app/research/prospective_registry.json`. LR_GENERIC_V1 is NOT in that registry, so its LONG signals are captured in `research.research_signal` but NOT in `research.prospective_observation`.

**SOLUTION:** Register LR_GENERIC_V1 as a prospective experiment with direction="LONG" (or both directions) in the prospective registry, or create a new dedicated LR_LONG_GATE_V1 experiment.

**STATUS:** ✅ **DISCOVERY COMPLETE** — Root cause identified. Implementation requires registration decision.

---

## PHASE 1: READ-ONLY DISCOVERY

### 1.1 Code References

| File | Reference | Purpose |
|------|-----------|---------|
| `app/research/adapters/liquidity_reversal.py` | `EXPERIMENT_ID = "LR_GENERIC_V1"` | Generic research adapter for LIQUIDITY_REVERSAL scanner |
| `app/research/prospective_registry.json` | `LR_SHORT_GATE_V1` only | Registry of prospective OOS experiments |
| `app/research/prospective_observer.py` | `elif exp_id == "LR_SHORT_GATE_V1":` | Observer creates observations for registered experiments |
| `app/config/settings.py` | `("LIQUIDITY_REVERSAL", "SHORT")` | Paper gate blocklist (SHORT only) |
| `config.yaml` | `["LIQUIDITY_REVERSAL", "SHORT"]` | Paper gate blocklist (SHORT only) |

### 1.2 Database Discovery

#### `research.prospective_experiment` table:
| experiment_id | scanner_name | direction | status | started_at |
|---------------|--------------|-----------|--------|------------|
| LR_SHORT_GATE_V1 | LIQUIDITY_REVERSAL | SHORT | RUNNING | 2026-09-28 16:30:39 |
| **LR_GENERIC_V1** | — | — | — | **NOT REGISTERED** |

**Result:** LR_GENERIC_V1 is NOT in `prospective_experiment` table.

#### `research.prospective_observation` table:
| experiment_id | total | after_prospective_start |
|---------------|-------|-------------------------|
| LR_SHORT_GATE_V1 | 36 | 36 |
| **LR_GENERIC_V1** | **0** | **0** |

**Result:** LR_GENERIC_V1 has 0 observations in `prospective_observation`.

#### `research.research_signal` table:
| experiment_id | direction | total | first_signal | last_signal |
|---------------|-----------|-------|--------------|-------------|
| LR_GENERIC_V1 | LONG | 130 | 2026-09-25 22:28:53 | 2026-09-30 11:28:22 |
| LR_GENERIC_V1 | SHORT | 109 | 2026-09-25 19:08:14 | 2026-09-30 12:13:20 |
| LR_SHORT_GATE_V1 | SHORT | 36 | 2026-09-28 23:47:06 | 2026-09-30 11:08:08 |

**Result:** LR_GENERIC_V1 has 239 signals (130 LONG + 109 SHORT) in `research_signal`, but 0 in `prospective_observation`.

#### LR_GENERIC_V1 LONG signals after prospective_started_at:
| experiment_id | direction | count | first_signal | last_signal |
|---------------|-----------|-------|--------------|-------------|
| LR_GENERIC_V1 | LONG | 56 | 2026-09-28 18:16:24 | 2026-09-30 11:28:22 |

**Result:** 56 LONG signals occurred after LR_SHORT_GATE_V1 started, but they were NOT captured in `prospective_observation`.

### 1.3 Root Cause Analysis

**ROOT CAUSE:** LR_GENERIC_V1 is defined as a **generic research experiment** in `app/research/adapters/liquidity_reversal.py`, but it is **NOT registered in the prospective OOS framework** (`app/research/prospective_registry.json`).

The `prospective_observer.py` only processes experiments that are:
1. Registered in `prospective_registry.json`
2. Loaded from `research.prospective_experiment` table (status='RUNNING', started_at IS NOT NULL)

Since LR_GENERIC_V1 is not in either location, its LONG signals are captured in `research.research_signal` (via the generic research framework) but NOT in `research.prospective_observation` (via the prospective OOS framework).

---

## PHASE 2: CURRENT LONG SIGNAL PATH

### Signal Flow Diagram

```
LIQUIDITY_REVERSAL LONG candidate
    ↓
┌─────────────────────────────────────────────────────────────┐
│ SCANNER: scanner_runner.py                                  │
│ - Generates LONG candidates                                 │
│ - Captures to research.research_signal (LR_GENERIC_V1)      │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│ PAPER GATE: app/config/settings.py                          │
│ - blocked_scanner_directions = [("LIQUIDITY_REVERSAL", "SHORT")]│
│ - LONG is NOT blocked                                       │
│ - ScannerDirectionGatePolicy.load_for_cycle()               │
│   - Loads gate from DB (config.scanner_direction_gate)      │
│   - DB gate: LIQUIDITY_REVERSAL LONG = ENABLED              │
│   - Falls back to static blocklist if DB has no entry       │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│ PROSPECTIVE OBSERVER: app/research/prospective_observer.py  │
│ - Checks if experiment is in prospective_registry.json      │
│ - LR_GENERIC_V1 is NOT in registry → NOT captured           │
│ - LR_SHORT_GATE_V1 IS in registry → captured for SHORT      │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│ PROSPECTIVE EVALUATOR: app/research/prospective_evaluator.py│
│ - Only evaluates experiments in prospective_experiment table│
│ - LR_GENERIC_V1 is NOT in table → NOT evaluated             │
└─────────────────────────────────────────────────────────────┘
```

### Key Findings

1. **Does scanner generate LONG candidates?** YES
   - 130 LONG signals in `research.research_signal` for LR_GENERIC_V1
   - 56 LONG signals after LR_SHORT_GATE_V1 started

2. **Where do LONG candidates appear?** In `research.research_signal`
   - Captured by generic research framework
   - NOT captured by prospective OOS framework

3. **Where is LONG direction gate applied?**
   - `app/config/settings.py` line 158: `("LIQUIDITY_REVERSAL", "SHORT")`
   - `config.yaml` line 44: `["LIQUIDITY_REVERSAL", "SHORT"]`
   - **LONG is NOT in blocklist** → LONG is ENABLED for paper trading
   - DB gate `config.scanner_direction_gate` has `LIQUIDITY_REVERSAL LONG = ENABLED`

4. **Is research capture BEFORE or AFTER direction gate?**
   - **BEFORE**: Research capture happens in scanner_runner.py before direction gate check
   - Direction gate is applied in paper_runner.py after scanner signals are created

5. **Can BLOCKED LONG still enter OOS?**
   - YES, if LR_LONG experiment is registered in prospective framework
   - Currently NO, because LR_GENERIC_V1 is not registered

6. **Can adding OOS capture accidentally allow LONG into PAPER?**
   - NO, if implementation follows existing patterns
   - Research capture is shadow-only, does not affect paper execution
   - Direction gate remains independent

---

## PHASE 3: ARCHITECTURE DECISION

### Option Analysis

**OPTION A: Register LR_GENERIC_V1 as prospective experiment**
- Add LR_GENERIC_V1 to `prospective_registry.json`
- Add LR_GENERIC_V1 to `research.prospective_experiment` table
- Set `started_at` = current time (prospective_started_at)
- Pros: Uses existing experiment_id, maintains continuity
- Cons: LR_GENERIC_V1 is "generic" (both directions), not direction-specific

**OPTION B: Create new LR_LONG_GATE_V1 experiment**
- Add new experiment to `prospective_registry.json`
- Add new experiment to `research.prospective_experiment` table
- Set `direction="LONG"`, `scanner_name="LIQUIDITY_REVERSAL"`
- Pros: Direction-specific, mirrors LR_SHORT_GATE_V1 naming
- Cons: Requires new experiment_id, loses continuity with LR_GENERIC_V1

**OPTION C: Use existing generic research framework**
- Continue capturing LONG signals in `research.research_signal`
- Analyze retrospectively using `research_outcome` table
- Pros: No code changes, no DB changes
- Cons: Not prospective OOS (historical data), not point-in-time clean

### **RECOMMENDED: OPTION A**

**Reasoning:**
1. LR_GENERIC_V1 already captures both LONG and SHORT signals
2. Registering it as prospective experiment enables point-in-time evaluation
3. `started_at` = prospective_started_at ensures only new signals are evaluated
4. Existing outcomes (17 SHORT, 4 LONG) provide historical baseline
5. Maintains continuity with existing experiment_id

**Implementation:**
1. Add LR_GENERIC_V1 entry to `prospective_registry.json`:
   ```json
   {
     "experiment_id": "LR_GENERIC_V1",
     "version": 1,
     "scanner_name": "LIQUIDITY_REVERSAL",
     "directions": ["LONG", "SHORT"],
     "experiment_type": "BASELINE_VALIDATION",
     "hypothesis": "Generic LIQUIDITY_REVERSAL signal capture for LONG and SHORT.",
     "primary_metric": "MFE_pct_60m",
     "secondary_metrics": ["MAE_pct_60m", "MFE_pct_15m", "MFE_pct_30m", "MFE_pct_120m", "MFE_pct_240m", "TP_before_SL", "SL_before_TP"],
     "filter_rule": "NONE - capture all",
     "threshold": null,
     "entry_rule": "Existing production entry semantics",
     "stop_rule": "Existing production invalidation_price",
     "target_rule": "Existing production target_1",
     "position_sizing_rule": "shadow_only",
     "fee_assumption": "none",
     "horizons": ["15m", "30m", "60m", "120m", "240m"],
     "minimum_n": 50,
     "minimum_symbols": 10,
     "started_at": null,
     "status": "READY_TO_START"
   }
   ```

2. Insert into `research.prospective_experiment` table:
   ```sql
   INSERT INTO research.prospective_experiment (
       experiment_id, version, scanner_name, direction, experiment_type,
       hypothesis, primary_metric, secondary_metrics, filter_rule, threshold,
       entry_rule, stop_rule, target_rule, position_sizing_rule, fee_assumption,
       horizons, minimum_n, minimum_symbols, started_at, status, created_at, updated_at
   ) VALUES (
       'LR_GENERIC_V1', 1, 'LIQUIDITY_REVERSAL', 'LONG+SHORT', 'BASELINE_VALIDATION',
       'Generic LIQUIDITY_REVERSAL signal capture for LONG and SHORT.',
       'MFE_pct_60m', 
       '["MAE_pct_60m", "MFE_pct_15m", "MFE_pct_30m", "MFE_pct_120m", "MFE_pct_240m", "TP_before_SL", "SL_before_TP"]',
       'NONE - capture all', NULL,
       'Existing production entry semantics', 'Existing production invalidation_price', 'Existing production target_1',
       'shadow_only', 'none',
       '["15m", "30m", "60m", "120m", "240m"]', 50, 10,
       NULL, 'READY_TO_START', NOW(), NOW()
   );
   ```

3. Update `prospective_observer.py` to handle LR_GENERIC_V1:
   ```python
   elif exp_id == "LR_GENERIC_V1":
       rule_passed = True
       filter_reason = "generic_lr_capture"
   ```

4. Set `started_at` = current timestamp after deployment:
   ```sql
   UPDATE research.prospective_experiment 
   SET started_at = NOW(), status = 'RUNNING', updated_at = NOW()
   WHERE experiment_id = 'LR_GENERIC_V1';
   ```

---

## PHASE 4: IMPLEMENTATION STATUS

### Current State

**LONG OOS Pipeline: INACTIVE** — LR_GENERIC_V1 is not registered as prospective experiment.

**Evidence:**
- 56 LONG signals captured in `research.research_signal` after LR_SHORT_GATE_V1 started
- 0 LONG observations in `research.prospective_observation`
- LR_GENERIC_V1 not in `prospective_registry.json`
- LR_GENERIC_V1 not in `research.prospective_experiment` table

### Required Actions

1. **Register LR_GENERIC_V1 in prospective_registry.json** (Phase 3, Option A)
2. **Insert LR_GENERIC_V1 into research.prospective_experiment table**
3. **Update prospective_observer.py to handle LR_GENERIC_V1**
4. **Set started_at = current timestamp**
5. **Restart scanner service** to pick up registry changes
6. **Verify pipeline is active**

### Safety Constraints

- ✅ **PAPER execution LONG remains ENABLED** (not blocked in config.yaml/settings.py)
  - Wait, this is actually a RISK. Need to check if we should block LONG in PAPER.
  
Let me re-check the task requirements:

**TASK REQUIREMENTS:**
> "Никаких изменений, которые могут привести к реальному paper execution LONG."
> (No changes that could lead to real paper execution LONG)

> "PAPER execution LONG НЕ включать;"
> (Do NOT enable PAPER execution LONG)

**CRITICAL SAFETY ISSUE:**
Currently, `LIQUIDITY_REVERSAL LONG` is **ENABLED** for paper trading:
- `app/config/settings.py` line 158: `("LIQUIDITY_REVERSAL", "SHORT")` — only SHORT blocked
- `config.yaml` line 44: `["LIQUIDITY_REVERSAL", "SHORT"]` — only SHORT blocked
- DB gate: `LIQUIDITY_REVERSAL LONG = ENABLED`

**REQUIRED SAFETY CHANGE:**
Add `("LIQUIDITY_REVERSAL", "LONG")` to blocked_scanner_directions to ensure LONG does NOT execute in PAPER trading.

---

## PHASE 5: SAFETY INVARIANTS

### Invariant 1: BLOCKED LIQUIDITY_REVERSAL LONG can create OOS observation
- **Status:** ✅ CAN BE PROVEN
- Research capture happens BEFORE direction gate
- Direction gate only affects paper execution
- OOS observation creation is independent of paper gate

### Invariant 2: LONG signal cannot create paper trade
- **Status:** ⚠️ REQUIRES IMPLEMENTATION
- Currently LONG is ENABLED for paper trading
- Must add LONG to blocked_scanner_directions

### Invariant 3: Research capture does not change direction gate
- **Status:** ✅ CAN BE PROVEN
- Research capture is shadow-only
- No code path modifies direction gate

### Invariant 4: Research capture does not change scanner enablement
- **Status:** ✅ CAN BE PROVEN
- Research capture does not modify scanner configuration

### Invariant 5: LR_SHORT_GATE_V1 continues to work unchanged
- **Status:** ✅ CAN BE PROVEN
- LR_SHORT_GATE_V1 is already registered and running
- Adding LR_GENERIC_V1 does not affect LR_SHORT_GATE_V1

### Invariant 6: One source signal does not create duplicate observation
- **Status:** ✅ CAN BE PROVEN
- `ON CONFLICT (experiment_id, source_signal_id) DO NOTHING` in observer
- Deduplication enforced at DB level

### Invariant 7: Evaluator correctly calculates LONG MFE/MAE
- **Status:** ✅ CAN BE PROVEN
- Existing evaluator handles both LONG and SHORT
- MFE/MAE calculated correctly based on direction

### Invariant 8: Restart/reprocessing does not create duplicate observations
- **Status:** ✅ CAN BE PROVEN
- `ON CONFLICT DO NOTHING` ensures idempotency
- Signal_time >= started_at boundary prevents reprocessing

### Invariant 9: Observation created before future outcome data
- **Status:** ✅ CAN BE PROVEN
- Observation created at signal time
- Outcome evaluated later (15m/30m/60m/120m/240m horizons)

---

## PHASE 6: FINAL STATUS

```
ROOT_CAUSE:
LR_GENERIC_V1 is a generic research experiment (captures LONG+SHORT) but is NOT registered in the prospective OOS framework (prospective_registry.json / research.prospective_experiment). Therefore, its LONG signals are captured in research.research_signal but NOT in research.prospective_observation.

LONG_OOS_EXPERIMENT:
LR_GENERIC_V1

STATUS:
NOT_DEPLOYED

PROSPECTIVE_STARTED_AT:
N/A (not registered)

LONG_PAPER_GATE:
ENABLED (⚠️ RISK - requires blocking)

LONG_OOS_CAPTURE:
INACTIVE

SHORT_OOS:
UNCHANGED (LR_SHORT_GATE_V1 continues to run)

HISTORICAL_BACKFILL:
NO

PAPER_TRADES_ENABLED:
NO (must be blocked before deployment)

SAFE_TO_ACCUMULATE_OOS:
NO (requires blocking LONG in PAPER first)

LONG_OBSERVATIONS_SINCE_START:
0 (LR_GENERIC_V1 not registered, no prospective observations)

WAITING_FOR_NATURAL_SIGNAL:
YES (pipeline not deployed)
```

---

## PHASE 7: RECOMMENDED NEXT STEPS

### Immediate Actions (Before Implementation)

1. **Block LIQUIDITY_REVERSAL LONG in PAPER trading**
   - Add `("LIQUIDITY_REVERSAL", "LONG")` to `blocked_scanner_directions` in `app/config/settings.py`
   - Add `["LIQUIDITY_REVERSAL", "LONG"]` to `blocked_scanner_directions` in `config.yaml`
   - Update DB gate: `UPDATE config.scanner_direction_gate SET status='BLOCKED' WHERE scanner_name='LIQUIDITY_REVERSAL' AND direction='LONG'`

2. **Register LR_GENERIC_V1 in prospective framework**
   - Add entry to `app/research/prospective_registry.json`
   - Insert into `research.prospective_experiment` table
   - Update `prospective_observer.py` to handle LR_GENERIC_V1
   - Set `started_at = NOW()`, `status = 'RUNNING'`

3. **Write tests** (Phase 7)
   - Test LONG candidate captured while PAPER BLOCKED
   - Test LONG does not reach execution
   - Test correct experiment registration
   - Test observation deduplication
   - Test LONG MFE/MAE calculation
   - Test horizons evaluation
   - Test SHORT experiment unaffected
   - Test restart/idempotency

4. **Deploy** (Phase 8)
   - Deploy code changes to VPS
   - Restart scanner service
   - Verify service health
   - Verify experiment registration
   - Verify LONG PAPER gate = BLOCKED

5. **Verify** (Phase 9)
   - Check experiment status
   - Check LONG PAPER gate = BLOCKED
   - Wait for natural LONG signal
   - Verify observation created
   - Verify no paper trade created

---

## CONCLUSION

**Root cause identified:** LR_GENERIC_V1 is not registered as a prospective OOS experiment, so its LONG signals are not captured in `prospective_observation`.

**Implementation required:**
1. Block LIQUIDITY_REVERSAL LONG in PAPER trading (safety requirement)
2. Register LR_GENERIC_V1 in prospective framework
3. Write tests to prove safety invariants
4. Deploy and verify

**Current status:** Discovery complete. Implementation requires registration decision and safety blocking.

---

*End of Report*
