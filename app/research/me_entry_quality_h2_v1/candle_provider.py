"""Issue #298 / H2 — pure 5m candle provider (caller-supplied rows only).

Research-only, pure: no I/O, no network, no database, no clock reads.
The caller (a bounded offline fetch or a read-only SELECT, both OUTSIDE this
package) supplies raw OHLCV rows; this module only validates and truncates.
The package NEVER fetches candles itself — the project's klines endpoint or
any other acquisition path is a separate deploy-gated concern.

Contract (fail closed, stable reason codes — semantics mirror the Issue
#297 HTF validator for the 5m UTC grid):

* timeframe is the UTC 5m grid (``open_ms % 300_000 == 0``; candle OPEN
  time is the timestamp; the candle ``[open, open + 300_000)`` is CLOSED at
  ``evaluation_asof_ms`` iff ``open + 300_000 <= evaluation_asof_ms``,
  inclusive at the boundary);
* only actually-closed candles are ever used — the trailing unclosed/future
  suffix is excluded BEFORE OHLCV/grid validation (ASOF isolation: a
  corrupt future payload can never influence a historical result) and is
  reported explicitly in ``dropped_unclosed``;
* strictly ascending input, no duplicates (``DUPLICATE_CANDLE`` identical /
  ``CONFLICTING_CANDLE`` otherwise), no gaps between the first and last
  closed candle (``CANDLE_GAP``), no off-grid timestamps, no invalid OHLCV;
* a CLOSED row hidden after the future suffix is a source-order violation
  (``OUT_OF_ORDER_CANDLE``) — a complete prefix can never conceal disorder;
* source provenance is verified UNCONDITIONALLY: the supplied
  :class:`HtfSourceIdentity` (reused unchanged from H1-C) must name a known
  source kind, carry a canonical SHA-256 ``payload_hash`` and match the
  supplied rows exactly (``SOURCE_PAYLOAD_MISMATCH`` otherwise).

Reason-code -> H2 state mapping (used by the pipeline):

* ``INCOMPLETE_COVERAGE_REASONS`` -> ``INCOMPLETE_COVERAGE`` (the data is
  honest but the required range is not fully covered);
* everything else -> ``INVALID_SOURCE`` (the payload cannot be trusted).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.models import Candle
from app.research.me_entry_quality_h1_v1.htf_candles import (
    REASON_CANDLE_GAP,
    REASON_CONFLICTING_CANDLE,
    REASON_DUPLICATE_CANDLE,
    REASON_EMPTY_SERIES,
    REASON_INSUFFICIENT_HISTORY,
    REASON_INVALID_OHLC,
    REASON_INVALID_TIMESTAMP,
    REASON_INVALID_VOLUME,
    REASON_MISSING_LATEST_CLOSED_CANDLE,
    REASON_NO_CLOSED_CANDLES,
    REASON_OK,
    REASON_OUT_OF_ORDER_CANDLE,
    REASON_UNALIGNED_CANDLE,
    latest_closed_open_ms,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    COVERAGE_COMPLETE,
    COVERAGE_NONE,
    COVERAGE_PARTIAL,
    CoverageReport,
    HtfSourceIdentity,
    KNOWN_SOURCE_KINDS,
    REASON_INVALID_SOURCE_IDENTITY,
    REASON_ROW_PARSE_ERROR,
    REASON_SOURCE_PAYLOAD_MISMATCH,
    REASON_UNKNOWN_SOURCE_KIND,
    build_source_identity,
    is_sha256_hex,
    source_payload_hash,
)

#: Reused unchanged from H1-C (Issue #308) — one provenance identity type.
CandleSourceIdentity = HtfSourceIdentity

__all__ = [
    "Candle5mSet",
    "Candle5mValidationResult",
    "CandleSourceIdentity",
    "CANDLE_5M_MS",
    "INCOMPLETE_COVERAGE_REASONS",
    "TIMEFRAME_5M",
    "build_source_identity",
    "is_sha256_hex",
    "provide_5m_candles",
    "source_payload_hash",
    "validate_5m_candles",
]

TIMEFRAME_5M = "5m"
CANDLE_5M_MS = 300_000

#: Minimum closed candles for a defined Wilder RSI(14) (period + 1).
MIN_CLOSED_CANDLES_RSI_SEED = 15

#: Honest-but-incomplete data: valid rows, insufficient range.
INCOMPLETE_COVERAGE_REASONS = frozenset({
    REASON_CANDLE_GAP,
    REASON_EMPTY_SERIES,
    REASON_INSUFFICIENT_HISTORY,
    REASON_MISSING_LATEST_CLOSED_CANDLE,
    REASON_NO_CLOSED_CANDLES,
})

_ROW_FIELDS = ("open_ms", "open", "high", "low", "close", "volume")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


@dataclass(frozen=True)
class Candle5mValidationResult:
    """Outcome of :func:`validate_5m_candles` (fail-closed, explicit)."""

    ok: bool
    reason: str = REASON_OK
    detail: str = ""
    evaluation_asof_ms: int = 0
    #: usable series: sorted, contiguous, on-grid and fully closed at ASOF
    closed_candles: tuple[Candle, ...] = ()
    #: trailing candles NOT usable at the ASOF (unclosed / future) — never
    #: used, never silently dropped
    dropped_unclosed: tuple[Candle, ...] = ()
    latest_closed_open_ms: int = 0

    @property
    def closed_open_ms(self) -> tuple[int, ...]:
        return tuple(int(c.timestamp) for c in self.closed_candles)


class _RowError(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _row_values(row: Any, index: int) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return {name: row.get(name) for name in _ROW_FIELDS}
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        if len(row) != 6:
            raise _RowError(
                REASON_ROW_PARSE_ERROR,
                f"row {index}: expected 6 fields {_ROW_FIELDS}, got {len(row)}",
            )
        return dict(zip(_ROW_FIELDS, row))
    if isinstance(row, Candle):
        return {
            "open_ms": getattr(row, "timestamp", None),
            **{name: getattr(row, name, None) for name in _ROW_FIELDS[1:]},
        }
    raise _RowError(
        REASON_ROW_PARSE_ERROR,
        f"row {index}: unsupported row type {type(row).__name__}",
    )


def _parse_row(row: Any, index: int) -> Candle:
    values = _row_values(row, index)
    open_ms = values["open_ms"]
    if not _is_int(open_ms):
        raise _RowError(
            REASON_ROW_PARSE_ERROR, f"row {index}: open_ms must be a real integer"
        )
    for name in _ROW_FIELDS[1:]:
        if not _finite_number(values[name]):
            raise _RowError(
                REASON_ROW_PARSE_ERROR,
                f"row {index}: {name} must be a finite number",
            )
    return Candle(
        int(open_ms),
        float(values["open"]),
        float(values["high"]),
        float(values["low"]),
        float(values["close"]),
        float(values["volume"]),
    )


def _lenient_open_ms(row: Any, index: int) -> int:
    values = _row_values(row, index)
    open_ms = values["open_ms"]
    if not _is_int(open_ms):
        raise _RowError(
            REASON_ROW_PARSE_ERROR, f"row {index}: open_ms must be a real integer"
        )
    return int(open_ms)


def _lenient_candle(row: Any, index: int) -> Candle | None:
    try:
        return _parse_row(row, index)
    except _RowError:
        return None


def _validate_ohlc(candle: Any, index: int) -> tuple[str, str]:
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


def validate_5m_candles(
    candles: Sequence[Any],
    *,
    evaluation_asof_ms: Any,
    min_closed_candles: int = MIN_CLOSED_CANDLES_RSI_SEED,
    require_latest_closed: bool = True,
) -> Candle5mValidationResult:
    """Validate one provider-supplied 5m series against a causal ASOF.

    Same fail-closed contract as the Issue #297 HTF validator, on the 5m
    UTC grid (see module docstring).  The result is a pure function of the
    inputs: reruns are identical and appended future candles cannot change
    an earlier-ASOF result.
    """
    if not _is_int(evaluation_asof_ms) or int(evaluation_asof_ms) < 0:
        return Candle5mValidationResult(
            ok=False,
            reason=REASON_INVALID_TIMESTAMP,
            detail=f"evaluation_asof_ms must be a non-negative integer, got {evaluation_asof_ms!r}",
        )
    asof_ms = int(evaluation_asof_ms)
    if not _is_int(min_closed_candles) or int(min_closed_candles) < 1:
        raise ValueError("min_closed_candles must be a positive integer")
    if not candles:
        return Candle5mValidationResult(
            ok=False,
            reason=REASON_EMPTY_SERIES,
            detail="no candles supplied",
            evaluation_asof_ms=asof_ms,
        )

    seen: dict[int, Any] = {}
    previous_open: int | None = None
    closed: list[Any] = []
    for index, candle in enumerate(candles):
        open_raw = getattr(candle, "timestamp", None)
        if not _is_int(open_raw) or int(open_raw) < 0:
            return Candle5mValidationResult(
                ok=False,
                reason=REASON_INVALID_TIMESTAMP,
                detail=f"timestamp must be a non-negative integer epoch-ms, got {open_raw!r} at index {index}",
                evaluation_asof_ms=asof_ms,
            )
        open_ms = int(open_raw)
        # ASOF isolation: unavailable candles cannot influence a past
        # result, even when future OHLCV or grid is malformed.
        if open_ms + CANDLE_5M_MS > asof_ms:
            break
        if open_ms % CANDLE_5M_MS:
            return Candle5mValidationResult(
                ok=False,
                reason=REASON_UNALIGNED_CANDLE,
                detail=f"open {open_ms} at index {index} is off the 5m UTC grid",
                evaluation_asof_ms=asof_ms,
            )
        if open_ms in seen:
            same = seen[open_ms]
            identical = all(
                float(getattr(same, name)) == float(getattr(candle, name))
                for name in ("open", "high", "low", "close", "volume")
            )
            return Candle5mValidationResult(
                ok=False,
                reason=REASON_DUPLICATE_CANDLE if identical else REASON_CONFLICTING_CANDLE,
                detail=f"repeated open {open_ms} at index {index}",
                evaluation_asof_ms=asof_ms,
            )
        if previous_open is not None and open_ms < previous_open:
            return Candle5mValidationResult(
                ok=False,
                reason=REASON_OUT_OF_ORDER_CANDLE,
                detail=f"open {open_ms} at index {index} is out of order",
                evaluation_asof_ms=asof_ms,
            )
        reason, detail = _validate_ohlc(candle, index)
        if reason != REASON_OK:
            return Candle5mValidationResult(
                ok=False,
                reason=reason,
                detail=detail,
                evaluation_asof_ms=asof_ms,
            )
        seen[open_ms] = candle
        previous_open = open_ms
        closed.append(candle)

    dropped = tuple(candles[len(closed):])
    if not closed:
        return Candle5mValidationResult(
            ok=False,
            reason=REASON_NO_CLOSED_CANDLES,
            detail=f"no candle is fully closed at {asof_ms}",
            evaluation_asof_ms=asof_ms,
            dropped_unclosed=dropped,
        )

    expected = int(closed[0].timestamp)
    for candle in closed:
        open_ms = int(candle.timestamp)
        if open_ms != expected:
            return Candle5mValidationResult(
                ok=False,
                reason=REASON_CANDLE_GAP,
                detail=f"missing 5m bucket(s) between {expected} and {open_ms}",
                evaluation_asof_ms=asof_ms,
                dropped_unclosed=dropped,
            )
        expected = open_ms + CANDLE_5M_MS

    if len(closed) < int(min_closed_candles):
        return Candle5mValidationResult(
            ok=False,
            reason=REASON_INSUFFICIENT_HISTORY,
            detail=f"{len(closed)} closed candles < required {int(min_closed_candles)}",
            evaluation_asof_ms=asof_ms,
            dropped_unclosed=dropped,
        )

    expected_latest = latest_closed_open_ms(asof_ms, CANDLE_5M_MS)
    if require_latest_closed and int(closed[-1].timestamp) != expected_latest:
        return Candle5mValidationResult(
            ok=False,
            reason=REASON_MISSING_LATEST_CLOSED_CANDLE,
            detail=(
                f"source tail stops at {int(closed[-1].timestamp)}; "
                f"newest closed bucket at {asof_ms} is {expected_latest}"
            ),
            evaluation_asof_ms=asof_ms,
            dropped_unclosed=dropped,
        )

    return Candle5mValidationResult(
        ok=True,
        reason=REASON_OK,
        evaluation_asof_ms=asof_ms,
        closed_candles=tuple(closed),
        dropped_unclosed=dropped,
        latest_closed_open_ms=expected_latest,
    )


@dataclass(frozen=True)
class Candle5mSet:
    """Validated, ASOF-truncated 5m candles plus provenance and coverage."""

    evaluation_asof_ms: int
    candles: tuple[Candle, ...]
    dropped_unclosed: tuple[Candle, ...]
    latest_closed_open_ms: int
    source_identity: CandleSourceIdentity
    coverage: CoverageReport
    ok: bool
    reason: str = REASON_OK
    detail: str = ""
    provider_version: str = "H2_CANDLE5M_1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_version": self.provider_version,
            "timeframe": TIMEFRAME_5M,
            "evaluation_asof_ms": self.evaluation_asof_ms,
            "candle_count": len(self.candles),
            "dropped_unclosed_count": len(self.dropped_unclosed),
            "latest_closed_open_ms": self.latest_closed_open_ms,
            "source_identity": self.source_identity.to_dict(),
            "coverage": self.coverage.to_dict(),
            "ok": self.ok,
            "reason": self.reason,
            "detail": self.detail,
        }


def _coverage_5m(
    present_opens: set[int],
    *,
    evaluation_asof_ms: int,
    canonical_lookback: int,
    dropped_unclosed_count: int,
) -> CoverageReport:
    latest = latest_closed_open_ms(evaluation_asof_ms, CANDLE_5M_MS)
    window_open = latest - (int(canonical_lookback) - 1) * CANDLE_5M_MS
    expected = tuple(
        window_open + i * CANDLE_5M_MS for i in range(int(canonical_lookback))
    )
    missing = tuple(slot for slot in expected if slot not in present_opens)
    present_buckets = int(canonical_lookback) - len(missing)
    if present_buckets == 0:
        status = COVERAGE_NONE
    elif not missing:
        status = COVERAGE_COMPLETE
    else:
        status = COVERAGE_PARTIAL
    return CoverageReport(
        status=status,
        expected_buckets=int(canonical_lookback),
        present_buckets=present_buckets,
        missing_open_ms=missing,
        window_open_ms=window_open,
        window_close_ms=latest + CANDLE_5M_MS,
        dropped_unclosed_count=int(dropped_unclosed_count),
    )


def provide_5m_candles(
    rows: Sequence[Any],
    *,
    evaluation_asof_ms: Any,
    source_identity: CandleSourceIdentity,
    canonical_lookback: int = 120,
    min_closed_candles: int = MIN_CLOSED_CANDLES_RSI_SEED,
    require_latest_closed: bool = True,
) -> Candle5mSet:
    """Validate raw source rows into CLOSED 5m candles at the ASOF.

    Fail-closed chain (mirrors H1-C's provider):

    1. source identity must be a known, self-consistent source with a
       canonical SHA-256 ``payload_hash``; ``row_count`` and the hash are
       verified UNCONDITIONALLY against the supplied rows;
    2. ASOF isolation: the trailing future/unclosed suffix is excluded
       BEFORE any OHLCV / grid validation, and a closed row hidden after
       the future suffix is rejected (``OUT_OF_ORDER_CANDLE``);
    3. every closed row must parse (``ROW_PARSE_ERROR``);
    4. :func:`validate_5m_candles` must pass — its reason codes propagate
       verbatim.  Missing data NEVER becomes a usable series.

    Coverage diagnostics are computed from the opens actually present for
    every outcome, valid or not.
    """
    if not _is_int(evaluation_asof_ms) or int(evaluation_asof_ms) < 0:
        raise ValueError("evaluation_asof_ms must be a non-negative integer")
    if not _is_int(canonical_lookback) or canonical_lookback < 1:
        raise ValueError("canonical_lookback must be a positive integer")

    asof_ms = int(evaluation_asof_ms)
    latest = latest_closed_open_ms(asof_ms, CANDLE_5M_MS)

    def _build(
        *,
        ok: bool,
        reason: str,
        detail: str,
        candles: tuple[Candle, ...] = (),
        dropped: tuple[Candle, ...] = (),
        present_opens: set[int] | None = None,
        dropped_count: int = 0,
    ) -> Candle5mSet:
        return Candle5mSet(
            evaluation_asof_ms=asof_ms,
            candles=candles,
            dropped_unclosed=dropped,
            latest_closed_open_ms=latest,
            source_identity=source_identity,
            coverage=_coverage_5m(
                present_opens if present_opens is not None else set(),
                evaluation_asof_ms=asof_ms,
                canonical_lookback=canonical_lookback,
                dropped_unclosed_count=dropped_count,
            ),
            ok=ok,
            reason=reason,
            detail=detail,
        )

    # 1. source identity — hash format + row_count + SHA-256 always verified
    if not isinstance(source_identity, HtfSourceIdentity):
        return _build(
            ok=False,
            reason=REASON_INVALID_SOURCE_IDENTITY,
            detail=f"source_identity must be CandleSourceIdentity, got {type(source_identity).__name__}",
        )
    if source_identity.source_kind not in KNOWN_SOURCE_KINDS:
        return _build(
            ok=False,
            reason=REASON_UNKNOWN_SOURCE_KIND,
            detail=f"unknown source kind {source_identity.source_kind!r}",
        )
    if not source_identity.source_label.strip():
        return _build(
            ok=False,
            reason=REASON_INVALID_SOURCE_IDENTITY,
            detail="source_label is empty",
        )
    if not is_sha256_hex(source_identity.payload_hash):
        return _build(
            ok=False,
            reason=REASON_INVALID_SOURCE_IDENTITY,
            detail="payload_hash must be a canonical lowercase hex SHA-256 digest",
        )
    if source_identity.row_count != len(rows):
        return _build(
            ok=False,
            reason=REASON_SOURCE_PAYLOAD_MISMATCH,
            detail=f"row_count {source_identity.row_count} != {len(rows)} supplied rows",
        )
    if source_payload_hash(rows) != source_identity.payload_hash:
        return _build(
            ok=False,
            reason=REASON_SOURCE_PAYLOAD_MISMATCH,
            detail="supplied rows do not hash to source_identity.payload_hash",
        )

    # 2-3. walk rows: strict-parse the CLOSED prefix, drop the future suffix
    parse_error: _RowError | None = None
    closed_candles: list[Candle] = []
    present_opens: set[int] = set()
    dropped_raw: list[Any] = []
    in_future_suffix = False

    for index, row in enumerate(rows):
        if in_future_suffix:
            try:
                suffix_open_ms = _lenient_open_ms(row, index)
            except _RowError:
                dropped_raw.append(row)
                continue
            if suffix_open_ms + CANDLE_5M_MS <= asof_ms:
                parse_error = parse_error or _RowError(
                    REASON_OUT_OF_ORDER_CANDLE,
                    f"row {index}: closed candle after unavailable future suffix",
                )
            dropped_raw.append(row)
            continue
        try:
            open_ms = _lenient_open_ms(row, index)
        except _RowError as exc:
            parse_error = parse_error or exc
            continue
        present_opens.add(open_ms)
        if open_ms + CANDLE_5M_MS > asof_ms:
            in_future_suffix = True
            dropped_raw.append(row)
            continue
        try:
            closed_candles.append(_parse_row(row, index))
        except _RowError as exc:
            parse_error = parse_error or exc

    dropped = tuple(
        candle
        for candle in (
            _lenient_candle(row, index)
            for index, row in enumerate(dropped_raw)
        )
        if candle is not None
    )

    if parse_error is not None:
        return _build(
            ok=False,
            reason=parse_error.reason,
            detail=parse_error.detail,
            dropped=dropped,
            present_opens=present_opens,
            dropped_count=len(dropped_raw),
        )

    # 4. strict causal validation of the CLOSED prefix
    validation = validate_5m_candles(
        closed_candles,
        evaluation_asof_ms=asof_ms,
        min_closed_candles=min_closed_candles,
        require_latest_closed=require_latest_closed,
    )
    return _build(
        ok=validation.ok,
        reason=validation.reason,
        detail=validation.detail,
        candles=validation.closed_candles,
        dropped=dropped,
        present_opens=present_opens,
        dropped_count=len(dropped_raw),
    )
