from __future__ import annotations

from dataclasses import replace
from numbers import Real

from app.scanners.models import SetupCandidate


# ---------------------------------------------------------------------------
# Quality-oriented feature weights.
#
# Each entry: feature_key -> (max_weight, reason_label).
#
# Numeric features must be normalised to [0, 1] by the scanner that emits
# them.  A value of 0 means the feature is absent or irrelevant; 1 is the
# best possible reading.  Boolean features are accepted for backwards
# compatibility and receive full weight when True.
#
# Design principles:
#   1. No single feature should dominate (>30 % of max possible score).
#   2. Risk geometry (R:R, stop quality) is the most predictive category.
#   3. Confirmation strengths are graduated, not binary.
#   4. Scanner-identity features (htf_context, liquidity_sweep, etc.) are
#      deliberately excluded — they are detection preconditions, not quality
#      indicators.
# ---------------------------------------------------------------------------
CONFIRMATION_WEIGHTS: dict[str, tuple[float, str]] = {
    # ── Risk geometry (most predictive) ──────────────────────────────────
    "rr_ratio":              (15.0, "RR_EXCELLENT"),
    "stop_distance_atr":     (10.0, "TIGHT_STOP"),

    # ── Confirmation strength (graduated) ────────────────────────────────
    "displacement_strength": (10.0, "DISPLACEMENT"),
    "volume_ratio":          (8.0,  "VOLUME_QUALITY"),
    "rejection_strength":    (8.0,  "REJECTION_QUALITY"),
    "sweep_depth":           (8.0,  "SWEEP_DEPTH"),

    # ── Structural proximity ─────────────────────────────────────────────
    "ob_distance":           (5.0,  "OB_PROXIMITY"),
    "retest_distance":       (5.0,  "RETEST_QUALITY"),
    "pullback_quality":      (5.0,  "PULLBACK_QUALITY"),

    # ── Regime alignment ─────────────────────────────────────────────────
    "regime_alignment":      (5.0,  "REGIME_ALIGNED"),

    # ── Boolean confirmations (only genuinely independent ones) ──────────
    "rsi_confirmation":      (3.0,  "RSI_CONFIRMATION"),
    "volume_spike":          (3.0,  "VOLUME_SPIKE"),

    # ── Squeeze / expansion quality (volatility scanner) ─────────────────
    "squeeze_duration":      (5.0,  "SQUEEZE_DURATION"),
    "expansion_ratio":       (5.0,  "EXPANSION_RATIO"),
    "bb_width_percentile":   (3.0,  "BB_SQUEEZE"),

    # ── Exhaustion quality (momentum scanner) ────────────────────────────
    "exhaustion_magnitude":  (5.0,  "EXHAUSTION_MAGNITUDE"),
    "body_ratio":            (3.0,  "CANDLE_BODY_RATIO"),

    # ── Legacy keys still emitted by some scanners ───────────────────────
    #    These keep older scanners working while they are migrated.
    "htf_context":           (0.0,  "HTF_CONTEXT"),          # deprecated, no weight
    "stop_distance_ok":      (0.0,  "ACCEPTABLE_STOP"),       # deprecated, no weight
    "volume_confirmation":   (5.0,  "VOLUME_CONFIRMATION"),   # graduated in breakout
    "structure_break":       (3.0,  "STRUCTURE_QUALITY"),     # graduated in momentum
    "retest_quality":        (5.0,  "RETEST_QUALITY"),        # graduated in breakout
    "level_touch_count":     (5.0,  "LEVEL_TOUCHES"),         # graduated in S/R
    "trend_alignment":       (0.0,  "TREND_ALIGNMENT"),       # deprecated, no weight
    "pullback_to_ema":       (0.0,  "PULLBACK_TO_EMA"),       # deprecated, no weight
    "rsi_cool":              (0.0,  "RSI_COOL"),              # deprecated, no weight
}


def _quality(value: object) -> float:
    """Return a float in [0, 1] representing confirmation quality."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, Real):
        return max(0.0, min(float(value), 1.0))
    return 0.0


def _reward_to_risk(candidate: SetupCandidate) -> float:
    """Raw R:R ratio (not normalised)."""
    if candidate.invalidation_price is None or candidate.target_1 is None:
        return 0.0
    risk = abs(candidate.reference_price - candidate.invalidation_price)
    if risk <= 0:
        return 0.0
    return abs(candidate.target_1 - candidate.reference_price) / risk


def score_candidate(candidate: SetupCandidate) -> SetupCandidate:
    """Score every scanner on one comparable, quality-sensitive scale.

    The score is *not* a scanner identity fingerprint; it measures how
    attractive the setup is relative to risk geometry and confirmation
    quality.  Each feature contributes at most its configured weight, and
    the total is clamped to [0, 100].
    """
    score = 10.0  # base — every valid setup starts here
    reasons: list[str] = ["VALID_SETUP"]

    # --- feature-based quality contributions ---
    for feature, (weight, reason) in CONFIRMATION_WEIGHTS.items():
        if weight <= 0:
            # deprecated / no-weight key — skip scoring but still allow
            # the feature to exist for backwards compatibility
            continue
        quality = _quality(candidate.features.get(feature))
        if quality > 0:
            score += weight * quality
            reasons.append(reason)

    # --- R:R bonus (already factored into rr_ratio feature when scanners
    #     emit it, but this fallback ensures scanners that still rely on
    #     the raw _reward_to_risk calculation get credit) ---
    rr = _reward_to_risk(candidate)
    has_rr_feature = candidate.features.get("rr_ratio") is not None
    if not has_rr_feature:
        # Fallback for scanners that haven't migrated yet
        if rr >= 2.0:
            score += 10
            reasons.append("RR_STRONG")
        elif rr >= 1.5:
            score += 5
            reasons.append("RR_GOOD")

    # --- ATR-relative stop quality (fallback for non-migrated scanners) ---
    stop_distance = abs(candidate.reference_price - candidate.invalidation_price)
    has_stop_atr = candidate.features.get("stop_distance_atr") is not None
    if not has_stop_atr and candidate.features.get("atr", 0) > 0:
        stop_atr = stop_distance / candidate.features["atr"]
        if stop_atr < 1.5:
            score += 5
            reasons.append("TIGHT_STOP")

    return replace(candidate, score=round(min(score, 100.0), 2), reasons=tuple(reasons))
