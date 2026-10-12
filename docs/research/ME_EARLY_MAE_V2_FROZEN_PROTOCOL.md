# ME_R_LONG Early MAE Exit V2 — Frozen Protocol

**Protocol version:** `2.1.0`
**Experiment ID:** `ME_R_LONG_EARLY_MAE_EXIT_OOS_V2`
**Status:** IMPLEMENTED / NOT ACTIVATED
**Mode:** STRICT RESEARCH-ONLY

## 1. Hypothesis

A prospective LONG virtual position in `MOMENTUM_EXHAUSTION_REVERSE_LONG_V1` that reaches MAE of at least `0.50R` within the first `15` minutes after virtual entry may obtain higher Net E[R] when closed at the frozen early-exit boundary, compared with the frozen baseline policy.

This protocol does not create paper or live positions. It evaluates a deterministic counterfactual using only source events and completed candles.

## 2. Source scanner

| Item | Frozen value |
|---|---|
| Scanner name | `MOMENTUM_EXHAUSTION_REVERSE_LONG_V1` |
| Scanner class | `app.scanners.momentum_exhaustion_reverse_long_v1.MomentumExhaustionReverseLongV1Scanner` |
| Scanner version | `1.0.0` |
| Source direction | `LONG` |
| Natural candidate source | `dds.scanner_setup` |
| Detection boundary | The first V1 candidate observed after the protocol activation timestamp |
| Historical backfill | Forbidden |
| Pre-activation candidates | Rejected as `PRE_FREEZE` and excluded from the clean prospective cohort |

The scanner is a natural source because it remains registered in the production scanner orchestrator and continues emitting LONG candidates while its production direction gate is BLOCKED. Research capture must occur before paper execution gates and must not depend on a paper trade.

The existing `ME_RL_V1_GENERIC_V1` research experiment remains a separate, closed research record. It must not be treated as the active source for this protocol.

## 3. Source availability and look-ahead boundary

The natural candidate is available to the V2 observer at the time the scanner returns it from the closed-candle scan. The observer records:

- `detected_at` as the scanner decision timestamp;
- `signal_candle_open_time` from the closed entry-timeframe candle;
- the frozen entry zone, stop, target, scanner parameters and features.

No observation may use a signal candle that closes after its corresponding `detected_at`. A source record with that ordering is `INVALID` with reason `SIGNAL_CANDLE_AFTER_ASOF`.

## 4. Virtual entry

Virtual entry is permitted only after a source event has been persisted and is available to the evaluator.

| Rule | Frozen value |
|---|---|
| Entry availability | Source event must be persisted and visible to the evaluator |
| Entry trigger | A completed 5m candle touches the frozen entry zone |
| Entry price | Deterministic conservative boundary price |
| Fill boundary | Candle close at or after the first touch time |
| Reference price as fill | Forbidden |
| Open or incomplete candle | Forbidden |
| Entry window | Detection timestamp through the frozen expiry boundary |
| No fill by expiry | `ENTRY_NOT_FILLED` |
| Position size | Unit virtual size; all results are normalized to R |

For a LONG entry zone `[entry_zone_low, entry_zone_high]`, the frozen conservative fill is:

- `virtual_entry_price = entry_zone_high` when the first completed candle touches the zone;
- `risk_distance = virtual_entry_price - invalidation_price`;
- `virtual_stop_price = invalidation_price`;
- `virtual_target_price = target_1`;
- `virtual_entry_at = first qualifying candle close time`.

This conservative boundary avoids treating `reference_price` as an executable price and avoids assuming a favorable intra-candle trajectory.

## 5. Baseline execution policy

The V1 execution policy is frozen from the production configuration and paper-engine semantics:

| Item | Frozen value |
|---|---|
| Policy | `FIXED_TP_SL_HORIZON_V1` |
| Stop | First stop breach |
| Take profit | Enabled, first target hit |
| Max hold | `240` minutes |
| Timeout exit | First completed 5m candle close at or after the 240-minute boundary |
| DCA | Disabled |
| Trailing | Disabled |
| Breakeven | Disabled |
| Funding | Not modeled |
| Intra-candle SL/TP ordering | `STOP_FIRST` |

The 240-minute rule is confirmed by both the frozen scanner parameters and `config.yaml` / paper-engine execution semantics. It is not inferred from a comment alone.

## 6. Early-exit variants

Exactly seven variants are frozen:

| Variant | Horizon | MAE threshold |
|---|---:|---:|
| `MAE_10m_040` | 10m | 0.40R |
| `MAE_10m_050` | 10m | 0.50R |
| `MAE_15m_040` | 15m | 0.40R |
| `MAE_15m_050` | 15m | 0.50R |
| `MAE_15m_060` | 15m | 0.60R |
| `MAE_15m_075` | 15m | 0.75R |
| `MAE_30m_050` | 30m | 0.50R |

