# FVG Expectancy Rejection OOS Review V1 — 2026-10-02

**READ-ONLY prospective OOS review**  
**Experiment:** `FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1`  
**Scanner:** `FVG_REACTION_LONG_LOCAL_STRUCT_V1`  
**Direction:** `LONG`

---

## CUTOFF

| Field | Value |
|---|---|
| analysis_cutoff_utc | 2026-10-02T13:38:51+00:00 |
| git_head (VPS) | 41685a21a363aec6e84affaeb3a9cb5cf98e116f |
| experiment_id | FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1 |
| first_signal | 2026-09-28 17:01:04.654369+00 |
| last_signal | 2026-10-02 13:36:54.368974+00 |
| raw_N | 323 |
| outcomes_N | 322 |
| finalized_N (at 240m) | 306 |
| symbols_N | 62 |
| OOS_days | 5 |
| formal minimum N | 50 |
| formal minimum symbols | 10 |

Dataset frozen at cutoff. All subsequent calculations use only observations with `signal_time <= cutoff`.

---

## RESEARCH LINEAGE

```text
historical observation
→ FVG_GENERIC_V1 (generic research experiment, FVG_REACTION_LONG_LOCAL_STRUCT_V1, LONG, 2026-09-25)
→ hypothesis: FVG_REACTION_LONG has negative historical expectancy (avg_r = -0.42)
→ historical result: avg_r_after_costs = -0.42, PF = 0.4593, WR = 0.1667, N = 78
→ reason for prospective rejection test:
   "check if there's a subset with positive edge within the bad overall sample"
→ prospective experiment: FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1
   (registered 2026-09-28 16:38:49, started 2026-09-28 16:43:14)
```

### Frozen hypothesis (from registry + adapter)

```text
hypothesis: "FVG_REACTION_LONG has negative historical expectancy (avg_r = -0.42).
             Prospective capture to check if there's a subset with positive edge
             within the bad overall sample."

frozen_parameters:
  min_fvg_atr: 0.05
  min_c2_body_ratio: 0.50
  max_bars_to_touch: 48
  sl_buffer_atr: 0.05
  target_r: 3.0
  expectancy_threshold: 0.0

filter_rule: "CAPTURE: candidates that pass risk_geometry, score_gate, direction_gate,
              but REJECTED by expectancy_filter (negative performance)"

entry_rule: "Existing production entry semantics"
stop_rule: "Existing production invalidation_price"
target_rule: "Existing production target_1"
```

### Historical baseline (authoritative, from registry `historical_baseline`)

| Field | Value |
|---|---|
| samples | 78 |
| entries | 78 |
| avg_r_after_costs | -0.42 |
| profit_factor | 0.4593 |
| win_rate | 0.1667 |

The prospective experiment was created to test whether the negative historical picture persists prospectively or whether a subset exists with positive edge.

---

## DATA

### Observation integrity

| Check | Result |
|---|---|
| raw_n | 323 |
| duplicate_observation_ids | 0 |
| non_long_rows | 1 |
| null_reference_price | 1 |
| null_invalidation_price | 1 |
| null_target_1 | 1 |
| null_features_json | 1 |
| observations_after_cutoff | 0 |
| duplicate_symbol_signal_time | 0 |
| invalid_stop_distance | 0 |
| invalid_target_above_entry | 0 |

**Classification:** `PASS_WITH_MINOR_GEOMETRY_EXCLUSIONS`

### Outcome integrity

| Check | Result |
|---|---|
| observations_without_outcome | 5 |
| finalized_at_240m | 306 |
| immature_at_240m | 16 |
| excluded_invalid_geometry | 1 |

### Maturity by horizon

| Horizon | Available | Finalized | Missing |
|---|---:|---:|---:|
| 15m | 318 | — | 5 |
| 30m | 315 | — | 8 |
| 60m | 314 | — | 9 |
| 120m | 310 | — | 13 |
| 240m | 306 | 306 | 17 |

### Eligible population

| Metric | Value |
|---|---|
| RAW N | 323 |
| ELIGIBLE N (finalized @240m, valid LONG geometry) | 306 |
| EXCLUDED N | 17 |
| FINALIZED ELIGIBLE N | 306 |

Exclusion reasons: `not_finalized_at_240m` = 16, `invalid_geometry` = 1.

---

## PROSPECTIVE RESULT

### Exit distribution (frozen geometry: TP = target_1, SL = invalidation_price, RR = 3.0)

| Exit type | N | Share |
|---|---:|---:|
| TP first | 46 | 15.0% |
| SL first | 173 | 56.9% |
| TIMEOUT (neither) | 87 | 28.4% |

### Gross (no costs)

| Metric | Value |
|---|---|
| N | 306 |
| WR | 0.1503 |
| Gross E[R] | -0.1144 |
| Gross PF | 0.7977 |
| Gross total R | -35.0 |
| Median R | -1.0 |
| Median MFE (240m) | 1.5077 |
| Median MAE (240m) | 1.2515 |

### Normal costs (0.21% round-trip)

| Metric | Value |
|---|---|
| WR | 0.1503 |
| Net E[R] | **-0.5344** |
| Net PF | **0.4122** |
| Net total R | -163.53 |
| Median Net R | -1.0856 |

### Stressed costs (0.31% round-trip)

| Metric | Value |
|---|---|
| WR | 0.1438 |
| Net E[R] | -0.7344 |
| Net PF | 0.3163 |
| Net total R | -224.74 |

### Bootstrap uncertainty (seed=42, 10000 resamples, iid bootstrap)

| Metric | Value |
|---|---|
| point estimate Net E[R] | -0.5344 |
| bootstrap mean | -0.5337 |
| bootstrap median | -0.5346 |
| 95% CI | [-0.6932, -0.3662] |
| P(E[R] > 0) | 0.0000 |

