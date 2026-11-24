"""Issue #297 (H1-A.1) — pure HTF candle validation for 4H and 1D series.

Research-only module.  Pure functions: no I/O, no network, no database, no
clock reads.  Every timestamp is a UTC epoch-millisecond integer and the
candle timestamp is the candle OPEN time.

Timeframe grids (UTC):

* ``4H`` — 14_400_000 ms.  A candle is on-grid iff
  ``open_ms % TIMEFRAME_4H_MS == 0``.  Because the Unix epoch is exactly
  1970-01-01T00:00:00Z and 4h divides the UTC day evenly, this places 4H
  opens at 00:00 / 04:00 / 08:00 / 12:00 / 16:00 / 20:00 UTC.  There is no
  local-timezone or DST dependency anywhere in this module.
* ``1D`` — 86_400_000 ms.  A candle is on-grid iff
  ``open_ms % TIMEFRAME_1D_MS == 0``, i.e. the daily open is EXACTLY
  00:00:00 UTC.  Daily candles are aligned to the UTC calendar day and never
  to a server-local calendar day (verified by
  ``tests/test_me_entry_quality_h1_candles_v1.py``): a local-timezone shift
  can never move the day boundary because no local-time API is consulted.

Availability rule (causal, inclusive at the boundary):

    a candle is usable at ``evaluation_asof_ms`` iff
    ``open_ms + timeframe_ms <= evaluation_asof_ms``

i.e. the candle becomes available exactly at its CLOSE time and never
before.  ``evaluation_asof_ms`` is the moment of the ORIGINAL event (the
signal time), never the time of the later computation.

Explicit rejection / truncation policy (Issue #297 §5 scenarios 5-9, 19):

* candles that are not fully closed at ``evaluation_asof_ms`` (including
  candles opening in the future) are NEVER used; they are truncated from the
  usable series and reported explicitly in ``dropped_unclosed`` — never
  silently;
* unsorted input, duplicate timestamps, missing buckets (gaps), off-grid
  timestamps and invalid OHLCV values fail closed with an explicit reason
  code — they are never silently repaired, reordered or gap-filled
  (automatic gap filling is forbidden);
* insufficient history fails closed with ``INSUFFICIENT_HISTORY``;
* NO failure is ever downgraded to a market mode: a data problem yields
  ``ok=False`` here and ``INVALID_SOURCE`` upstream — never ``NEUTRAL``.

The result is a pure function of ``(candles, timeframe, evaluation_asof_ms,
min_closed_candles)``: reruns are identical, appended future candles cannot
change an earlier ASOF result, and the server timezone is irrelevant.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from app.models import Candle

# --- timeframes -----------------------------------------------------------

TIMEFRAME_4H = "4H"
TIMEFRAME_1D = "1D"

TIMEFRAME_4H_MS = 240 * 60_000      # 14_400_000
TIMEFRAME_1D_MS = 1_440 * 60_000    # 86_400_000

#: canonical timeframe name -> grid period in UTC epoch milliseconds
TIMEFRAME_MS: dict[str, int] = {
    TIMEFRAME_4H: TIMEFRAME_4H_MS,
    TIMEFRAME_1D: TIMEFRAME_1D_MS,
}

# --- reason codes (stable, fail-closed) -----------------------------------

REASON_OK = "OK"
REASON_INVALID_TIMEFRAME = "INVALID_TIMEFRAME"
REASON_INVALID_ASOF = "INVALID_ASOF"
REASON_EMPTY_SERIES = "EMPTY_SERIES"
REASON_INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
REASON_UNALIGNED_CANDLE = "UNALIGNED_CANDLE"
REASON_DUPLICATE_CANDLE = "DUPLICATE_CANDLE"
REASON_CONFLICTING_CANDLE = "CONFLICTING_CANDLE"
REASON_OUT_OF_ORDER_CANDLE = "OUT_OF_ORDER_CANDLE"
REASON_CANDLE_GAP = "CANDLE_GAP"
REASON_INVALID_OHLC = "INVALID_OHLC"
REASON_INVALID_VOLUME = "INVALID_VOLUME"
REASON_NO_CLOSED_CANDLES = "NO_CLOSED_CANDLES"
REASON_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
REASON_MISSING_LATEST_CLOSED_CANDLE = "MISSING_LATEST_CLOSED_CANDLE"

#: frozen-candidate minimum usable history: with ``swing_lookback = 3`` at
#: least ``2 * 3 + 1 = 7`` closed candles are needed for a single swing to be
#: geometrically possible.  NOT an approved OOS parameter.
MIN_CLOSED_CANDLES = 7


@dataclass(frozen=True)
class CandleValidationResult:
    """Outcome of :func:`validate_htf_candles` (fail-closed, explicit)."""

    ok: bool
    reason: str = REASON_OK
    detail: str = ""
    timeframe: str = ""
    evaluation_asof_ms: int = 0
    #: usable series: sorted, strictly ascending, contiguous, on-grid and
    #: fully closed at ``evaluation_asof_ms``
    closed_candles: tuple[Candle, ...] = ()
    #: trailing candles NOT usable at ``evaluation_asof_ms`` (unclosed or
    #: future).  Reported explicitly; NEVER used, NEVER silently dropped.
    dropped_unclosed: tuple[Candle, ...] = ()
    #: open time of the newest closed bucket expected at ``asof`` (diagnostic)
    latest_closed_open_ms: int = 0

    @property
    def closed_open_ms(self) -> tuple[int, ...]:
        return tuple(int(c.timestamp) for c in self.closed_candles)


def _is_int(value: Any) -> bool:
    # bool is an int subclass — timestamps must be real integers.
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_ohlc(candle: Any, index: int) -> tuple[str, str]:
    """Validate numeric OHLCV fields.  Returns ``(reason, detail)`` / ``OK``."""
    prices = []
    for name in ("open", "high", "low", "close"):
        value = getattr(candle, name, None)
        if not _finite_number(value):
            return REASON_INVALID_OHLC, f"non-finite or non-numeric {name} at index {index}"
        value = float(value)
        if value <= 0.0:
            return REASON_INVALID_OHLC, f"non-positive {name} at index {index}"
        prices.append(value)
    open_, high, low, close = prices
    if low > high:
        return REASON_INVALID_OHLC, f"low > high at index {index}"
    if low > min(open_, close) or high < max(open_, close):
        return REASON_INVALID_OHLC, f"open/close outside [low, high] at index {index}"
    volume = getattr(candle, "volume", None)
    if not _finite_number(volume):
        return REASON_INVALID_VOLUME, f"non-finite or non-numeric volume at index {index}"
    if float(volume) < 0.0:
        return REASON_INVALID_VOLUME, f"negative volume at index {index}"
    return REASON_OK, ""


def latest_closed_open_ms(evaluation_asof_ms: int, timeframe_ms: int) -> int:
    """Open time of the newest bucket closed at ``evaluation_asof_ms``.

    The bucket ``[b, b + timeframe_ms)`` is closed iff
    ``b + timeframe_ms <= evaluation_asof_ms`` (inclusive at the boundary:
    a candle is available exactly at its close time).
    """
    return (int(evaluation_asof_ms) - int(timeframe_ms)) // int(timeframe_ms) * int(timeframe_ms)


def validate_htf_candles(
    candles: Sequence[Any],
    *,
    timeframe: str,
    evaluation_asof_ms: Any,
    min_closed_candles: int = MIN_CLOSED_CANDLES,
    require_latest_closed: bool = True,
) -> CandleValidationResult:
    """Validate one provider-supplied HTF series against a causal ASOF.

    Contract (all fail-closed with a stable reason code):

    1. ``timeframe`` is ``"4H"`` or ``"1D"``; ``evaluation_asof_ms`` is a
       non-negative integer epoch-ms timestamp.
    2. every candle timestamp is an integer UTC epoch-ms OPEN time aligned to
       the timeframe grid (``open_ms % timeframe_ms == 0``);
    3. input is strictly ascending by open time — unsorted input is rejected,
       never silently reordered;
    4. duplicate timestamps are rejected (``DUPLICATE_CANDLE`` for identical
       OHLCV, ``CONFLICTING_CANDLE`` otherwise);
    5. any missing bucket strictly between the first and the last CLOSED
       candle is a ``CANDLE_GAP`` — gaps are never gap-filled or ignored;
    6. every OHLC price is a finite positive number with
       ``low <= min(open, close) <= max(open, close) <= high`` and volume is a
       finite non-negative number;
    7. only candles with ``open_ms + timeframe_ms <= evaluation_asof_ms`` are
       usable; every other candle is explicitly reported in
       ``dropped_unclosed`` and never used;
    8. with ``require_latest_closed`` the newest bucket closed at the ASOF
       must be present — a stale/partial tail is an incomplete source
       (``MISSING_LATEST_CLOSED_CANDLE``), never silently tolerated;
    9. at least ``min_closed_candles`` closed candles are required
       (``INSUFFICIENT_HISTORY`` otherwise).
    """
    if timeframe not in TIMEFRAME_MS:
        return CandleValidationResult(
            ok=False,
            reason=REASON_INVALID_TIMEFRAME,
            detail=f"unknown timeframe {timeframe!r}",
            timeframe=str(timeframe),
        )
    timeframe_ms = TIMEFRAME_MS[timeframe]
    if not _is_int(evaluation_asof_ms) or int(evaluation_asof_ms) < 0:
        return CandleValidationResult(
            ok=False,
            reason=REASON_INVALID_ASOF,
            detail=f"evaluation_asof_ms must be a non-negative integer, got {evaluation_asof_ms!r}",
            timeframe=timeframe,
        )
    asof_ms = int(evaluation_asof_ms)
    if not candles:
        return CandleValidationResult(
            ok=False,
            reason=REASON_EMPTY_SERIES,
            detail="no candles supplied",
            timeframe=timeframe,
            evaluation_asof_ms=asof_ms,
        )

    seen: dict[int, Any] = {}
    previous_open: int | None = None
    closed: list[Any] = []
    for index, candle in enumerate(candles):
        open_raw = getattr(candle, "timestamp", None)
        if not _is_int(open_raw) or int(open_raw) < 0:
            return CandleValidationResult(
                ok=False,
                reason=REASON_INVALID_TIMESTAMP,
                detail=f"timestamp must be a non-negative integer epoch-ms, got {open_raw!r} at index {index}",
                timeframe=timeframe,
                evaluation_asof_ms=asof_ms,
            )
        open_ms = int(open_raw)
        if open_ms % timeframe_ms:
            return CandleValidationResult(
                ok=False,
                reason=REASON_UNALIGNED_CANDLE,
                detail=f"open {open_ms} at index {index} is off the {timeframe} UTC grid",
                timeframe=timeframe,
                evaluation_asof_ms=asof_ms,
            )
        if open_ms in seen:
            same = seen[open_ms]
            identical = all(
                float(getattr(same, name)) == float(getattr(candle, name))
                for name in ("open", "high", "low", "close", "volume")
            )
            return CandleValidationResult(
                ok=False,
                reason=REASON_DUPLICATE_CANDLE if identical else REASON_CONFLICTING_CANDLE,
                detail=f"repeated open {open_ms} at index {index}",
                timeframe=timeframe,
                evaluation_asof_ms=asof_ms,
            )
        if previous_open is not None and open_ms < previous_open:
            return CandleValidationResult(
                ok=False,
                reason=REASON_OUT_OF_ORDER_CANDLE,
                detail=f"open {open_ms} at index {index} is out of order",
                timeframe=timeframe,
                evaluation_asof_ms=asof_ms,
            )
        reason, detail = _validate_ohlc(candle, index)
        if reason != REASON_OK:
            return CandleValidationResult(
                ok=False,
                reason=reason,
                detail=detail,
                timeframe=timeframe,
                evaluation_asof_ms=asof_ms,
            )
        seen[open_ms] = candle
        previous_open = open_ms
        if open_ms + timeframe_ms <= asof_ms:
            closed.append(candle)

    # candles form a strictly ascending grid-aligned sequence here, so the
    # not-yet-closed / future candles form exactly one trailing suffix
    dropped = tuple(candles[len(closed):])
    if not closed:
        return CandleValidationResult(
            ok=False,
            reason=REASON_NO_CLOSED_CANDLES,
            detail=f"no candle is fully closed at {asof_ms}",
            timeframe=timeframe,
            evaluation_asof_ms=asof_ms,
            dropped_unclosed=dropped,
        )

    # gap accounting over the CLOSED prefix only — never gap-filled
    expected = int(closed[0].timestamp)
    for candle in closed:
        open_ms = int(candle.timestamp)
        if open_ms != expected:
            return CandleValidationResult(
                ok=False,
                reason=REASON_CANDLE_GAP,
                detail=f"missing {timeframe} bucket(es) between {expected} and {open_ms}",
                timeframe=timeframe,
                evaluation_asof_ms=asof_ms,
            )
        expected = open_ms + timeframe_ms

    if len(closed) < int(min_closed_candles):
        return CandleValidationResult(
            ok=False,
            reason=REASON_INSUFFICIENT_HISTORY,
            detail=f"{len(closed)} closed candles < required {int(min_closed_candles)}",
            timeframe=timeframe,
            evaluation_asof_ms=asof_ms,
            dropped_unclosed=dropped,
        )

    expected_latest = latest_closed_open_ms(asof_ms, timeframe_ms)
    if require_latest_closed and int(closed[-1].timestamp) != expected_latest:
        return CandleValidationResult(
            ok=False,
            reason=REASON_MISSING_LATEST_CLOSED_CANDLE,
            detail=(
                f"source tail stops at {int(closed[-1].timestamp)}; "
                f"newest closed bucket at {asof_ms} is {expected_latest}"
            ),
            timeframe=timeframe,
            evaluation_asof_ms=asof_ms,
            dropped_unclosed=dropped,
        )

    return CandleValidationResult(
        ok=True,
        reason=REASON_OK,
        timeframe=timeframe,
        evaluation_asof_ms=asof_ms,
        closed_candles=tuple(closed),
        dropped_unclosed=dropped,
        latest_closed_open_ms=expected_latest,
    )
