# P0 — Score Constructor Inversion: Analysis & Fix Specification

**Priority:** P0 — Critical
**Date:** 2025-07-25
**Status:** Analysis Complete — Awaiting Implementation

---

## 1. Problem Statement

Score 50+ yields **-0.72 R (LONG)** and **-1.01 R (SHORT)** — the opposite of the expected monotonic improvement. High-scoring setups are systematically *worse* than low-scoring ones, meaning the scoring system is actively selecting losing trades.

### Observed Behavior (score_bucket_expectancy view)

| score_bucket | avg_r (LONG) | avg_r (SHORT) | Expected |
|-------------|-------------|---------------|----------|
| <30         | positive?   | positive?     | lowest   |
| 30-39       | —           | —             | low      |
| 40-49       | —           | —             | medium   |
| **50+**     | **-0.72**   | **-1.01**     | **best** |

The score-to-R relationship is **inverted** at the top end.

---

## 2. Root Cause Analysis

### 2.1 Core Problem: Features Measure Detection Preconditions, Not Edge

The `score_candidate()` function in `app/scanners/scoring.py` iterates over `CONFIRMATION_WEIGHTS` and sums `weight × quality` for each feature present in `candidate.features`. The critical flaw:

**Every scanner's boolean features are its own detection preconditions, not independent quality confirmations.**

Example — LIQUIDITY_SWEEP_CHOCH_OB scanner:
```python
features={"htf_context": True, "liquidity_sweep": True, "choch": True,
          "ob_confluence": True, "displacement": True, "retest_quality": True}
```

These are **required conditions** for the scanner to fire. They're always True when the scanner produces a candidate. The scoring system then "confirms" the setup by awarding points for conditions that *already had to be true* for the setup to exist.

**Result:** LIQUIDITY_SWEEP_CHOCH_OB always scores ~75:
```
10 (base) + 10 (htf_context) + 15 (liquidity_sweep) + 15 (choch) +
10 (ob_confluence) + 5 (displacement) + 10 (retest_quality) = 75
```

This is not a quality measure — it's a scanner identity fingerprint.

### 2.2 Feature Weight Distribution Creates Artificial Score Clustering

| Scanner | Features (all boolean) | Generic Score |
|---------|----------------------|---------------|
| LIQUIDITY_SWEEP_CHOCH_OB | htf, sweep, choch, ob, displacement, retest | ~75 |
| BREAKOUT_RETEST | htf, volume, retest, stop_distance | ~40 |
| TREND_PULLBACK | htf, trend, pullback, rsi, stop_distance | ~40 (own scorer) |
| VOLATILITY_COMPRESSION | htf, squeeze, expansion, volume, stop_distance | ~40 |
| SUPPORT_RESISTANCE | htf, level_touches, rejection, structure, volume, stop_distance | ~45 |
| MOMENTUM_EXHAUSTION | htf, new_high/low_failed, weak_continuation, structure, rsi, volume, stop_distance | ~55 |
| LIQUIDITY_REVERSAL | sweep, rejection, structure, volume, stop_distance | ~40 |

**LIQUIDITY_SWEEP_CHOCH_OB dominates the 50+ bucket** because it has the most boolean features (6× True), each with high weights. But having more detection preconditions ≠ better trade quality.

### 2.3 High Score Correlates with High Volatility, Not Edge

During volatile periods:
- More swing points → more liquidity sweeps detected
- More CHOCH patterns → more displacement signals
- More breakouts → more retests
- More features fire → higher scores

But high volatility = wider price ranges = more stop-outs = negative R.

**The score is a proxy for "market was busy" not "setup is good."**

### 2.4 Missing Quality Dimensions

The scoring system ignores every dimension that actually predicts edge:

| Missing Dimension | Why It Matters |
|------------------|----------------|
| R:R ratio quality | Higher R:R setups should score higher (partially in `_reward_to_risk` but only +10 max) |
| ATR-relative stop distance | Tight stops in low volatility = better setups |
| Volume profile quality | Volume confirmation is binary, not graduated |
| Regime alignment strength | TREND_UP + LONG should score more than TREND_UP + SHORT |
| Setup freshness | Recent formations are more reliable |
| Multi-timeframe alignment | HTF trend + LTF entry alignment |
| Historical scanner expectancy | Known-proven scanners should score higher |

### 2.5 Dual Scoring Path (TREND_PULLBACK vs Generic)

