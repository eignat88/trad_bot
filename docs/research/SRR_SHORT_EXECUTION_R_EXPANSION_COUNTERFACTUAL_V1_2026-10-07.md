# SRR_SHORT_EXECUTION_R_EXPANSION_COUNTERFACTUAL_V1

**Status:** DISCOVERY CLOSURE — READ-ONLY RESEARCH

**Parent closed experiment:** `SRR_OOS_SCANNER_V1_PROSPECTIVE / SHORT / CLOSED_NEGATIVE`

**Parent discovery:** `SRR_SHORT_EXECUTION_R_EXPANSION_COUNTERFACTUAL_V1`

**Selected prospective candidate:** `SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1`

**Evidence-completeness limitation:** The full candle-level counterfactual replay artifacts for this discovery are not present in the current checkout. The exact discovery values below are treated as frozen task inputs for implementation. The agent did not independently reproduce the 2.00/B result locally, and no missing replay evidence is invented or derived from the old frozen/discovery CSV.

---

## 1. Purpose

The previous frozen SRR SHORT protocol was closed negative under its historical reassessment methodology. This discovery tested whether widening the execution-R geometry could produce a positive prospective candidate under a single consistent execution-R normalization.

This is **discovery evidence only**. It is not prospective validation evidence and not permission for paper or live execution.

## 2. Frozen discovery population

| Item | Value |
|---|---:|
| Frozen observation IDs | 688 |
| Symbols | 68 |
| OOS days | 7 |
| 5m candle path coverage | 688 / 688 |
| Historical source | Bybit public historical 5m klines |
| Treatment | DISCOVERY ONLY |

The historical 688 observations are forbidden for prospective validation. They must never be inserted into the new prospective experiment, recalculated into it, backfilled by migration, or captured by the observer.

## 3. Historical reassessment defects

### 3.1 BOTH_HIT / AMBIGUOUS classification defect

The previous frozen reassessment classified all 133 both-hit paths as `AMBIGUOUS`.

The frozen 688-observation DB outcome flags were:

| Class | Count |
|---|---:|
| `TP_FIRST` | 218 |
| `SL_FIRST` | 157 |
| both hit, TP-before-SL, non-ambiguous | 48 |
| both hit, SL-before-TP, non-ambiguous | 48 |
| both hit, TP-before-SL, true ambiguous | 26 |
| both hit, SL-before-TP, true ambiguous | 11 |
| true `ambiguous_intrabar` total | 37 |
| `TIMEOUT` | 180 |
| both-hit total | 133 |

Therefore:

`133 both-hit paths = 48 ordered TP-first + 48 ordered SL-first + 37 true same-candle ambiguous`

The evaluator's actual first-hit semantics are candle-by-candle:

```text
SHORT:
  TP hit now if candle.low <= target
  SL hit now if candle.high >= stop
  same candle:
      ambiguous_intrabar = true
      under STOP_FIRST -> stop wins
  otherwise:
      first executable hit determines order
```

Once a trade exits, later candles must not change the execution result. The forbidden shortcut is:

`if tp_hit AND sl_hit -> AMBIGUOUS`

That shortcut is not used by the new prospective analysis.

### 3.2 Mixed-R normalization defect

The previous reassessment mixed structural-R units with execution-R units.

Barrier outcomes were represented in structural-R units:

| Outcome | Representation |
|---|---:|
| TP | `+1.50 structural R` |
| SL | `-0.75 structural R` |
| AMBIGUOUS under STOP_FIRST | `-0.75 structural R` |

But TIMEOUT `derived_gross_r` was normalized by:

`execution_R = 0.75 * structural_R`

This was verified exactly for all TIMEOUT trades:

| Check | Result |
|---|---:|
| TIMEOUT N | 180 |
| MATCH <= 1e-9 | 180 |
| MISMATCH | 0 |
| MAX ABS ERROR | approximately `2.22e-16` |

Therefore the previous derived metrics mixed structural-R units for TP/SL with execution-R units for TIMEOUT. They are not authoritative for execution-R comparison.

## 4. Discovery grid

For every candidate:

`execution_R = stop_multiplier * structural_R`

`structural_R = abs(reference_price - invalidation_price)`

Stop multipliers tested:

- `0.75`
- `1.00`
- `1.25`
- `1.50`
- `2.00`

Target schemes tested:

### Scheme A — `A_PRESERVE_RR`

`target distance = 2 * execution_R`

### Scheme B — `B_PRESERVE_ABSOLUTE_TP`

`target distance = 1.50 * structural_R`

Other frozen discovery settings:

| Setting | Value |
|---|---|
| Primary hold | 120 minutes |
| Same-candle collision | STOP_FIRST |
| Normal round-trip cost | 0.21% |
| Elevated round-trip cost | 0.31% |
| R normalization | consistent execution_R |
| Timeout | last eligible 5m candle close before 120m cutoff |

## 5. Selected candidate

