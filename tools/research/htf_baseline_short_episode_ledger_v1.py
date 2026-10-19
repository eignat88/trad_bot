"""READ-ONLY, candidate-level touch-episode ledger for Issue #174.

Episode = maximal adjacent sequence of qualifying 5m contact candles for
the SAME causal HTF resistance. This is a diagnostic definition, not an
approved production deduplication or an independent-trades estimator.
"""
from __future__ import annotations

from hashlib import sha256
from typing import Sequence

from app.models import Candle
from app.research import htf_keylevel_baseline_short_causal_observer_v1 as mod
from app.research.htf_keylevel_point_b import (
    _causal_atr_series, _find_htf_key_levels, KEY_LEVEL_TOUCH_TOLERANCE_ATR,
)


def event_episode(symbol: str, execution_candles: Sequence[Candle],
                  htf_candles: Sequence[Candle], touch_ms: int) -> dict:
    # Match the observer's frozen 200-bar diagnostic view exactly.
    bars = list(execution_candles[-mod.CANONICAL_EXECUTION_BARS:])
    atr = _causal_atr_series(bars)
    level, _ = _find_htf_key_levels(htf_candles, bars, atr[0], "SHORT")
    if level is None or level.level_type != "resistance":
        raise ValueError("no eligible resistance for emitted signal")
    indices = [i for i, c in enumerate(bars) if int(c.timestamp) == touch_ms]
    if len(indices) != 1:
        raise ValueError("selected touch missing from observer prefix")
    idx = indices[0]

    def qualifying(i: int) -> bool:
        c = bars[i]
        return (int(c.timestamp) >= level.available_timestamp
                and atr[i] > 0
                and float(c.high) >= level.price
                and min(abs((float(c.low) - level.price) / atr[i]),
                        abs((level.price - float(c.high)) / atr[i]))
                <= KEY_LEVEL_TOUCH_TOLERANCE_ATR)

    if not qualifying(idx):
        raise ValueError("observer touch did not satisfy level contact rule")
    first = idx
    last = idx
    while first > 0 and qualifying(first - 1):
        first -= 1
    while last + 1 < len(bars) - 1 and qualifying(last + 1):
        last += 1

    # Level identity includes formation timestamp (not merely float price).
    level_key = (
        f"{symbol}|SHORT|{int(level.timestamp)}|"
        f"{int(level.available_timestamp)}|{level.price:.12g}"
    )
    level_id = sha256(level_key.encode()).hexdigest()
    episode_key = f"{level_id}|{int(bars[first].timestamp)}"
    return {
        "level_id": level_id,
        "level_price": float(level.price),
        "level_formation_ms": int(level.timestamp),
        "level_available_ms": int(level.available_timestamp),
        "episode_id": sha256(episode_key.encode()).hexdigest(),
        "episode_start_ms": int(bars[first].timestamp),
        "episode_end_observed_ms": int(bars[last].timestamp),
        "episode_contact_bars_asof": last - first + 1,
        "episode_left_censored": first == 0,
        "touch_is_episode_start": first == idx,
        "identity_rule": "SAME_HTF_LEVEL_AND_CONTIGUOUS_5M_CONTACT",
    }
