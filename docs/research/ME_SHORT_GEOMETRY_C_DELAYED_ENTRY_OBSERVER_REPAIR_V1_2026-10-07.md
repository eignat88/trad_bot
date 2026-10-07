# ME_SHORT_GEOMETRY_C_DELAYED_ENTRY_OBSERVER_REPAIR_V1

Date: 2026-10-07

## Scope

Repair observer-side semantics for `ME_SHORT_GEOM_C_V1`.

No historical outcomes are rewritten.
No pre-fix C observations are modified.
No evaluator semantics are changed.
A/B geometry semantics remain unchanged.

## Root cause

`MOMENTUM_EXHAUSTION` already knew the authoritative trigger price as:

`candles_5m[-1].close`

but that price was not persisted into candidate metadata used by the prospective observer.

Historical C observer logic used:

`features.get("entry_price", reference_price)`

and silently fell back to `reference_price` when `entry_price` was absent.

As a result, C could lose its delayed-entry intervention semantics and collapse to the structural reference entry.

## Repair

The original MOMENTUM_EXHAUSTION SHORT scanner now persists:

`features["trigger_5m_close"] = current_price`

where:

`current_price = candles_5m[-1].close`

The metadata is added in `_scan_short()` after quality-feature construction, preserving the existing quality-feature builder contract.

`ME_SHORT_GEOM_C_V1` now requires `trigger_5m_close`.

If the value is missing, invalid, or non-positive, C fails closed and no C observation is created.

There is no fallback to `reference_price`.

## Frozen C geometry

For SHORT:

- variant_entry = trigger_5m_close
- base_risk_distance = invalidation_price - reference_price
- base_target_distance = reference_price - target_1
- variant_stop = variant_entry + base_risk_distance
- variant_target = variant_entry - base_target_distance

The original distances are preserved around the delayed entry.

## Historical policy

- pre-fix C observations rewritten: NO
- production outcomes recomputed: NO
- pre-fix C cohort valid for final C validation: NO
- clean post-fix cohort must use a deployment boundary: YES

The post-fix clean boundary will be the actual scanner deployment/restart timestamp.

Eligibility:

`signal_time > C_OBSERVER_FIX_DEPLOY_TS`

## Tests

Dedicated repair tests:
- authoritative trigger 5m close is persisted by original ME SHORT scanner
- quality-feature builder contract remains unchanged
- C uses trigger_5m_close as variant_entry
- risk distance preserved
- target distance preserved
- missing trigger close fails closed
- legacy entry_price does not silently substitute for trigger_5m_close
- A/B routing remains intact
- evaluator complete variant tuple regression remains intact

Results:

- dedicated repair tests: PASS
- MOMENTUM_EXHAUSTION_R regression: 19 passed
- bounded observer/evaluator suite: 115 passed

## Repair verdict

REPAIR_SAFE_TO_DEPLOY

## Current lifecycle status

`ME_SHORT_GEOM_C_V1` is NOT validated for executable edge.

After deployment it must enter a new clean post-fix holdout cohort.

Expected status after deployment:

`PIPELINE_REPAIRED_BUT_VALIDATION_NOT_COMPLETE`

Natural post-deploy C observation proof is still required.
