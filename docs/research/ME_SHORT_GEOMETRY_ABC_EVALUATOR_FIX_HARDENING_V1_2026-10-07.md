# ME SHORT A/B/C Evaluator Fix Hardening V1

Date: 2026-10-07

## Audit State

```text
AUDIT_TS = 2026-10-07T12:36:39Z
GIT_HEAD = 0d98b56aeab083ab97d19698b4b24d2f52e201e2
BRANCH = main
WORKTREE = existing evaluator/test diff plus unrelated research artifacts
```

The local evaluator diff was preserved before hardening in `audit_db/me_short_geometry_abc_evaluator_fix_hardening_v1_20261007/evaluator_diff_before.txt`.

## Variant Tuple Policy

```text
complete tuple behavior = use variant_entry, variant_stop, variant_target atomically
partial tuple behavior = deterministically fall back to the complete base tuple
base fallback behavior = reference_price, invalidation_price, target_1
```

The current local fix implements this policy. It does not create hybrid geometry for incomplete tuples.

## DB-Wide Blast Radius

The following inventory was read from `research.prospective_observation` on the VPS:

| experiment_id | total_n | variant_entry_n | variant_stop_n | variant_target_n | complete_tuple_n | partial_tuple_n |
|---|---:|---:|---:|---:|---:|---:|
| ME_SHORT_GEOM_A_V1 | 503 | 503 | 503 | 503 | 503 | 0 |
| ME_SHORT_GEOM_B_V1 | 503 | 503 | 503 | 503 | 503 | 0 |
| ME_SHORT_GEOM_C_V1 | 503 | 503 | 503 | 503 | 503 | 0 |
| SRR_OOS_SCANNER_V1_PROSPECTIVE | 1122 | 1122 | 1122 | 1122 | 1122 | 0 |
| SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1 | 14 | 14 | 14 | 14 | 14 | 0 |
| VC_SHORT_EXECUTION_V1 | 1122 | 62 | 62 | 62 | 62 | 0 |
| All other prospective experiments | total 14385 | 0 | 0 | 0 | 0 | 0 |

Exact affected historical IDs:

```text
AFFECTED_HISTORICAL_EXPERIMENT_IDS = [
  ME_SHORT_GEOM_A_V1,
  ME_SHORT_GEOM_B_V1,
  ME_SHORT_GEOM_C_V1,
  SRR_OOS_SCANNER_V1_PROSPECTIVE,
  SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1,
  VC_SHORT_EXECUTION_V1
]

UNAFFECTED_VARIANT_EXPERIMENT_IDS = []

PARTIAL_TUPLE_EXPERIMENT_IDS = []
```

No experiment has a partial tuple. The bug is not ME-only: any observation with a complete `variant_*` tuple was historically exposed to the old mixed semantics.

## Historical Experiment Impact

| Experiment | Variant tuple usage | Historical outcomes trustworthy? | Impact | Recommended next step |
|---|---|---|---|---|
| ME_SHORT_GEOM_A_V1 | complete tuple equals base | YES | control outcome remains valid | keep control |
| ME_SHORT_GEOM_B_V1 | complete tuple differs in stop | NO | wider stop ignored by old evaluator | offline reconstruction only |
| ME_SHORT_GEOM_C_V1 | complete tuple but delayed-entry input defect | NO for delayed-entry claim | C not a valid delayed-entry treatment | observer fix + new holdout |
| SRR_OOS_SCANNER_V1_PROSPECTIVE | complete tuple for frozen exits | NO for historical frozen-exit outcomes | old evaluator mixed base stop/target | offline reconstruction or new validation |
| SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1 | complete tuple | NO | frozen execution geometry was not fully used | separate reconstruction/revalidation task |
| VC_SHORT_EXECUTION_V1 | complete tuple for 62 observations | NO for those 62 outcomes | variant stop/target ignored historically | separate reconstruction/revalidation task |

## Fix Behavior

```text
variant_entry used = YES when complete tuple exists
variant_stop used = YES when complete tuple exists
variant_target used = YES when complete tuple exists
tuple atomicity = YES
MFE/MAE uses effective entry = YES
R normalization uses effective tuple = YES
return metrics use effective entry = YES
ambiguity logic unchanged = YES
```

For incomplete tuples, the fixed evaluator falls back to the entire base tuple. This is deterministic and prevents accidental hybrids.

## Regression Tests

Dedicated tests were added in `tests/test_prospective_evaluator_variant_tuple_hardening_v1.py`:

```text
base path
full variant tuple
B wider stop
variant target
variant stop
variant entry
partial tuple cases
A control unchanged
non-ME unchanged
ambiguity
MFE/MAE normalization
R normalization
return metrics
```

All 14 dedicated tests pass. The broader relevant suite also passes:

```text
170 passed
```

No full repository test suite was run in this phase; the bounded relevant suite was run instead.

## No Production Rewrite

```text
production rows rewritten = NO
historical outcomes changed = NO
DB statuses changed = NO
```

Historical affected outcomes remain evidence of old evaluator behavior.

## C Observer Bug

```text
fixed by this task = NO
still requires separate fix = YES
```

The evaluator hardening only corrects tuple selection. It does not correct C's delayed-entry observation input, where `features["entry_price"]` was absent and the stored C entry remained equal to A.

## Merge Readiness

```text
FIX VERDICT = FIX_SAFE_TO_MERGE
MERGE READY = YES
```

Criteria are satisfied:

- complete tuple semantics match frozen geometry;
- atomicity proven;
- partial tuple behavior explicit and tested;
- base path unchanged;
- A control unchanged;
- unrelated experiments without variant tuples unchanged;
- DB-wide blast radius inventoried;
- no partial tuples hidden in production;
- regression tests pass;
- no schema migration required;
- no production data rewrite required;
- C observer defect explicitly excluded.

This task does not commit, push, open a PR, merge, or deploy.

## Post-Merge Actions

```text
1. merge evaluator fix
2. deploy evaluator only
3. restart evaluator only if deployment procedure requires
4. verify evaluator service health
5. do NOT recompute production historical outcomes
6. run offline corrected reconstruction for affected variant experiments, prioritizing ME B and SRR frozen exits
7. fix C observer delayed-entry separately
8. create a new clean C holdout
```

## Current ME Status

```text
A = VALIDATION_BLOCKED pending merge/deploy; historical control remains usable
B = VALIDATION_BLOCKED; requires corrected offline reconstruction
C = VALIDATION_BLOCKED; requires observer fix + new clean holdout
```

## Artifacts

- Results JSON: `docs/research/me_short_geometry_abc_evaluator_fix_hardening_v1_results.json`
- Raw audit: `audit_db/me_short_geometry_abc_evaluator_fix_hardening_v1_20261007/`
- Tests: `tests/test_prospective_evaluator_variant_tuple_hardening_v1.py`