The orchestrator preserves scanner-specific scores:
```python
scored = [
    c if c.score > 0 and c.reasons else score_candidate(c)
    for c in all_candidates
]
```

This creates **two incompatible scoring scales**:
- TREND_PULLBACK: 0-100 based on regime, EMA, RSI, candle quality
- All others: 0-100 based on boolean feature count

A score of 60 from TREND_PULLBACK and 60 from LIQUIDITY_SWEEP_CHOCH_OB represent completely different quality levels.

---

## 3. Hypothesis Validation Plan

Before implementing fixes, validate these hypotheses with data:

### 3.1 Feature-Outcome Correlation Audit

Query the database to check correlation between each feature and outcome R:

```sql
-- For each feature, compute avg R when feature is present vs absent
SELECT
    f.key AS feature_name,
    COUNT(*) AS samples,
    ROUND(AVG(o.result_r), 4) AS avg_r,
    ROUND(AVG(o.fee_slippage_adjusted_result_r), 4) AS avg_r_adjusted,
    ROUND(
        COUNT(*) FILTER (WHERE o.first_event IN ('TP1', 'TP2'))::numeric /
        NULLIF(COUNT(*) FILTER (WHERE o.entry_touched), 0), 4
    ) AS win_rate
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
CROSS JOIN LATERAL jsonb_each_text(s.features) AS f(key, value)
WHERE o.entry_touched = true
GROUP BY f.key
HAVING COUNT(*) >= 10
ORDER BY avg_r DESC;
```

**Expected finding:** Most boolean features will show flat or negative correlation with R.

### 3.2 Per-Scanner Score Distribution

```sql
SELECT
    scanner_name,
    direction,
    CASE
        WHEN s.score < 30 THEN '<30'
        WHEN s.score < 40 THEN '30-39'
        WHEN s.score < 50 THEN '40-49'
        ELSE '50+'
    END AS bucket,
    COUNT(*) AS samples,
    ROUND(AVG(o.result_r), 4) AS avg_r,
    ROUND(AVG(o.fee_slippage_adjusted_result_r), 4) AS avg_r_adjusted
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
WHERE o.entry_touched = true
GROUP BY scanner_name, direction, bucket
HAVING COUNT(*) >= 5
ORDER BY scanner_name, direction, bucket;
```

**Expected finding:** LIQUIDITY_SWEEP_CHOCH_OB will dominate 50+ with negative R.

### 3.3 Volatility-Feature Correlation

```sql
-- Check if high-score setups cluster in high-volatility regimes
SELECT
    s.market_regime,
    CASE
        WHEN s.score < 30 THEN '<30'
        WHEN s.score < 50 THEN '30-49'
        ELSE '50+'
    END AS bucket,
    COUNT(*) AS samples,
    ROUND(AVG(o.result_r), 4) AS avg_r
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
WHERE o.entry_touched = true
GROUP BY s.market_regime, bucket
HAVING COUNT(*) >= 5
ORDER BY s.market_regime, bucket;
```

---

## 4. Fix Specification

### Phase 1: Feature Decoupling (Immediate)

**Goal:** Stop measuring "scanner identity" and start measuring "setup quality."

#### 4.1 Redefine Feature Semantics

Current: `features` = scanner detection preconditions (always True when scanner fires)
New: `features` = quality measurements (varies per setup)

**Changes per scanner:**

| Scanner | Remove (detection preconditions) | Add (quality measurements) |
|---------|----------------------------------|---------------------------|
| LIQUIDITY_SWEEP_CHOCH_OB | `htf_context`, `liquidity_sweep`, `choch`, `ob_confluence` | `sweep_depth` (% below level), `displacement_strength` (body/range ratio), `ob_distance` (price distance from OB center), `rr_ratio` (calculated R:R) |
| BREAKOUT_RETEST | `htf_context`, `breakout_level` | `breakout_volume_ratio` (volume at breakout / avg), `retest_distance` (how close to retest zone center), `rr_ratio` |
| TREND_PULLBACK | `htf_context`, `trend_alignment`, `pullback_to_ema` | Keep current quality features (already good) |
| VOLATILITY_COMPRESSION | `htf_context`, `squeeze_detected`, `volatility_expansion` | `squeeze_duration` (bars in squeeze), `expansion_ratio` (recent/prev range), `bb_width_percentile` |
| SUPPORT_RESISTANCE | `htf_context`, `level_touches`, `rejection` | `level_touch_count`, `rejection_strength` (wick/body ratio), `rr_ratio` |
| MOMENTUM_EXHAUSTION | `htf_context`, `new_high_failed`, `weak_continuation` | `exhaustion_magnitude` (how far past prior swing), `body_ratio` (body/range), `rr_ratio` |
| LIQUIDITY_REVERSAL | `liquidity_sweep`, `rejection`, `structure_confirmation` | `sweep_depth`, `rejection_strength`, `rr_ratio` |

