"""Issue #314 / H1-D — causal ASOF coverage (``H1D_CAUSAL_ASOF_COVERAGE_V1``).

Pure and deterministic.  No network, no database, no clock, no I/O.

Contract (Issue #314):

* the causal ASOF is ``source_event.signal_time`` at **microsecond**
  precision — NEVER ``captured_at``.  A candle is admissible iff its CLOSE
  time is at or before the ASOF: ``(open_ms + timeframe_ms) * 1000 <=
  asof_us``.  Future / unclosed candles never influence anything and are
  reported explicitly in ``dropped_unclosed_opens``;
* only FULLY CLOSED candles on the UTC 4H / 1D grids
  (``open_ms % timeframe_ms == 0``; 4H opens at 00/04/08/12/16/20 UTC, 1D
  opens at 00:00 UTC);
* the canonical window is exactly ``CANONICAL_LOOKBACK = 60`` closed
  candles per timeframe ending at the last candle closed at the ASOF;
* strict fail-closed checks — wrong bucket alignment, duplicates,
  out-of-order rows, non-positive prices, ``high < low``, NaN/inf, negative
  volume → ``INVALID_SOURCE`` with an exact reason code; a hole strictly
  INSIDE the delivered closed series is a ``CANDLE_GAP`` →
  ``INVALID_SOURCE``; insufficient history (window shorter than 60/60 with
  a contiguous source, e.g. 59/60) → ``INCOMPLETE_COVERAGE`` with the
  EXACT missing buckets;
* never synthetic-fill, never look-ahead: nothing is repaired, reordered,
  interpolated or extrapolated — a data problem is never a market mode.

Terminology note (spec reconciliation): the issue text says the window
ends "at the last candle that closed strictly before the ASOF" and, in the
same breath, "a candle is admissible iff its close time <= ASOF at µs
precision".  The operative rule implemented here — matching the frozen
Issue #297 / #308 inclusive-at-close-boundary contract — is the explicit
``iff``: ``close_us <= asof_us`` (a candle becomes available exactly at its
close instant).  The two readings differ only for an ASOF that lands
EXACTLY on a close instant; the boundary tests pin the chosen semantics at
±1 ms / ±1 µs around it.

Empty payload → ``INVALID_SOURCE``/``EMPTY_SERIES`` (nothing was delivered,
so there is no source at all); a delivered-but-short source is
``INCOMPLETE_COVERAGE``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

H1D_COVERAGE_VERSION = "H1D_CAUSAL_ASOF_COVERAGE_V1"

CANONICAL_LOOKBACK = 60

TIMEFRAME_4H = "4H"
TIMEFRAME_1D = "1D"
TIMEFRAME_MS: dict[str, int] = {
    TIMEFRAME_4H: 240 * 60_000,      # 14_400_000
    TIMEFRAME_1D: 1_440 * 60_000,    # 86_400_000
}

# ─── status codes (fail closed) ─────────────────────────────────────────────
COVERAGE_COMPLETE = "COMPLETE_COVERAGE"
COVERAGE_INCOMPLETE = "INCOMPLETE_COVERAGE"
COVERAGE_INVALID = "INVALID_SOURCE"

# ─── reason codes (stable) ──────────────────────────────────────────────────
REASON_OK = "OK"
REASON_INVALID_ASOF = "INVALID_ASOF"
REASON_EMPTY_SERIES = "EMPTY_SERIES"
REASON_ROW_PARSE_ERROR = "ROW_PARSE_ERROR"
REASON_UNALIGNED_CANDLE = "UNALIGNED_CANDLE"
REASON_DUPLICATE_CANDLE = "DUPLICATE_CANDLE"
REASON_CONFLICTING_CANDLE = "CONFLICTING_CANDLE"
REASON_OUT_OF_ORDER_CANDLE = "OUT_OF_ORDER_CANDLE"
REASON_CANDLE_GAP = "CANDLE_GAP"
REASON_INVALID_OHLC = "INVALID_OHLC"
REASON_INVALID_VOLUME = "INVALID_VOLUME"
REASON_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

_ROW_FIELDS = ("open_ms", "open", "high", "low", "close", "volume")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


@dataclass(frozen=True)
class CoverageResult:
    """Exact causal-ASOF coverage diagnostics for ONE timeframe payload."""

    status: str
    reason: str
    detail: str
    timeframe: str
    asof_us: int
    asof_ms: int
    window_open_ms: int
    window_latest_open_ms: int
    expected_buckets: int
    present_buckets: int
    missing_open_ms: tuple[int, ...]
    row_count: int
    closed_row_count: int
    dropped_unclosed_opens: tuple[int, ...]
    first_closed_open_ms: int | None
    last_closed_open_ms: int | None
    coverage_version: str = H1D_COVERAGE_VERSION

    @property
    def ok(self) -> bool:
        return self.status == COVERAGE_COMPLETE

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage_version": self.coverage_version,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "timeframe": self.timeframe,
            "asof_us": self.asof_us,
            "asof_ms": self.asof_ms,
            "window_open_ms": self.window_open_ms,
            "window_latest_open_ms": self.window_latest_open_ms,
            "expected_buckets": self.expected_buckets,
            "present_buckets": self.present_buckets,
            "missing_open_ms": list(self.missing_open_ms),
            "row_count": self.row_count,
            "closed_row_count": self.closed_row_count,
            "dropped_unclosed_opens": list(self.dropped_unclosed_opens),
            "first_closed_open_ms": self.first_closed_open_ms,
            "last_closed_open_ms": self.last_closed_open_ms,
        }


def latest_closed_open_ms(asof_us: int, timeframe_ms: int) -> int:
    """Open time of the newest bucket fully closed at ``asof_us`` (µs).

    The bucket ``[b, b + tf)`` is closed at ``asof_us`` iff
    ``(b + tf) * 1000 <= asof_us`` (inclusive exactly at the close instant).
    """
    return ((int(asof_us) // 1000) - int(timeframe_ms)) // int(timeframe_ms) * int(timeframe_ms)


def canonical_window_bounds(timeframe: str, asof_us: int) -> tuple[int, int]:
    """``(window_open_ms, window_close_ms)`` of the canonical 60-candle window.

    ``window_close_ms`` is the EXCLUSIVE end (= last closed open + tf).
    """
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    if not _is_int(asof_us) or int(asof_us) < 0:
        raise ValueError("asof_us must be a non-negative integer")
    tf_ms = TIMEFRAME_MS[timeframe]
    latest = latest_closed_open_ms(int(asof_us), tf_ms)
    window_open = latest - (CANONICAL_LOOKBACK - 1) * tf_ms
    return window_open, latest + tf_ms


def is_closed_at(open_ms: int, timeframe_ms: int, asof_us: int) -> bool:
    """Causal admissibility at MICROSECOND precision: ``close_us <= asof_us``."""
    return (int(open_ms) + int(timeframe_ms)) * 1000 <= int(asof_us)


class _Invalid(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _row_values(row: Any, index: int) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return {name: row.get(name) for name in _ROW_FIELDS}
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        if len(row) != 6:
            raise _Invalid(
                REASON_ROW_PARSE_ERROR,
                f"row {index}: expected 6 fields {_ROW_FIELDS}, got {len(row)}",
            )
        return dict(zip(_ROW_FIELDS, row))
    raise _Invalid(
        REASON_ROW_PARSE_ERROR,
        f"row {index}: unsupported row type {type(row).__name__}",
    )


def _parse_closed_row(row: Any, index: int) -> None:
    """STRICT parse + OHLCV validity for a CLOSED row (fail closed)."""
    values = _row_values(row, index)
    open_ms = values["open_ms"]
    if not _is_int(open_ms) or int(open_ms) < 0:
        raise _Invalid(
            REASON_ROW_PARSE_ERROR,
            f"row {index}: open_ms must be a non-negative real integer",
        )
    prices: list[float] = []
    for name in ("open", "high", "low", "close"):
        if not _finite(values[name]):
            raise _Invalid(
                REASON_INVALID_OHLC,
                f"row {index}: non-finite or non-numeric {name}",
            )
        price = float(values[name])
        if price <= 0.0:
            raise _Invalid(
                REASON_INVALID_OHLC,
                f"row {index}: non-positive {name} ({price})",
            )
        prices.append(price)
    open_, high, low, close = prices
    if low > high:
        raise _Invalid(
            REASON_INVALID_OHLC, f"row {index}: low > high ({low} > {high})")
    if low > min(open_, close) or high < max(open_, close):
        raise _Invalid(
            REASON_INVALID_OHLC,
            f"row {index}: open/close outside [low, high]",
        )
    if not _finite(values["volume"]):
        raise _Invalid(
            REASON_INVALID_VOLUME,
            f"row {index}: non-finite or non-numeric volume",
        )
    if float(values["volume"]) < 0.0:
        raise _Invalid(
            REASON_INVALID_VOLUME, f"row {index}: negative volume")


def _lenient_open_ms(row: Any, index: int) -> int:
    """Open time of any row (incl. not-yet-closed) — integer only."""
    values = _row_values(row, index)
    open_ms = values["open_ms"]
    if not _is_int(open_ms):
        raise _Invalid(
            REASON_ROW_PARSE_ERROR,
            f"row {index}: open_ms must be a real integer",
        )
    return int(open_ms)


def _rows_identical(row_a: Any, row_b: Any) -> bool:
    try:
        values_a = _row_values(row_a, -1)
        values_b = _row_values(row_b, -1)
    except _Invalid:
        return False
    return all(values_a[name] == values_b[name] for name in _ROW_FIELDS)


def evaluate_coverage(
    rows: Sequence[Any],
    *,
    timeframe: str,
    asof_us: int,
    canonical_lookback: int = CANONICAL_LOOKBACK,
) -> CoverageResult:
    """Exact causal-ASOF coverage of ONE raw payload (pure, fail closed).

    See the module docstring for the full contract.  The result ALWAYS
    carries exact diagnostics (missing buckets, counts) — a partial source
    is never relabeled and a data problem never becomes a market mode.
    """
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    if not _is_int(asof_us) or int(asof_us) < 0:
        raise ValueError("asof_us must be a non-negative integer")
    if not _is_int(canonical_lookback) or int(canonical_lookback) < 1:
        raise ValueError("canonical_lookback must be a positive integer")

    tf_ms = TIMEFRAME_MS[timeframe]
    asof = int(asof_us)
    lookback = int(canonical_lookback)
    latest = latest_closed_open_ms(asof, tf_ms)
    window_open = latest - (lookback - 1) * tf_ms
    expected_buckets = tuple(window_open + i * tf_ms for i in range(lookback))

    def _missing(present_opens: set[int]) -> tuple[int, ...]:
        return tuple(slot for slot in expected_buckets if slot not in present_opens)

    def _build(
        status: str,
        reason: str,
        detail: str,
        *,
        missing: tuple[int, ...] = (),
        present: int = 0,
        closed_opens: tuple[int, ...] = (),
        dropped: tuple[int, ...] = (),
    ) -> CoverageResult:
        return CoverageResult(
            status=status,
            reason=reason,
            detail=detail,
            timeframe=timeframe,
            asof_us=asof,
            asof_ms=asof // 1000,
            window_open_ms=window_open,
            window_latest_open_ms=latest,
            expected_buckets=lookback,
            present_buckets=present,
            missing_open_ms=missing,
            row_count=len(rows),
            closed_row_count=len(closed_opens),
            dropped_unclosed_opens=dropped,
            first_closed_open_ms=closed_opens[0] if closed_opens else None,
            last_closed_open_ms=closed_opens[-1] if closed_opens else None,
        )

    def _invalid(
        exc: _Invalid, *, parsed_opens: tuple[int, ...] = ()
    ) -> CoverageResult:
        """Materialize an ``INVALID_SOURCE`` result with full diagnostics."""
        present_set = set(parsed_opens)
        missing = _missing(present_set)
        return _build(
            COVERAGE_INVALID, exc.reason, exc.detail,
            missing=missing,
            present=lookback - len(missing),
            closed_opens=parsed_opens,
        )

    if not rows:
        return _build(
            COVERAGE_INVALID, REASON_EMPTY_SERIES,
            "no rows delivered — there is no source payload",
            missing=expected_buckets,
            present=0,
        )

    # 1. structural pass over ALL rows: parse open times, require strictly
    #    ascending order, reject duplicates and off-grid timestamps.
    opens: list[int] = []
    seen: dict[int, int] = {}
    previous_open: int | None = None
    for index, row in enumerate(rows):
        try:
            open_ms = _lenient_open_ms(row, index)
        except _Invalid as exc:
            return _invalid(exc, parsed_opens=tuple(opens))
        if open_ms % tf_ms != 0:
            return _invalid(_Invalid(
                REASON_UNALIGNED_CANDLE,
                f"row {index}: open {open_ms} is off the {timeframe} UTC grid",
            ), parsed_opens=tuple(opens))
        if open_ms in seen:
            same_index = seen[open_ms]
            identical = _rows_identical(rows[same_index], row)
            reason = (
                REASON_DUPLICATE_CANDLE if identical else REASON_CONFLICTING_CANDLE
            )
            return _invalid(_Invalid(
                reason,
                f"row {index}: repeated open {open_ms} (first at row {same_index})",
            ), parsed_opens=tuple(opens))
        if previous_open is not None and open_ms < previous_open:
            return _invalid(_Invalid(
                REASON_OUT_OF_ORDER_CANDLE,
                f"row {index}: open {open_ms} is out of order",
            ), parsed_opens=tuple(opens))
        seen[open_ms] = index
        previous_open = open_ms
        opens.append(open_ms)

    # 2. causal split at µs precision: closed rows are strictly validated,
    #    the unclosed/future suffix is reported and NEVER used.
    closed_opens: list[int] = []
    dropped: list[int] = []
    for index, row in enumerate(rows):
        open_ms = opens[index]
        if not is_closed_at(open_ms, tf_ms, asof):
            dropped.append(open_ms)
            continue
        if dropped:
            # impossible with ascending opens — belt and braces, fail closed.
            return _invalid(_Invalid(
                REASON_OUT_OF_ORDER_CANDLE,
                f"row {index}: closed candle after unclosed/future rows",
            ), parsed_opens=tuple(opens))
        try:
            _parse_closed_row(row, index)
        except _Invalid as exc:
            return _invalid(exc, parsed_opens=tuple(opens))
        closed_opens.append(open_ms)

    if not closed_opens:
        return _build(
            COVERAGE_INCOMPLETE, REASON_INSUFFICIENT_HISTORY,
            f"no candle is fully closed at asof_us={asof}",
            missing=expected_buckets,
            present=0,
            closed_opens=(),
            dropped=tuple(dropped),
        )

    # 3. gap accounting over the delivered closed series — a hole strictly
    #    inside it is a corrupt source (INVALID_SOURCE), never gap-filled.
    expected = closed_opens[0]
    for open_ms in closed_opens:
        if open_ms != expected:
            return _invalid(_Invalid(
                REASON_CANDLE_GAP,
                f"missing {timeframe} bucket(s) between {expected} and {open_ms}",
            ), parsed_opens=tuple(closed_opens))
        expected = open_ms + tf_ms

    # 4. canonical window coverage (60 buckets ending at the last closed one).
    present_set = set(closed_opens)
    missing = _missing(present_set)
    present = lookback - len(missing)
    if not missing:
        return _build(
            COVERAGE_COMPLETE, REASON_OK,
            f"{present}/{lookback} canonical closed candles present",
            present=present,
            closed_opens=tuple(closed_opens),
            dropped=tuple(dropped),
        )
    return _build(
        COVERAGE_INCOMPLETE, REASON_INSUFFICIENT_HISTORY,
        f"{present}/{lookback} canonical closed candles present — "
        f"missing {len(missing)} bucket(s)",
        missing=missing,
        present=present,
        closed_opens=tuple(closed_opens),
        dropped=tuple(dropped),
    )
