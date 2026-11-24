"""Issue #297 (H1-A.3 / H1-A.4) — HTF market-structure classification.

Research-only, pure functions: no I/O, no network, no database, no clock
reads.  Classification runs at the ORIGINAL event time
(``evaluation_asof_ms``) over causally closed candles only — never at the
time of the later computation.

Per-timeframe regimes (independent for 4H and 1D):

===============  =============================================================
``BULLISH``      the last two CONFIRMED Swing Highs form HH and the last two
                 CONFIRMED Swing Lows form HL
``BEARISH``      the last two CONFIRMED Swing Highs form LH and the last two
                 CONFIRMED Swing Lows form LL
``NEUTRAL``      valid, sufficient source history but mixed / undefined
                 structure (including EQUAL highs or lows)
``INVALID_SOURCE``  insufficient data or a data-integrity violation
                 (gaps, invalid OHLC, duplicates, insufficient history or
                 insufficient CONFIRMED structure —
                 ``INSUFFICIENT_CONFIRMED_STRUCTURE``).  Missing data is
                 NEVER converted into ``NEUTRAL``.
===============  =============================================================

Swing / BOS causality invariants (Issue #297 §6):

* ``confirmed_at_ms <= evaluation_asof_ms`` for every swing used;
* ``break_confirmed_at_ms <= evaluation_asof_ms`` for every BOS used, and
  ``level_confirmed_at_ms < break_confirmed_at_ms``;
* history is analyzed through a FIXED canonical lookback window (the most
  recent ``canonical_lookback`` closed candles), so changing older history
  can never change a fixed-ASOF result;
* appending future candles can never change an earlier ASOF result;
* results are deterministic across reruns and input permutations of the
  same correctly-ordered data.

BOS (``BULLISH_BOS`` / ``BEARISH_BOS`` / ``NO_BOS``) is a SEPARATE
diagnostic: it never overrides the HH/HL / LH/LL regime in this version.

Frozen-candidate parameters (NOT approved as frozen OOS parameters):

* ``swing_lookback = 3`` (research default; parameterized for offline
  sensitivity runs at 2/3/5);
* ``canonical_lookback = 60`` closed candles;
* ``EQUAL_REL_TOL = 1e-9`` (float-noise equality policy, see
  ``structure.pair_relation``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from app.models import Candle

from .htf_candles import (
    TIMEFRAME_1D,
    TIMEFRAME_4H,
    TIMEFRAME_MS,
    validate_htf_candles,
)
from .structure import (
    BOS_BEARISH,
    BOS_BULLISH,
    BOS_NONE,
    RELATION_HIGHER,
    RELATION_LOWER,
    SWING_HIGH,
    SWING_LOW,
    SWING_LOOKBACK_DEFAULT,
    BosEvent,
    Swing,
    detect_bos_events,
    detect_swings,
    pair_relation,
    usable_swings,
)

H1_VERSION = "1.0.0"

REGIME_BULLISH = "BULLISH"
REGIME_BEARISH = "BEARISH"
REGIME_NEUTRAL = "NEUTRAL"
REGIME_INVALID_SOURCE = "INVALID_SOURCE"

BOS_NOT_EVALUATED = "NOT_EVALUATED"

COVERAGE_VALID = "VALID"
COVERAGE_INVALID_SOURCE = "INVALID_SOURCE"

REASON_INSUFFICIENT_CONFIRMED_STRUCTURE = "INSUFFICIENT_CONFIRMED_STRUCTURE"
REASON_OK = "OK"

COMBINED_REGIME_SEPARATOR = "/"

#: frozen-candidate research default — the analysis window is the most recent
#: ``CANONICAL_LOOKBACK_CANDLES`` closed candles.  NOT approved for OOS.
CANONICAL_LOOKBACK_CANDLES = 60

REGIMES_3 = (REGIME_BULLISH, REGIME_BEARISH, REGIME_NEUTRAL)

#: all nine 4H/1D combinations, preserved verbatim — conflicting HTF modes
#: are NEVER merged into one averaged feature
VALID_COMBINED_REGIMES: tuple[str, ...] = tuple(
    f"{four_hour}{COMBINED_REGIME_SEPARATOR}{daily}"
    for four_hour in REGIMES_3
    for daily in REGIMES_3
)


@dataclass(frozen=True)
class TimeframeState:
    """Independent classification state for ONE timeframe."""

    timeframe: str
    regime: str
    bos: str
    coverage_status: str
    reason: str = REASON_OK
    detail: str = ""
    evaluation_asof_ms: int = 0
    swing_lookback: int = SWING_LOOKBACK_DEFAULT
    canonical_lookback: int = CANONICAL_LOOKBACK_CANDLES
    window_candle_count: int = 0
    window_open_ms: int = 0
    confirmed_swings: tuple[Swing, ...] = ()
    bos_events: tuple[BosEvent, ...] = ()

    @property
    def valid(self) -> bool:
        return self.regime != REGIME_INVALID_SOURCE

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "bos": self.bos,
            "coverage_status": self.coverage_status,
            "reason": self.reason,
            "detail": self.detail,
        }


def _invalid_state(
    timeframe: str,
    *,
    reason: str,
    detail: str,
    evaluation_asof_ms: int,
    swing_lookback: int,
    canonical_lookback: int,
    confirmed_swings: tuple[Swing, ...] = (),
) -> TimeframeState:
    return TimeframeState(
        timeframe=timeframe,
        regime=REGIME_INVALID_SOURCE,
        bos=BOS_NOT_EVALUATED,
        coverage_status=COVERAGE_INVALID_SOURCE,
        reason=reason,
        detail=detail,
        evaluation_asof_ms=int(evaluation_asof_ms),
        swing_lookback=int(swing_lookback),
        canonical_lookback=int(canonical_lookback),
        confirmed_swings=confirmed_swings,
    )


def _analysis_window(
    closed: Sequence[Candle], canonical_lookback: int
) -> tuple[Candle, ...]:
    return tuple(closed[-int(canonical_lookback):])


def _bos_label(events: Sequence[BosEvent]) -> str:
    """Direction of the most recent BOS event.

    Deterministic tie-breaking for the pathological case of a single close
    breaking both a confirmed Swing High and a confirmed Swing Low level:
    the break of the more recently confirmed level wins; if still tied,
    ``BULLISH_BOS`` wins by fixed rank.  Never ambiguous, never averaged.
    """
    if not events:
        return BOS_NONE
    ranked = max(
        events,
        key=lambda e: (
            e.break_confirmed_at_ms,
            e.level_confirmed_at_ms,
            1 if e.direction == BOS_BULLISH else 0,
        ),
    )
    return ranked.direction


def classify_timeframe(
    candles: Sequence[Any],
    *,
    timeframe: str,
    evaluation_asof_ms: Any,
    swing_lookback: int = SWING_LOOKBACK_DEFAULT,
    canonical_lookback: int = CANONICAL_LOOKBACK_CANDLES,
) -> TimeframeState:
    """Classify ONE HTF regime causally at ``evaluation_asof_ms``.

    Fail-closed: any candle-source violation yields ``INVALID_SOURCE`` with
    the explicit source reason code — never a fabricated trend and never a
    silent ``NEUTRAL``.
    """
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    if (
        not isinstance(swing_lookback, int)
        or isinstance(swing_lookback, bool)
        or swing_lookback < 1
    ):
        raise ValueError("swing_lookback must be a positive integer")
    if (
        not isinstance(canonical_lookback, int)
        or isinstance(canonical_lookback, bool)
        or canonical_lookback < 2 * swing_lookback + 1
    ):
        raise ValueError("canonical_lookback must be an integer >= 2 * swing_lookback + 1")

    asof_ok = isinstance(evaluation_asof_ms, int) and not isinstance(evaluation_asof_ms, bool)
    asof_ms = int(evaluation_asof_ms) if asof_ok else -1
    min_closed = 2 * int(swing_lookback) + 1
    validation = validate_htf_candles(
        candles,
        timeframe=timeframe,
        evaluation_asof_ms=evaluation_asof_ms,
        min_closed_candles=min_closed,
    )
    if not validation.ok:
        return _invalid_state(
            timeframe,
            reason=validation.reason,
            detail=validation.detail,
            evaluation_asof_ms=max(asof_ms, 0),
            swing_lookback=swing_lookback,
            canonical_lookback=canonical_lookback,
        )

    window = _analysis_window(validation.closed_candles, canonical_lookback)
    timeframe_ms = TIMEFRAME_MS[timeframe]
    swings = detect_swings(
        window, timeframe_ms=timeframe_ms, swing_lookback=swing_lookback
    )
    confirmed = usable_swings(swings, asof_ms)
    highs = tuple(
        sorted(
            (s for s in confirmed if s.swing_type == SWING_HIGH),
            key=lambda s: (s.confirmed_at_ms, s.formed_at_ms, s.index),
        )
    )
    lows = tuple(
        sorted(
            (s for s in confirmed if s.swing_type == SWING_LOW),
            key=lambda s: (s.confirmed_at_ms, s.formed_at_ms, s.index),
        )
    )
    events = detect_bos_events(
        window, swings, timeframe_ms=timeframe_ms, evaluation_asof_ms=asof_ms
    )

    common = dict(
        evaluation_asof_ms=asof_ms,
        swing_lookback=int(swing_lookback),
        canonical_lookback=int(canonical_lookback),
        window_candle_count=len(window),
        window_open_ms=int(window[0].timestamp),
    )

    if len(highs) < 2 or len(lows) < 2:
        return TimeframeState(
            timeframe=timeframe,
            regime=REGIME_INVALID_SOURCE,
            bos=_bos_label(events),
            coverage_status=COVERAGE_VALID,
            reason=REASON_INSUFFICIENT_CONFIRMED_STRUCTURE,
            detail=(
                f"confirmed swing highs={len(highs)} swing lows={len(lows)} "
                f"at {asof_ms}; need >= 2 of each"
            ),
            confirmed_swings=confirmed,
            bos_events=events,
            **common,
        )

    high_relation = pair_relation(highs[-2].price, highs[-1].price)
    low_relation = pair_relation(lows[-2].price, lows[-1].price)
    hh = high_relation == RELATION_HIGHER
    lh = high_relation == RELATION_LOWER
    hl = low_relation == RELATION_HIGHER
    ll = low_relation == RELATION_LOWER

    if hh and hl:
        regime = REGIME_BULLISH
        reason = REASON_OK
    elif lh and ll:
        regime = REGIME_BEARISH
        reason = REASON_OK
    else:
        regime = REGIME_NEUTRAL
        reason = REASON_OK

    return TimeframeState(
        timeframe=timeframe,
        regime=regime,
        bos=_bos_label(events),
        coverage_status=COVERAGE_VALID,
        reason=reason,
        detail=(
            f"last highs {highs[-2].price:.12g}->{highs[-1].price:.12g}, "
            f"last lows {lows[-2].price:.12g}->{lows[-1].price:.12g}"
        ),
        confirmed_swings=confirmed,
        bos_events=events,
        **common,
    )


def classify_combined(
    candles_4h: Sequence[Any],
    candles_1d: Sequence[Any],
    *,
    evaluation_asof_ms: Any,
    swing_lookback: int = SWING_LOOKBACK_DEFAULT,
    canonical_lookback: int = CANONICAL_LOOKBACK_CANDLES,
) -> dict[str, Any]:
    """Combined 4H/1D contract (Issue #297 §H1-A.4).

    The two timeframe states are computed and preserved INDEPENDENTLY.
    ``combined_regime`` keeps every one of the nine ``<4H>/<1D>``
    combinations verbatim (conflicting modes are never averaged).  If either
    timeframe is ``INVALID_SOURCE`` the other one still reports its own valid
    state, but ``combined_eligible`` is False — the combined cohort is not
    fully eligible.
    """
    state_4h = classify_timeframe(
        candles_4h,
        timeframe=TIMEFRAME_4H,
        evaluation_asof_ms=evaluation_asof_ms,
        swing_lookback=swing_lookback,
        canonical_lookback=canonical_lookback,
    )
    state_1d = classify_timeframe(
        candles_1d,
        timeframe=TIMEFRAME_1D,
        evaluation_asof_ms=evaluation_asof_ms,
        swing_lookback=swing_lookback,
        canonical_lookback=canonical_lookback,
    )
    combined_regime = (
        f"{state_4h.regime}{COMBINED_REGIME_SEPARATOR}{state_1d.regime}"
    )
    return {
        "h1_version": H1_VERSION,
        "evaluation_asof_ms": state_4h.evaluation_asof_ms,
        "htf_4h": state_4h.to_dict(),
        "htf_1d": state_1d.to_dict(),
        "combined_regime": combined_regime,
        "combined_eligible": combined_regime in VALID_COMBINED_REGIMES,
        "swing_lookback": int(swing_lookback),
        "canonical_lookback": int(canonical_lookback),
    }
