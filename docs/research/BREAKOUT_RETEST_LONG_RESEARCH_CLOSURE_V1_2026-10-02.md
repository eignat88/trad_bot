# BREAKOUT_RETEST LONG Research Closure V1

**Date:** 2026-10-02  
**Closure by:** MiMo-v2.5  
**Type:** Research lifecycle / documentation task only  
**Safety:** READ-ONLY for runtime/scanner/config/DB. Only research metadata documentation was created.

---

## 1. Closure Identity

| Field | Value |
|---|---|
| Closure record | `BREAKOUT_RETEST_LONG_RESEARCH_CLOSURE_V1` |
| Closure date | 2026-10-02 |
| Experiment | `BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1` |
| Scanner | `BREAKOUT_RETEST` |
| Direction | `LONG` |
| Framework | `research.prospective_*` |
| Evidence review cutoff | `2026-10-02T09:51:17Z` |
| Lifecycle recheck cutoff | `2026-10-02 10:25:24 UTC` |
| VPS Git HEAD at recheck | `b80262a6eaf8654e43c01d14431bea30954a25fb` |
| Research status | **`CLOSED_NEGATIVE`** |
| Evidence verdict | **`EXPECTANCY_REJECTION_CONFIRMED`** |
| Closure type | Research lifecycle / documentation only |
| Production changes | **NONE** |

---

## 2. Research Hypothesis

The frozen research hypothesis was defined in:

- `app/research/prospective_registry.json`
- `app/research/adapters/breakout_retest_expectancy_oos.py`

### Frozen hypothesis

> `BREAKOUT_RETEST LONG` has `PF = 1.0976`, below the expectancy-filter threshold `1.20`. Prospective capture checks whether new signals have positive edge despite historical performance.

### Frozen contract

| Field | Frozen value |
|---|---|
| Experiment type | `EXPECTANCY_REJECTION_OOS` |
| Capture rule | Candidates passing risk geometry, score gate, and direction gate but rejected by expectancy filter |
| Expectancy threshold | `profit_factor < 1.20` |
| Entry rule | Existing production entry semantics |
| Stop rule | Existing production invalidation price |
| Target rule | Existing production target 1 |
| Position sizing | `shadow_only` |
| Fee assumption | none |
| Formal minimum N | 50 |
| Formal minimum symbols | 10 |

---

## 3. Evidence Reviewed

### 3.1 Authoritative OOS Review

Primary evidence:

`docs/research/BREAKOUT_RETEST_EXPECTANCY_REJECTION_OOS_REVIEW_V1_2026-10-02.md`

The review used a frozen finalized dataset export from production PostgreSQL, analyzed offline in READ-ONLY mode.

### 3.2 Supporting artifacts

| Artifact | Path |
|---|---|
| Results JSON | `audit_db/breakout_retest_expectancy_oos_review_v1/breakout_retest_expectancy_oos_review_v1_results.json` |
| Daily CSV | `audit_db/breakout_retest_expectancy_oos_review_v1/breakout_retest_expectancy_oos_review_v1_daily.csv` |
| Finalized dataset | `audit_db/breakout_retest_expectancy_oos_review_v1/dataset_final.tsv` |
| Dataset export SQL | `research_snapshot/breakout_retest_expectancy_oos_review_v1_dataset.sql` |
| Offline analysis script | `tools/research/breakout_retest_expectancy_oos_review_v1_offline.py` |
| Registry contract | `app/research/prospective_registry.json` |
| Adapter contract | `app/research/adapters/breakout_retest_expectancy_oos.py` |

Artifacts are consistent. No research recalculation was required for this closure.

---

## 4. Historical Baseline

Frozen registry baseline:

| Metric | Historical value |
|---|---:|
| Samples | 63 |
| Entries | 56 |
| Avg R after costs | +0.0037 |
| Profit factor | 1.0976 |
| Win rate | 16.07% |

The historical baseline was weak and only slightly below the production expectancy threshold of `1.20`.

---

## 5. Prospective OOS Result

### 5.1 Finalized population

| Metric | Value |
|---|---:|
| Finalized N | 416 |
| Distinct symbols | 54 |
| OOS days | 5 |
| First signal | `2026-09-28 16:45:54.617234+00` |
| Last signal at review cutoff | `2026-10-02 05:45:36.809524+00` |
| Formal minimum N | 50 |
| Formal minimum symbols | 10 |

### 5.2 Data integrity

