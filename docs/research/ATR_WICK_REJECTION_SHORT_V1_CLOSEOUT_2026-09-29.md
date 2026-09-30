# ATR_WICK RESEARCH CLOSEOUT

**Date:** 2026-09-30
**Status:** CLOSED_NEGATIVE

---

## ATR_WICK RESEARCH CLOSEOUT

| Field | Value |
|---|---|
| **Research branch** | ATR_WICK_REJECTION_SHORT_V1 |
| **Final experiment** | ATR_WICK_FILTER_OOS_V2_D |
| **Discovery N** | 283 |
| **Discovery Good/Bad** | 9.77 (127 good / 13 bad) |
| **Prospective OOS N** | 20,357 |
| **Prospective Good/Bad** | 0.72 |
| **Post-mortem hypotheses tested** | 34 (across 6 feature dimensions + 3 interactions) |
| **Post-mortem result** | NO_STABLE_V3_HYPOTHESIS |
| **Final experiment status** | COMPLETED |
| **Closed at** | 2026-09-30 05:35 UTC |
| **Final observation timestamp** | 2026-09-30 05:00:00 UTC |
| **Final observations (V2D)** | 20,357 |
| **Final outcomes (V2D)** | 20,357 |
| **Finalized (V2D)** | 19,819 (97.4%) |
| **Pending outcomes (V2D)** | 538 (2.6%) — horizon not yet elapsed at shutdown time |
| **Final observations (V1 shadow)** | 87,450 |
| **Final outcomes (V1 shadow)** | 87,450 |
| **Pending outcomes (V1 shadow)** | 2,670 |
| **Scanner runtime** | `trad-bot-v2d-scanner.service` — STOPPED + DISABLED |
| | `trad-bot-shadow-atr-wick.service` — STOPPED + DISABLED |
| **Evaluator runtime** | `trad-bot-v2d-evaluator.timer` — STOPPED + DISABLED |
| | `trad-bot-shadow-evaluator.timer` — STOPPED + DISABLED |
| **Data retained** | ✅ All observations, outcomes, discovery metadata preserved |
| **Code retained** | ✅ All scanner/evaluator/filter code retained in git |
| **Resurrection protection** | ✅ All 4 units disabled, removed from multi-user.target.wants |
| **Other OOS affected** | ❌ None — shared evaluator, scanner, paper all still active |
| **Git commit** | No commit needed — no code changes, only systemd + registry |

---

## Component Shutdown Summary

### Stopped and Disabled (Dedicated)
| Unit | Type | PRE State | POST State |
|---|---|---|---|
| `trad-bot-v2d-scanner.service` | Service | active, enabled | inactive, disabled |
| `trad-bot-v2d-evaluator.timer` | Timer | active, enabled | inactive, disabled |
| `trad-bot-shadow-atr-wick.service` | Service | active, enabled | inactive, disabled |
| `trad-bot-shadow-evaluator.timer` | Timer | active, enabled | inactive, disabled |

### Verified Still Running (Shared)
| Unit | Status |
|---|---|
| `trad-bot-scanner.service` | ✅ active |
| `trad-bot-paper.service` | ✅ active |
| `trad-bot-research-evaluator.service` | ✅ active |

---

## Data Snapshot at Close

| Table | Count |
|---|---|
| `dds.v2d_signal` (V2D) | 20,357 |
| `dds.v2d_outcome` (V2D) | 20,357 |
| `dds.v2d_outcome` finalized | 19,819 |
| `dds.v2d_outcome` pending | 538 |
| `dds.shadow_signal` (V1) | 87,450 |
| `dds.shadow_signal_outcome` (V1) | 87,450 |
| `dds.shadow_signal_outcome` pending | 2,670 |
| `dds.shadow_oos_experiment_registry` | COMPLETED with notes |

---

## Registry Update

```
experiment_id:     ATR_WICK_FILTER_OOS_V2_D
status:            COMPLETED
notes:             CLOSED_NEGATIVE: V2_D prospective OOS N=20357 failed to 
                   replicate discovery edge (Good/Bad 9.77 -> 0.72). 
                   Post-mortem tested 34 hypotheses across 6 feature 
                   dimensions and found NO_STABLE_V3_HYPOTHESIS. 
                   Scanners stopped 2026-09-30 05:10 UTC. Research branch 
                   closed to avoid further data mining.
```

---

## Verification Evidence

1. **New observations = 0** — confirmed via Δ check (N stayed at 20,357 over 2+ minute interval)
2. **Max signal_time frozen** — 2026-09-30 05:00:00 UTC
3. **All dedicated units disabled** — confirmed via `systemctl is-enabled` and symlink check
4. **Resurrection safe** — no v2d/shadow units in multi-user.target.wants
5. **Other OOS unaffected** — research evaluator processing FVG, ME_R, SRR, etc.
6. **All data preserved** — no DELETE/TRUNCATE executed

---

## Pending Outcomes Note

538 V2D outcomes and 2,670 shadow outcomes remain with `is_final = false`. These are signals whose maximum evaluation horizon (240m = 4 hours) had not yet elapsed at the time evaluators were stopped. This is acceptable because:

- The signals and their partial outcomes (15m, 30m, 60m, 120m horizons) are fully preserved
- The missing 240m horizon data does not affect any published metric (primary analysis used 60m horizon)
- These pending records serve as complete research provenance

---

## FINAL VERDICT

### `CLOSED_NEGATIVE`

ATR_WICK_REJECTION_SHORT_V1 research branch is permanently closed. The discovery edge (Good/Bad = 9.77) was a statistical artifact of a small sample (N=283). Prospective OOS (N=20,357) showed Good/Bad = 0.72. Post-mortem analysis of 34 hypotheses across 6 feature dimensions found no stable conditional edge. V3 hypothesis was not created. Further parameter search on this scanner would constitute data mining.

---

*Closeout performed by MiMo-v2.5 on 2026-09-30 05:35 UTC*