#### 4.2 Revise CONFIRMATION_WEIGHTS

Replace the current weights with quality-oriented weights:

```python
CONFIRMATION_WEIGHTS = {
    # Risk geometry (most predictive)
    "rr_ratio": (15.0, "RR_QUALITY"),
    "stop_distance_atr": (10.0, "STOP_QUALITY"),
    
    # Confirmation strength (graduated, not boolean)
    "displacement_strength": (10.0, "DISPLACEMENT"),
    "volume_ratio": (8.0, "VOLUME_QUALITY"),
    "rejection_strength": (8.0, "REJECTION_QUALITY"),
    "sweep_depth": (8.0, "SWEEP_DEPTH"),
    
    # Structural alignment
    "ob_distance": (5.0, "OB_PROXIMITY"),
    "retest_distance": (5.0, "RETEST_QUALITY"),
    "pullback_quality": (5.0, "PULLBACK_QUALITY"),
    
    # Regime alignment
    "regime_alignment": (5.0, "REGIME_ALIGNED"),
    
    # Boolean confirmations (only when genuinely independent)
    "rsi_confirmation": (3.0, "RSI_CONFIRMATION"),
    "volume_spike": (3.0, "VOLUME_SPIKE"),
}
```

#### 4.3 Update score_candidate() Function

```python
def score_candidate(candidate: SetupCandidate) -> SetupCandidate:
    """Score based on setup quality, not scanner identity."""
    score = 10.0  # base
    reasons: list[str] = ["VALID_SETUP"]
    
    for feature, (weight, reason) in CONFIRMATION_WEIGHTS.items():
        quality = _quality(candidate.features.get(feature))
        if quality > 0:
            score += weight * quality
            reasons.append(reason)
    
    # R:R bonus (significant predictor)
    rr = _reward_to_risk(candidate)
    if rr >= 2.0:
        score += 10  # was 5+5 for 1.5 and 2.0
        reasons.append("RR_EXCELLENT")
    elif rr >= 1.5:
        score += 5
        reasons.append("RR_GOOD")
    
    # ATR-relative stop quality bonus
    stop_distance = abs(candidate.reference_price - candidate.invalidation_price)
    if candidate.features.get("atr", 0) > 0:
        stop_atr_ratio = stop_distance / candidate.features["atr"]
        if stop_atr_ratio < 1.5:
            score += 5
            reasons.append("TIGHT_STOP")
    
    return replace(candidate, score=round(min(score, 100.0), 2), reasons=tuple(reasons))
```

### Phase 2: Scanner Feature Overhaul (Next Sprint)

Each scanner must emit quality features instead of detection preconditions.

#### 4.4 LIQUIDITY_SWEEP_CHOCH_OB Changes

**Before:**
```python
features={"htf_context": True, "liquidity_sweep": True, "choch": True,
          "ob_confluence": True, "displacement": True, "retest_quality": True}
```

**After:**
```python
features={
    "sweep_depth": min(sweep_depth_pct / 0.02, 1.0),  # deeper sweep = higher quality
    "displacement_strength": body_ratio,  # how strong the displacement candle
    "ob_distance": 1.0 - abs(current_price - ob_center) / ob_range,  # proximity to OB center
    "rr_ratio": min(rr / 3.0, 1.0),  # normalized R:R
    "stop_distance_atr": 1.0 - min(stop_atr / 2.0, 1.0),  # tighter stop = higher score
    "volume_spike": volume_ratio > 1.5,
    "rsi_confirmation": rsi_quality,  # graduated, not boolean
}
```

#### 4.5 BREAKOUT_RETEST Changes

**Before:**
```python
features={"htf_context": True, "breakout_level": resistance,
          "volume_confirmation": ..., "retest_quality": ..., "stop_distance_ok": True}
```

**After:**
```python
features={
    "volume_ratio": min(breakout_vol / avg_vol / 2, 1.0),  # keep graduated
    "retest_distance": 1.0 - abs(current_price - resistance) / (resistance * retest_margin),
    "rr_ratio": min(rr / 3.0, 1.0),
    "stop_distance_atr": 1.0 - min(stop_atr / 2.0, 1.0),
    "regime_alignment": 1.0 if market_regime == "TREND_UP" and direction == "LONG" else 0.3,
}
```

