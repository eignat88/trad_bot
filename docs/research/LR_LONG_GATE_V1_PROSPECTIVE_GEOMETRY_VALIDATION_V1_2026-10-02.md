# LR_LONG_GATE_V1 Prospective Geometry Validation V1 — 2026-10-02

**READ-ONLY prospective validation**  
**Experiment:** `LR_LONG_GATE_V1`  
**Scanner:** `LIQUIDITY_REVERSAL`  
**Direction:** `LONG`  
**Framework:** prospective  
**Protocol artifact:** `docs/research/LR_LONG_GEOMETRY_PROSPECTIVE_VALIDATION_V1_PROTOCOL_2026-10-01.md`

---

## 0. CRITICAL DISTINCTION

`LR_LONG_GATE_V1` is a **separate frozen prospective holdout hypothesis**. It is NOT `LR_GENERIC_V1 LONG` (already `CLOSED_NEGATIVE` per `docs/research/LR_LONG_RESEARCH_CLOSURE_V1_2026-10-01.md`). This validation does NOT reopen `LR_GENERIC_V1 LONG`. Datasets are not mixed.

---

## FROZEN PROTOCOL SNAPSHOT

| Field | Value |
|---|---|
| FREEZE_TS | 2026-10-01 13:48:27 UTC |
| Git SHA (at freeze) | 85cc87a21399c34424cc3ca5bbd513961c665f68 |
| Frozen experiment ID | LR_LONG_GEOMETRY_PROSPECTIVE_VALIDATION_V1 |
| Eligibility rule | post-freeze observations with risk_gate: abs(entry - invalidation_price) / entry >= 0.3% |
| Entry rule | reference_price / swept_level (production semantics) |
| Geometry rule | SL=0.50R, TP=3.0R |
| SL | 0.50R |
| TP | 3.0R |
| Timeout | 120 minutes |
| Fee assumptions | taker 0.055% per side |
| Slippage assumptions | 0.05% per side (normal); 0.10% per side (elevated sensitivity) |
| Ambiguity handling | STOP_FIRST (conservative) |
| Minimum N | 50 (post-freeze, gate-passed) |
| Minimum symbols | (not explicitly stated in protocol checkpoint; registry minimum_symbols=5) |
| Minimum OOS days | 7 |
| Completion criteria | eligible N >= 50 AND distinct UTC OOS days >= 7 |
| Predefined subgroup rules | none |

No conflicts found between discovery and protocol artifacts.

---

## DATASET

| Metric | Value |
|---|---|
| Audit cutoff | 2026-10-02 14:53:31 UTC |
| Git HEAD | 7bc1cd9e0a16ed6aab92cdd9fba094390401bc59 |
| Raw N | 51 |
| Pre-freeze N | 33 |
| Post-freeze N | 18 |
| Post-freeze first signal | 2026-10-01 15:23:56 UTC |
| Post-freeze last signal | 2026-10-02 14:51:50 UTC |
| Gate-pass N | 13 |
| Gate-fail N | 5 |
| Eligible N (gate-passed + finalized) | 9 |
| Finalized N | 9 |
| Gate-pass not finalized | 4 |
| Symbols | 7 |
| OOS days | 2 |

### Integrity

| Check | Result |
|---|---|
| duplicate observation IDs | 0 |
| duplicate symbol+signal_time | 0 |
| non-LONG rows | 1 |
| null reference price | 1 |
| null invalidation price | 1 |
| zero/negative risk distance | 0 |
| observations after cutoff | 0 |
| pre-freeze inserted after freeze | 0 |
| post-freeze sourced from pre-freeze | 0 |

**Classification:** `PASS`

---

## CHECKPOINT

Frozen protocol checkpoint: `eligible N >= 50 AND OOS days >= 7`

| Metric | Current | Required | Met |
|---|---:|---:|---|
| Eligible N | 9 | >= 50 | NO |
| OOS days | 2 | >= 7 | NO |

**Checkpoint NOT reached.** Per protocol Section 6, performance metrics (Net E[R], PF, WR) are FORBIDDEN before checkpoint. They are computed below as **interim diagnostic only** and must NOT be used for validation verdict.

---

## INTERIM DIAGNOSTIC (NOT FOR VALIDATION VERDICT)

### Geometry pass/fail (post-freeze, frozen risk gate 0.3%)

| Metric | Value |
|---|---:|
| Post-freeze N | 18 |
| Gate PASS N | 13 |
| Gate FAIL N | 5 |
| PASS rate | 72.2% |

### Execution simulation (frozen SL=0.50R TP=3.0R timeout=120m STOP_FIRST)

