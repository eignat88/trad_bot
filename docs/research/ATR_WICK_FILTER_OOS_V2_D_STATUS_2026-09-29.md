# ATR_WICK V2_D OOS STATUS

- **Prospective start:** 2026-09-24 15:05 UTC
- **Clean OOS start:** 2026-09-24 15:05 UTC
- **OOS age:** 5 days (120+ hours)
- **Observations:** 18,387
- **Symbols:** 93
- **Fully evaluated:** 17,635
- **Pending:** 752
- **Good (MFE>=1.0 & MAE<=0.5):** 3,251 (17.83%)
- **Bad (MAE>=1.0 & MFE<1.0):** 4,512 (24.74%)
- **Good/Bad:** 0.72
- **Avg MFE 60m:** 1.1741%
- **Avg MAE 60m:** 1.1778%
- **Observations/day:** 3,677
- **Pipeline health:** OK
- **OOS integrity:** OK
- **Sample maturity:** SUFFICIENT_FOR_INITIAL_ANALYSIS
- **Historical replication:** NOT_REPLICATING (0.72 vs 9.77)
- **Estimated additional accumulation:** 30+ days for robust confirmation
- **FINAL STATUS:** KEEP_ACCUMULATING

---

# ATR_WICK_FILTER_OOS_V2_D — Audit Report

**Date:** 2026-09-29
**Status:** Prospective OOS Running
**Reviewer:** MiMo-v2.5

## 1. Experiment Status

| Field | Value |
|---|---|
| experiment_id | `ATR_WICK_FILTER_OOS_V2_D` |
| source_experiment_id | `ATR_WICK_FILTER_OOS_V1` |
| status | `EXPLORATORY_LOCKED` |
| hypothesis | ATR wick rejection SHORT signals may perform better when StochRSI is in the middle range 0.2 <= stoch_rsi < 0.6 |
| filter_definition | `{"stoch_rsi_lt": 0.6, "stoch_rsi_gte": 0.2, "volume_filter": null}` |
| discovery_started_at | 2026-09-24 08:40:00 UTC |
| discovery_ended_at | 2026-09-24 09:55:00 UTC |
| discovery_sample_n | 283 |
| discovery_good | 127 |
| discovery_bad | 13 |
| discovery_ratio | 9.77 |
| discovery_avg_mfe_60m | 1.5713 |
| discovery_avg_mae_60m | 0.4856 |
| **prospective_started_at** | **2026-09-24 15:05:00.77 UTC** |
| created_at | 2026-09-24 10:59:43 UTC |

**Prospective Start Confirmation:**
The prospective OOS data collection started at **2026-09-24 15:05:00 UTC**, as recorded in `dds.shadow_oos_experiment_registry`. All observations with `signal_time >= 2026-09-24 15:05:00` are considered prospective OOS.

## 2. Pipeline Health

### Scanner
- **Status:** Active
- **Service:** `trad-bot-v2d-scanner.service` (PID 914906)
- **Uptime:** 3 days (since 2026-09-26 06:23 UTC)
- **Interval:** 300 seconds
- **Last Cycle:** Successful
- **Output:** 28 filter_pass signals per cycle

### Evaluator
- **Status:** Active
- **Service:** `trad-bot-v2d-evaluator.service` (Triggered by timer)
- **Timer:** `trad-bot-v2d-evaluator.timer` (Every 5 minutes)
- **Last Run:** 2026-09-29 16:05 UTC
- **Results:** Checked 96 signals, finalized 17 outcomes.

### Data Flow
1. **Scanner** produces `dds.v2d_signal` (18,387 total).
2. **Evaluator** consumes signals and produces `dds.v2d_outcome` (18,309 total).
3. **Pending:** 752 signals waiting for evaluation (mostly recent ones).
4. **Finalized:** 17,635 fully evaluated outcomes.

**Conclusion:** Pipeline is healthy and moving data correctly.

## 3. Prospective OOS Sample

