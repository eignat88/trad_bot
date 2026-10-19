"""#176 closed-1h, point-in-time HTF resistance snapshot.

Independent research adapter; does not invoke the legacy window-start chooser.
Uses the frozen swing/touch thresholds, but a different *selection contract*:
at each decision use only fully-closed 1h bars and the last closed 5m price.
Do not treat this new selection contract as frozen/approved for OOS yet.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Sequence

from app.models import Candle
from app.research.htf_baseline_short_stateful_episode_v1 import LevelSnapshot
from app.research.htf_keylevel_point_b import (
    KEY_LEVEL_MIN_TOUCHES, LEVEL_TOUCH_COUNT_TOLERANCE_ATR,
    _historical_atr, _causal_atr_series,
)

HOUR = 3_600_000
FIVE = 300_000
SWING_RIGHT = 4


def causal_resistance_universe(
    htf_closed: Sequence[Candle], exec_closed: Sequence[Candle], asof_ms: int
) -> tuple[LevelSnapshot, ...]:
    if not exec_closed or len(htf_closed) < 26:
        return ()
    if any(int(c.timestamp) + FIVE > asof_ms for c in exec_closed):
        raise ValueError("future 5m input")
    if int(exec_closed[-1].timestamp) + FIVE != asof_ms:
        raise ValueError("execution prefix must end at closed decision bar")
    times = [int(c.timestamp) for c in htf_closed]
    if any(t % HOUR or t + HOUR > asof_ms for t in times):
        raise ValueError("future or unclosed 1h input")
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("unordered 1h candles")
    reference = float(exec_closed[-1].close)
    atr = _causal_atr_series(exec_closed)[-1]
    if atr <= 0:
        return ()
    eligible = []
    for i in range(SWING_RIGHT, len(htf_closed)-SWING_RIGHT):
        c = htf_closed[i]
        price = float(c.high)
        if not all(htf_closed[i-j].high <= price for j in range(1, SWING_RIGHT+1)):
            continue
        if not all(htf_closed[i+j].high <= price for j in range(1, SWING_RIGHT+1)):
            continue
        if not any(htf_closed[i-j].high < price for j in range(1, SWING_RIGHT+1)):
            continue
        if not any(htf_closed[i+j].high < price for j in range(1, SWING_RIGHT+1)):
            continue
        available = int(htf_closed[i+SWING_RIGHT].timestamp) + HOUR
        if available > asof_ms or price <= reference:
            continue
        tol = max(price * .003, atr * LEVEL_TOUCH_COUNT_TOLERANCE_ATR)
        count = sum(
            abs(float(x.high)-price) <= tol or abs(float(x.low)-price) <= tol
            for x in htf_closed[:i]
        )
        if count < KEY_LEVEL_MIN_TOUCHES:
            continue
        formation = int(c.timestamp)
        raw = f"SHORT|{formation}|{available}|{price:.12g}"
        eligible.append((
            price, formation,
            LevelSnapshot(sha256(raw.encode()).hexdigest(), price, available)
        ))
    return tuple(x[2] for x in sorted(eligible, key=lambda x: (x[0], x[1], x[2].level_id)))


def causal_resistance_snapshot(htf_closed, exec_closed, asof_ms):
    """Backward compatible nearest selected level; research-only."""
    universe = causal_resistance_universe(htf_closed, exec_closed, asof_ms)
    return universe[0] if universe else None


def causal_contact(candle: Candle, level: LevelSnapshot | None, atr: float) -> bool:
    if level is None or atr <= 0 or candle.timestamp < level.available_ms:
        return False
    from app.research.htf_keylevel_point_b import KEY_LEVEL_TOUCH_TOLERANCE_ATR
    return (
        float(candle.high) >= level.price
        and min(abs((float(candle.low)-level.price)/atr),
                abs((level.price-float(candle.high))/atr))
            <= KEY_LEVEL_TOUCH_TOLERANCE_ATR
    )
