"""Research-only causal reaction observer for HTF key-level BASELINE SHORT.

No Point-B requirement, no DB access, no scanner routing or trading actions.
Consumes ONLY a closed 5m prefix available at evaluation_asof.
This is a proposal for a NEW prospective cohort, not a reconstruction of the
historical Point-B-conditioned baseline and not a deployable observer service.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Sequence

from app.models import Candle
from app.research.htf_keylevel_point_b import (
    _causal_atr_series, _find_htf_key_levels, _find_reaction_end,
    KEY_LEVEL_TOUCH_TOLERANCE_ATR, LOCAL_STRUCTURE_LOOKBACK,
)

BAR_MS = 5 * 60_000
HTF_MS = 60 * 60_000
EXPERIMENT_ID = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"


@dataclass(frozen=True)
class CausalBaselineSignal:
    experiment_id: str
    symbol: str
    direction: str
    event_id: str
    signal_candle_open_ms: int
    known_at_ms: int
    observed_at_ms: int
    entry_reference_price: float
    invalidation_price: float
    target_1: float
    touch_open_ms: int
    reaction_open_ms: int


def detect_causal_baseline_short(
    *, symbol: str, execution_candles: Sequence[Candle],
    htf_candles: Sequence[Candle], evaluation_asof: datetime,
) -> CausalBaselineSignal | None:
    """Emit only when the latest closed 5m candle FIRST confirms the reaction.

    Returns None on an unavailable/invalid prefix. The caller must persist
    first-seen events prospectively and deduplicate by event_id; this function
    NEVER backfills missed earlier signals.
    """
    if not isinstance(evaluation_asof, datetime) or evaluation_asof.tzinfo is None:
        raise ValueError("evaluation_asof must be timezone-aware")
    if not symbol:
        raise ValueError("symbol required")
    asof = int(evaluation_asof.astimezone(timezone.utc).timestamp() * 1000)
    if len(execution_candles) < 20 or len(htf_candles) < LOCAL_STRUCTURE_LOOKBACK * 2 + 2:
        return None
    # Provider contract: no open/future bars, no unordered/duplicate/missing 5m.
    opens = [int(c.timestamp) for c in execution_candles]
    if any(t % BAR_MS or t + BAR_MS > asof for t in opens):
        return None
    if any(b - a != BAR_MS for a, b in zip(opens, opens[1:])):
        return None
    if asof - (opens[-1] + BAR_MS) >= BAR_MS:
        return None  # stale provider prefix
    # Historical 1H bars MUST be closed at ASOF; do not trust provider "latest" data.
    if any(int(c.timestamp) % HTF_MS or int(c.timestamp) + HTF_MS > asof
           for c in htf_candles):
        return None
    if any(b.timestamp <= a.timestamp for a, b in zip(htf_candles, htf_candles[1:])):
        return None
    atr = _causal_atr_series(execution_candles)
    level, range_data = _find_htf_key_levels(htf_candles, execution_candles, atr[0], "SHORT")
    if level is None or range_data is None or level.level_type != "resistance":
        return None
    for touch_index, candle in enumerate(execution_candles[:-1]):
        if int(candle.timestamp) < level.available_timestamp:
            continue
        c_atr = atr[touch_index]
        if c_atr <= 0:
            continue
        near = min(
            abs((float(candle.low) - level.price) / c_atr),
            abs((level.price - float(candle.high)) / c_atr),
        ) <= KEY_LEVEL_TOUCH_TOLERANCE_ATR
        if not (near and float(candle.high) >= level.price):
            continue
        # Critical: only emit when reaction FIRST becomes confirmable at
        # the most recent closed candle. Never emit historical old reactions.
        reaction = _find_reaction_end(
            closed=execution_candles,
            touch_index=touch_index,
            level=level,
            direction="SHORT",
            atr_by_index=atr,
        )
        if reaction is None or reaction[0] != len(execution_candles) - 1:
            continue
        end, _, _, high, _, _, _, _ = reaction
        entry = float(execution_candles[end].close)
        if not high > entry > 0:
            continue
        risk = high - entry
        event_key = f"{EXPERIMENT_ID}|{symbol}|SHORT|{level.price:.12g}|{int(candle.timestamp)}"
        return CausalBaselineSignal(
            experiment_id=EXPERIMENT_ID,
            symbol=symbol, direction="SHORT",
            event_id=sha256(event_key.encode()).hexdigest(),
            signal_candle_open_ms=int(execution_candles[end].timestamp),
            known_at_ms=int(execution_candles[end].timestamp) + BAR_MS,
            observed_at_ms=asof,
            entry_reference_price=entry,
            invalidation_price=float(high),
            target_1=entry - 2.0 * risk,
            touch_open_ms=int(candle.timestamp),
            reaction_open_ms=int(execution_candles[end].timestamp),
        )
    return None