| Metric | Value |
|---|---|
| OOS Days | 6 (Partial for 24th and 29th) |
| Total Observations | 18,387 |
| Distinct Symbols | 93 |
| Observations/Day | ~3,677 |
| Evaluated 15m | 18,309 (100%) |
| Evaluated 30m | 18,253 (99%) |
| Evaluated 60m | 18,192 (99%) |
| Evaluated 120m | 18,027 (98%) |
| Evaluated 240m | 17,635 (96%) |
| Fully Finalized | 17,635 |
| Pending | 752 |

### Daily Accumulation

| UTC Date | Observations | Symbols | Finalized |
|---|---:|---:|---:|
| 2026-09-24 | 1,484 | 53 | 1,361 |
| 2026-09-25 | 3,764 | 61 | 3,719 |
| 2026-09-26 | 3,490 | 68 | 3,522 |
| 2026-09-27 | 3,504 | 65 | 3,560 |
| 2026-09-28 | 3,657 | 63 | 3,658 |
| 2026-09-29 | 2,488 | 58 | 2,417 |

**Trend:** N is growing stably at ~3,500/day.

## 4. OOS Quality Metrics

### Overall (MFE/MAE based on V1 definition)

| Metric | Value |
|---|---|
| N | 18,219 |
| Good (MFE>=1.0 & MAE<=0.5) | 3,251 (17.83%) |
| Bad (MAE>=1.0 & MFE<1.0) | 4,512 (24.74%) |
| Neutral | 10,456 (57.43%) |
| **Good/Bad Ratio** | **0.72** |
| Avg MFE 60m | 1.17% |
| Median MFE 60m | 0.69% |
| Avg MAE 60m | 1.18% |
| Median MAE 60m | 0.67% |

### By Horizon

| Horizon | N | Avg MFE | Avg MAE |
|---|---:|---:|---:|
| 15m | 18,364 | 0.48% | 0.47% |
| 30m | 18,281 | 0.78% | 0.77% |
| 60m | 18,219 | 1.17% | 1.18% |
| 120m | 18,064 | 1.72% | 1.77% |
| 240m | 17,663 | 2.38% | 2.64% |

### By Target Achievement (Finalized Only)

| Target | Reached | Hit Adverse | Good/Bad |
|---|---:|---:|---:|
| 0.5% | 13,939 (78.8%) | 8,142 (46.1%) | 1.71 |
| 1.0% | 10,966 (62.0%) | 5,198 (29.4%) | 2.11 |
| 1.5% | 8,683 (49.1%) | 3,698 (21.0%) | 2.35 |
| 2.0% | 6,911 (39.1%) | N/A | N/A |

**Note:** The "Good/Bad Ratio" using MFE/MAE definitions (0.72) is much lower than the Discovery ratio (9.77). This suggests the filter performance is significantly worse in the prospective OOS compared to the discovery sample.

## 5. Discovery vs OOS Comparison

| Metric | Discovery | Prospective OOS | Delta |
|---|---:|---:|---:|
| N | 283 | 18,219 | +64x |
| Good % | 44.88% | 17.83% | -27.05% |
| Bad % | 4.59% | 24.74% | +20.15% |
| **Good/Bad Ratio** | **9.77** | **0.72** | **-92.6%** |
| Avg MFE 60m | 1.57% | 1.17% | -0.40% |
| Avg MAE 60m | 0.49% | 1.18% | +0.69% |

**Conclusion:** The historical edge is **NOT REPLICATING** on the prospective OOS. The Good/Bad ratio has collapsed from 9.77 to 0.72. The Bad rate has increased dramatically.

## 6. Stability Over Time

### Early OOS (Sep 24-25)

| Metric | Value |
|---|---|
| N | 5,080 |
| Good % | 14.4% |
| Bad % | 29.1% |
| Avg MFE | 1.05% |
| Avg MAE | 1.21% |

### Recent OOS (Sep 27-29)

| Metric | Value |
|---|---|
| N | 9,635 |
| Good % | 18.9% |
| Bad % | 24.0% |
| Avg MFE | 1.24% |
| Avg MAE | 1.19% |

**Trend:** Performance has slightly improved from the initial drop but remains far below discovery levels. There is no strong evidence of degrading performance over time (i.e., no "strong start then crash" pattern), but the baseline is already poor.

## 7. Concentration

