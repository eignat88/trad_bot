# SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1_PROTOCOL

**Status:** FROZEN PROTOCOL — NOT ACTIVATED

**Experiment ID:** `SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1`

**Version:** `1`

**Parent discovery:** `SRR_SHORT_EXECUTION_R_EXPANSION_COUNTERFACTUAL_V1`

**Parent closed experiment:** `SRR_OOS_SCANNER_V1_PROSPECTIVE / SHORT / CLOSED_NEGATIVE`

**Implementation branch:** `research/srr-short-execution-r-expansion-prospective-v1`

**Protocol date:** `2026-10-07`

---

## 1. Purpose

Validate exactly one frozen SRR SHORT execution-geometry hypothesis on strictly new prospective OOS data:

> A frozen SRR SHORT execution protocol using entry at reference_price, SL at 2.00 structural_R, TP at 1.50 structural_R, 120-minute max hold, and STOP_FIRST same-candle handling has positive net expectancy after normal project transaction costs on strictly new prospective OOS data.

This is research-only, observe-only, strictly prospective validation. It does not authorize paper execution, live execution, scanner changes, signal filtering, historical backfill, or parameter retuning.

## 2. Source population and capture rule

The source scanner is:

`SUPPORT_RESISTANCE_REACTION`

The source scanner's SHORT semantics are:

| Source field | Frozen mapping |
|---|---|
| `reference_price` | `closest_resistance` |
| `invalidation_price` | `closest_resistance * 1.005` |
| `structural_R` | `abs(reference_price - invalidation_price)` |
| `direction` | `SHORT` |

Capture conditions:

1. scanner name equals `SUPPORT_RESISTANCE_REACTION`;
2. direction equals `SHORT`;
3. experiment status and direction lifecycle permit capture;
4. `signal_time > freeze_ts` — strict `>`, never `>=`;
5. `reference_price > 0`;
6. `invalidation_price > 0`;
7. `structural_R > 0`;
8. no additional eligibility filter.

One scanner candidate may be routed to multiple independent prospective experiments. This experiment must not interfere with `SRR_OOS_SCANNER_V1_PROSPECTIVE` or any other SRR experiment.

## 3. Contamination boundary

Before controlled activation:

```text
registry status       = READY_TO_START
registry freeze_ts    = NULL
DB status             = READY_TO_START
DB started_at         = NULL
observation count     = 0
outcome count         = 0
```

At controlled activation time `T`:

```text
registry freeze_ts = T
DB status          = RUNNING
DB started_at      = T
capture rule       = signal_time > T
```

The exact same UTC timestamp `T` must be used in every system. No independent `NOW()` calls may create mismatched boundaries.

The historical 688 frozen observations are:

```text
DISCOVERY ONLY
FORBIDDEN FOR PROSPECTIVE VALIDATION
NEVER INSERTED
NEVER RECALCULATED INTO THIS EXPERIMENT
NEVER BACKFILLED BY MIGRATION
NEVER CAPTURED BY OBSERVER REPLAY
```

## 4. Frozen execution geometry

For every captured SHORT observation:

| Field | Frozen definition |
|---|---|
| Entry | `reference_price` |
| `structural_R` | `abs(reference_price - invalidation_price)` |
| SL | `entry + 2.00 * structural_R` |
| TP | `entry - 1.50 * structural_R` |
| MAX_HOLD | 120 minutes |
| Intrabar policy | STOP_FIRST |
| Execution window | `signal_time < candle.open_time < signal_time + 120m` |

The evaluator excludes candles with:

```text
timestamp <= signal_ts
timestamp >= cutoff_ts
```

Therefore the signal candle is excluded, and the candle at exactly `+120m` is excluded.

The timeout exit uses the last eligible 5m candle close before the 120m cutoff.

## 5. Single execution-R normalization

For this experiment, the authoritative execution R is:

```text
structural_R = abs(entry - invalidation_price)
execution_R  = 2.00 * structural_R
```

For SHORT realized outcomes:

| Path class | Gross R |
|---|---:|
| TP_FIRST | `+0.75` |
| SL_FIRST | `-1.00` |
| AMBIGUOUS under STOP_FIRST | `-1.00` |
| TIMEOUT | `(entry - timeout_close) / execution_R` |

Never mix structural-R units and execution-R units in the same metric.

### 5.1 Timeout valuation

The evaluator's `return_at_120m` is a conventional long-direction percentage return based on the last eligible candle close before the 120m cutoff. For this SHORT experiment, timeout execution R is derived as:

```text
timeout_close = entry * (1.0 - return_at_120m / 100.0)
timeout_gross_R = (entry - timeout_close) / execution_R
                = -(return_at_120m / 100.0) * entry / execution_R
```

Tests prove equivalence to direct timeout-close P/L. No 240m return, future close, nearest post-horizon candle, or candle at exactly +120m is used as the timeout execution price.

## 6. Path classification

The shared evaluator retains horizon-wide flags:

- `tp_hit`
- `sl_hit`
- `tp_before_sl`
- `sl_before_tp`
- `ambiguous_intrabar`

The prospective analysis derives the realized execution path strictly as:

```text
if ambiguous_intrabar:
    AMBIGUOUS
elif tp_before_sl:
    TP_FIRST
elif sl_before_tp:
    SL_FIRST
else:
    TIMEOUT
```

The forbidden legacy shortcut is:

```text
if tp_hit AND sl_hit -> AMBIGUOUS
```

