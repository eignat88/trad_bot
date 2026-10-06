"""Prospective HTF key-level reaction detector for HTF_KEYLEVEL_SR_BREAK_POINT_B_V1.

This module is intentionally isolated from production scanner semantics.  It
implements only deterministic, past-only research geometry for:

HTF S/R interaction -> reaction -> local structure break -> Point B retracement.

The core rule is KEY_LEVEL_TOUCH != OOS_SIGNAL: a touch produces a
setup_event_id and a matched BASELINE cohort, while POINT_B is emitted only
after the frozen retracement condition is objectively completed.

All calculations consume a closed 1H level history followed by sequential
execution candles.  A signal at candle T is a pure function of the input prefix
ending at T; changing later candles cannot alter an already-emitted result.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from app.models import Candle
from app.scanners.models import MarketContext, SetupCandidate, SetupState

EXPERIMENT_ID = "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"
BASELINE_EXPERIMENT_ID = "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE"
HYPOTHESIS_ID = "HTF_KEYLEVEL_POINT_B_EDGE_V1"
SCANNER_NAME = "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1"
HTF_TIMEFRAME = "1H"
EXECUTION_TIMEFRAME = "5M"

# Frozen broad protocol constants.  They are deliberately generous to avoid
# hidden optimization: ATR units only normalize geometry and never score setups.
KEY_LEVEL_TOUCH_TOLERANCE_ATR = 1.0
REACTION_DISTANCE_ATR = 0.75
REACTION_BODY_MIN_ATR = 0.25
STRUCTURE_BREAK_DISTANCE_ATR = 0.25
POINT_B_RETRACE_MIN = 0.25
POINT_B_RETRACE_MAX = 0.75
FALLBACK_ATR_FRACTION = 0.02
ATR_PERIOD = 14
LOCAL_STRUCTURE_LOOKBACK = 12
KEY_LEVEL_MIN_TOUCHES = 2
LEVEL_TOUCH_COUNT_TOLERANCE_ATR = 0.25


@dataclass(frozen=True)
class KeyLevel:
    """Persistent HTF support/resistance level known from prior history."""

    price: float
    level_type: str
    index: int
    timestamp: int
    touches: int
    strength: float
    available_timestamp: int

    @property
    def age(self) -> int:
        return self.index


@dataclass(frozen=True)
class DetectionResult:
    """Immutable result of one deterministic detector run."""

    signal: SetupCandidate | None = None
    baseline: SetupCandidate | None = None
    stages: tuple[str, ...] = ()
    funnel: dict[str, int] | None = None
    setup_event_id: str | None = None
    reason: str | None = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_closed(execution_candle: Candle, current: datetime) -> bool:
    """Use only the latest fully closed 5m candle."""
    open_ms = execution_candle.timestamp
    close_ms = open_ms + 5 * 60_000
    return _as_utc(current) >= datetime.fromtimestamp(close_ms / 1000, timezone.utc)


def _historical_atr(candles: Sequence[Candle], fallback_price: float) -> float:
    """Wilder ATR using only closed execution candles."""
    if len(candles) < ATR_PERIOD + 1:
        value = fallback_price * FALLBACK_ATR_FRACTION
        return float(value)
    true_ranges = []
    for i in range(1, len(candles)):
        candle = candles[i]
        previous_close = candles[i - 1].close
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    atr = sum(true_ranges[:ATR_PERIOD]) / ATR_PERIOD
    for true_range in true_ranges[ATR_PERIOD:]:
        atr = (atr * (ATR_PERIOD - 1) + true_range) / ATR_PERIOD
    return float(atr)


def _causal_atr_series(closed: Sequence[Candle]) -> list[float]:
    """Return one Wilder ATR value per causal execution-candle prefix.

    ``atr_by_index[i]`` is computed only from ``closed[: i + 1]``.  Event
    stages must consume ``atr_by_index[index]`` when evaluating the decision
    candle at ``index`` so that later closed candles cannot change an
    already-confirmed historical event.
    """
    return [
        _historical_atr(closed[: index + 1], float(closed[index].close))
        for index in range(len(closed))
    ]


def _candle_time(candle: Candle) -> datetime:
    return datetime.fromtimestamp(candle.timestamp / 1000, timezone.utc)


def _stable_id(*parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _find_htf_key_levels(
    htf_candles: Sequence[Candle],
    execution_candles: Sequence[Candle],
    level_atr: float,
    direction: str | None = None,
) -> tuple[KeyLevel | None, tuple[float, float, float] | None]:
    """Build support/resistance levels from closed HTF swing history.

    Touch counts and level formation use only HTF candles strictly older than
    the first execution candle.  This preserves the production S/R concept
    without importing the future-confirmed swing_engine into eligibility.
    """
    if len(htf_candles) < LOCAL_STRUCTURE_LOOKBACK * 2 + 2:
        return None, None
    if not execution_candles:
        return None, None

    # Exclude the current in-progress 1H candle.  Levels are formed only from
    # HTF candles closed strictly before the first execution candle.
    htf_end = len(htf_candles)
    if htf_end > 1 and _candle_time(htf_candles[-1]) >= _candle_time(execution_candles[0]):
        htf_end -= 1
    if htf_end < LOCAL_STRUCTURE_LOOKBACK * 2 + 2:
        return None, None
    candidates: list[KeyLevel] = []
    lookback = 4
    for i in range(lookback, htf_end - lookback):
        candle = htf_candles[i]
        is_high = all(htf_candles[i - j].high <= candle.high for j in range(1, lookback + 1)) and all(
            htf_candles[i + j].high <= candle.high for j in range(1, lookback + 1)
        )
        is_low = all(htf_candles[i - j].low >= candle.low for j in range(1, lookback + 1)) and all(
            htf_candles[i + j].low >= candle.low for j in range(1, lookback + 1)
        )
        if not (is_high or is_low):
            continue
        level_type = "resistance" if is_high else "support"
        price = float(candle.high if is_high else candle.low)
        tolerance = max(price * 0.003, level_atr * LEVEL_TOUCH_COUNT_TOLERANCE_ATR)
        touches = sum(
            1
            for prior in htf_candles[:i]
            if abs(prior.high - price) <= tolerance or abs(prior.low - price) <= tolerance
        )
        if touches < KEY_LEVEL_MIN_TOUCHES:
            continue
        candidates.append(
            KeyLevel(
                price=price,
                level_type=level_type,
                index=i,
                timestamp=int(candle.timestamp),
                touches=touches,
                strength=min(1.0, touches / 5.0),
                available_timestamp=int(htf_candles[i + lookback].timestamp) + 60 * 60_000,
            )
        )

    if not candidates:
        return None, None

    current_price = float(execution_candles[0].close)
    current_execution = float(execution_candles[0].timestamp)
    historical_prices = [
        float(item.close)
        for item in execution_candles
        if item.timestamp < current_execution
    ]
    level_reference_price = historical_prices[-1] if historical_prices else current_price
    supports = [
        level for level in candidates
        if level.level_type == "support" and level.price < level_reference_price and level.available_timestamp <= current_execution
    ]
    resistances = [
        level for level in candidates
        if level.level_type == "resistance" and level.price > level_reference_price and level.available_timestamp <= current_execution
    ]
    nearest_support = max(supports, key=lambda item: (item.price, -item.index), default=None)
    nearest_resistance = min(resistances, key=lambda item: (item.price, item.index), default=None)

    if direction == "LONG":
        selected = nearest_support
    elif direction == "SHORT":
        selected = nearest_resistance
    else:
        selected = nearest_support if nearest_support is not None else nearest_resistance
    levels = [selected] if selected is not None else []
    if not levels:
        return None, None
    range_high = max(item.price for item in levels)
    range_low = min(item.price for item in levels)
    range_position = (
        (level_reference_price - range_low) / (range_high - range_low)
        if range_high > range_low
        else 0.5
    )
    return selected, (range_high, range_low, range_position)


def _find_reaction_end(
    *,
    closed: Sequence[Candle],
    touch_index: int,
    level: KeyLevel,
    direction: str,
    atr_by_index: Sequence[float],
) -> tuple[int, float, float, float, float, int, float, float] | None:
    """Find the first causal reaction confirmation after the key-level touch."""
    reaction_end: int | None = None
    reaction_low = float("inf")
    reaction_high = float("-inf")
    reaction_open = 0.0
    reaction_close = 0.0
    reaction_volume = 0.0
    reaction_count = 0
    for index in range(touch_index + 1, len(closed)):
        candle = closed[index]
        reaction_low = min(reaction_low, float(candle.low))
        reaction_high = max(reaction_high, float(candle.high))
        if reaction_count == 0:
            reaction_open = float(candle.open)
        reaction_close = float(candle.close)
        reaction_volume += float(candle.volume)
        reaction_count += 1
        body = abs(reaction_close - reaction_open)
        distance = (
            reaction_high - reaction_low
            if direction == "LONG"
            else reaction_high - reaction_low
        )
        candle_atr = atr_by_index[index]
        if direction == "LONG":
            extreme_distance = (
                (reaction_high - level.price) / candle_atr
                if candle_atr > 0
                else 0.0
            )
        else:
            extreme_distance = (
                (level.price - reaction_low) / candle_atr
                if candle_atr > 0
                else 0.0
            )
        body_atr = body / candle_atr if candle_atr > 0 else 0.0
        average_volume = (
            sum(float(item.volume) for item in closed[: index + 1]) / (index + 1)
            if index >= 0
            else 0.0
        )
        volume_ratio = float(candle.volume) / average_volume if average_volume > 0 else 0.0
        if (
            reaction_count >= 2
            and distance >= REACTION_DISTANCE_ATR * candle_atr
            and extreme_distance >= REACTION_DISTANCE_ATR
            and body_atr >= REACTION_BODY_MIN_ATR
            and volume_ratio >= 1.0
        ):
            return index, reaction_open, reaction_close, reaction_high, reaction_low, reaction_count, body, volume_ratio
    return None


def _find_structure_break(
    *,
    closed: Sequence[Candle],
    reaction_end: int,
    level_type: str,
    direction: str,
    atr_by_index: Sequence[float],
) -> tuple[int, float, float, int] | None:
    """Find the first causal local structure break after reaction confirmation."""
    break_index: int | None = None
    break_price: float | None = None
    structure_price: float | None = None
    structure_index: int | None = None
    for index in range(reaction_end + 1, len(closed)):
        reference = _structure_reference(closed, index, direction)
        if reference is None:
            continue
        candidate_reference, candidate_index = reference
        if structure_price is None or (
            (level_type == "support" and candidate_reference > structure_price)
            or (level_type == "resistance" and candidate_reference < structure_price)
        ):
            structure_price, structure_index = candidate_reference, candidate_index
        candidate_atr = atr_by_index[index]
        if direction == "LONG" and float(closed[index].close) > candidate_reference + STRUCTURE_BREAK_DISTANCE_ATR * candidate_atr:
            return index, float(closed[index].close), structure_price if structure_price is not None else float("nan"), structure_index if structure_index is not None else max(0, reaction_end - 1)
        if direction == "SHORT" and float(closed[index].close) < candidate_reference - STRUCTURE_BREAK_DISTANCE_ATR * candidate_atr:
            return index, float(closed[index].close), structure_price if structure_price is not None else float("nan"), structure_index if structure_index is not None else max(0, reaction_end - 1)
    return None


def _structure_reference(
    candles: Sequence[Candle],
    end_index: int,
    direction: str,
) -> tuple[float, int] | None:
    """Past-only directional local structure reference at the decision candle."""
    start = max(0, end_index - LOCAL_STRUCTURE_LOOKBACK + 1)
    window = candles[start:end_index]
    if not window:
        return None
    if direction == "LONG":
        reference = max(float(item.high) for item in window)
    else:
        reference = min(float(item.low) for item in window)
    if direction == "LONG":
        reference_index = start + max(
            range(len(window)),
            key=lambda offset: (window[offset].high, -offset),
        )
    else:
        reference_index = start + min(
            range(len(window)),
            key=lambda offset: (window[offset].low, offset),
        )
    return reference, reference_index


def _make_candidate(
    *,
    direction: str,
    event: dict[str, Any],
    stage: str,
    signal_time: datetime,
    signal_candle: Candle,
    event_atr: float,
    setup_event_id: str,
    cohort: str,
) -> SetupCandidate:
    """Build a research-only candidate.  No production score is invented."""
    entry = float(signal_candle.close)
    if direction == "LONG":
        structural_stop = float(event["reaction_low"])
        target_1 = entry + 2.0 * (entry - structural_stop)
        invalidation = structural_stop
        target_2 = entry + 3.0 * (entry - structural_stop)
    else:
        structural_stop = float(event["reaction_high"])
        target_1 = entry - 2.0 * (structural_stop - entry)
        invalidation = structural_stop
        target_2 = entry - 3.0 * (structural_stop - entry)

    risk_abs = (
        structural_stop - entry
        if direction == "SHORT"
        else entry - structural_stop
    )
    if risk_abs <= 0:
        raise ValueError(f"invalid structural risk: {risk_abs}")

    features: dict[str, Any] = {
        "experiment_id": EXPERIMENT_ID,
        "hypothesis_id": HYPOTHESIS_ID,
        "setup_event_id": setup_event_id,
        "cohort": cohort,
        "event_stage": stage,
        "htf_timeframe": HTF_TIMEFRAME,
        "htf_range_high": float(event["range_high"]),
        "htf_range_low": float(event["range_low"]),
        "htf_range_position": float(event["range_position"]),
        "key_level_price": float(event["key_level_price"]),
        "key_level_type": str(event["key_level_type"]),
        "key_level_age": int(event["key_level_age"]),
        "key_level_touch_count": int(event["key_level_touch_count"]),
        "key_level_strength": float(event["key_level_strength"]),
        "touch_time": _as_utc(event["touch_time"]).isoformat(),
        "touch_price": float(event["touch_price"]),
        "distance_to_level_atr": float(event["distance_to_level_atr"]),
        "reaction_time": _as_utc(event["reaction_time"]).isoformat(),
        "reaction_extreme_price": float(
            event["reaction_high"] if direction == "LONG" else event["reaction_low"]
        ),
        "reaction_distance_atr": float(event["reaction_distance_atr"]),
        "reaction_high": float(event["reaction_high"]),
        "reaction_low": float(event["reaction_low"]),
        "reaction_candles": int(event["reaction_candles"]),
        "reaction_body_atr": float(event["reaction_body_atr"]),
        "reaction_volume_ratio": float(event["reaction_volume_ratio"]),
        "structure_reference_price": float(event["structure_reference_price"]),
        "structure_reference_time": _as_utc(
            _candle_time(event["structure_reference_candle"])
        ).isoformat(),
        "break_time": (
            _as_utc(event["break_time"]).isoformat() if event.get("break_time") else None
        ),
        "break_price": float(event["break_price"]) if event.get("break_price") is not None else None,
        "break_distance_atr": (
            float(event["break_distance_atr"]) if event.get("break_distance_atr") is not None else None
        ),
        "point_b_time": _as_utc(event["point_b_time"]).isoformat() if event.get("point_b_time") else None,
        "point_b_price": float(event["point_b_price"]) if event.get("point_b_price") is not None else None,
        "point_b_retrace_pct": (
            float(event["point_b_retrace_pct"]) if event.get("point_b_retrace_pct") is not None else None
        ),
        "point_b_distance_to_break_level_atr": (
            float(event["point_b_distance_to_break_level_atr"])
            if event.get("point_b_distance_to_break_level_atr") is not None
            else None
        ),
        "signal_time": _as_utc(signal_time).isoformat(),
        "entry_reference_price": entry,
        "signal_candle_open_time": int(signal_candle.timestamp),
        "structural_stop_price": float(structural_stop),
        "risk_abs": float(risk_abs),
        "risk_pct": float(risk_abs / entry * 100.0) if entry > 0 else None,
        "atr_at_signal": float(event_atr),
        "cost_treatment": "research_metrics_only_no_execution_costs",
        "invalidation_type": (
            "close_below_original_support_or_reaction_low"
            if direction == "LONG"
            else "close_above_original_resistance_or_reaction_high"
        ),
        "invalidation_before_tp": None,
        "range_position_gate": "feature_only_no_gate",
        "market_regime": event.get("market_regime"),
        "volatility_regime": None,
        "volume_regime": None,
        "key_level_detected_time": _as_utc(event["level_time"]).isoformat(),
        "key_level_detected_price": float(event["level_price"]),
        "symbol": str(event["symbol"]),
        "direction": direction,
        "level_price": float(event["key_level_price"]),
        "level_type": str(event["key_level_type"]),
    }
    return SetupCandidate(
        scanner_name=SCANNER_NAME,
        scanner_version="1.0.0",
        symbol=str(event["symbol"]),
        direction=direction,
        htf_timeframe=HTF_TIMEFRAME,
        setup_timeframe=EXECUTION_TIMEFRAME,
        entry_timeframe=EXECUTION_TIMEFRAME,
        detected_at=_as_utc(signal_time),
        setup_started_at=_as_utc(event["touch_time"]),
        signal_candle_open_time=int(signal_candle.timestamp),
        reference_price=entry,
        entry_zone_low=entry,
        entry_zone_high=entry,
        invalidation_price=float(invalidation),
        target_1=float(target_1),
        target_2=float(target_2),
        score=0.0,
        market_regime=event.get("market_regime"),
        reasons=(stage,),
        features=features,
        state=SetupState.DETECTED,
    )


def detect_point_b_setup(
    *,
    symbol: str,
    direction: str,
    execution_candles: Sequence[Candle],
    htf_candles: Sequence[Candle],
    current_time: datetime,
    market_regime: str | None = None,
) -> DetectionResult:
    """Detect one HTF key-level setup using only the supplied candle prefix."""
    if direction not in {"LONG", "SHORT"}:
        raise ValueError(f"unsupported direction: {direction}")
    if len(execution_candles) < 20:
        return DetectionResult(reason="insufficient_execution_history")
    first_execution_timestamp = float(execution_candles[0].timestamp)
    closed = [
        candle for candle in execution_candles
        if _is_closed(candle, current_time)
    ]
    if len(closed) < 20:
        return DetectionResult(reason="insufficient_closed_execution_history")

    atr_by_index = _causal_atr_series(closed)
    initial_atr = atr_by_index[0]
    level, range_data = _find_htf_key_levels(htf_candles, closed, initial_atr, direction)
    if level is None or range_data is None:
        return DetectionResult(reason="no_htf_key_level")
    range_high, range_low, range_position = range_data
    if direction == "LONG" and level.level_type != "support":
        return DetectionResult(reason="long_requires_support")
    if direction == "SHORT" and level.level_type != "resistance":
        return DetectionResult(reason="short_requires_resistance")

    touch_index: int | None = None
    touch_price: float | None = None
    for index, candle in enumerate(closed):
        if candle.timestamp < level.available_timestamp:
            continue
        candle_atr = atr_by_index[index]
        low_distance = (float(candle.low) - level.price) / candle_atr if candle_atr > 0 else float("inf")
        high_distance = (level.price - float(candle.high)) / candle_atr if candle_atr > 0 else float("inf")
        near = min(abs(low_distance), abs(high_distance)) <= KEY_LEVEL_TOUCH_TOLERANCE_ATR
        aligned = candle.low <= level.price if direction == "LONG" else candle.high >= level.price
        if near and aligned:
            touch_index = index
            touch_price = min(float(candle.low), level.price) if direction == "LONG" else max(
                float(candle.high), level.price
            )
            break
    if touch_index is None:
        return DetectionResult(reason="no_key_level_touch")

    reaction = _find_reaction_end(
        closed=closed,
        touch_index=touch_index,
        level=level,
        direction=direction,
        atr_by_index=atr_by_index,
    )
    if reaction is None:
        return DetectionResult(reason="no_reaction_confirmed")
    reaction_end, reaction_open, reaction_close, reaction_high, reaction_low, reaction_count, reaction_body, reaction_volume_ratio = reaction
    reaction_distance = reaction_high - reaction_low

    structure_break = _find_structure_break(
        closed=closed,
        reaction_end=reaction_end,
        level_type=level.level_type,
        direction=direction,
        atr_by_index=atr_by_index,
    )
    if structure_break is None:
        return DetectionResult(
            reason="no_structure_break",
            stages=("KEY_LEVEL_DETECTED", "KEY_LEVEL_TOUCHED", "REACTION_CONFIRMED"),
        )
    break_index, break_price, structure_price, structure_index = structure_break

    point_b_index: int | None = None
    point_b_price: float | None = None
    point_b_retrace: float | None = None
    leg = abs(float(break_price) - structure_price)
    if leg <= 0:
        return DetectionResult(
            reason="non_positive_break_leg",
            stages=("KEY_LEVEL_DETECTED", "KEY_LEVEL_TOUCHED", "REACTION_CONFIRMED", "STRUCTURE_BROKEN"),
        )
    for index in range(break_index + 1, len(closed)):
        candle = closed[index]
        if direction == "LONG":
            low = float(candle.low)
            retrace = (float(break_price) - low) / leg
            candidate_price = float(candle.close)
        else:
            high = float(candle.high)
            retrace = (high - float(break_price)) / leg
            candidate_price = float(candle.close)
        if POINT_B_RETRACE_MIN <= retrace <= POINT_B_RETRACE_MAX:
            point_b_index, point_b_price, point_b_retrace = index, candidate_price, retrace
            break

    if point_b_index is None:
        return DetectionResult(
            reason="no_point_b_in_frozen_retrace_bounds",
            stages=(
                "KEY_LEVEL_DETECTED", "KEY_LEVEL_TOUCHED", "REACTION_CONFIRMED",
                "STRUCTURE_BROKEN",
            ),
        )

    if direction == "LONG" and float(closed[point_b_index].close) <= reaction_low:
        return DetectionResult(
            reason="structural_invalidation_before_entry",
            stages=(
                "KEY_LEVEL_DETECTED", "KEY_LEVEL_TOUCHED", "REACTION_CONFIRMED",
                "STRUCTURE_BROKEN",
            ),
        )
    if direction == "SHORT" and float(closed[point_b_index].close) >= reaction_high:
        return DetectionResult(
            reason="structural_invalidation_before_entry",
            stages=(
                "KEY_LEVEL_DETECTED", "KEY_LEVEL_TOUCHED", "REACTION_CONFIRMED",
                "STRUCTURE_BROKEN",
            ),
        )
    point_b_atr = atr_by_index[point_b_index]
    point_b_distance = abs(float(point_b_price) - float(break_price)) / point_b_atr if point_b_atr > 0 else None
    setup_event_id = _stable_id(
        EXPERIMENT_ID, symbol, direction, round(level.price, 10),
        int(closed[touch_index].timestamp),
    )
    event = {
        "symbol": symbol,
        "direction": direction,
        "market_regime": market_regime,
        "level_price": level.price,
        "level_type": level.level_type,
        "level_age": level.age,
        "level_time": _candle_time(htf_candles[level.index]),
        "key_level_price": level.price,
        "key_level_type": level.level_type,
        "key_level_age": level.age,
        "key_level_touch_count": level.touches,
        "key_level_strength": level.strength,
        "range_high": range_high,
        "range_low": range_low,
        "range_position": range_position,
        "touch_time": _candle_time(closed[touch_index]),
        "touch_price": touch_price,
        "distance_to_level_atr": abs(float(touch_price) - level.price) / atr_by_index[touch_index] if atr_by_index[touch_index] > 0 else None,
        "reaction_time": _candle_time(closed[reaction_end]),
        "reaction_high": reaction_high,
        "reaction_low": reaction_low,
        "reaction_distance_atr": reaction_distance / atr_by_index[reaction_end] if atr_by_index[reaction_end] > 0 else None,
        "reaction_candles": reaction_count,
        "reaction_body_atr": reaction_body / atr_by_index[reaction_end] if atr_by_index[reaction_end] > 0 else None,
        "reaction_volume_ratio": reaction_volume_ratio,
        "structure_reference_price": structure_price,
        "structure_reference_candle": closed[structure_index],
        "break_time": _candle_time(closed[break_index]),
        "break_price": break_price,
        "break_distance_atr": abs(float(break_price) - structure_price) / atr_by_index[break_index] if atr_by_index[break_index] > 0 else None,
        "point_b_time": _candle_time(closed[point_b_index]),
        "point_b_price": point_b_price,
        "point_b_retrace_pct": point_b_retrace * 100.0,
        "point_b_distance_to_break_level_atr": point_b_distance,
    }
    stages = (
        "KEY_LEVEL_DETECTED", "KEY_LEVEL_TOUCHED", "REACTION_CONFIRMED",
        "STRUCTURE_BROKEN", "POINT_B_FORMED",
    )
    funnel = {
        "key_levels": 1,
        "touches": 1,
        "reactions": 1,
        "breaks": 1,
        "point_b": 1,
    }
    signal = _make_candidate(
        direction=direction,
        event=event,
        stage="OOS_SIGNAL",
        signal_time=_candle_time(closed[point_b_index]),
        signal_candle=closed[point_b_index],
        event_atr=point_b_atr,
        setup_event_id=setup_event_id,
        cohort="POINT_B",
    )
    baseline_signal_time = _candle_time(closed[reaction_end])
    try:
        baseline = _make_candidate(
            direction=direction,
            event=event,
            stage="BASELINE",
            signal_time=baseline_signal_time,
            signal_candle=closed[reaction_end],
            event_atr=atr_by_index[reaction_end],
            setup_event_id=setup_event_id,
            cohort="BASELINE",
        )
    except ValueError:
        baseline = None
    return DetectionResult(
        signal=signal,
        baseline=baseline,
        stages=stages,
        funnel=funnel,
        setup_event_id=setup_event_id,
    )


def detect_context_setup(ctx: MarketContext, direction: str) -> DetectionResult:
    """Adapter for an existing MarketContext without changing scanner output."""
    return detect_point_b_setup(
        symbol=ctx.symbol,
        direction=direction,
        execution_candles=list(ctx.candles_5m),
        htf_candles=list(ctx.candles_1h),
        current_time=ctx.evaluated_at,
        market_regime=ctx.market_regime,
    )


def detect_both_directions(ctx: MarketContext) -> list[DetectionResult]:
    return [detect_context_setup(ctx, direction) for direction in ("LONG", "SHORT")]


def iter_result_candidates(result: DetectionResult) -> Iterable[SetupCandidate]:
    if result.signal is not None:
        yield result.signal
    if result.baseline is not None:
        yield result.baseline


def result_candidates(result: DetectionResult) -> list[SetupCandidate]:
    return list(iter_result_candidates(result))


def event_identity(result: DetectionResult) -> str | None:
    return result.setup_event_id


def copy_candidate_with_signal(candidate: SetupCandidate) -> SetupCandidate:
    """Defensive helper used by tests and persistence adapters."""
    return replace(candidate)
