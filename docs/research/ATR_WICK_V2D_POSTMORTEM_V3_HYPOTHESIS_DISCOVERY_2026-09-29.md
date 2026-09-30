# ATR_WICK V2_D POST-MORTEM & V3 HYPOTHESIS DISCOVERY

**Date:** 2026-09-29
**Status:** Research Complete
**Reviewer:** MiMo-v2.5

---

## ATR_WICK V2_D → V3 DISCOVERY SUMMARY

### Dataset
- **N:** 18,477 observations
- **Period:** 2026-09-24 15:00 UTC – 2026-09-29 16:15 UTC (5.05 days)
- **Symbols:** 94 distinct symbols
- **Source:** ATR_WICK_FILTER_OOS_V2_D prospective OOS data
- **Data integrity:** ✅ No duplicates, no leakage, referential integrity OK

### V2_D Failure Diagnosis

**Primary finding:** The StochRSI filter [0.20, 0.60) does NOT improve ATR_WICK SHORT performance.

**Discovery vs OOS comparison:**
| Metric | Discovery | Prospective OOS | Change |
|---|---|---|---|
| N | 283 | 18,477 | +65x |
| Good/Bad | 9.77 | 0.72 | -92.6% |
| Good % | 44.88% | 17.63% | -27.25% |
| Bad % | 4.59% | 24.45% | +19.86% |
| Avg MFE 60m | 1.57% | 1.17% | -0.40% |
| Avg MAE 60m | 0.49% | 1.18% | +0.69% |

**Discovery/OOS population differences:**
1. **Discovery sample was extremely small (N=283)** - highly susceptible to selection bias
2. **Discovery showed unusually low Bad rate (4.59%)** - likely random variation
3. **Discovery MFE/MAE ratio was 3.2:1** - OOS shows 1:1 (break-even)
4. **Discovery Good rate was 44.88%** - OOS shows 17.63% (2.5x lower)

**Likely explanation:**
The discovery edge was a statistical artifact from a very small sample (N=283). The true underlying edge of ATR_WICK SHORT signals with StochRSI filter is approximately zero or slightly negative.

---

### Search Scope

**Features tested:**
1. StochRSI (5 buckets + 8 fine-grained buckets)
2. Wick geometry (3 buckets)
3. ATR regime (3 buckets)
4. Volume regime (3 buckets)
5. Trend regime (3 buckets)
6. Time-of-day (4 buckets)

**Buckets tested:** 26 total feature buckets
**Interactions tested:** 3 (Wick×ATR, StochRSI×ATR, Wick×Volume)
**Temporal blocks:** 6 days
**Hypothesis count:** 34 total hypotheses tested

---

### Best Stable Segment (if any)

**NO STABLE V3 HYPOTHESIS FOUND**

Despite testing 34 different hypotheses across 6 feature dimensions, no segment demonstrates:

1. **Consistent Good/Bad > 1.0** across all temporal blocks
2. **Statistically significant improvement** over baseline
3. **Logical interpretability** tied to ATR wick rejection mechanism
4. **Cross-sectional stability** across multiple symbols
5. **Neighbor robustness** around threshold boundaries

**Closest candidates (but rejected):**

1. **LOW_ATR regime** (Good/Bad = 0.90, N=6,104)
   - Temporally unstable: ranges from 0.49 (Sep 24) to 1.07 (Sep 28)
   - Cross-sectionally unstable: median symbol Good/Bad = 0.85
   - Rejected: No temporal stability

2. **00-06 UTC time bucket** (Good/Bad = 1.00, N=4,679)
   - Temporally unstable: ranges from 0.81 (Sep 28) to 1.33 (Sep 24)
   - Cross-sectionally unstable: median symbol Good/Bad = 0.92
   - Rejected: No temporal stability

3. **HIGH_WICK × LOW_ATR interaction** (Good/Bad = 0.80, N=784)
   - Temporally unstable: ranges from 0.67 (Sep 25) to 1.12 (Sep 28)
   - Cross-sectionally unstable: only 57 symbols
   - Rejected: Insufficient N and no stability

---

### Feature-by-Feature Analysis

#### 1. StochRSI Analysis
| Bucket | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|
| [0.20, 0.40) | 9,221 | 0.73 | 17.72% | 24.43% |
| [0.40, 0.60) | 9,274 | 0.72 | 17.53% | 24.47% |

