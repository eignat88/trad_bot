# ME_RL_V2_EXHAUSTION_FILTER_EXECUTION_REASSESSMENT_V1

## Status

HISTORICAL_REPLAY_COMPLETED

Verdict:
PRELIMINARY_NEGATIVE_PASS_EDGE / REVERSE_FILTER_INCONCLUSIVE

Research only. Not a prospective OOS validation.
No production deployment or trading enablement.

## Source

Experiment: ME_RL_V2_GENERIC_V1

Source observations: 318
Finalized outcomes at replay: 315
Complete 240-minute candle windows: 130
Excluded observations: 188

Filter hypothesis under assessment:
exhaustion_magnitude <= 0.4

This threshold is a candidate historical filter, not an
independently verified frozen V3 protocol.

## Execution models

A_SIGNAL_REFERENCE:
- Entry reconstructed from entry_zone_low / 0.998
- Original absolute SL/TP
- Hypothetical execution from next 5m boundary

B_NEXT_OPEN:
- Entry at first eligible 5m candle open
- Original absolute SL/TP preserved
- Actual risk distance recalculated
- Diagnostic execution sensitivity model only

Common settings:
- LONG
- 240-minute horizon
- STOP_FIRST
- Taker fee: 0.055% per side
- Normal slippage: 0.05% per side
- Elevated slippage: 0.10% per side

## Results — normal costs

| Model | Group | N | Net E[R] | Net PF |
|---|---|---:|---:|---:|
| A | CONTROL | 130 | -0.081616 | 0.775687 |
| A | PASS | 105 | -0.183066 | 0.495177 |
| A | REJECT | 25 | +0.344472 | 1.933626 |
| B | CONTROL | 130 | -0.094309 | 0.743097 |
| B | PASS | 105 | -0.179305 | 0.510239 |
| B | REJECT | 25 | +0.262674 | 1.707495 |

## Model B outcome distribution

PASS:
- TP: 12
- SL: 23
- TIMEOUT: 70
- Total Net R: -18.827009
- Positive days: 4/14

REJECT:
- TP: 16
- SL: 8
- TIMEOUT: 1
- Total Net R: +6.566839
- Positive days: 7/11

## Exclusions

PASS: 152
- No closed candles: 91
- Incomplete window start: 51
- Incomplete window end: 10

REJECT: 36
- No closed candles: 18
- Incomplete window start: 15
- Not finalized: 3

## Limitations

1. Historical retrospective analysis; not clean prospective OOS.
2. Only 130/318 observations have fully eligible windows.
3. The missing-candle cohort may introduce selection bias.
4. REJECT contains only 25 executed replay observations.
5. Next-open execution is not proven achievable in live trading.
6. No cluster-robust confidence interval or independent holdout.
7. The frozen V3 filter protocol was not independently located.
8. Results may change with historical candle backfill.
9. Runtime availability of signal/features was not independently audited.
10. The results are diagnostic, not authorization for paper/live execution.

## Decision

Do not promote exhaustion_magnitude <= 0.4 as a
validated V3 execution gate.

Do not automatically substitute exhaustion_magnitude > 0.4.

Keep V3 prospective implementation pending protocol review,
coverage repair, and independent validation.

Do not modify V2 scanner, paper/live routing, active OOS
experiments, existing outcomes, or production configuration.

## Reproduction

Script:
tools/research/me_rl_v2_exhaustion_filter_execution_reassessment_v1.py

Tests:
tests/test_me_rl_v2_exhaustion_filter_execution_reassessment_v1.py

Artifacts:
- ME_RL_V2_EXHAUSTION_FILTER_EXECUTION_REASSESSMENT_V1_SUMMARY.json
- ME_RL_V2_EXHAUSTION_FILTER_EXECUTION_REASSESSMENT_V1_TRADES.csv
- ME_RL_V2_EXHAUSTION_FILTER_EXECUTION_REASSESSMENT_V1_EXCLUSIONS.csv

The script reads a live database snapshot and may not reproduce
the exact historical cohort after new outcomes or backfill.
Preserved CSV/JSON files are the recorded replay artifacts.
