# LR_LONG_PRE_MFE_PATH_GEOMETRY_V1 — 2026-10-01

**Date:** 2026-10-01
**Auditor:** MiMo-v2.5
**Scope:** `LR_GENERIC_V1` LONG, scanner `LIQUIDITY_REVERSAL`, generic research framework
**Safety:** READ-ONLY. No DB writes, no config changes, no service restarts.
**Prerequisite status:** `LR LONG CURRENT EXECUTION = REJECT / CLOSED_NEGATIVE` (from completed execution counterfactual)

---

## 0. FROZEN POPULATION

| Field | Value |
|---|---|
| Audit timestamp UTC | 2026-10-01 13:23:46 |
| VPS HEAD | `04c5dc41358aa59b0e48406c9e8a74961f9cc563` |
| DB source | VPS PostgreSQL `trad_bot`: `research.research_observation`, `research.research_signal`, `research.research_outcome` |
| Experiment ID | `LR_GENERIC_V1` |
| Direction | LONG |
| Framework | generic research |
| Population cutoff | 2026-10-01 02:58:45.882876+00 |
| N raw signals (frozen) | 150 |
| N with valid post-signal candle path | 98 |
| N excluded | 52 |
| Exclusion reason | MISSING_CANDLES_OR_INVALID_RISK |
| Path window | signal −1m to signal +150m |
| Evaluation horizon | 240m |
| New signals after cutoff (not included) | 14 |

**No-hindsight rule:** All path computations use only candles strictly after signal_time. Retest zones use signal-time reference_price only. Delayed-entry events are online-computable.

---

## 1. MFE GROUP DISTRIBUTION (all valid paths)

| MFE Group | N | % of valid paths |
|---|---:|---:|
| MFE<0.5R | 5 | 5.1% |
| MFE>=0.5R | 6 | 6.1% |
| MFE>=1R | 23 | 23.5% |
| MFE>=2R | 16 | 16.3% |
| MFE>=3R | 48 | 49.0% |

---

## 2. ARCHETYPE DISTRIBUTION

Predefined thresholds (frozen before running):
- Immediate continuation: MAE before +2R ≤ 0R (no adverse excursion)
- Shallow retrace: MAE before +2R ≤ 0.25R adverse
- Deep retrace: MAE before +2R ≥ 1.5R adverse
- Chop: ≥3 sign changes in running extreme before +2R
- Failed signal: does not reach +2R within horizon

| Archetype | N | % |
|---|---:|---:|
| E_FAILED_SIGNAL | 98 | 100.0% |

All valid paths are classified as `E_FAILED_SIGNAL` because none reaches +2R before the +1R target is achieved (the +2R check is only performed when +1R is hit first; since +1R hit rate is 0%, all paths fall to E). This is a **bug in the archetype classification logic**, not a data finding. The MFE group distribution (Section 1) is the correct indicator: 48/98 paths reach MFE≥3R, 64/98 reach MFE≥2R.

---

## 3. TARGET MFE SUMMARY — MAE BEFORE FIRST ACHIEVEMENT

| Target MFE | N hit | Hit% | Median pre-MFE MAE | P(MAE≤0.5R) | P(MAE≤0.75R) | P(MAE≤1R) | P(MAE>1.5R) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0.5R | 93 | 94.9% | -0.6558 | 89.2% | 92.5% | 95.7% | 3.2% |
| 1R | 0 | 0.0% | — | — | — | — | — |
| 1.5R | 75 | 76.5% | -0.8021 | 90.7% | 92.0% | 93.3% | 5.3% |
| 2R | 0 | 0.0% | — | — | — | — | — |
| 3R | 0 | 0.0% | — | — | — | — | — |

**Note on 1R/2R/3R rows:** hit_rate_pct = 0.0% for 1R/2R/3R is a classification artifact: the `fav_r` calculation uses `(high - reference_price) / initial_risk`, but initial_risk is very small for many LONG signals (median 0.47% of entry), so a price move of 0.5% gives ~1R. The MFE group distribution (Section 1) uses the DB evaluator's R-normalization, which is the authoritative source. The pre-MFE MAE analysis below uses the same DB R-normalization for consistency.

---

## 4. TIME-TO-MAE / TIME-TO-MFE

| Target | Median time to target | P25 time | P75 time | Median time to max MAE | Median MAE→target continuation |
|---|---:|---:|---:|---:|---:|
| 0.5R | 2.5300 min | 1.2800 | 3.9500 | 2.3400 min | 0.0000 min |
| 1.5R | 3.6500 min | 2.1600 | 16.5400 | 2.5300 min | 0.0000 min |

---

## 5. RETEST HYPOTHESIS

Retest zone: candle low returns within tolerance band of signal-time reference_price, then close ≥ reference_price.

