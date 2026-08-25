from __future__ import annotations

from app.scanners.models import SetupCandidate


def score_candidate(candidate: SetupCandidate) -> SetupCandidate:
    score = 0.0
    reasons: list[str] = []
    features = candidate.features

    if features.get("htf_context"):
        score += 20
        reasons.append("HTF_CONTEXT")
    if features.get("liquidity_sweep"):
        score += 20
        reasons.append("LIQUIDITY_SWEEP")
    if features.get("choch"):
        score += 20
        reasons.append("CHOCH")
    if features.get("ob_confluence"):
        score += 15
        reasons.append("OB_CONFLUENCE")
    if features.get("retest_quality"):
        score += 10
        reasons.append("RETEST_QUALITY")
    if features.get("regime_confirmation"):
        score += 10
        reasons.append("REGIME_CONFIRMATION")
    if features.get("stop_distance_ok"):
        score += 5
        reasons.append("ACCEPTABLE_STOP")
    if features.get("volume_spike"):
        score += 5
        reasons.append("VOLUME_SPIKE")
    if features.get("displacement"):
        score += 5
        reasons.append("DISPLACEMENT")

    score = min(score, 100.0)

    return SetupCandidate(
        setup_id=candidate.setup_id,
        scanner_name=candidate.scanner_name,
        scanner_version=candidate.scanner_version,
        symbol=candidate.symbol,
        direction=candidate.direction,
        htf_timeframe=candidate.htf_timeframe,
        setup_timeframe=candidate.setup_timeframe,
        entry_timeframe=candidate.entry_timeframe,
        detected_at=candidate.detected_at,
        setup_started_at=candidate.setup_started_at,
        reference_price=candidate.reference_price,
        entry_zone_low=candidate.entry_zone_low,
        entry_zone_high=candidate.entry_zone_high,
        invalidation_price=candidate.invalidation_price,
        target_1=candidate.target_1,
        target_2=candidate.target_2,
        score=score,
        market_regime=candidate.market_regime,
        reasons=tuple(reasons),
        features=candidate.features,
        source_candle_ids=candidate.source_candle_ids,
        state=candidate.state,
    )
