from __future__ import annotations

from dataclasses import replace
from numbers import Real

from app.scanners.models import SetupCandidate


# Shared, scanner-independent confirmations. Boolean values receive the full
# weight; a numeric value in [0, 1] represents confirmation quality.
CONFIRMATION_WEIGHTS = {
    "htf_context": (10.0, "HTF_CONTEXT"),
    "trend_alignment": (5.0, "TREND_ALIGNMENT"),
    "pullback_quality": (10.0, "PULLBACK_QUALITY"),
    "rsi_confirmation": (5.0, "RSI_CONFIRMATION"),
    "liquidity_sweep": (15.0, "LIQUIDITY_SWEEP"),
    "choch": (15.0, "CHOCH"),
    "ob_confluence": (10.0, "OB_CONFLUENCE"),
    "retest_quality": (10.0, "RETEST_QUALITY"),
    "regime_confirmation": (5.0, "REGIME_CONFIRMATION"),
    "structure_break": (5.0, "STRUCTURE_QUALITY"),
    "weak_continuation": (5.0, "WEAK_CONTINUATION"),
    "rsi_extreme": (5.0, "RSI_CONFIRMATION"),
    "stop_distance_ok": (5.0, "ACCEPTABLE_STOP"),
    "volume_confirmation": (5.0, "VOLUME_CONFIRMATION"),
    "volume_spike": (5.0, "VOLUME_SPIKE"),
    "displacement": (5.0, "DISPLACEMENT"),
}


def _quality(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, Real):
        return max(0.0, min(float(value), 1.0))
    return 0.0


def _reward_to_risk(candidate: SetupCandidate) -> float:
    if candidate.invalidation_price is None or candidate.target_1 is None:
        return 0.0
    risk = abs(candidate.reference_price - candidate.invalidation_price)
    if risk <= 0:
        return 0.0
    return abs(candidate.target_1 - candidate.reference_price) / risk


def score_candidate(candidate: SetupCandidate) -> SetupCandidate:
    """Score every scanner on one comparable, quality-sensitive scale."""
    score = 10.0
    reasons: list[str] = ["VALID_SETUP"]

    for feature, (weight, reason) in CONFIRMATION_WEIGHTS.items():
        quality = _quality(candidate.features.get(feature))
        if quality:
            score += weight * quality
            reasons.append(reason)

    rr = _reward_to_risk(candidate)
    if rr >= 1.5:
        score += 5
        reasons.append("RR_1_5")
    if rr >= 2.0:
        score += 5
        reasons.append("RR_2")

    return replace(candidate, score=round(min(score, 100.0), 2), reasons=tuple(reasons))