| Check | Result |
|---|---:|
| Missing reference price | 0 |
| Missing invalidation price | 0 |
| Missing target price | 0 |
| Wrong direction | 0 |
| Future-dated rows | 0 |
| Duplicate outcomes | 0 |
| Missing mature 60m data | 0 |

Data integrity: `PASS`.

### 5.3 Outcome distribution

| Outcome | N | Share |
|---|---:|---:|
| SL first | 257 | 61.8% |
| TP first | 59 | 14.2% |
| Mature timeout | 100 | 24.0% |

### 5.4 Gross result versus historical baseline

| Metric | Prospective OOS | Historical baseline |
|---|---:|---:|
| N | 416 | 56 entries |
| Avg return / avg R after costs | **-0.2679%** | **+0.0037 R** |
| PF | **0.566** | **1.0976** |
| Win rate | 14.18% | 16.07% |
| Median return | -1.00% | not available in registry |
| Bootstrap mean 95% CI | **[-0.3891%, -0.1370%]** | not available in registry |
| P(mean > 0) | 0.0001 | not available in registry |

Conclusion: prospective OOS does not replicate the historical edge. The gross PF is below `1.0`, and the bootstrap CI is entirely negative.

---

## 6. Cost-Adjusted Result

### 6.1 Normal project costs

Normal round-trip cost: `0.21% of entry`.

| Metric | Value |
|---|---:|
| Avg return after costs | **-0.4779%** |
| PF after costs | **0.401** |
| Median return after costs | -1.21% |
| Bootstrap mean 95% CI | **[-0.5991%, -0.3470%]** |
| P(mean > 0) | 0.0000 |

### 6.2 Elevated slippage sensitivity

Stressed round-trip cost: `0.71% of entry`.

| Metric | Value |
|---|---:|
| Avg return after stressed costs | **-0.9779%** |
| PF after stressed costs | **0.203** |
| Bootstrap mean 95% CI | **[-1.0991%, -0.8470%]** |
| P(mean > 0) | 0.0000 |

Conclusion: normal project costs worsen the negative result; elevated slippage worsens it further. No cost scenario supports executable edge.

---

## 7. Stability Checks

### 7.1 Day concentration

| OOS day | N | Gross avg % | Gross PF | Normal net avg % | Normal net PF |
|---|---:|---:|---:|---:|---:|
| 2026-09-28 | 28 | -1.0000 | 0.000 | -1.2100 | 0.000 |
| 2026-09-29 | 101 | -0.3146 | 0.433 | -0.5246 | 0.295 |
| 2026-09-30 | 99 | -0.3068 | 0.572 | -0.5168 | 0.425 |
| 2026-10-01 | 123 | -0.6848 | 0.021 | -0.8948 | 0.014 |
| 2026-10-02 | 65 | +0.9683 | 4.934 | +0.7583 | 3.210 |

Interpretation: only one day is positive. The overall bootstrap CI is negative, and the result is not robust across days.

### 7.2 Symbol concentration

The dataset spans 54 symbols. Largest positive contributors include XRPUSDT, ARBUSDT, PENGUUSDT, WLDUSDT, and QNTUSDT. Largest negative contributors include DOGEUSDT, BNBUSDT, XAGUSDT, XLMUSDT, and SNDKUSDT.

Interpretation: the negative result is broad-based and not explained by one symbol.

### 7.3 Regime sensitivity

| Regime | N | Avg return % | PF |
|---|---:|---:|---:|
| HIGH_VOLATILITY | 93 | -0.3335 | 0.508 |
| RANGE | 268 | -0.2588 | 0.577 |
| TREND_DOWN | 12 | -0.1562 | 0.766 |
| TREND_UP | 43 | -0.2136 | 0.582 |

Interpretation: all represented regimes are negative. No regime subset supports the historical edge under frozen production semantics.

### 7.4 Subgroup check

No additional frozen subgroup acceptance threshold was defined in the registry for this experiment.

No frozen subgroup hypothesis justified a new execution counterfactual.

---

## 8. Why MFE Does Not Imply Edge

MFE/MAE are descriptive diagnostics only:

| Metric | N | Avg | Median |
|---|---:|---:|---:|
| MFE 60m | 416 | 1.0967% | 0.7070% |
| MAE 60m | 416 | 0.7087% | 0.3883% |
| MFE 240m | 416 | 1.8110% | 1.3819% |
| MAE 240m | 416 | 1.4830% | 0.9668% |

