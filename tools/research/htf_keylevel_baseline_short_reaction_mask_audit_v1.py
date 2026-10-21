"""Read-only event-masking diagnostic for causal HTF baseline SHORT.

Mirrors the frozen level/touch/reaction geometry. Never changes the observer.
All counters are per as-of decision, not independent setup counts.
"""
from __future__ import annotations

from collections import Counter
from typing import Sequence

from app.models import Candle
from app.research.htf_keylevel_point_b import (
    _causal_atr_series, _find_htf_key_levels, _find_reaction_end,
    KEY_LEVEL_TOUCH_TOLERANCE_ATR,
)


def inspect_reaction_masking(
    execution: Sequence[Candle], htf: Sequence[Candle],
) -> dict:
    """Report which touch the unchanged detector would choose, and later touches.

    The unchanged observer iterates touches chronologically and emits the first
    one whose reaction ends at the latest closed candle. If an earlier touch
    has an old reaction, the observer continues to subsequent touches.
    """
    out = {
        "level_found": False, "touch_count": 0,
        "confirmed_touch_count": 0, "fresh_touch_count": 0,
        "selected_first_touch_ms": None,
        "first_confirmed_touch_ms": None,
        "first_confirmed_reaction_ms": None,
        "first_confirmed_reaction_lag_bars": None,
        "later_touch_count": 0,
        "later_confirmed_touch_count": 0,
        "later_fresh_touch_count": 0,
        "fresh_after_historical": False,
        "would_emit": False,
    }
    if len(execution) < 20 or not htf:
        return out
    atr = _causal_atr_series(execution)
    level, ranges = _find_htf_key_levels(htf, execution, atr[0], "SHORT")
    if level is None or ranges is None or level.level_type != "resistance":
        return out
    out["level_found"] = True
    out["level_price"] = float(level.price)
    last_index = len(execution)-1
    historical_before_fresh = False
    for i, c in enumerate(execution[:-1]):
        if c.timestamp < level.available_timestamp or atr[i] <= 0:
            continue
        near = min(
            abs((float(c.low)-level.price)/atr[i]),
            abs((level.price-float(c.high))/atr[i]),
        ) <= KEY_LEVEL_TOUCH_TOLERANCE_ATR
        if not (near and float(c.high) >= level.price):
            continue
        out["touch_count"] += 1
        if out["selected_first_touch_ms"] is None:
            out["selected_first_touch_ms"] = int(c.timestamp)
        reaction = _find_reaction_end(
            closed=execution, touch_index=i, level=level,
            direction="SHORT", atr_by_index=atr,
        )
        if reaction is None:
            continue
        out["confirmed_touch_count"] += 1
        end = int(reaction[0])
        if out["first_confirmed_touch_ms"] is None:
            out["first_confirmed_touch_ms"] = int(c.timestamp)
            out["first_confirmed_reaction_ms"] = int(execution[end].timestamp)
            out["first_confirmed_reaction_lag_bars"] = last_index-end
        else:
            out["later_confirmed_touch_count"] += 1
        if end == last_index:
            out["fresh_touch_count"] += 1
            if historical_before_fresh:
                out["fresh_after_historical"] = True
            high = float(reaction[3])
            entry = float(execution[end].close)
            if high > entry > 0:
                out["would_emit"] = True
        else:
            historical_before_fresh = True
    # Later touches are chronological touches beyond the first touch.
    out["later_touch_count"] = max(out["touch_count"]-1, 0)
    out["later_fresh_touch_count"] = out["fresh_touch_count"] - (
        1 if out["fresh_touch_count"] and out["selected_first_touch_ms"]
        == out["first_confirmed_touch_ms"]
        and out["first_confirmed_reaction_lag_bars"] == 0 else 0
    )
    return out


def masking_summary(records: list[dict]) -> dict:
    counts = Counter()
    lags = []
    samples = []
    for x in records:
        counts["evaluated_decisions"] += 1
        for field in (
            "level_found", "would_emit", "fresh_after_historical",
        ):
            counts[field] += bool(x.get(field))
        if x.get("confirmed_touch_count", 0):
            counts["decisions_with_confirmed_reaction"] += 1
        if x.get("later_touch_count", 0):
            counts["decisions_with_later_touches"] += 1
        if x.get("later_confirmed_touch_count", 0):
            counts["decisions_with_later_confirmations"] += 1
        if x.get("fresh_touch_count", 0):
            counts["decisions_with_fresh_reaction"] += 1
        lag = x.get("first_confirmed_reaction_lag_bars")
        if lag is not None:
            lags.append(int(lag))
        if (x.get("later_touch_count", 0) or x.get("fresh_after_historical")) and len(samples) < 12:
            samples.append({
                "asof_ms": x.get("asof_ms"),
                "first_touch_ms": x.get("selected_first_touch_ms"),
                "first_reaction_ms": x.get("first_confirmed_reaction_ms"),
                "first_reaction_lag_bars": lag,
                "touch_count": x.get("touch_count"),
                "fresh_touch_count": x.get("fresh_touch_count"),
                "fresh_after_historical": x.get("fresh_after_historical"),
            })
    return {
        "decision_counts": dict(counts),
        "first_reaction_lag_bars_min": min(lags) if lags else None,
        "first_reaction_lag_bars_max": max(lags) if lags else None,
        "first_reaction_lag_bars_median": sorted(lags)[len(lags)//2] if lags else None,
        "sample_decisions": samples,
        "interpretation": "Per-decision counts only; not independent events or OOS",
    }