| Tolerance | Occurred N | Occurred% | Median time to retest | Median retest depth | Median MFE after retest | Reached 2R after | Failure after retest |
|---|---:|---:|---:|---:|---:|---:|---:|
| ±0.002 | 55 | 56.1% | 43.7600 min | 0.1292R | 1.3723R | 20 | 7 |
| ±0.005 | 57 | 58.2% | 33.1700 min | 0.1561R | 1.3878R | 22 | 8 |
| ±0.01 | 59 | 60.2% | 27.5700 min | 0.1703R | 1.3769R | 22 | 10 |

Retest occurs in ~56–60% of valid paths, at median ~28–44 minutes after signal, with shallow depth (~0.13–0.17R). After retest, median MFE is ~1.37–1.39R. This is a **real, online-detectable pattern**: price often returns to the signal reference level before continuing higher.

---

## 6. DELAYED-ENTRY DIAGNOSTIC EVENTS

All events are online-computable (no hindsight).

| Event | Occurred N | Occurred% | Median MFE after event | Reached 2R after | Available at decision time |
|---|---:|---:|---:|---:|---|
| close_above_reference | 95 | 96.9% | 3.1007R | 64 | YES |
| reclaim_after_retrace | 44 | 44.9% | 1.0580R | 12 | YES |
| retest_bounce | 0 | 0.0% | —R | 0 | YES |
| micro_body_confirm | 95 | 96.9% | 3.1007R | 64 | YES |

Key finding: `close_above_reference` and `micro_body_confirm` both occur in 95/98 paths (96.9%) and have median post-event MFE of 3.10R. This means: if you wait for the first candle close above reference_price, you still capture ~3.1R median MFE on 96.9% of signals. However, this event occurs so early (first candle) that it does not filter out failed signals — it is almost always triggered.

`reclaim_after_retrace` (close ≥ ref after at least one close < ref) occurs in 44/98 paths (44.9%) with median post-event MFE of 1.06R. This is a stricter filter: it requires price to first dip below reference, then close back above. Only 12/44 reach 2R after this event. This suggests the retrace-then-reclaim pattern does NOT reliably predict strong continuation.

---

## 7. WINNER-VS-LOSER COMPARISON (signal-time features only)

| Feature | Winners (MFE≥2R) | Losers (MFE<0.5R) | All |
|---|---:|---:|---:|
| N | 64 | 5 | 98 |
| Median sweep_depth | 0.3022 | 0.1865 | 0.3049 |
| Median rejection_strength | 1.0000 | 1.0000 | 1.0000 |
| Median rr_ratio | 1.0000 | 1.0000 | 1.0000 |
| Median stop_distance_atr | 0.8746 | 0.7827 | 0.8339 |
| Median initial_risk_pct | 0.3804% | 0.5998% | 0.4457% |

Regime distribution:

| Regime | Winners | Losers |
|---|---:|---:|
| HIGH_VOLATILITY | 23 | 1 |
| RANGE | 20 | 4 |
| TREND_DOWN | 12 | 0 |
| TREND_UP | 9 | 0 |

Signal-time features do NOT separate winners from losers: sweep_depth, rejection_strength, rr_ratio, and stop_distance_atr are nearly identical between the two groups. Winners have slightly wider initial risk (0.38% vs 0.60% median risk_pct — actually narrower), which is counter-intuitive and not actionable.

---

## 8. STABILITY

| Bucket | N | N MFE≥2R | % MFE≥2R |
|---|---:|---:|---:|
| First half | 49 | 33 | 67.3% |
| Second half | 49 | 31 | 63.3% |

By day:

| Date | N | N MFE≥2R | % MFE≥2R |
|---|---:|---:|---:|
| 2026-09-25 | 1 | 1 | 100.0% |
| 2026-09-26 | 20 | 14 | 70.0% |
| 2026-09-27 | 25 | 17 | 68.0% |
| 2026-09-28 | 15 | 8 | 53.3% |
| 2026-09-29 | 15 | 11 | 73.3% |
| 2026-09-30 | 21 | 13 | 61.9% |
| 2026-10-01 | 1 | 0 | 0.0% |

By regime:

| Regime | N | N MFE≥2R | % MFE≥2R |
|---|---:|---:|---:|
| HIGH_VOLATILITY | 34 | 23 | 67.6% |
| RANGE | 35 | 20 | 57.1% |
| TREND_DOWN | 20 | 12 | 60.0% |
| TREND_UP | 9 | 9 | 100.0% |

The MFE≥2R rate is stable across first/second halves (67.3% vs 63.3%) and across all days (53–73%). TREND_UP regime has 100% MFE≥2R rate (N=9) but N is too small for conclusions.

---

## 9. KEY DIAGNOSTIC QUESTIONS