That shortcut must never be used. Ordered paths remain `TP_FIRST` or `SL_FIRST` even when both barriers occur over the full horizon.

## 7. Cost model

### Primary scenario — NORMAL_COST

```text
round-trip cost       = 0.21%
normal_cost_abs       = 0.0021 * entry
normal_cost_R         = normal_cost_abs / execution_R
net_R_normal          = gross_R - normal_cost_R
```

### Elevated sensitivity — ELEVATED_COST

```text
round-trip cost       = 0.31%
elevated_cost_abs     = 0.0031 * entry
elevated_cost_R       = elevated_cost_abs / execution_R
net_R_elevated        = gross_R - elevated_cost_R
```

Normal-cost metrics are the primary decision scenario. Elevated-cost metrics are mandatory sensitivity results. The experiment must not claim cost robustness if normal cost is positive while elevated cost is negative.

## 8. Primary and secondary metrics

### Primary metric

`Net E[R] after normal project costs, normalized by execution_R = 2.00 * structural_R`

### Secondary metrics

- Gross E[R]
- Net PF normal
- Gross PF
- Total Net R
- Win rate
- TP count
- SL count
- AMBIGUOUS count
- TIMEOUT count
- normal cost_R distribution
- elevated Net E[R]
- elevated PF
- day concentration
- symbol concentration
- bootstrap 95% CI
- `P(E[R] > 0)`
- leave-one-day-out results
- top-positive-symbol exclusion diagnostics

## 9. Sample gates

The following gates are frozen before activation:

| Gate | Minimum |
|---|---:|
| `minimum_n` | 300 |
| `minimum_symbols` | 30 |
| `minimum_oos_days` | 14 |

These are a conservative design decision, aligned with the existing HTF prospective registration convention. They are not derived from the discovery population size as permission to conclude after seven days.

No early positive result may be treated as validation.

## 10. Predeclared verdict rules

Before all sample gates are reached:

```text
INCONCLUSIVE_CONTINUE_OOS
```

After all sample gates are reached, the primary normal-cost validation requires all of:

1. `Net E[R] > 0`
2. `Net PF > 1`
3. bootstrap 95% CI lower bound `> 0`
4. no catastrophic day or symbol concentration

If elevated-cost results are negative while normal-cost results pass, the appropriate label is:

```text
POSITIVE_PRIMARY_NORMAL_COST_ELEVATED_COST_NEGATIVE
EXECUTION_FRAGILE
```

This does not establish cost robustness.

If the full frozen sample gates and normal-cost criteria fail, the result is negative/close under the frozen methodology.

No threshold, filter, or verdict rule may be created after prospective results begin accumulating.

## 11. Lifecycle rule

This is a new independent experiment. It must not:

- reopen `SRR_OOS_SCANNER_V1_PROSPECTIVE`;
- alter its LONG or SHORT terminal lifecycle record;
- overwrite existing closed direction records;
- reactivate terminal experiment states.

Existing lifecycle protections remain in force:

```text
CANCELLED
COMPLETED
CLOSED_NEGATIVE
```

If this new experiment reaches a terminal state, new observations must stop. Existing immature outcomes may continue to mature only according to current project lifecycle conventions.

## 12. Activation procedure

Activation is one controlled event at timestamp `T`. It is not performed by this implementation task.

1. Confirm expected Git HEAD, expected branch, deploy-safe working tree, migration 060 applied, registry entry inactive, DB row inactive, observation count zero, outcome count zero, services healthy.
2. Obtain one UTC timestamp `T`.
3. Write exactly `T` to registry `freeze_ts`.
4. Set DB `status = RUNNING`.
5. Set DB `started_at = T`.
6. Ensure registry and DB status/timestamps are exactly equal to `T`.
7. Restart only the scanner service if needed for registry reload.
8. Restart the research evaluator service only if evaluator/registry reload requires it.
9. Verify registry `freeze_ts == T`.
10. Verify DB `started_at == T`.
11. Verify pre-freeze observation count is zero.
12. Verify the first natural captured signal satisfies `signal_time > T`.

If any mismatch occurs, stop. Do not repair ad hoc.

## 13. Post-activation verification

Required checks:

```text
experiment metadata exists
registry metadata exists
registry freeze_ts == DB started_at
DB status = RUNNING
observation count
outcome count
min/max signal_time
COUNT(*) where signal_time <= freeze_ts = 0
COUNT(*) where direction != SHORT = 0
variant_entry == reference_price
variant_stop == reference_price + 2 * abs(reference_price - invalidation_price)
variant_target == reference_price - 1.5 * abs(reference_price - invalidation_price)
frozen feature metadata integrity
duplicate observation check
evaluator errors after restart
first mature 15m/30m/60m/120m/240m outcome generation
```

Project-wide outcome finalization behavior at 240m may remain unchanged even though the execution hold is 120m.

## 14. No-retuning statement

This protocol freezes exactly one candidate.

No additional filter is permitted. No cost_R eligibility threshold is permitted. The stop multiplier, TP multiplier, timeout, symbol set, regime set, and score threshold must not be retuned during collection.

The elevated-cost warning is mandatory and does not by itself invalidate the primary normal-cost hypothesis, but it must be reported as execution fragility whenever negative.

## 15. Deployment scope

This implementation does not merge, push, deploy, restart services, activate collection, or mutate production DB. It only prepares code, migration, registry metadata, protocol artifacts, tests, and controlled activation/deploy procedures for a later explicit instruction.