#### 4.6 VOLATILITY_COMPRESSION Changes

**Before:**
```python
features={"htf_context": True, "squeeze_detected": True,
          "volatility_expansion": True, "volume_spike": ..., "stop_distance_ok": True}
```

**After:**
```python
features={
    "squeeze_duration": min(squeeze_bars / 20, 1.0),  # longer squeeze = better
    "expansion_ratio": min((avg_recent / avg_prev - 1) / 2, 1.0),  # stronger expansion
    "bb_width_percentile": 1.0 - (bb_width / threshold),  # tighter squeeze = higher
    "volume_ratio": min(volume / avg_volume / 1.3, 1.0),
    "rr_ratio": min(rr / 3.0, 1.0),
    "stop_distance_atr": 1.0 - min(stop_atr / 2.0, 1.0),
}
```

### Phase 3: Score Normalization (After Phase 2)

#### 4.7 Cross-Scanner Score Calibration

After the feature overhaul, scores will be more meaningful but may still cluster differently per scanner. Add calibration:

```python
# In orchestrator.py, after scoring all candidates
def _calibrate_scores(self, candidates: list[SetupCandidate]) -> list[SetupCandidate]:
    """Normalize scores to [0, 100] across scanners."""
    if not candidates:
        return candidates
    
    # Group by scanner
    by_scanner: dict[str, list[SetupCandidate]] = {}
    for c in candidates:
        by_scanner.setdefault(c.scanner_name, []).append(c)
    
    calibrated = []
    for scanner_name, scanner_candidates in by_scanner.items():
        scores = [c.score for c in scanner_candidates]
        if len(scores) < 2:
            calibrated.extend(scanner_candidates)
            continue
        
        min_score = min(scores)
        max_score = max(scores)
        score_range = max_score - min_score
        
        if score_range > 0:
            for c in scanner_candidates:
                normalized = ((c.score - min_score) / score_range) * 80 + 10  # [10, 90]
                calibrated.append(replace(c, score=round(normalized, 2)))
        else:
            calibrated.extend(scanner_candidates)
    
    return calibrated
```

### Phase 4: Score-to-Threshold Recalibration

#### 4.8 Update Minimum Score Threshold

Currently in `orchestrator.py`:
```python
if c.score >= 20:
    valid.append(c)
```

After the fix, score semantics change. The threshold needs recalibration:
- Run backtest with new scoring
- Find optimal threshold that maximizes Sharpe ratio
- Likely will be higher (40-50) since scores will be more spread

#### 4.9 Update Expectancy Filter Integration

The `ExpectancyFilter` already works at scanner/direction level. After fixing scoring, consider adding a **score-based expectancy filter**:

```sql
-- New view: expectancy by score bucket per scanner
CREATE OR REPLACE VIEW dds.score_calibration AS
SELECT
    scanner_name,
    direction,
    CASE
        WHEN s.score < 20 THEN '0-19'
        WHEN s.score < 40 THEN '20-39'
        WHEN s.score < 60 THEN '40-59'
        WHEN s.score < 80 THEN '60-79'
        ELSE '80-100'
    END AS score_bucket,
    COUNT(*) AS samples,
    ROUND(AVG(o.result_r), 4) AS avg_r,
    ROUND(AVG(o.fee_slippage_adjusted_result_r), 4) AS avg_r_adjusted,
    ROUND(
        COUNT(*) FILTER (WHERE o.first_event IN ('TP1', 'TP2'))::numeric /
        NULLIF(COUNT(*) FILTER (WHERE o.entry_touched), 0), 4
    ) AS win_rate
FROM dds.signal_outcome o
JOIN dds.scanner_setup s ON s.setup_id = o.setup_id
WHERE o.entry_touched = true
GROUP BY scanner_name, direction, score_bucket
HAVING COUNT(*) >= 5;
```

---

## 5. Implementation Checklist

### Immediate (Phase 1 — Feature Decoupling)
- [ ] **Audit all 7 scanners' feature emission** — document which features are preconditions vs quality
- [ ] **Revise CONFIRMATION_WEIGHTS** in `app/scanners/scoring.py`
- [ ] **Update score_candidate()** to use graduated quality scores
- [ ] **Update _outcome_enriched view** in `app/db/schema.sql` with new score buckets
- [ ] **Run hypothesis validation queries** (Section 3) to confirm root cause

