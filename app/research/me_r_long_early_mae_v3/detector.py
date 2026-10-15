"""Independent closed-candle implementation of original V1 LONG rules."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.scanners.models import SetupCandidate, SetupState
from app.scanners.momentum_exhaustion_reverse_long_v1 import (
    MomentumExhaustionReverseLongV1Scanner,
)
from app.scanners.swing_engine import find_swing_highs

from .closed_context import (
    DetectionContext,
    build_closed_context,
    to_frozen_v1_candles,
)
from .identity import source_identity
from .protocol import SCANNER_NAME, SCANNER_VERSION, SOURCE_DIRECTION, validate_measured_source_availability
from .temporal import validate_detection_temporal


@dataclass(frozen=True)
class V3Detection:
    status: str
    reason: str
    source: dict | None
    context: DetectionContext | None
    candidate: SetupCandidate | None


def detect_v1_closed(
    context_source,
    asof: datetime,
    source_fetch_completed_at: datetime | None = None,
) -> V3Detection:
    try:
        context = build_closed_context(
            context_source, asof, source_fetch_completed_at
        )
        candidate = _detect_original_v1(context)
        if candidate is None:
            return V3Detection("NO_SIGNAL", "NO_SETUP", None, context, None)
        source = _source_payload(context, candidate)
        return V3Detection("SIGNAL", "CLOSED_V1_VALIDATED", source, context, candidate)
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        return V3Detection("REJECTED", str(exc), None, None, None)


def v1_reference_candidate(context: DetectionContext) -> SetupCandidate | None:
    """Run the frozen V1 scanner on an equivalent completed-candle context."""
    scanner = MomentumExhaustionReverseLongV1Scanner()
    frozen_context = _frozen_context(context)
    results = scanner.scan(frozen_context)
    if not results:
        return None
    candidates = [
        candidate for candidate in results
        if candidate.scanner_name == scanner.name
        and candidate.scanner_version == scanner.version
        and candidate.direction == SOURCE_DIRECTION
    ]
    return candidates[0] if len(candidates) == 1 else None


def _detect_original_v1(context: DetectionContext) -> SetupCandidate | None:
    candles_15m = list(context.candles_15m)
    candles_5m = list(context.candles_5m)
    if len(candles_15m) < 30 or len(candles_5m) < 15:
        return None

    swing_highs = find_swing_highs(to_frozen_v1_candles(context.candles_15m), 5)
    if len(swing_highs) < 2:
        return None
    prev_high = swing_highs[-2].price

    recent_high = max(c.high for c in candles_5m[-5:])
    if recent_high <= prev_high:
        return None

    last = candles_5m[-1]
    current_price = last.close
    if current_price > prev_high * 1.003:
        return None
    if last.close > last.open:
        return None
    candle_range = last.high - last.low
    body = abs(last.close - last.open)
    if candle_range > 0 and body / candle_range > 0.7:
        return None
    if context.indicators.rsi < 65:
        return None

    reference = MomentumExhaustionReverseLongV1Scanner()
    features = reference._build_long_features(
        candles_5m,
        prev_high,
        recent_high,
        current_price,
        current_price * 0.975,
        current_price * 1.03,
        context.indicators.atr if context.indicators.atr > 0 else current_price * 0.015,
        context.indicators.rsi,
    )
    features.update(
        {
            "stop_loss_pct": 2.5,
            "take_profit_pct": 3.0,
            "hold_minutes": 240,
            "source_scanner": "MOMENTUM_EXHAUSTION",
            "source_direction": "SHORT",
            "ruleset": "ORIGINAL_V1_NO_RSI_DELTA_FILTER",
        }
    )

    return SetupCandidate(
        scanner_name=SCANNER_NAME,
        scanner_version=SCANNER_VERSION,
        symbol=context.symbol,
        direction=SOURCE_DIRECTION,
        detected_at=context.evaluated_at,
        setup_started_at=context.source_fetch_started_at,
        signal_candle_open_time=int(last.open_at.timestamp() * 1000),
        reference_price=recent_high,
        entry_zone_low=current_price * 0.998,
        entry_zone_high=current_price * 1.001,
        invalidation_price=current_price * 0.975,
        target_1=current_price * 1.03,
        target_2=None,
        state=SetupState.SETUP_READY,
        market_regime=context.market_regime,
        features=features,
    )


def _source_payload(
    context: DetectionContext, candidate: SetupCandidate
) -> dict:
    signal = context.signal_candle
    if context.source_fetch_completed_at is None:
        raise ValueError("SOURCE_FETCH_COMPLETION_PROVENANCE_REQUIRED")
    validate_measured_source_availability(
        signal.close_at,
        context.source_fetch_completed_at,
        candidate.detected_at,
    )
    temporal = validate_detection_temporal(
        signal.close_at,
        candidate.detected_at,
        context.source_fetch_completed_at,
    )
    open_at = signal.open_at
    if int(open_at.timestamp() * 1000) != candidate.signal_candle_open_time:
        raise ValueError("SIGNAL_CANDLE_IDENTITY_MISMATCH")
    if signal.close_at != open_at.replace() + (signal.close_at - signal.open_at):
        raise ValueError("SIGNAL_CANDLE_CLOSE_MISMATCH")

    return {
        "source_setup_id": source_identity(
            context.symbol, SCANNER_NAME, open_at
        ),
        "symbol": context.symbol,
        "instrument_id": None,
        "scanner_name": SCANNER_NAME,
        "scanner_version": SCANNER_VERSION,
        "direction": SOURCE_DIRECTION,
        "source_candle_open_at": open_at,
        "source_candle_close_at": signal.close_at,
        "detected_at": candidate.detected_at,
        "source_fetch_started_at": context.source_fetch_started_at,
        "source_fetch_completed_at": context.source_fetch_completed_at,
        "source_available_at": temporal.source_available_at,
        "entry_zone_low": candidate.entry_zone_low,
        "entry_zone_high": candidate.entry_zone_high,
        "reference_price": candidate.reference_price,
        "invalidation_price": candidate.invalidation_price,
        "target_price": candidate.target_1,
        "features": dict(candidate.features),
    }


def _frozen_context(context: DetectionContext):
    from app.scanners.models import IndicatorSnapshot, MarketContext, MarketLevels

    return MarketContext(
        symbol=context.symbol,
        candles_5m=tuple(to_frozen_v1_candles(context.candles_5m)),
        candles_15m=tuple(to_frozen_v1_candles(context.candles_15m)),
        candles_1h=tuple(to_frozen_v1_candles(context.candles_1h)),
        candles_4h=(),
        indicators=context.indicators,
        market_regime=context.market_regime,
        levels=MarketLevels(),
        evaluated_at=context.evaluated_at,
        source_fetch_started_at=context.source_fetch_started_at,
    )
