# VC_SHORT_EXECUTION_PROSPECTIVE_PROTOCOL_V1

**Status:** FROZEN PROTOCOL DRAFT — registration complete; collection begins only through the existing prospective framework.
**FREEZE_TS:** `2026-10-02T07:30:21Z`
**Git head:** `678166ae42f0c7ea879fa0f5136841720115b6b7`

## Purpose

Test whether the frozen `VC_SHORT_BB_WIDTH_V1` signal-selection filter becomes an executable prospective edge under pre-frozen execution geometry and project costs. Existing `VC_SHORT_BB_WIDTH_V1` observations/outcomes are not used to choose execution parameters.

## Frozen Source Hypothesis

| Field | Value |
|---|---|
| Scanner | `VOLATILITY_COMPRESSION` |
| Direction | `SHORT` |
| Frozen filter | `bb_width_percentile < 0.569723` |
| Source experiment | `VC_SHORT_BB_WIDTH_V1` |
| New experiment | `VC_SHORT_EXECUTION_V1` |

## Frozen Population

Only `VOLATILITY_COMPRESSION` SHORT observations passing the frozen BB-width filter may be captured. Entry uses `reference_price`; invalidation must be above entry; target_1 must be below entry. Duplicate identity is `(experiment_id, source_signal_id)`. Invalid geometry is rejected.

Only signals with `signal_time > FREEZE_TS` may contribute to primary validation. Rows before that timestamp are `PRE_FREEZE / CONTAMINATED_FOR_VALIDATION`; backfill is forbidden.

## Frozen Execution Geometry

- `ENTRY_PRICE = reference_price`
- `ENTRY_TRIGGER = immediate at signal time`
- `structural_R = abs(entry_price - invalidation_price)`
- `SL_R = 1.0`, `stop_price = invalidation_price`
- `TP = target_1`, requiring `target_1 < reference_price`
- `MAX_HOLD_MINUTES = 120`
- timeout exits at the first 5m candle close at or after the 120-minute mark
- `PRIMARY_INTRABAR_POLICY = STOP_FIRST`

## Frozen Cost Model

| Scenario | Taker fee/side | Slippage/side |
|---|---:|---:|
| FEE_ONLY | 0.055% | 0% |
| NORMAL_COST | 0.055% | 0.05% |
| ELEVATED_SLIPPAGE | 0.055% | 0.10% |

Primary verdict uses `NORMAL_COST`.

## Frozen Evaluation

Primary metric: `Net E[R] after normal project costs`.

Before the frozen checkpoint, report only operational counters: raw post-freeze signals, eligible/finalized counts, symbols, days, funnel, and pipeline health. After the checkpoint, report Gross E[R], Net E[R], Gross PF, Net PF, win rate, TP/SL/timeout/ambiguous counts, total Net R, max losing streak, bootstrap 95% CI, and `P(Net E[R] > 0)`.

Bootstrap resamples: `10,000`.

Minimum evidence: `MIN_ELIGIBLE_FINALIZED_N = 50`, `MIN_OOS_DAYS = 7`, `MIN_DISTINCT_SYMBOLS = 10`. Results must not depend on one day, one symbol, or one regime bucket.

## Verdict Taxonomy

- `EXECUTABLE_EDGE_CONFIRMED`
- `NO_EXECUTABLE_EDGE`
- `INCONCLUSIVE_CONTINUE_OOS`
- `INVALIDATED_BY_DATA_QUALITY`
- `PIPELINE_FAILURE`

No automatic paper or live promotion follows any verdict.

## Anti-Optimization Statement

Historical outcome parameter selection: **NO**
Historical SL/TP grid: **NO**
Historical regime selection: **NO**
Historical backfill: **NO**