### Short-term (Phase 2 — Scanner Feature Overhaul)
- [ ] **LIQUIDITY_SWEEP_CHOCH_OB**: Replace boolean features with quality metrics
- [ ] **BREAKOUT_RETEST**: Replace boolean features with quality metrics
- [ ] **VOLATILITY_COMPRESSION**: Replace boolean features with quality metrics
- [ ] **SUPPORT_RESISTANCE**: Replace boolean features with quality metrics
- [ ] **MOMENTUM_EXHAUSTION**: Replace boolean features with quality metrics
- [ ] **LIQUIDITY_REVERSAL**: Replace boolean features with quality metrics
- [ ] **TREND_PULLBACK**: Already uses quality scoring — verify alignment with new weights
- [ ] **Update all unit tests** in `tests/test_scanner_runtime.py`, `tests/test_trend_pullback_modernized.py`

### Medium-term (Phase 3 — Score Normalization)
- [ ] **Implement cross-scanner calibration** in orchestrator
- [ ] **Run backtest** with new scoring to validate monotonicity
- [ ] **Recalibrate score threshold** (currently 20, likely 40-50)
- [ ] **Update DB views** for new score bucket ranges

### Long-term (Phase 4 — Score-based Expectancy)
- [ ] **Create score_calibration view** in schema.sql
- [ ] **Implement score-based filtering** in ExpectancyFilter
- [ ] **Add score decay** for stale setups (score decreases over time)
- [ ] **Add regime-specific score adjustments**

---

## 6. Success Metrics

After implementation, validate:

1. **Monotonicity:** avg_r should increase monotonically with score bucket
   - `avg_r(<30) < avg_r(30-39) < avg_r(40-49) < avg_r(50+)`
   
2. **No scanner dominance:** No single scanner should dominate any score bucket
   - Each scanner should be distributed across all buckets
   
3. **Positive R at top:** Score 50+ should have positive avg_r for both LONG and SHORT
   - Target: avg_r > 0.1 for 50+ bucket
   
4. **Feature correlation:** No single feature should contribute >30% of total score
   - Score should be distributed across multiple quality dimensions

---

## 7. Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Feature overhaul breaks existing setups | High | Run backtest before/after, compare trade counts |
| Score recalibration causes threshold to miss good setups | Medium | Keep paper mode bootstrap, monitor expectancy |
| Cross-scanner calibration introduces bias | Medium | Validate with out-of-sample data |
| DB view migration loses historical data | Low | Views are read-only, schema changes are additive |

---

## 8. Files to Modify

| File | Change Type | Priority |
|------|-------------|----------|
| `app/scanners/scoring.py` | Rewrite CONFIRMATION_WEIGHTS and score_candidate() | P0 |
| `app/scanners/liquidity_sweep_choch.py` | Replace boolean features with quality metrics | P0 |
| `app/scanners/breakout_retest.py` | Replace boolean features with quality metrics | P0 |
| `app/scanners/volatility_compression.py` | Replace boolean features with quality metrics | P1 |
| `app/scanners/support_resistance.py` | Replace boolean features with quality metrics | P1 |
| `app/scanners/momentum_exhaustion.py` | Replace boolean features with quality metrics | P1 |
| `app/scanners/liquidity_reversal.py` | Replace boolean features with quality metrics | P1 |
| `app/scanners/orchestrator.py` | Add score calibration, update threshold | P1 |
| `app/db/schema.sql` | Update _outcome_enriched view, add score_calibration view | P1 |
| `tests/test_scanner_runtime.py` | Update score assertions | P1 |
| `tests/test_trend_pullback_modernized.py` | Verify compatibility | P2 |
| `tests/test_scanner_backtest.py` | Update score expectations | P2 |

---

## 9. Dependencies

- **Data validation (Phase 3.1-3.3 queries)** must complete before feature overhaul
- **Backtest framework** must support new feature schema for validation
- **Paper trading** must continue collecting data during transition
- **DB migration** must be backward-compatible (additive view changes only)

---

## 10. Timeline Estimate

| Phase | Duration | Owner |
|-------|----------|-------|
| Phase 1: Feature Decoupling | 2-3 days | — |
| Phase 2: Scanner Overhaul | 5-7 days | — |
| Phase 3: Score Normalization | 2-3 days | — |
| Phase 4: Score-based Expectancy | 3-5 days | — |
| **Total** | **12-18 days** | — |

---

*This document supersedes informal observations. All hypotheses require database validation before implementation.*