Positive MFE exists, but it does not convert into executable edge:

- SL first occurs in 61.8% of the sample;
- gross PF is 0.566;
- normal-cost PF is 0.401;
- bootstrap CIs are negative;
- all represented regimes are negative.

Therefore MFE cannot be used as evidence for this frozen hypothesis.

---

## 9. Why The Hypothesis Is Closed

The closure is based on the following evidence:

1. Prospective OOS expectancy is negative.
2. Prospective PF is materially below `1.0`.
3. The bootstrap CI is entirely below zero.
4. Normal costs worsen the result.
5. Elevated slippage worsens the result further.
6. All represented market regimes are negative.
7. The negative result is not explained by one symbol.
8. The negative result is not explained by one OOS day.
9. Positive MFE exists but does not convert to executable edge.
10. No frozen subgroup hypothesis justified a new execution counterfactual.

---

## 10. Final Research Verdict

```text
BREAKOUT_RETEST LONG RESEARCH STATUS = CLOSED_NEGATIVE

Evidence verdict:        EXPECTANCY_REJECTION_CONFIRMED
Prospective OOS:         DOES NOT REPLICATE historical edge
Gross PF:                0.566
Normal-cost PF:          0.401
Gross bootstrap CI:      [-0.3891%, -0.1370%]
Normal-cost bootstrap:   [-0.5991%, -0.3470%]
Stressed-cost PF:        0.203
```

The frozen BREAKOUT_RETEST LONG expectancy-rejection hypothesis is closed negatively.

---

## 11. Lifecycle Decision

| Lifecycle item | Decision |
|---|---|
| Current frozen hypothesis | `CLOSED_NEGATIVE` |
| Same-hypothesis further OOS | `NOT_REQUIRED` |
| Same-hypothesis re-optimization | `DO_NOT_PROCEED` |
| Retrospective subgroup mining | `DO_NOT_PROCEED` |
| Execution counterfactual on current OOS | `DO_NOT_CREATE` |
| Paper | `DO_NOT_PROCEED` |
| Live | `DO_NOT_PROCEED` |
| Scanner activation based on this hypothesis | `DO_NOT_PROCEED` |

---

## 12. Reopen Condition

Reopen condition:

```text
ONLY_NEW_INDEPENDENT_HYPOTHESIS
```

A BREAKOUT_RETEST LONG research branch may be reopened only if all of the following are true:

1. A new independent signal-time hypothesis is formulated.
2. The hypothesis is formulated before viewing a new prospective sample.
3. Parameters are frozen before prospective validation.
4. A new prospective validation protocol is created.
5. A separate experiment ID is used.
6. The current 416-row OOS sample is not reused for post-hoc selection of the new hypothesis.

The following are **not** considered a new hypothesis:

- selecting a different SL/TP on the same 416 observations;
- searching for the best symbol subset after viewing the result;
- searching for the best regime after viewing the result;
- excluding poor days after viewing the result;
- changing thresholds post hoc;
- optimizing on the current OOS sample.

---

## 13. Runtime / Research Lifecycle Reconciliation

### READ-ONLY runtime check at closure time

| Field | Current state |
|---|---|
| Check timestamp | `2026-10-02 10:25:24 UTC` |
| VPS Git HEAD | `b80262a6eaf8654e43c01d14431bea30954a25fb` |
| DB experiment status | `RUNNING` |
| Started at | `2026-09-28 16:43:14.854001+00` |
| Observations at recheck | 448 |
| Last observation at recheck | `2026-10-02 10:14:13.040499+00` |
| Outcome rows at recheck | 447 |
| Finalized outcomes at recheck | 418 |
| Evaluator active list | present |
| Evaluator recent activity | checked = 31–32 per cycle |

### Mismatch statement at closure time

```text
runtime/DB state != research lifecycle state
```

### Lifecycle reconciliation completed

The mismatch was subsequently resolved by a separate lifecycle reconciliation task:

- `BREAKOUT_RETEST_LONG_RESEARCH_LIFECYCLE_RECONCILIATION_TASK`

Reconciliation action taken:

| Field | Before reconciliation | After reconciliation |
|---|---|---|
| DB experiment status | `RUNNING` | `CANCELLED` |
| Observations | 449 | 449 (preserved) |
| Finalized outcomes | 419 | 419 (preserved) |
| Evaluator active list | included | excluded |
| New observations after reconciliation | — | 0 |