---

## STABILITY

### Day concentration

| Day | N | WR | Gross E[R] |
|---|---:|---:|---:|
| 2026-09-28 | 12 | 0.000 | -0.833 |
| 2026-09-29 | 95 | 0.211 | +0.158 |
| 2026-09-30 | 71 | 0.099 | -0.380 |
| 2026-10-01 | 79 | 0.089 | -0.443 |
| 2026-10-02 | 49 | 0.245 | +0.449 |

Largest day share by N: 31.0% (2026-09-29).

### Leave-one-day-out

| Excluded day | N | Gross E[R] | Gross PF | Net E[R] | Net PF |
|---|---:|---:|---:|---:|---:|
| 2026-09-28 | 294 | -0.085 | 0.847 | -0.496 | 0.440 |
| 2026-09-29 | 211 | -0.237 | 0.609 | -0.668 | 0.317 |
| 2026-09-30 | 235 | -0.034 | 0.936 | -0.456 | 0.473 |
| 2026-10-01 | 227 | -0.000 | 1.000 | -0.407 | 0.514 |
| 2026-10-02 | 257 | -0.222 | 0.642 | -0.653 | 0.336 |

**Conclusion:** Net E[R] remains negative under every leave-one-day-out exclusion. The negative conclusion does NOT depend on a single day.

### Symbol concentration

| Metric | Value |
|---|---|
| Top-1 symbol share by N | 3.9% |
| Top-3 symbol share by N | 10.5% |
| Top-5 symbol share by N | 16.7% |

Excluding top-1 symbol: Net E[R] = -0.528, Net PF = 0.419.
Excluding top-3 symbols: Net E[R] = -0.469, Net PF = 0.455.

**Conclusion:** Broad symbol dispersion; result is not driven by any small symbol group.

### Regime sensitivity

| Regime | N | WR | Gross E[R] | Gross PF | Net E[R] | Net PF |
|---|---:|---:|---:|---:|---:|---:|
| HIGH_VOLATILITY | 92 | 0.109 | -0.228 | 0.588 | -0.361 | 0.463 |
| RANGE | 147 | 0.143 | -0.122 | 0.778 | -0.559 | 0.393 |
| TREND_DOWN | 38 | 0.211 | +0.026 | 1.043 | -0.943 | 0.326 |
| TREND_UP | 29 | 0.241 | +0.103 | 1.167 | -0.426 | 0.558 |

**Conclusion:** No regime produces positive Net E[R]. TREND_UP is least negative gross but costs destroy it.

---

## FVG-SPECIFIC FEATURES

Pre-specified features tested (present in signal-time observation, part of frozen hypothesis/protocol):

| Feature | Median | Lo N | Lo Net E[R] | Hi N | Hi Net E[R] | Separation |
|---|---:|---:|---:|---:|---:|---|
| fvg_atr | 0.553 | 153 | -0.157 | 153 | -0.072 | None |
| c2_body_ratio | 0.772 | 153 | -0.020 | 153 | -0.209 | None |
| bars_to_touch | 1.0 | 273 | -0.117 | 33 | -0.091 | None |

**Result:** None of the pre-specified FVG features show stable separation between winners and losers.

Post-hoc mining performed: **NO**

---

## HISTORICAL RECONCILIATION

| Metric | Historical | Prospective | Change |
|---|---:|---:|---:|
| N | 78 | 306 | +228 |
| WR | 0.167 | 0.150 | -0.017 |
| Net E[R] | -0.42 | -0.534 | -0.114 |
| PF | 0.459 | 0.412 | -0.047 |
| Median MFE | — | 1.508 | — |
| Median MAE | — | 1.251 | — |

Classification: **UNCHANGED_NEGATIVE** — the negative historical picture is confirmed prospectively with a 4x larger sample.

---

## SUBSET

| Question | Answer |
|---|---|
| Pre-specified viable subset found? | NO |
| Exploratory-only subset found? | NO |
| Frozen confirmation required? | NO |

---

## EXECUTION COUNTERFACTUAL

**NOT_JUSTIFIED** — the prospective evidence does not show a viable pre-specified subset. There is no positive-expectancy candidate to test alternative execution on.

---

## VERDICT

```text
NEGATIVE_BASELINE_CONFIRMED
```

### Closure recommendation

```text
Research status = CLOSED_NEGATIVE
Further same-hypothesis OOS = NOT_REQUIRED
Same-hypothesis optimization = DO_NOT_PROCEED
Retrospective subgroup mining = DO_NOT_PROCEED
Paper/live = DO_NOT_PROCEED
Reopen only with NEW_INDEPENDENT_HYPOTHESIS
```

Note: DB lifecycle change NOT performed in this task. A separate lifecycle cleanup task is required to set `prospective_experiment.status = 'CANCELLED'` and/or `prospective_experiment_direction_state.status = 'CLOSED_NEGATIVE'`.

---

## PRODUCTION CHANGES

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
Git HEAD: 41685a21a363aec6e84affaeb3a9cb5cf98e116f
UTC cutoff: 2026-10-02T13:38:51+00:00
DB source tables: research.prospective_observation, research.prospective_outcome
Historical artifact: app/research/prospective_registry.json (historical_baseline)
Analysis script: tools/research/fvg_expectancy_rejection_oos_review_v1.py
Command: python tools/research/fvg_expectancy_rejection_oos_review_v1.py --dataset audit_db/fvg_oos_review_export/fvg_dataset.tsv --outdir docs/research
Random seed: 42
Raw N: 323
Eligible N: 306
Excluded N: 17
```

Repeat run on same cutoff produces identical results (deterministic).

---

*End of FVG Expectancy Rejection OOS Review V1.*