`MAE_15m_050` is the primary variant.

Each virtual entry produces exactly one observation for each variant. A variant is evaluated only from completed 5m candles whose interval ends no later than the variant horizon. The window is never extended to current wall-clock time.

## 7. MAE, MFE and exit model

For LONG:

```text
risk_distance = virtual_entry_price - virtual_stop_price
MAE_R = max(0, virtual_entry_price - window_low) / risk_distance
MFE_R = max(0, window_high - virtual_entry_price) / risk_distance
Gross R = (exit_price - virtual_entry_price) / risk_distance
Net R = Gross R - round-trip fee/slippage cost in R
```

The frozen cost model applies the same model to the baseline and all seven variants:

```text
taker fee       = 0.055% per side
normal slippage = 0.05% per side
round-trip cost = 0.21% of notional
```

The virtual position size is one unit. Risk distance is positive for every eligible source; invalid geometry is stored separately and never enters the eligible cohort.

## 8. Trigger and first-hit semantics

For a variant horizon:

1. Load only completed 5m candles fully contained in `[virtual_entry_at, virtual_entry_at + horizon]`.
2. Reject duplicate timestamps, gaps, unclosed candles and post-horizon candles.
3. Compute MAE and MFE from the permitted window.
4. Mark `EVALUATED_TRIGGERED` when MAE reaches the frozen threshold.
5. Mark `EVALUATED_NO_TRIGGER` when the horizon is complete and the threshold is not reached.
6. Store `trigger_result` even when the variant never triggers.

The early-exit price is the close of the first completed 5m candle at or after the horizon boundary. If no completed candle is available, the variant is `INCOMPLETE_COVERAGE`, not finalized.

When both stop and target are touched in one candle, the frozen ordering is `STOP_FIRST`. The source status is `AMBIGUOUS`, the ordering is stored explicitly, and the outcome remains usable only under the frozen conservative ordering.

## 9. Baseline and paired outcomes

For every eligible virtual fill, the evaluator computes:

- baseline stop / target / timeout exit;
- baseline Gross R and Net R;
- each variant early or inherited exit;
- each variant Gross R and Net R;
- `delta_net_r = variant_net_r - baseline_net_r`;
- exit reason and first-hit classification;
- source coverage status.

If a variant threshold is reached, only the virtual variant position is closed under the frozen early-exit policy. If the threshold is not reached, the variant inherits the baseline outcome. If the baseline position reaches its terminal exit before the variant horizon, the variant is `BASELINE_EXITED_EARLY` and no early exit is modeled after that terminal event.

## 10. Missing and ambiguous candle policy

| Condition | Treatment |
|---|---|
| Missing 5m candle in required window | `INCOMPLETE_COVERAGE`; no final result |
| Duplicate 5m timestamp | `AMBIGUOUS`; no final result until resolved by source provenance |
| Unclosed candle | Excluded |
| Candle close after ASOF boundary | Excluded |
| Missing baseline terminal candle | `INCOMPLETE_COVERAGE` |
| SL/TP in same candle | Frozen `STOP_FIRST`; retain ambiguity marker |
| Source detected before activation | `PRE_FREEZE`; excluded |

## 11. Persistence and lifecycle

The V2 schema is independent from `dds.paper_trade` and does not use a foreign key to paper trade identifiers.

Source statuses:

`DETECTED`, `ENTRY_PENDING`, `ENTRY_FILLED`, `ENTRY_NOT_FILLED`, `INVALID`, `FINALIZED`.

Variant statuses:

`PENDING`, `EVALUATED_TRIGGERED`, `EVALUATED_NO_TRIGGER`, `BASELINE_EXITED_EARLY`, `INCOMPLETE_COVERAGE`, `AMBIGUOUS`, `FINALIZED`.

Required identity:

```text
unique(source_event_id, variant_id, protocol_version)
```

All source/variant writes are transactional and idempotent. Restart recovery resumes only non-terminal records. Terminal records cannot be silently overwritten.

## 12. Frozen activation

The activation timestamp is set when the migration and code are deployed and verified. It is not derived from a historical setup timestamp. The first natural post-activation candidate is the start of the clean prospective cohort.

The old `dds.me_r_long_early_exit_observation` table is not rewritten, migrated in place or reused.

## 13. Prohibited operations

This protocol forbids:

- changing V1 scanner detection;
- enabling V1 LONG paper execution;
- changing direction gates;
- creating paper or live trades;
- changing balances, positions or risk controls;
- calling production exit handlers;
- inserting historical events into the clean cohort;
- deploying without review and tests.