The minimal DB change was:

```sql
UPDATE research.prospective_experiment
SET status = 'CANCELLED',
    updated_at = NOW()
WHERE experiment_id = 'BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1'
  AND status = 'RUNNING';
```

Result: `UPDATE 1`.

All observations, outcomes, finalized outcomes, timestamps, source_signal_ids, and research artifacts were preserved. No rows were deleted. No scanner, gate, paper, live, registry, or service changes were made.

### Closure persistence

A subsequent persistence task ensured the closed experiment cannot be reactivated automatically by a future deploy/restart from `main`:

- `BREAKOUT_RETEST_LONG_CLOSURE_PERSISTENCE_FINALIZE_V1`
- `BREAKOUT_RETEST_LONG_CLOSURE_PERSISTENCE_GITFLOW_V1`

Persistence mechanism implemented in `app/research/prospective_observer.py`:

- DB terminal states `CANCELLED` / `COMPLETED` block prospective capture;
- registry terminal statuses `CANCELLED` / `COMPLETED` / `INACTIVE` are filtered;
- terminal decision is cached per process;
- lifecycle lookup failures remain fail-open;
- LR gate `filter_reason` persistence bug was corrected.

Validation:

- 5 focused persistence tests passed;
- 298 relevant regression tests passed;
- deployed to VPS as PR #116, merge commit `2e310b86e5d489a1087cee9508f0da8f0b459413`;
- post-deploy verification confirmed:
  - DB status = `CANCELLED`;
  - evaluator active list excludes the experiment;
  - observation count unchanged (460 before/after deploy);
  - 0 new observations after deploy;
  - scanner/paper/research-evaluator all active/running;
  - other prospective experiments unaffected.

---

## 14. Safety Statement

This closure task performed READ-ONLY evidence review and documentation.

### Not performed

- Scanner code changes
- Expectancy threshold changes
- Direction gate changes
- DB status changes in this task (the status change was performed by the separate reconciliation task)
- Production row DELETE/UPDATE
- Registry changes
- Service disablement
- Scanner/paper/evaluator restarts
- Paper/live changes
- New parameter search
- New BREAKOUT_RETEST scanner creation
- Post-hoc subgroup mining

### Created artifacts

- `docs/research/BREAKOUT_RETEST_LONG_RESEARCH_CLOSURE_V1_2026-10-02.md` — this record
- `audit_db/breakout_retest_long_research_closure_v1/lifecycle_check.txt` — READ-ONLY runtime check evidence
- `audit_db/breakout_retest_long_lifecycle_reconciliation/` — lifecycle reconciliation evidence
- `audit_db/breakout_retest_long_lifecycle_reconciliation_final/` — persistence validation evidence

---

## 15. Final Status

```text
BREAKOUT_RETEST LONG RESEARCH CLOSURE

Experiment:
BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1

Research status:
CLOSED_NEGATIVE

Evidence verdict:
EXPECTANCY_REJECTION_CONFIRMED

Gross:
N = 416
PF = 0.566
Avg = -0.2679%
95% CI = [-0.3891%, -0.1370%]
P(mean > 0) = 0.0001

Normal costs:
PF = 0.401
Avg = -0.4779%
95% CI = [-0.5991%, -0.3470%]
P(mean > 0) = 0.0000

Stressed costs:
PF = 0.203
Avg = -0.9779%
95% CI = [-1.0991%, -0.8470%]

Outcome structure:
SL first = 257 / 61.8%
TP first = 59 / 14.2%
Timeout = 100 / 24.0%

Historical baseline:
PF = 1.0976
Avg R after costs = +0.0037
WR = 16.07%

Same-hypothesis OOS:
NOT_REQUIRED

Re-optimization:
DO_NOT_PROCEED

Execution counterfactual:
DO_NOT_CREATE

Paper/live:
DO_NOT_PROCEED

Scanner activation:
DO_NOT_PROCEED

Reopen:
ONLY_NEW_INDEPENDENT_HYPOTHESIS

Runtime lifecycle:
DB status = CANCELLED
Evaluator active = NO
New observations after closure = 0
Historical data preserved = YES

Closure artifact:
docs/research/BREAKOUT_RETEST_LONG_RESEARCH_CLOSURE_V1_2026-10-02.md

Production changes:
NONE
```

---

*End of BREAKOUT_RETEST LONG Research Closure V1.*
