# ME_SHORT_GEOMETRY_B_CORRECTED_OUTCOME_RECONSTRUCTION_V1

Date: 2026-10-07

## Scope

Offline/read-only reconstruction and historical semantics audit for `ME_SHORT_GEOM_B_V1`. No production DB writes, no outcome rewrite, no deploy, no parameter retuning.

## Historical geometry semantics

- checked_n: 203
- hypothesis_h1: COMPLETE_PERSISTED_VARIANT
- hypothesis_h2: VARIANT_ENTRY_PLUS_BASE_STOP_TARGET
- h1_exact: 35
- h2_exact: 0
- h1_better: 203
- h2_better: 0
- ties: 0
- geometry_semantics_verdict: **COMPLETE_VARIANT_STRONGLY_SUPPORTED**

Historical B outcomes strongly support use of the complete persisted variant tuple (`variant_entry`, `variant_stop`, `variant_target`). The previously suspected `variant_stop ignored` defect is not observed for historical B.

## Path reproducibility

- status: **NOT_EXACTLY_REPRODUCIBLE_FROM_CURRENT_DB_SNAPSHOT**
- historical_source: LIVE_BYBIT_GET_KLINES
- offline_source: MARKET_CANDLE_DB_SNAPSHOT

Historical production outcomes were generated from the evaluator's live candle retrieval path, while this audit uses the persisted market-candle DB snapshot. Therefore exact historical OHLC/path reproduction is not available from the current snapshot.

## Final verdicts

- reconstruction_validity_verdict: **EXACT_PATH_RECONSTRUCTION_NOT_AVAILABLE**
- historical_geometry_semantics_verdict: **B_COMPLETE_VARIANT_GEOMETRY_CONFIRMED**
- historical_tuple_bug_impact: **NOT_OBSERVED_FOR_B**
- db_snapshot_sensitivity_verdict: **SECONDARY_POSITIVE_MFE_60M_SENSITIVITY_NOT_EXECUTABLE_EDGE**
- executable_edge: **NOT_ESTABLISHED**
- lifecycle_recommendation: **KEEP_BLOCKED**
- final_conclusion: **KEEP_BLOCKED**

## Operational decision

- Production outcomes rewrite: **NO**
- Historical B invalidation due to tuple bug: **NO**
- Executable edge established: **NO**
- Lifecycle: **KEEP_BLOCKED**

The positive DB-snapshot MFE sensitivity is secondary evidence only and does not establish an executable trading edge.
