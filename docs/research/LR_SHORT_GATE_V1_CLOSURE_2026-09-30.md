# LR_SHORT_GATE_V1 — Experiment Closure Record

**Date:** 2026-09-30  
**Closed by:** MiMo-v2.5  
**Safety:** READ-ONLY for data. Only status change + report.

---

## Experiment Details

| Field | Value |
|-------|-------|
| **Experiment ID** | LR_SHORT_GATE_V1 |
| **Scanner** | LIQUIDITY_REVERSAL |
| **Direction** | SHORT |
| **Previous status** | RUNNING |
| **Final status** | CANCELLED |
| **Started** | 2026-09-28 16:30:39 UTC |
| **Closed** | 2026-09-30 13:44:19 UTC |
| **Duration** | ~2 days |

## Final Data State

| Metric | Value |
|--------|-------|
| **Final observations** | 40 |
| **Final outcomes** | 38 |
| **Unique symbols** | 32 |
| **First observation** | 2026-09-28 23:47:06 UTC |
| **Last observation** | 2026-09-30 13:33:42 UTC |
| **OOS days** | 3 |

## Closure Reason

**NO_EXECUTABLE_EDGE**

### Evidence
| Metric | Value |
|--------|-------|
| Net E[R] | −0.344 |
| Profit Factor | 0.9915 |
| Median R | −1.10 |
| Win rate | 47.6% |
| Production entry rate | 55.3% (21/38) |
| +2R before stop | 0% (0/21) |
| +1R before stop | 47.6% (10/21) |

**Evidence source:** `docs/research/LIQUIDITY_REVERSAL_SHORT_COUNTERFACTUAL_DATA_INTEGRITY_V2_2026-09-30.md`

---

## Closure Mechanism

### Changes Made

1. **DB status:** `research.prospective_experiment.status` = `RUNNING` → `CANCELLED`
   - Allowed by CHECK constraint: `prospective_experiment_status_check`
   - Valid values: READY_TO_START, RUNNING, PAUSED, COMPLETED, CANCELLED
   - Evaluator (`evaluator_runner._load_prospective_experiments`) filters on `status = 'RUNNING'`
   - Setting to CANCELLED stops evaluation on next cycle (dynamic reload, no restart needed)

2. **Registry JSON:** Removed `LR_SHORT_GATE_V1` from `app/research/prospective_registry.json`
   - Scanner capture uses JSON registry (in-memory, loaded at scanner startup)
   - Removing from registry stops capture on next scanner restart
   - Registry went from 10 → 9 experiments

### Why Both Changes Were Needed

| Component | Reads from | Stops capture when |
|-----------|-----------|-------------------|
| Evaluator | DB `status = 'RUNNING'` | status ≠ RUNNING (dynamic) |
| Scanner capture | JSON registry (in-memory) | removed from registry (requires restart) |

---

## Data Preservation

| Check | Status |
|-------|--------|
| Observations preserved | ✅ 40/40 |
| Outcomes preserved | ✅ 38/38 |
| source_signal_id preserved | ✅ |
| features/payload preserved | ✅ |
| timestamps preserved | ✅ |
| Nothing deleted | ✅ |
| Nothing archived | ✅ |
| Data available for counterfactual | ✅ |

**Suitable as historical research dataset:** YES  
**Next research:** LR_SHORT_ENTRY_GEOMETRY_COUNTERFACTUAL_V1

---

## Paper Gate State

| Source | LIQUIDITY_REVERSAL SHORT | Notes |
|--------|--------------------------|-------|
| `config.yaml` blocked_scanner_directions | **BLOCKED** | Listed in blocklist |
| `config.scanner_direction_gate` (DB) | **ENABLED** | "manual unblock" 2026-09-03 |
| Effective gate (DB overrides config) | **ENABLED** ⚠️ | DB gate takes priority |

### ⚠️ GATE DRIFT DETECTED

**BLOCKER:** There is a gate drift between config.yaml and DB gate state:
- `config.yaml`: LIQUIDITY_REVERSAL SHORT is in `blocked_scanner_directions` → BLOCKED
- `config.scanner_direction_gate`: status = ENABLED (manual unblock from 2026-09-03)
- **Effective state:** DB gate takes priority → ENABLED

**Per task instructions, this drift was NOT automatically fixed.** The DB gate says ENABLED, meaning the paper engine would allow entries if a READY_TO_TRADE setup existed for LIQUIDITY_REVERSAL SHORT.

**However:** LIQUIDITY_REVERSAL SHORT is not expected to generate READY_TO_TRADE setups because:
1. The scanner produces DETECTED status for observe-only candidates
2. The direction gate in the scanner pipeline marks them as observe-only
3. Paper engine only picks up READY_TO_TRADE setups

**Recommendation:** Operator should manually review and update `config.scanner_direction_gate` to set LIQUIDITY_REVERSAL SHORT to BLOCKED or OBSERVE_ONLY to align with config.yaml intent.

---

## Services Status

| Service | Status | Restarted? |
|---------|--------|------------|
| trad-bot-scanner | active (running) | NO |
| trad-bot-paper | active (running) | NO |
| trad-bot-research-evaluator | active (running) | NO |

**No restarts performed.** The evaluator dynamically reloads experiment status each cycle. The scanner will pick up the registry change on its next natural restart.

---

## Post-Change Verification

| Check | Result |
|-------|--------|
| DB status = CANCELLED | ✅ |
| Observations after closure | 0 |
| Outcomes preserved | 38/38 |
| Registry updated | ✅ (9 experiments remain) |
| LR_SHORT_GATE_V1 in registry | ❌ removed |
| Evaluator will skip | ✅ (status ≠ RUNNING) |
| Capture will skip | ✅ (after scanner restart) |

---

## Git

| Item | Value |
|------|-------|
| Changed files | `docs/research/LR_SHORT_GATE_V1_CLOSURE_2026-09-30.md` |
| DB change | `research.prospective_experiment.status` (not in git) |
| Registry change | `app/research/prospective_registry.json` |

---

*End of Closure Record*