**Conclusion:** No meaningful difference between StochRSI sub-ranges. The filter is uniformly unhelpful.

#### 2. Wick Geometry Analysis
| Bucket | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|
| LOW_WICK | 10,258 | 0.70 | 17.48% | 24.81% |
| MID_WICK | 6,058 | 0.75 | 17.79% | 23.72% |
| HIGH_WICK | 2,179 | 0.72 | 17.85% | 24.78% |

**Conclusion:** Wick size has minimal impact on outcomes. MID_WICK shows slightly better Bad rate (23.72% vs 24.81%), but not statistically significant.

#### 3. ATR Regime Analysis
| Bucket | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|
| LOW_ATR | 6,104 | 0.90 | 9.91% | 10.99% |
| MID_ATR | 6,103 | 0.73 | 23.10% | 31.77% |
| HIGH_ATR | 6,288 | 0.65 | 19.94% | 30.45% |

**Conclusion:** LOW_ATR shows best Good/Bad (0.90), but both Good% and Bad% are very low (9.91% and 10.99%). This suggests reduced signal quality overall, not improved selectivity.

#### 4. Volume Regime Analysis
| Bucket | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|
| LOW_VOL | 6,104 | 0.71 | 17.05% | 24.05% |
| MID_VOL | 6,103 | 0.71 | 18.20% | 25.66% |
| HIGH_VOL | 6,288 | 0.75 | 17.76% | 23.71% |

**Conclusion:** Volume regime has minimal impact. HIGH_VOL shows slightly better Good/Bad (0.75), but not statistically significant.

#### 5. Trend Regime Analysis
| Bucket | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|
| STRONG_DOWN | 8,745 | 0.69 | 17.53% | 25.33% |
| FLAT | 1,033 | 0.62 | 10.26% | 16.55% |
| STRONG_UP | 8,717 | 0.76 | 18.70% | 24.54% |

**Conclusion:** STRONG_UP shows best Good/Bad (0.76), but still below 1.0. FLAT shows worst performance (0.62), suggesting trendlessness is harmful for SHORT signals.

#### 6. Time-of-Day Analysis
| Bucket | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|
| 00-06 UTC | 4,679 | 1.00 | 19.56% | 19.60% |
| 06-12 UTC | 4,366 | 0.43 | 12.73% | 29.41% |
| 12-18 UTC | 5,210 | 0.75 | 19.00% | 25.45% |
| 18-24 UTC | 4,252 | 0.81 | 19.00% | 23.47% |

**Conclusion:** 00-06 UTC shows Good/Bad = 1.00, but this is the only bucket at break-even. Other buckets are significantly worse. This suggests time-of-day is not a reliable filter.

---

### Interaction Effects Analysis

#### Wick × ATR Interaction
| Wick | ATR | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|---|
| HIGH_WICK | LOW_ATR | 784 | 0.80 | 8.93% | 11.22% |
| MID_WICK | LOW_ATR | 1,986 | 0.86 | 9.92% | 11.53% |
| LOW_WICK | LOW_ATR | 3,337 | 0.95 | 10.16% | 10.64% |
| HIGH_WICK | MID_ATR | 708 | 0.76 | 24.72% | 32.49% |
| MID_WICK | MID_ATR | 1,968 | 0.84 | 24.64% | 29.27% |
| LOW_WICK | MID_ATR | 3,431 | 0.66 | 21.83% | 33.05% |
| HIGH_WICK | HIGH_ATR | 689 | 0.65 | 20.90% | 32.22% |
| MID_WICK | HIGH_ATR | 2,107 | 0.63 | 18.98% | 30.09% |
| LOW_WICK | HIGH_ATR | 3,497 | 0.67 | 20.30% | 30.23% |

**Best interaction:** LOW_WICK × LOW_ATR (Good/Bad = 0.95, N=3,337)
- Temporally unstable: ranges from 0.81 (Sep 25) to 1.25 (Sep 28)
- Cross-sectionally unstable: only 60 symbols
- Rejected: No temporal stability

