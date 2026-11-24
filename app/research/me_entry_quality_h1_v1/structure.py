"""Issue #297 (H1-A.2) — causal swing structure and BOS diagnostics.

Research-only, pure functions: no I/O, no network, no database, no clock
reads.  Input is a validated, contiguous, fully-closed HTF series (see
``htf_candles.validate_htf_candles``); every candle timestamp is the candle
OPEN time in UTC epoch milliseconds.

Swing semantics (frozen-candidate; documented tie-breaking):

* **Swing High** at representative index ``i`` with ``swing_lookback = k``:
  ``high[i] >= high[i +/- j]`` for all ``j = 1..k`` AND at least one strict
  ``high[i - j] < high[i]`` on the left and one strict ``high[i + j] <
  high[i]`` on the right.  (Swing Low is symmetric on ``low`` with ``>=`` /
  ``>``.)  The strictness clauses forbid a swing inside a flat region.
* **Equal-extremum (plateau) policy** — deterministic, no repeated
  confirmation of one level:

  * a maximal run of consecutive candles with exactly equal highs (lows) is
    ONE formation level: only the first qualifying index of the run becomes
    the representative swing; later qualifying indices of the same run are
    suppressed;
  * two independent swings separated by a strictly lower (higher) print are
    separate formations even when their prices are exactly equal — they are
    never suppressed for sharing a price (P2-11 parity).

* **Timestamps**:

  * ``formed_at_ms`` — open time of the representative extremum candle;
  * ``confirmed_at_ms`` — CLOSE time of the last right candle required for
    confirmation: ``timestamp[i + k] + timeframe_ms``.  A swing is unusable
    before ``confirmed_at_ms`` (``usable_swings`` enforces
    ``confirmed_at_ms <= evaluation_asof_ms``).

* **Equality semantics** (``pair_relation``): prices ``a`` and ``b`` are
  EQUAL iff ``abs(a - b) <= EQUAL_REL_TOL * max(abs(a), abs(b))`` with
  ``EQUAL_REL_TOL = 1e-9``.  This tolerance absorbs float-representation
  noise ONLY — it is deliberately far below one market tick and is NOT a
  market "same level" tolerance.  EQUAL never counts as HH/HL/LH/LL.

BOS (break of structure) — diagnostic only, never overrides the regime:

* a **Bullish BOS** is recorded when a candle CLOSES strictly above a
  previously confirmed Swing High level; a **Bearish BOS** when a candle
  CLOSES strictly below a previously confirmed Swing Low level.  Wick-only
  excursions beyond the level with a close back inside are NOT breaks
  (close-confirmation protocol);
* the level must be confirmed strictly BEFORE the break close
  (``level_confirmed_at_ms < break_confirmed_at_ms``).  (For the geometry of
  a swing the ``k`` right candles can never close beyond the level, so a
  same-close tie is impossible — the strict check is belt-and-braces.)
* each confirmed level produces at most ONE BOS event: its first
  close-beyond candle.  One candle may produce at most one event per
  direction, attributed to the NEAREST confirmed level in the break
  direction (highest Swing High below the close / lowest Swing Low above the
  close);
* every event carries ``level_confirmed_at_ms`` and ``break_confirmed_at_ms``
  and both are required to be ``<= evaluation_asof_ms``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.models import Candle

#: frozen-candidate research default — NOT an approved frozen OOS parameter.
#: Parameterized so offline sensitivity tests can run lookback 2/3/5.
SWING_LOOKBACK_DEFAULT = 3

SWING_HIGH = "SWING_HIGH"
SWING_LOW = "SWING_LOW"

RELATION_HIGHER = "HIGHER"
RELATION_LOWER = "LOWER"
RELATION_EQUAL = "EQUAL"

BOS_BULLISH = "BULLISH_BOS"
BOS_BEARISH = "BEARISH_BOS"
BOS_NONE = "NO_BOS"

#: float-representation noise tolerance (relative), far below one market tick
EQUAL_REL_TOL = 1e-9


@dataclass(frozen=True)
class Swing:
    """One causally confirmed structural extremum."""

    formed_at_ms: int
    confirmed_at_ms: int
    price: float
    swing_type: str
    #: index of the representative candle inside the analyzed window
    index: int = -1


@dataclass(frozen=True)
class BosEvent:
    """One close-confirmed break of a previously confirmed swing level."""

    direction: str
    level_price: float
    level_type: str
    level_formed_at_ms: int
    level_confirmed_at_ms: int
    break_open_ms: int
    break_confirmed_at_ms: int


def pair_relation(previous: float, current: float) -> str:
    """HIGHER / LOWER / EQUAL with an explicit float-noise equality policy."""
    tol = EQUAL_REL_TOL * max(abs(float(previous)), abs(float(current)))
    delta = float(current) - float(previous)
    if delta > tol:
        return RELATION_HIGHER
    if delta < -tol:
        return RELATION_LOWER
    return RELATION_EQUAL


def _strictly_beyond(close: float, level_price: float) -> bool:
    """True iff ``close`` is beyond the level using the equality policy."""
    return pair_relation(level_price, close) == RELATION_HIGHER


def detect_swings(
    candles: Sequence[Candle],
    *,
    timeframe_ms: int,
    swing_lookback: int = SWING_LOOKBACK_DEFAULT,
) -> tuple[Swing, ...]:
    """Detect all geometric swings in a validated closed series.

    Pure geometry: the result contains swings whose ``confirmed_at_ms`` may
    exceed a given evaluation time.  Gate usage through
    :func:`usable_swings` — a swing MUST NOT influence classification before
    ``confirmed_at_ms``.
    """
    if not isinstance(swing_lookback, int) or isinstance(swing_lookback, bool) or swing_lookback < 1:
        raise ValueError("swing_lookback must be a positive integer")
    if int(timeframe_ms) <= 0:
        raise ValueError("timeframe_ms must be positive")
    k = int(swing_lookback)
    n = len(candles)
    swings: list[Swing] = []
    if n < 2 * k + 1:
        return ()

    for swing_type, attr, keep in (
        (SWING_HIGH, "high", "max"),
        (SWING_LOW, "low", "min"),
    ):
        run_price: float | None = None
        run_repped = False
        for i in range(k, n - k):
            price = float(getattr(candles[i], attr))
            # P2-11 parity: plateau collapse per maximal equal-price run
            if price != run_price:
                run_price = price
                run_repped = False
            left = [float(getattr(candles[i - j], attr)) for j in range(1, k + 1)]
            right = [float(getattr(candles[i + j], attr)) for j in range(1, k + 1)]
            if keep == "max":
                left_ok = all(v <= price for v in left) and any(v < price for v in left)
                right_ok = all(v <= price for v in right) and any(v < price for v in right)
            else:
                left_ok = all(v >= price for v in left) and any(v > price for v in left)
                right_ok = all(v >= price for v in right) and any(v > price for v in right)
            if not (left_ok and right_ok) or run_repped:
                continue
            run_repped = True
            swings.append(
                Swing(
                    formed_at_ms=int(candles[i].timestamp),
                    confirmed_at_ms=int(candles[i + k].timestamp) + int(timeframe_ms),
                    price=price,
                    swing_type=swing_type,
                    index=i,
                )
            )
    swings.sort(key=lambda s: (s.confirmed_at_ms, s.formed_at_ms, s.swing_type, s.index))
    return tuple(swings)


def usable_swings(swings: Sequence[Swing], evaluation_asof_ms: int) -> tuple[Swing, ...]:
    """Swings confirmed at or before the evaluation ASOF (causal gate).

    ``confirmed_at_ms <= evaluation_asof_ms`` is mandatory: a formed-but-not
    -yet-confirmed swing is NEVER used.
    """
    asof = int(evaluation_asof_ms)
    return tuple(s for s in swings if int(s.confirmed_at_ms) <= asof)


def detect_bos_events(
    candles: Sequence[Candle],
    swings: Sequence[Swing],
    *,
    timeframe_ms: int,
    evaluation_asof_ms: int,
) -> tuple[BosEvent, ...]:
    """Detect close-confirmed BOS events within a closed series window.

    Only swings with ``confirmed_at_ms <= evaluation_asof_ms`` are used as
    levels and every emitted event satisfies
    ``level_confirmed_at_ms < break_confirmed_at_ms <= evaluation_asof_ms``.
    """
    asof = int(evaluation_asof_ms)
    levels = usable_swings(swings, asof)
    events: list[BosEvent] = []
    used_levels: set[int] = set()
    for index, candle in enumerate(candles):
        open_ms = int(candle.timestamp)
        break_confirmed = open_ms + int(timeframe_ms)
        if break_confirmed > asof:
            continue  # never detect a break on an unclosed candle
        close = float(candle.close)
        for direction, level_type, pick in (
            (BOS_BULLISH, SWING_HIGH, "max"),
            (BOS_BEARISH, SWING_LOW, "min"),
        ):
            candidates = [
                (level_index, swing)
                for level_index, swing in enumerate(levels)
                if level_index not in used_levels
                and swing.swing_type == level_type
                and int(swing.confirmed_at_ms) < break_confirmed
                and (
                    _strictly_beyond(close, swing.price)
                    if direction == BOS_BULLISH
                    else _strictly_beyond(swing.price, close)
                )
            ]
            if not candidates:
                continue
            chosen = (
                max(candidates, key=lambda item: item[1].price)
                if pick == "max"
                else min(candidates, key=lambda item: item[1].price)
            )
            level_index, swing = chosen
            # A close may cross several confirmed levels at once.  Publish
            # only the nearest one, but retire ALL crossed levels: their
            # first close-beyond is this candle, not a later one.
            used_levels.update(idx for idx, _ in candidates)
            events.append(
                BosEvent(
                    direction=direction,
                    level_price=float(swing.price),
                    level_type=level_type,
                    level_formed_at_ms=int(swing.formed_at_ms),
                    level_confirmed_at_ms=int(swing.confirmed_at_ms),
                    break_open_ms=open_ms,
                    break_confirmed_at_ms=break_confirmed,
                )
            )
    events.sort(key=lambda e: (e.break_confirmed_at_ms, e.level_confirmed_at_ms, e.direction))
    return tuple(events)
