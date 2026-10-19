"""Pure, fail-closed 5m SHORT execution counterfactual for issue #170.

Discovery-only. No DB, Bybit client, order placement, or frozen evaluator imports.
Input candles use OPEN timestamps; all 48 candles must be complete as of ASOF.
Entry is the first eligible bar open after an already-confirmed signal timestamp.
This is a bar-open fill PROXY, not proof of exchange execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Sequence

CANDLE_MS = 300_000
HOLD_BARS = 48
NORMAL_ROUND_TRIP_COST = 0.0021  # illustrative scenario, not verified fees
ELEVATED_ROUND_TRIP_COST = 0.0031


@dataclass(frozen=True)
class ShortReplayResult:
    status: str
    reason: str
    path_class: str | None = None
    entry_ms: int | None = None
    exit_ms: int | None = None
    entry: float | None = None
    exit: float | None = None
    gross_r: float | None = None
    net_r_normal: float | None = None
    net_r_elevated: float | None = None
    ambiguous_intrabar: bool = False


def _ms(value: Any) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive signal time")
        return int(value.astimezone(timezone.utc).timestamp() * 1000)
    if type(value) is int:
        return value
    raise ValueError("signal timestamp must be timezone-aware datetime or ms integer")


def replay_short(
    *, signal_time: Any, stop: float, target: float,
    candles: Sequence[Any], evaluation_asof_ms: int,
) -> ShortReplayResult:
    """Conservative 240m path, including the entry bar, STOP_FIRST.

    Signal time must be the actual timestamp at which the signal is known.
    A close-stamped 5m signal at T may use the bar opening at T as a PROXY;
    an open-stamped signal must instead be advanced to its close before calling.
    """
    blocked = lambda why: ShortReplayResult("BLOCKED", why)
    try:
        t = _ms(signal_time)
        asof = _ms(evaluation_asof_ms)
        stop, target = float(stop), float(target)
    except (TypeError, ValueError, OverflowError):
        return blocked("INVALID_INPUT")
    if t % CANDLE_MS != 0:
        return blocked("SIGNAL_NOT_5M_ALIGNED")
    if not isfinite(stop) or not isfinite(target) or stop <= 0 or target <= 0 or target >= stop:
        return blocked("INVALID_SHORT_LEVELS")
    if len(candles) != HOLD_BARS:
        return blocked("INCOMPLETE_240M_COVERAGE")
    for i, c in enumerate(candles):
        try:
            ts = int(c.timestamp)
            o, h, l, cl = map(float, (c.open, c.high, c.low, c.close))
        except (TypeError, ValueError, AttributeError):
            return blocked("INVALID_CANDLE")
        if ts != t + i * CANDLE_MS:
            return blocked("MISSING_OR_DUPLICATE_CANDLE")
        if ts + CANDLE_MS > asof:
            return blocked("CANDLE_NOT_CLOSED_ASOF")
        if not all(isfinite(v) and v > 0 for v in (o, h, l, cl)) or h < max(o, cl) or l > min(o, cl):
            return blocked("INVALID_OHLC")
    entry = float(candles[0].open)
    if not stop > entry > target:
        return blocked("INVALID_FILL_BASED_SHORT_GEOMETRY")
    risk = stop - entry
    exit_price = float(candles[-1].close)
    klass = "TIMEOUT"
    exit_time = t + HOLD_BARS * CANDLE_MS
    ambiguous = False
    for c in candles:
        sl = float(c.high) >= stop
        tp = float(c.low) <= target
        if sl or tp:
            ambiguous = sl and tp
            klass = "SL_FIRST" if sl else "TP_FIRST"
            exit_price = stop if sl else target
            exit_time = int(c.timestamp) + CANDLE_MS  # fill timing uncertain within bar
            break
    gross = (entry - exit_price) / risk
    # Research cost approximation relative to executed entry and its stop distance.
    cost_normal = entry * NORMAL_ROUND_TRIP_COST / risk
    cost_elevated = entry * ELEVATED_ROUND_TRIP_COST / risk
    return ShortReplayResult(
        status="EVALUATED", reason="MODEL_ONLY_NOT_EXECUTABLE_PROOF",
        path_class=klass, entry_ms=t, exit_ms=exit_time,
        entry=entry, exit=exit_price, gross_r=gross,
        net_r_normal=gross - cost_normal,
        net_r_elevated=gross - cost_elevated,
        ambiguous_intrabar=ambiguous,
    )