#### StochRSI × ATR Interaction
| StochRSI | ATR | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|---|
| [0.20, 0.40) | LOW_ATR | 3,005 | 0.89 | 9.35% | 10.55% |
| [0.40, 0.60) | LOW_ATR | 3,102 | 0.92 | 10.51% | 11.44% |
| [0.20, 0.40) | MID_ATR | 3,047 | 0.75 | 23.93% | 32.10% |
| [0.40, 0.60) | MID_ATR | 3,060 | 0.71 | 22.22% | 31.47% |
| [0.20, 0.40) | HIGH_ATR | 3,175 | 0.66 | 20.00% | 30.30% |
| [0.40, 0.60) | HIGH_ATR | 3,118 | 0.66 | 20.08% | 30.56% |

**Best interaction:** [0.40, 0.60) × LOW_ATR (Good/Bad = 0.92, N=3,102)
- Temporally unstable: ranges from 0.78 (Sep 25) to 1.18 (Sep 28)
- Cross-sectionally unstable: only 59 symbols
- Rejected: No temporal stability

#### Wick × Volume Interaction
| Wick | Volume | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|---|
| MID_WICK | HIGH_VOL | 1,888 | 0.83 | 19.01% | 22.78% |
| LOW_WICK | HIGH_VOL | 3,714 | 0.71 | 17.12% | 24.23% |
| HIGH_WICK | HIGH_VOL | 691 | 0.77 | 17.95% | 23.30% |
| MID_WICK | MID_VOL | 2,009 | 0.73 | 17.72% | 24.24% |
| LOW_WICK | MID_VOL | 3,385 | 0.70 | 18.38% | 26.09% |
| HIGH_WICK | MID_VOL | 713 | 0.67 | 18.79% | 27.91% |
| MID_WICK | LOW_VOL | 2,164 | 0.71 | 17.24% | 24.17% |
| LOW_WICK | LOW_VOL | 3,166 | 0.71 | 17.12% | 24.16% |
| HIGH_WICK | LOW_VOL | 777 | 0.73 | 16.86% | 23.17% |

**Best interaction:** MID_WICK × HIGH_VOL (Good/Bad = 0.83, N=1,888)
- Temporally unstable: ranges from 0.67 (Sep 25) to 1.05 (Sep 28)
- Cross-sectionally unstable: only 89 symbols
- Rejected: No temporal stability

---

### Temporal Stability Analysis

| Date | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|
| 2026-09-24 | 1,484 | 0.49 | 15.36% | 31.20% |
| 2026-09-25 | 3,764 | 0.51 | 14.19% | 27.71% |
| 2026-09-26 | 3,490 | 0.93 | 18.88% | 20.37% |
| 2026-09-27 | 3,504 | 0.72 | 16.67% | 23.00% |
| 2026-09-28 | 3,657 | 0.97 | 23.30% | 24.01% |
| 2026-09-29 | 2,608 | 0.66 | 15.80% | 23.93% |

**Conclusion:** High temporal volatility (Good/Bad ranges from 0.49 to 0.97). No stable trend over time.

---

### Symbol Stability Analysis

**Top 20 symbols by observations:**
| Symbol | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|
| BTWUSDT | 448 | 0.22 | 8.48% | 37.72% |
| NEARUSDT | 446 | 0.67 | 22.42% | 33.63% |
| WLDUSDT | 434 | 0.73 | 24.65% | 33.87% |
| ENAUSDT | 427 | 0.50 | 19.91% | 39.58% |
| ARBUSDT | 425 | 0.62 | 22.35% | 36.24% |
| XPLUSDT | 423 | 0.66 | 23.17% | 35.22% |
| AAVEUSDT | 408 | 0.60 | 17.16% | 28.68% |
| ONDOUSDT | 405 | 0.70 | 21.73% | 31.11% |
| ZECUSDT | 402 | 1.07 | 22.64% | 21.14% |
| SUIUSDT | 401 | 0.78 | 23.19% | 29.68% |
| LINKUSDT | 395 | 0.71 | 18.48% | 26.08% |
| UNIUSDT | 395 | 1.15 | 27.34% | 23.80% |
| DOGEUSDT | 391 | 0.82 | 15.09% | 18.41% |
| LTCUSDT | 385 | 0.93 | 14.81% | 15.84% |
| PUMPFUNUSDT | 384 | 0.46 | 17.45% | 38.02% |
| XRPUSDT | 383 | 0.92 | 14.88% | 16.19% |
| ETHUSDT | 361 | 1.00 | 4.43% | 4.43% |
| AVAXUSDT | 359 | 0.64 | 21.17% | 33.15% |
| QNTUSDT | 358 | 0.79 | 15.36% | 19.55% |
| ADAUSDT | 357 | 0.66 | 17.65% | 26.89% |

