# FVG Reaction Long Research Closure V1 — 2026-10-02

**Experiment:** `FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1`  
**Scanner:** `FVG_REACTION_LONG_LOCAL_STRUCT_V1`  
**Direction:** `LONG`

---

## Closure decision

```text
Research status = CLOSED_NEGATIVE
```

## Basis

Prospective OOS review `FVG_EXPECTANCY_REJECTION_OOS_REVIEW_V1` on frozen dataset at cutoff `2026-10-02T13:38:51+00:00`:

| Metric | Value |
|---|---|
| Raw N | 323 |
| Eligible N (finalized @240m) | 306 |
| Gross E[R] | -0.114 |
| Net E[R] (normal costs 0.21%) | **-0.534** |
| Net PF | **0.412** |
| 95% CI | [-0.693, -0.366] |
| P(E[R] > 0) | 0.0000 |

Verdict: `NEGATIVE_BASELINE_CONFIRMED`

The negative historical baseline (avg_r=-0.42, PF=0.459, WR=0.167, N=78) is confirmed prospectively with a 4x larger sample. No pre-specified FVG feature subset shows stable positive expectancy. Result is stable under leave-one-day-out, symbol exclusion, and regime sensitivity.

## Closure conditions

```text
Further same-hypothesis OOS = NOT_REQUIRED
Same-hypothesis optimization = DO_NOT_PROCEED
Retrospective subgroup mining = DO_NOT_PROCEED
Paper/live = DO_NOT_PROCEED
Reopen only with NEW_INDEPENDENT_HYPOTHESIS
```

## Runtime lifecycle

DB lifecycle change NOT performed in this task. `FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1` may still be `RUNNING` in production DB. A separate lifecycle cleanup task is required to reconcile runtime status with this closure decision.

## Artifacts

- `docs/research/FVG_EXPECTANCY_REJECTION_OOS_REVIEW_V1_2026-10-02.md` — full review report
- `docs/research/fvg_expectancy_rejection_oos_review_v1_results.json` — machine-readable results
- `docs/research/fvg_expectancy_rejection_oos_review_v1_observations.csv` — observation-level data
- `tools/research/fvg_expectancy_rejection_oos_review_v1.py` — deterministic analysis script

## Authoritative research lineage

```text
historical observation (FVG_GENERIC_V1, N=78)
→ hypothesis: negative historical expectancy (avg_r=-0.42)
→ historical result: avg_r=-0.42, PF=0.459, WR=0.167
→ reason: check for subset with positive edge
→ prospective experiment: FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1
→ OOS review: NEGATIVE_BASELINE_CONFIRMED
→ closure: CLOSED_NEGATIVE
```

---

*End of FVG Reaction Long Research Closure V1.*
