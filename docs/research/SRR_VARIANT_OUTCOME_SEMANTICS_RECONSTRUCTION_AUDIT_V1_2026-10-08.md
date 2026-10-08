# SRR_VARIANT_OUTCOME_SEMANTICS_RECONSTRUCTION_AUDIT_V1

**Audit date:** 2026-10-08
**Snapshot cutoff:** 2026-10-08T05:54:56.705000Z
**Evaluator fix deploy:** 2026-10-07T13:04:55Z
**Snapshot SHA256:** `bfb7b4eb8a412e4b2dd893b52ec4b62736974e4f5cb55f9a32fdd18d335a391a`

## Final verdict

**SRR_HISTORICAL_TUPLE_SELECTION_VALID**

**Operational decision:** NO_ACTION_REQUIRED for historical evaluator tuple-selection repair in all three audited experiments.

This verdict applies only to the historical selection of entry/stop/target. It does not establish historical candle-path equivalence, full outcome correctness, or executable trading edge.

## Experiment results

| Experiment | Observations | Finalized | Geometry interventions | Pre-fix variant outcomes | Pre-fix horizons | Tuple mismatch | Protocol mismatch | Decision |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `SRR_LONG_BASELINE_V1` | 1130 | 1111 | 0 | 0 | 0 | 0 | 0 | `NO_ACTION_REQUIRED` |
| `SRR_OOS_SCANNER_V1_PROSPECTIVE` | 1122 | 1120 | 1122 | 1120 | 5600 | 0 | 0 | `NO_ACTION_REQUIRED` |
| `SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1` | 50 | 47 | 50 | 14 | 52 | 0 | 0 | `NO_ACTION_REQUIRED` |

## Findings

- The pre-fix evaluator selected variant entry, stop and target together when variant_entry was present; otherwise it selected the base geometry.
- No material historical tuple-selection mismatch was identified among the exported SRR observations.
- No frozen-protocol geometry mismatch was identified.
- `SRR_OOS_SCANNER_V1_PROSPECTIVE` has 1120 outcomes containing pre-fix evaluations (5600 evaluated horizons).
- `SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1` has 14 outcomes containing pre-fix evaluations (52 horizons).
- The affected-observations CSV is empty because no material historical tuple-selection mismatch was identified.

## Limitations

- Exact time-of-evaluation Bybit candle responses were not preserved or proven equivalent to current market data.
- Historical TP/SL results were not independently reconstructed from an authoritative original candle path.
- Zero tuple mismatches does not mean zero possible outcome errors.
- The result is not an executable-edge or profitability validation.
- The audit is based on one frozen production export; later observations are outside its scope.

## Next action

No historical outcome rewrite, new holdout, or offline reconstruction is required solely to correct the examined evaluator tuple-selection semantics. Continue or close individual SRR experiments according to their separate frozen OOS protocols and lifecycle decisions.

## Safety

All final analysis used a locally saved READ-ONLY VPS export. No production database writes, service restarts, registry changes or outcome rewrites were performed during this local analysis.

## Artifacts

- `srr_variant_outcome_semantics_reconstruction_audit_v1_results.json`
- `srr_variant_outcome_semantics_reconstruction_audit_v1_experiments.csv`
- `srr_variant_outcome_semantics_reconstruction_audit_v1_affected_observations.csv`
- `srr_variant_outcome_semantics_reconstruction_audit_v1_summary.csv`
