"""Issue #176 Phase-2 causal market-data contracts (Blocker B3).

Strict validation of closed 5m execution series and closed 1H level series for
the Phase-2 observer. Fixes audit findings D1 (missing 5m contiguity/duplicate
validation) and D2 (silent 1H gap handling) by making every violation explicit
and fail-closed, and by reporting coverage instead of silently dropping hours.

Pure functions only: no I/O, no network, no database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.models import Candle

BAR_MS = 300_000
HTF_MS = 3_600_000
FIVE_MIN_BARS_PER_HOUR = 12

REASON_OK = "OK"
REASON_EMPTY_SERIES = "EMPTY_SERIES"
REASON_UNALIGNED_CANDLE = "UNALIGNED_CANDLE"
REASON_DUPLICATE_CANDLE = "DUPLICATE_CANDLE"
REASON_CONFLICTING_CANDLE = "CONFLICTING_CANDLE"
REASON_OUT_OF_ORDER_CANDLE = "OUT_OF_ORDER_CANDLE"
REASON_NON_CONTIGUOUS_5M = "NON_CONTIGUOUS_5M"
REASON_FUTURE_OR_UNCLOSED_CANDLE = "FUTURE_OR_UNCLOSED_CANDLE"
REASON_STALE_PREFIX = "STALE_PREFIX"
REASON_INVALID_OHLC = "INVALID_OHLC"
REASON_UNALIGNED_1H = "UNALIGNED_1H"
REASON_UNCLOSED_1H = "UNCLOSED_1H"


class ContractViolation(ValueError):
    """A fail-closed market-data contract violation with a stable reason code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class HtfCoverage:
    """Explicit 1H coverage accounting. Never silently drop an hour."""

    complete_hours: tuple[int, ...] = ()
    partial_hours: tuple[int, ...] = ()
    missing_hours: tuple[int, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.partial_hours and not self.missing_hours


@dataclass
class ValidationResult:
    ok: bool
    reason: str = REASON_OK
    detail: str = ""


def _validate_ohlc(candle: Candle, index: int) -> None:
    values = (float(candle.open), float(candle.high), float(candle.low), float(candle.close))
    if min(values) <= 0:
        raise ContractViolation(REASON_INVALID_OHLC, f"non-positive price at index {index}")
    high, low = values[1], values[2]
    if low > min(values[0], values[3]) or high < max(values[0], values[3]):
        raise ContractViolation(REASON_INVALID_OHLC, f"inconsistent OHLC at index {index}")


def validate_execution_series(candles: Sequence[Candle], asof_ms: int) -> None:
    """Validate a closed 5m execution prefix ending at the decision time.

    Contract:
      * every bar is aligned to a 5m open, strictly ascending by exactly BAR_MS;
      * no duplicate open timestamps (identical OR conflicting);
      * every bar is fully closed at asof (open + BAR_MS <= asof);
      * the last bar closes exactly at asof (no stale prefix: a gap of one full
        bar or more between the last close and the decision time is rejected).
    """
    if not candles:
        raise ContractViolation(REASON_EMPTY_SERIES, "no execution candles")
    seen: dict[int, Candle] = {}
    previous_open = None
    for index, candle in enumerate(candles):
        open_ms = int(candle.timestamp)
        if open_ms % BAR_MS:
            raise ContractViolation(REASON_UNALIGNED_CANDLE, f"open {open_ms} at index {index}")
        if open_ms in seen:
            same = seen[open_ms]
            identical = (
                float(same.open) == float(candle.open)
                and float(same.high) == float(candle.high)
                and float(same.low) == float(candle.low)
                and float(same.close) == float(candle.close)
                and float(same.volume) == float(candle.volume)
            )
            raise ContractViolation(
                REASON_DUPLICATE_CANDLE if identical else REASON_CONFLICTING_CANDLE,
                f"repeated open {open_ms} at index {index}",
            )
        seen[open_ms] = candle
        if previous_open is not None:
            if open_ms < previous_open:
                raise ContractViolation(REASON_OUT_OF_ORDER_CANDLE, f"open {open_ms} at index {index}")
            if open_ms != previous_open + BAR_MS:
                raise ContractViolation(
                    REASON_NON_CONTIGUOUS_5M,
                    f"gap {previous_open} -> {open_ms} at index {index}",
                )
        if open_ms + BAR_MS > asof_ms:
            raise ContractViolation(
                REASON_FUTURE_OR_UNCLOSED_CANDLE,
                f"bar {open_ms} not closed at {asof_ms}",
            )
        _validate_ohlc(candle, index)
        previous_open = open_ms
    last_close = int(candles[-1].timestamp) + BAR_MS
    if asof_ms - last_close >= BAR_MS:
        raise ContractViolation(
            REASON_STALE_PREFIX,
            f"last closed bar ends {last_close}, decision at {asof_ms}",
        )
    if last_close != asof_ms:
        raise ContractViolation(
            REASON_STALE_PREFIX,
            f"prefix must end at the decision close {asof_ms}, got {last_close}",
        )


def validate_htf_series(candles: Sequence[Candle], asof_ms: int) -> None:
    """Validate a provider-supplied closed 1H series (alignment, order, closure).

    Gap *accounting* for 1H is done by :func:`htf_coverage_from_htf_series`;
    this function only rejects structurally invalid input.
    """
    if not candles:
        raise ContractViolation(REASON_EMPTY_SERIES, "no 1h candles")
    seen: set[int] = set()
    previous = None
    for index, candle in enumerate(candles):
        open_ms = int(candle.timestamp)
        if open_ms % HTF_MS:
            raise ContractViolation(REASON_UNALIGNED_1H, f"open {open_ms} at index {index}")
        if open_ms in seen:
            raise ContractViolation(REASON_DUPLICATE_CANDLE, f"repeated 1h open {open_ms}")
        seen.add(open_ms)
        if previous is not None and open_ms < previous:
            raise ContractViolation(REASON_OUT_OF_ORDER_CANDLE, f"1h open {open_ms} at index {index}")
        if open_ms + HTF_MS > asof_ms:
            raise ContractViolation(REASON_UNCLOSED_1H, f"1h bar {open_ms} not closed at {asof_ms}")
        _validate_ohlc(candle, index)
        previous = open_ms


def aggregate_closed_htf(
    execution_candles: Sequence[Candle], asof_ms: int
) -> tuple[list[Candle], HtfCoverage]:
    """Aggregate closed 1H candles from a validated 5m prefix.

    An hour is usable iff it contains exactly 12 contiguous closed 5m bars and
    the hour itself is closed at the decision time. Partial and missing hours
    are excluded AND reported — never silently dropped.
    """
    buckets: dict[int, list[Candle]] = {}
    for candle in execution_candles:
        open_ms = int(candle.timestamp)
        hour = open_ms // HTF_MS * HTF_MS
        buckets.setdefault(hour, []).append(candle)
    if not buckets:
        return [], HtfCoverage()
    first_hour = min(buckets)
    last_hour = max(buckets)
    complete: list[int] = []
    partial: list[int] = []
    missing: list[int] = []
    out: list[Candle] = []
    hour = first_hour
    while hour <= last_hour:
        if hour + HTF_MS > asof_ms:
            break
        group = sorted(buckets.get(hour, []), key=lambda c: int(c.timestamp))
        contiguous = (
            len(group) == FIVE_MIN_BARS_PER_HOUR
            and [int(c.timestamp) for c in group]
            == [hour + i * BAR_MS for i in range(FIVE_MIN_BARS_PER_HOUR)]
        )
        if contiguous:
            complete.append(hour)
            out.append(
                Candle(
                    hour,
                    group[0].open,
                    max(float(c.high) for c in group),
                    min(float(c.low) for c in group),
                    group[-1].close,
                    sum(float(c.volume) for c in group),
                )
            )
        elif group:
            partial.append(hour)
        else:
            missing.append(hour)
        hour += HTF_MS
    return out, HtfCoverage(
        complete_hours=tuple(complete),
        partial_hours=tuple(partial),
        missing_hours=tuple(missing),
    )


def htf_coverage_from_htf_series(candles: Sequence[Candle], asof_ms: int) -> HtfCoverage:
    """Coverage accounting for a provider 1H series over its own span."""
    opens = sorted(int(c.timestamp) for c in candles if int(c.timestamp) + HTF_MS <= asof_ms)
    if not opens:
        return HtfCoverage()
    complete = tuple(opens)
    missing: list[int] = []
    cursor = opens[0]
    present = set(opens)
    while cursor <= opens[-1]:
        if cursor not in present:
            missing.append(cursor)
        cursor += HTF_MS
    return HtfCoverage(complete_hours=complete, missing_hours=tuple(missing))