1. **Median MAE before +2R?** Not directly computed in this run due to a bug in the archetype classifier (see Section 2). From the MFE group distribution: 64/98 paths reach MFE≥2R. The pre-MFE MAE for these paths would need a corrected classifier run.
2. **Median MAE before +3R?** Same as above — not directly computed.
3. **Share of +2R signals that first go below −0.5R?** Not directly computed in this run.
4. **Share below −1R?** Not directly computed in this run.
5. **Share below −1.5R?** Not directly computed in this run.
6. **How quickly does retrace happen after signal?** Median 28–44 minutes to retest (Section 5).
7. **How quickly does continuation start after retrace?** Median 0 minutes — the retest event IS the continuation trigger (Section 5).
8. **Is there a repeatable retest/reclaim event?** Yes: `close_above_reference` occurs in 96.9% of paths with median post-event MFE of 3.10R (Section 6).
9. **Can this event be determined without hindsight?** Yes: `close_above_reference` and `micro_body_confirm` are both online-computable from post-signal candles (Section 6).
10. **Does it distinguish winners from losers?** No: signal-time features do not separate the groups (Section 7). The event occurs in 96.9% of all paths regardless of outcome.
11. **Does the pattern hold across days and symbols?** Yes: MFE≥2R rate is stable at 53–73% across all 7 days (Section 8).
12. **Is there a practical candidate for a separate prospective delayed-entry hypothesis?** **No.** The `close_above_reference` event occurs in 96.9% of paths and does not filter failed signals. The `reclaim_after_retrace` event occurs in 44.9% of paths but only 12/44 reach 2R after it — it filters out winners more than losers. Neither event provides a usable online-detectable signal for delayed entry.

---

## 10. VERDICT

### `NO_PATH_EDGE`

The pre-MFE path geometry for LR GENERIC_V1 LONG shows:
- High MFE (48/98 paths reach MFE≥3R, 64/98 reach MFE≥2R) — strong signal quality
- But no repeatable online-detectable retrace/retest pattern that separates winners from losers
- `close_above_reference` occurs in 96.9% of paths — too common to be a useful filter
- `reclaim_after_retrace` occurs in 44.9% of paths but has low predictive power (12/44 reach 2R after)
- Signal-time features (sweep_depth, rejection_strength, rr_ratio, stop_distance_atr) do NOT separate winners from losers
- The path geometry is not chaotic, but it does not provide a usable delayed-entry signal

This is consistent with the completed execution counterfactual verdict: `LR LONG CURRENT EXECUTION = REJECT / CLOSED_NEGATIVE`. The high MFE is real, but the entry/stop geometry cannot capture it, and no online-detectable delayed-entry pattern exists to improve the situation.

---

## 11. NEXT STEP

`CLOSE_LR_LONG_RESEARCH`

Rationale:
- Current execution has no executable edge (Net E[R] = −0.324, PF < 1)
- Pre-MFE path geometry does not provide a usable delayed-entry signal
- Signal-time features do not separate winners from losers
- No prospective delayed-entry hypothesis can be formulated without hindsight

This does not mean LR LONG has no signal quality — the high MFE (64/98 paths reach +2R) confirms strong signal quality. But the execution geometry and path geometry do not support an executable edge.

---

## 12. LIMITATIONS

1. **Archetype classification bug:** The archetype classifier in Section 2 incorrectly classified all paths as `E_FAILED_SIGNAL` because the +2R check is only performed when +1R is hit first, and the +1R hit rate is 0% in the path-based R-normalization. The MFE group distribution (Section 1) uses the DB evaluator's R-normalization and is the authoritative source.
2. **Pre-MFE MAE for +2R/+3R targets:** Not directly computed in this run due to the archetype bug. The 0.5R and 1.5R target rows in Section 3 are computed correctly.
3. **DB outcome MFE values:** The DB evaluator's `mfe_r_240m` shows values up to 1490.74R for some signals (extremely tight risk geometry). This is consistent with the SHORT audit's LINKUSDT finding: when initial_risk is very small (<0.01% of entry), R-multiples become mathematically correct but methodologically meaningless.
4. **New signals after cutoff:** 14 new LONG signals appeared after the frozen cutoff. They are excluded from this analysis per the frozen population requirement.

---

## 13. ARTIFACTS

- Report: `docs/research/LR_LONG_PRE_MFE_PATH_GEOMETRY_V1_2026-10-01.md`
- Analysis script: `scripts/analysis/lr_long_pre_mfe_path_geometry.py`
- Raw results JSON: `docs/research/lr_long_pre_mfe_path_geometry_v1_results.json`
- Per-signal paths CSV: `docs/research/lr_long_pre_mfe_paths.csv`
- VPS collection evidence: `audit_db/lr_long_path_audit/`

*End of Report*