| Field | Frozen value |
|---|---|
| Stop multiplier | `2.00` |
| Target scheme | `B_PRESERVE_ABSOLUTE_TP` |
| Direction | SHORT |
| Entry | `reference_price` |
| `structural_R` | `abs(reference_price - invalidation_price)` |
| SL | `entry + 2.00 * structural_R` |
| TP | `entry - 1.50 * structural_R` |
| `execution_R` | `2.00 * structural_R` |
| Target in execution-R terms | `+0.75R` |
| Stop in execution-R terms | `-1.00R` |
| MAX_HOLD | 120 minutes |
| Intrabar policy | STOP_FIRST |
| Normal cost | 0.21% round trip |
| Normal cost_R | approximately `0.21` execution R in the discovery population |
| Elevated cost | 0.31% round trip |
| Elevated cost_R | approximately `0.31` execution R in the discovery population |

No additional filter, cost_R eligibility threshold, retuning, symbol exclusion, regime filter, timeout optimization, or score threshold is part of the frozen prospective candidate.

## 6. Discovery result

For stop multiplier `2.00` and Scheme B:

| Metric | Discovery value |
|---|---:|
| N | 688 |
| Symbols | 68 |
| Days | 7 |
| TP_FIRST | 339 |
| SL_FIRST | 76 |
| AMBIGUOUS | 0 |
| TIMEOUT | 273 |

Normal-cost discovery:

| Metric | Value |
|---|---:|
| Net E[R] | `+0.0630306021` |
| Net PF | `1.2817698292` |
| Total Net R | `+43.3651` |
| Win rate | `0.61919` |
| 95% bootstrap CI | `[+0.01750, +0.10669]` |
| P(mean > 0) | `0.9974` |
| Gross E[R] | approximately `+0.2730` |
| Median normal cost_R | `0.2100` |

## 7. Day robustness

Leave-one-day-out normal-cost results remained positive for all `7/7` days.

Approximate minimum:

| Metric | Value |
|---|---:|
| Net E[R] | `+0.0415` |
| Net PF | `1.176` |

Specific LODO results:

| Excluded day | Net E[R] | PF |
|---|---:|---:|
| 2026-09-30 | `+0.06243` | `1.2791` |
| 2026-10-01 | `+0.04704` | `1.2026` |
| 2026-10-02 | `+0.04154` | `1.1763` |
| 2026-10-03 | `+0.05573` | `1.2406` |
| 2026-10-04 | `+0.08650` | `1.4007` |
| 2026-10-05 | `+0.09118` | `1.4515` |
| 2026-10-06 | `+0.05755` | `1.2565` |

## 8. Symbol concentration robustness

| Subset | N | Net E[R] | PF | Total Net R |
|---|---:|---:|---:|---:|
| Base | 688 | `+0.06303` | `1.2818` | `+43.37` |
| Exclude top-1 positive symbol | 677 | `+0.05528` | `1.2432` | `+37.43` |
| Exclude top-3 positive symbols | 643 | `+0.04229` | `1.1801` | `+27.19` |
| Exclude top-5 positive symbols | 604 | `+0.03301` | `1.1376` | `+19.94` |

Discovery is therefore not dependent on one or a few winning symbols. These symbol exclusions are diagnostics only and are not encoded into the prospective experiment.

## 9. Elevated-cost warning

The candidate is not robust to elevated cost assumptions.

At `0.31%` round trip:

| Metric | Value |
|---|---:|
| Net E[R] | `-0.03697` |
| Net PF | `0.85999` |
| Total Net R | `-25.43` |
| 95% CI | `[-0.08250, +0.00669]` |
| P(mean > 0) | `0.0516` |

Therefore prospective reporting must record:

- normal-cost metrics as primary;
- elevated-cost sensitivity as a mandatory secondary result;
- no claim of cost robustness;
- no paper/live activation even if early normal-cost results look positive.

## 10. Discovery verdict

```text
DISCOVERY VERDICT = POSITIVE_PROSPECTIVE_CANDIDATE

NOT VALIDATED
NOT PAPER/LIVE READY
REQUIRES STRICTLY NEW OOS
```

The selected candidate is frozen for prospective validation only. No additional tuning, filtering, backfill, or production execution is authorized by this discovery artifact.

---

## 11. Evidence and implementation scope

Authoritative local context available in this checkout includes:

- `docs/research/srr_short_frozen_protocol_reassessment_v1_results.json`
- `docs/research/srr_short_execution_geometry_discovery_v1_results.json`
- `docs/research/srr_short_execution_r_expansion_counterfactual_v1_phase0.json`
- `tools/research/srr_short_frozen_protocol_reassessment_v1.py`
- `tools/research/srr_short_execution_geometry_discovery_v1.py`
- `tools/research/srr_short_execution_r_expansion_counterfactual_v1_phase0.py`

The full selected-candidate replay script and complete candle-path output are not present in this checkout. The closure artifact therefore records the task-supplied frozen values and explicitly does not claim local independent reproduction.