| Metric | Value |
|---|---|
| Top 1 Symbol | BTWUSDT (446 obs, 2.4%) |
| Top 5 Symbols | 2,165 obs (11.8%) |
| Top 10 Symbols | 4,190 obs (22.8%) |
| Symbols with N >= 3 | 93 (100%) |
| Symbols with N >= 5 | 92 (99%) |

**Conclusion:** The sample is **highly cross-sectional**. No single symbol dominates the results. This is a positive sign for the robustness of the (negative) result.

## 8. Duplicates & Leakage

- **Duplicate Signals:** 0 (Unique constraint `(experiment_id, symbol, signal_time)` is effective).
- **Missing Outcomes:** 80 signals without outcomes (0.4%), likely due to recent insertion.
- **Missing Signals:** 0 outcomes without signals (Referential integrity OK).
- **Re-evaluation:** 0 duplicates detected.
- **Leakage:**
    - Signals are generated from closed 5m candles (`signal_time` is candle close time).
    - Evaluation happens after the horizon elapses (`evaluated_at > signal_time + horizon`).
    - **No evidence of future data leakage.**
- **Parameter Changes:** The filter parameters `0.20 <= stoch_rsi < 0.60` are locked in `app/shadow/atr_wick_v2d_filter.py` and have not changed since deployment.

**Conclusion:** Data integrity is **OK**.

## 9. Configuration Immutability

- **Filter:** `STOCH_RSI_MIN = 0.20`, `STOCH_RSI_MAX = 0.60` (Frozen in code).
- **Direction:** `SHORT` (Frozen in code).
- **Source Scanner:** `ATR_WICK_REJECTION_SHORT_V1` (Unchanged).
- **No config drift detected.**

## 10. Maturity Assessment

### Pipeline Health: **OK**
Scanner and Evaluator are running, producing and processing data as expected.

### OOS Integrity: **OK**
No duplicates, no leakage, referential integrity is maintained.

### Sample Maturity: **SUFFICIENT_FOR_INITIAL_ANALYSIS**
With N=18,000+ and 6 days of data, we have enough statistical power to reject the hypothesis that the Discovery performance (Ratio=9.77) is being replicated. The result is statistically significant.

### Historical Replication: **NOT_REPLICATING**
The Prospective OOS Good/Bad ratio (0.72) is orders of magnitude lower than the Discovery ratio (9.77). The "edge" discovered in the historical sample does not exist in the live market for this specific filter configuration.

## 11. Future Accumulation Estimate

- **Current Speed:** ~3,600 obs/day.
- **Current N:** 18,219.
- **Required N for Decision:** We already have sufficient N to reject the Discovery hypothesis.
- **Required N for Stability:** To confirm the "Bad" rate (24.7%) is stable, we would need ~20,000+ Bad events. Currently at 4,512. We need roughly 15,000 more Bad events.
- **Estimated Time:** ~10-15 days to reach a very high confidence level on the "Bad" rate stability.

## 12. Final Verdict

**DISCOVERY EDGE NOT REPLICATED.**

The ATR_WICK_FILTER_OOS_V2_D experiment was designed to test if a StochRSI filter (0.2-0.6) could improve the ATR Wick SHORT strategy.

- **Discovery Phase:** Showed a promising Good/Bad ratio of 9.77 (127 Good vs 13 Bad).
- **Prospective OOS:** Shows a Good/Bad ratio of **0.72** (3,251 Good vs 4,512 Bad).

The filter appears to be **harming** the strategy's performance by filtering out good trades (lower Good rate) or keeping bad trades (higher Bad rate), or both. The absolute performance (Avg MFE 1.17%, Avg MAE 1.18%) is barely break-even after accounting for slippage and fees.

**Recommendation:**
Do **NOT** promote this scanner configuration to Paper Trading. The current OOS results indicate it is not profitable. Continue accumulating data to be absolutely certain of the "Bad" rate, but the conclusion is likely negative.

### FINAL STATUS: `KEEP_ACCUMULATING` (with negative outlook)

*Note: While "KEEP_ACCUMULATING" is the standard status for sufficient data, the actual recommendation based on these metrics is to likely DISCONTINUE this specific experiment unless further analysis reveals a specific sub-segment that is profitable (which is not evident in the aggregate data).*