**Observations:**
- Only 3 symbols show Good/Bad > 1.0 (ZECUSDT, UNIUSDT, ETHUSDT)
- ETHUSDT has very low N (361) and low Good/Bad (1.00)
- Most symbols show Good/Bad < 1.0
- High cross-sectional variance suggests no consistent edge

---

### Anti-Data-Mining Validation

**Hypotheses tested:** 34
**Significant findings:** 0
**Multiple testing adjustment:** Not required (no findings reached significance)

**Violations detected:** None
- No brute-force grid search
- No post-hoc exclusion of symbols/days
- No outcome definition changes
- No overfitting to specific thresholds

---

### Hypothesis Search Log

| Feature | Buckets Tested | Interactions Tested | Rationale | Result |
|---|---|---|---|---|
| StochRSI | 5 coarse + 8 fine | 2 | Primary V2_D filter | No edge |
| Wick geometry | 3 | 2 | Core ATR_WICK feature | No edge |
| ATR regime | 3 | 2 | Volatility context | No edge |
| Volume regime | 3 | 1 | Confirmation signal | No edge |
| Trend regime | 3 | 0 | Directional context | No edge |
| Time-of-day | 4 | 0 | Session effects | No edge |

---

### Neighbor Robustness Check

**StochRSI fine-grained analysis (0.20-0.60):**
| Bucket | N | Good/Bad | Good% | Bad% |
|---|---|---|---|---|
| 0.20-0.25 | 2,303 | 0.75 | 17.89% | 23.84% |
| 0.25-0.30 | 2,329 | 0.76 | 19.06% | 24.99% |
| 0.30-0.35 | 2,296 | 0.73 | 17.86% | 24.48% |
| 0.35-0.40 | 2,305 | 0.68 | 16.66% | 24.47% |
| 0.40-0.45 | 2,393 | 0.72 | 17.63% | 24.40% |
| 0.45-0.50 | 2,295 | 0.64 | 16.08% | 25.23% |
| 0.50-0.55 | 2,348 | 0.80 | 18.53% | 23.04% |
| 0.55-0.60 | 2,253 | 0.73 | 18.29% | 25.17% |

**Conclusion:** No smooth gradient or clear "sweet spot". Performance is uniformly poor across all StochRSI values.

---

## FINAL RESULT

### `NO_STABLE_V3_HYPOTHESIS`

---

## Recommendation

**Do NOT proceed with V3 hypothesis development for ATR_WICK_REJECTION_SHORT_V1.**

**Rationale:**
1. **No conditional edge found** across 34 tested hypotheses
2. **Temporal instability** - no segment shows consistent performance over time
3. **Cross-sectional instability** - results are driven by random symbol variation
4. **Neighbor instability** - no smooth parameter gradients around any threshold
5. **Mechanism unclear** - no logical explanation for why any segment should outperform

**The fundamental problem:** ATR_WICK SHORT signals with StochRSI filter appear to have zero or slightly negative edge in the current market regime. This is likely a structural issue with the signal definition itself, not a parameter tuning problem.

---

## Answer to Main Question

> **Есть ли в ATR_WICK данных устойчивый и достаточно простой conditional edge, заслуживающий совершенно нового prospective OOS V3, или текущие данные говорят, что дальнейший поиск будет преимущественно data mining?**

**Ответ:** Текущие данные говорят, что дальнейший поиск будет преимущественно data mining.

Ни один из 34 протестированных гипотез не показал устойчивого conditional edge. Все обнаруженные "улучшения" оказались нестабильными во времени и между символами. Продолжение исследования ATR_WICK_REJECTION_SHORT_V1 с целью формирования V3 гипотезы будет представлять собой data mining без разумных оснований для ожидания реального edge.

**Рекомендация:** Закрыть направление ATR_WICK_REJECTION_SHORT_V1 и сосредоточиться на других scanner гипотезах.

---

## Artifacts

- **Main report:** `docs/research/ATR_WICK_V2D_POSTMORTEM_V3_HYPOTHESIS_DISCOVERY_2026-09-29.md`
- **Analysis script:** `scripts/atr_wick_v2d_postmortem.py`
- **V3 specification:** Not created (no hypothesis found)

---

**End of Report**