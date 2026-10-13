# ME_RL_V2_EXHAUSTION_FILTER_EXECUTION_REASSESSMENT_V1

## Status

HISTORICAL_REPLAY_COMPLETED

Verdict:
PRELIMINARY_NEGATIVE_PASS_EDGE / REVERSE_FILTER_INCONCLUSIVE

Research only. Not prospective OOS validation.
No production deployment or trading enablement.

## Source

- Experiment: ME_RL_V2_GENERIC_V1
- Source observations: 321
- Complete 240-minute candle windows: 144
- Filter: exhaustion_magnitude <= 0.4 (historical candidate)

## Replay integrity

- PostgreSQL transaction: READ ONLY, REPEATABLE READ
- Candle quality: only validated candles accepted
- Entry models: A_SIGNAL_REFERENCE and B_NEXT_OPEN
- Horizon: 240 minutes
- Intrabar collision: STOP_FIRST
- Taker fee: 0.055% per side
- Normal slippage: 0.05% per side
- Elevated slippage: 0.10% per side

## Results — normal costs

| Model | Group | N | Net E[R] | Net PF |
|---|---|---:|---:|---:|
| A_SIGNAL_REFERENCE | CONTROL | 144 | -0.118331 | 0.687265 |
| A_SIGNAL_REFERENCE | PASS | 116 | -0.193126 | 0.46677 |
| A_SIGNAL_REFERENCE | REJECT | 28 | +0.191534 | 1.429969 |
| B_NEXT_OPEN | CONTROL | 144 | -0.132586 | 0.655615 |
| B_NEXT_OPEN | PASS | 116 | -0.189310 | 0.482784 |
| B_NEXT_OPEN | REJECT | 28 | +0.102413 | 1.220902 |

## Exclusions

| Reason | N |
|---|---:|
| COVERAGE_INCOMPLETE_WINDOW_END | 10 |
| COVERAGE_INCOMPLETE_WINDOW_START | 66 |
| COVERAGE_NO_CLOSED_CANDLES | 98 |
| NOT_FINALIZED | 3 |

## Limitations

- Retrospective historical replay, not clean prospective OOS.
- Dataset changed after the original snapshot; historical cohorts are not identical.
- Only complete 240-minute candle windows are included.
- Missing candle coverage may introduce selection bias.
- Next-open execution is not proven achievable in live trading.
- No cluster-robust confidence interval or independent holdout.
- Frozen V3 filter protocol has not been independently verified.

## Decision

Do not promote exhaustion_magnitude <= 0.4 as a validated V3 execution gate.

Do not automatically replace it with exhaustion_magnitude > 0.4.

Keep V3 pending protocol review, coverage repair, and independent validation.

No scanner, paper/live, database, OOS registry, or production configuration changes.

## Artifacts

- ME_RL_V2_EXHAUSTION_FILTER_EXECUTION_REASSESSMENT_V1_SUMMARY.json
- ME_RL_V2_EXHAUSTION_FILTER_EXECUTION_REASSESSMENT_V1_TRADES.csv
- ME_RL_V2_EXHAUSTION_FILTER_EXECUTION_REASSESSMENT_V1_EXCLUSIONS.csv

The original historical snapshot was preserved separately before regeneration.
The updated artifacts represent one later database snapshot and must not be
presented as the unchanged original 318-observation cohort.