| Metric | Gross | Net (normal 0.21%) | Net (elevated 0.31%) |
|---|---:|---:|---:|
| N | 9 | 9 | 9 |
| TP_FIRST | 3 | 3 | 3 |
| SL_FIRST | 6 | 6 | 6 |
| TIMEOUT | 0 | 0 | 0 |
| AMBIGUOUS | 0 | 0 | 0 |
| WR | 33.3% | 33.3% | 33.3% |
| E[R] | +0.284 | **-0.026** | -0.173 |
| PF | 1.426 | **0.971** | 0.824 |
| Total R | +2.558 | -0.230 | -1.557 |
| Median R | -1.000 | -1.297 | -1.439 |
| Max losing streak | 4 | 4 | 4 |

### Bootstrap (seed=42, 10000 resamples)

| Metric | Value |
|---|---|
| Point Net E[R] | -0.026 |
| Bootstrap median | -0.028 |
| 95% CI | [-0.935, +1.273] |
| P(E[R] > 0) | 0.408 |

### Day concentration

| Day | N | Net E[R] | PF | Net R |
|---|---:|---:|---:|---:|
| 2026-10-01 | 6 | -0.694 | 0.363 | -4.166 |
| 2026-10-02 | 3 | +1.312 | 3.868 | +3.936 |

Largest day share by N: 66.7% (2026-10-01).

Leave-one-day-out:
- Exclude 2026-10-01: N=3, Net E[R]=+1.312, PF=3.868
- Exclude 2026-10-02: N=6, Net E[R]=-0.694, PF=0.363

### Symbol concentration

| Symbol | N | Net E[R] | Net R |
|---|---:|---:|---:|
| ENAUSDT | 3 | +1.344 | +4.031 |
| PUMPFUNUSDT | 1 | -1.301 | -1.301 |
| SUIUSDT | 1 | +2.375 | +2.375 |
| ARBUSDT | 1 | -1.316 | -1.316 |
| ZECUSDT | 1 | -1.297 | -1.297 |
| STXUSDT | 1 | -1.348 | -1.348 |
| ARKUSDT | 1 | -1.372 | -1.372 |

Exclude top-1 (ENAUSDT): N=6, Net E[R]=-0.710, PF=0.358
Exclude top-3: N=4, Net E[R]=-1.334, PF=0.0

---

## DISCOVERY VS HOLDOUT (comparison only, no combined estimate)

| Metric | Discovery (risk-gated) | Prospective Holdout (interim) |
|---|---:|---:|
| N | 131 | 9 |
| Symbols | 50 | 7 |
| PASS rate | — | 72.2% |
| Net E[R] | +0.302 | -0.026 |
| PF | 1.81 | 0.971 |
| WR | — | 33.3% |

Classification: **TOO_EARLY_TO_COMPARE** — holdout checkpoint not reached (9/50 N, 2/7 days).

---

## VERDICT

```text
INCONCLUSIVE_NEEDS_MORE_OOS
```

Reason: Collection minimum NOT reached (eligible N=9 vs required 50; OOS days=2 vs required 7). Frozen protocol checkpoint criteria not satisfied. Performance metrics are interim diagnostics only. Sample too small and temporally concentrated for validation verdict.

---

## NEXT ACTION

```text
CONTINUE_FROZEN_OOS_UNCHANGED
```

Waiting for:
- more OOS days (need >= 7 distinct UTC days, currently 2);
- more finalized gate-passed observations (need >= 50, currently 9);
- lower day concentration (currently 66.7% on one day);
- lower symbol concentration (currently ENAUSDT dominates).

Frozen parameters remain unchanged: SL=0.50R, TP=3.0R, timeout=120m, risk_gate=0.3%.

---

## LIFECYCLE RECONCILIATION

```text
LR_GENERIC_V1 LONG:
  CLOSED_NEGATIVE
  UNCHANGED

LR_LONG_GATE_V1:
  INCONCLUSIVE_NEEDS_MORE_OOS (validation in progress)

Relationship:
  SEPARATE HYPOTHESES / SEPARATE DATASETS
```

---

## PRODUCTION / RUNTIME CHANGES

| Area | Changed |
|---|---|
| DB | NO |
| Scanner | NO |
| Config | NO |
| Paper/live | NO |
| Services | NO |

---

## REPRODUCIBILITY

```text
Git HEAD: 7bc1cd9e0a16ed6aab92cdd9fba094390401bc59
Audit cutoff: 2026-10-02 14:53:31 UTC
Freeze timestamp: 2026-10-01 13:48:27 UTC
DB source: research.prospective_observation, research.prospective_outcome
Protocol artifact: docs/research/LR_LONG_GEOMETRY_PROSPECTIVE_VALIDATION_V1_PROTOCOL_2026-10-01.md
Analysis script: tools/research/lr_long_gate_v1_prospective_geometry_validation_v1.py
Command: python tools/research/lr_long_gate_v1_prospective_geometry_validation_v1.py --dataset audit_db/lr_long_gate_v1_validation_export/dataset.tsv --outdir docs/research
Random seed: 42
Bootstrap iterations: 10000
Raw N: 51
Post-freeze N: 18
Eligible N: 9
```

---

*End of LR_LONG_GATE_V1 Prospective Geometry Validation V1.*
