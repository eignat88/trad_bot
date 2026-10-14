"""Independent closed-candle implementation of original V1 LONG rules."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.scanners.models import SetupCandidate, SetupState
from app.scanners.swing_engine import find_swing_highs

from .closed_context import DetectionContext, build_closed_context
from .identity import source_identity
from .protocol import SCANNER_NAME, SCANNER_VERSION, SOURCE_DIRECTION
from .temporal import validate_detection_temporal


@dataclass(frozen=True)
class V3Detection:
    status: str
    reason: str
    source: dict | None
    context: DetectionContext | None
    candidate: SetupCandidate | None


def detect_v1_closed(context_source, asof) -> V3Detection:
    try:
        context = build_closed_context(context_source, asof)
        candidate = _detect_original_v1(context)
        if candidate is None:
            return V3Detection("NO_SIGNAL", "NO_SETUP", None, context, None)
        source = _source_payload(context, candidate)
        return V3Detection("SIGNAL", "CLOSED_V1_VALIDATED", source, context, candidate)
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        return V3Detection("REJECTED", str(exc), None, None, None)


def _detect_original_v1(context: DetectionContext) -> SetupCandidate | None:
    candles_15m = list(context.candles_15m)
    candles_5m = list(context.candles_5m)
    if len(candles_15m) < 30 or len(candles_5m) < 15:
        return None
    swing_highs = find_swing_highs(candles_15m, 5)
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
        state=SetupState.SETUP_READY,
        market_regime=context.market_regime,
        features={
            "ruleset": "ORIGINAL_V1_NO_RSI_DELTA_FILTER",
            "closed_1h_rsi": context.indicators.rsi,
        },
    )


def _source_payload(
    context: DetectionContext, candidate: SetupCandidate
) -> dict:
    signal_open = candidate.signal_candle_open_time / 1000
    source_close = context.evaluated_at.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Use the signal candle's actual close, not an arbitrary wall-clock time.
    source_close = candidate.detected_at
    source_close = _aligned_5m_close(context)
    validate_detection_temporal(
        source_close, candidate.detected_at, context.evaluated_at
    )
    return {
        "source_setup_id": source_identity(
            context.symbol, candidate.signal_candle_open_time
        ),
        "symbol": context.symbol,
        "instrument_id": None,
        "scanner_name": SCANNER_NAME,
        "scanner_version": SCANNER_VERSION,
        "direction": SOURCE_DIRECTION,
        "signal_candle_open_at": candidate.detected_at.replace(
            minute=0, second=0, microsecond=0
        ),
        "detected_at": candidate.detected_at,
        "source_candle_close_at": source_close,
        "source_fetch_started_at": context.source_fetch_started_at,
        "entry_zone_low": candidate.entry_zone_low,
        "entry_zone_high": candidate.entry_zone_high,
        "reference_price": candidate.reference_price,
        "invalidation_price": candidate.invalidation_price,
        "target_price": candidate.target_1,
        "features": dict(candidate.features),
    }


def _aligned_5m_close(context: DetectionContext):
    latest = context.candles_5m[-1]
    return latest.close_at
