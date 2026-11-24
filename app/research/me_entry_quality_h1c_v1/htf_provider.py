"""Issue #308 / H1-C.2 — read-only HTF candle provider (research-only).

Wraps raw 4H / 1D candle rows from an EXTERNAL read-only source with

* :class:`HtfSourceIdentity` — immutable provenance of the exact payload
  used (source kind, label, fetch time, row count, payload hash);
* strict fail-closed validation and causal ASOF truncation — reuses the
  Issue #297 validator unchanged
  (:func:`app.research.me_entry_quality_h1_v1.htf_candles.validate_htf_candles`);
* :class:`CoverageReport` — canonical-window coverage (``COMPLETE`` /
  ``PARTIAL`` / ``NONE``) with the exact missing buckets;
* explicit diagnostic reason codes.

Scope guarantees (Issue #308):

* READ-ONLY: this module never fetches, never writes, never caches.  Rows
  are supplied by the caller (a bounded offline fetch or a read-only SQL
  SELECT); the package itself performs NO I/O, NO network, NO database,
  NO clock reads;
* only ACTUALLY CLOSED candles at ``evaluation_asof_ms`` are returned —
  unclosed / future rows are reported in ``dropped_unclosed`` and never
  used;
* UTC epoch-millisecond timestamps everywhere (candle OPEN time).  Exact
  time anchoring: a candle is usable iff ``open_ms + timeframe_ms <=
  evaluation_asof_ms`` (inclusive at the close boundary).

Source inventory finding (see ``docs/research/308_me_entry_quality_h1c_v1.md``):
the production database currently stores NO 4H/1D candle history, so real
runs must fetch rows from a bounded public-kline source (additional API
calls) or from a future deployed candle store.  This module is deliberately
source-agnostic and refuses unknown sources instead of guessing.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.models import Candle
from app.research.me_entry_quality_h1_v1.htf_candles import (
    MIN_CLOSED_CANDLES,
    TIMEFRAME_MS,
    CandleValidationResult,
    latest_closed_open_ms,
    validate_htf_candles,
)

H1C_PROVIDER_VERSION = "H1C_SOURCE_1.0.0"

# ─── source kinds (closed set — unknown kinds fail closed) ────────────────────
SOURCE_KIND_BYBIT_KLINE_V5 = "BYBIT_KLINE_V5"
SOURCE_KIND_MARKET_CANDLE_TABLE = "MARKET_CANDLE_TABLE"
SOURCE_KIND_OFFLINE_SNAPSHOT = "OFFLINE_SNAPSHOT"
KNOWN_SOURCE_KINDS: tuple[str, ...] = (
    SOURCE_KIND_BYBIT_KLINE_V5,
    SOURCE_KIND_MARKET_CANDLE_TABLE,
    SOURCE_KIND_OFFLINE_SNAPSHOT,
)

# ─── reason codes ─────────────────────────────────────────────────────────────
REASON_OK = "OK"
REASON_INVALID_SOURCE_IDENTITY = "INVALID_SOURCE_IDENTITY"
REASON_UNKNOWN_SOURCE_KIND = "UNKNOWN_SOURCE_KIND"
REASON_SOURCE_PAYLOAD_MISMATCH = "SOURCE_PAYLOAD_MISMATCH"
REASON_ROW_PARSE_ERROR = "ROW_PARSE_ERROR"

# ─── coverage statuses ────────────────────────────────────────────────────────
COVERAGE_COMPLETE = "COMPLETE"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_NONE = "NONE"

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
class HtfSourceIdentity:
    """Immutable provenance of ONE candle payload.

    ``payload_hash`` is the SHA-256 of the canonicalized row multiset, so a
    re-ordered delivery of the same rows has the same identity while any
    content change (tamper / different fetch) is detectable.
    """

    source_kind: str
    source_label: str
    fetched_at_ms: int | None
    row_count: int
    payload_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_label": self.source_label,
            "fetched_at_ms": self.fetched_at_ms,
            "row_count": self.row_count,
            "payload_hash": self.payload_hash,
        }


def _canonical_row_line(row: Any) -> str:
    """One canonical line per raw row (lenient: provenance, not validation)."""
    values: list[Any] = []
    if isinstance(row, Mapping):
        values = [row.get(name) for name in _ROW_FIELDS]
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        values = [row[i] if i < len(row) else None for i in range(6)]
    else:
        values = [getattr(row, name, None) for name in _ROW_FIELDS]
    return "|".join(repr(v) for v in values)


def source_payload_hash(rows: Sequence[Any]) -> str:
    """Deterministic SHA-256 over the canonicalized row multiset.

    Order-insensitive: the same rows delivered in a different order hash
    identically.  The empty multiset hashes to the standard SHA-256 of the
    empty byte string (well-defined), while :func:`build_source_identity`
    still refuses to mint an identity for an empty payload.
    """
    lines = sorted(_canonical_row_line(row) for row in rows)
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


#: canonical hash of an empty payload (SHA-256 of the empty byte string)
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()


def is_sha256_hex(text: Any) -> bool:
    """True iff ``text`` is a canonical lowercase hex SHA-256 digest."""
    return (
        isinstance(text, str)
        and len(text) == 64
        and all(c in "0123456789abcdef" for c in text)
    )


def build_source_identity(
    source_kind: str,
    source_label: str,
    rows: Sequence[Any],
    *,
    fetched_at_ms: int | None = None,
) -> HtfSourceIdentity:
    """Build an identity for a fetched payload (kind must be a known source)."""
    if source_kind not in KNOWN_SOURCE_KINDS:
        raise ValueError(f"unknown source kind {source_kind!r}")
    if not isinstance(source_label, str) or not source_label.strip():
        raise ValueError("source_label must be a non-empty string")
    if fetched_at_ms is not None and not _is_int(fetched_at_ms):
        raise ValueError("fetched_at_ms must be an int or None")
    if not rows:
        raise ValueError("source identity must name real data (empty payload)")
    return HtfSourceIdentity(
        source_kind=source_kind,
        source_label=source_label,
        fetched_at_ms=None if fetched_at_ms is None else int(fetched_at_ms),
        row_count=len(rows),
        payload_hash=source_payload_hash(rows),
    )


@dataclass(frozen=True)
class CoverageReport:
    """Canonical-window coverage of the CLOSED candle series."""

    status: str
    expected_buckets: int
    present_buckets: int
    missing_open_ms: tuple[int, ...]
    window_open_ms: int
    window_close_ms: int
    dropped_unclosed_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "expected_buckets": self.expected_buckets,
            "present_buckets": self.present_buckets,
            "missing_open_ms": list(self.missing_open_ms),
            "window_open_ms": self.window_open_ms,
            "window_close_ms": self.window_close_ms,
            "dropped_unclosed_count": self.dropped_unclosed_count,
        }


@dataclass(frozen=True)
class HtfCandleSet:
    """Validated, ASOF-truncated candles plus provenance and coverage."""

    timeframe: str
    evaluation_asof_ms: int
    candles: tuple[Candle, ...]
    dropped_unclosed: tuple[Candle, ...]
    latest_closed_open_ms: int
    source_identity: HtfSourceIdentity
    coverage: CoverageReport
    ok: bool
    reason: str = REASON_OK
    detail: str = ""
    provider_version: str = H1C_PROVIDER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_version": self.provider_version,
            "timeframe": self.timeframe,
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
    raise _RowError(
        REASON_ROW_PARSE_ERROR,
        f"row {index}: unsupported row type {type(row).__name__}",
    )


def _parse_row(row: Any, index: int) -> Candle:
    """STRICT parse — used only for rows that are CLOSED at the ASOF."""
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
    """Open time of any row (diagnostics/coverage); integer only."""
    values = _row_values(row, index)
    open_ms = values["open_ms"]
    if not _is_int(open_ms):
        raise _RowError(
            REASON_ROW_PARSE_ERROR, f"row {index}: open_ms must be a real integer"
        )
    return int(open_ms)


def _lenient_candle(row: Any, index: int) -> Candle | None:
    """Best-effort candle for reporting the dropped future suffix."""
    try:
        return _parse_row(row, index)
    except _RowError:
        return None


def _coverage(
    present_opens: set[int],
    *,
    timeframe_ms: int,
    evaluation_asof_ms: int,
    canonical_lookback: int,
    dropped_unclosed_count: int,
) -> CoverageReport:
    """Canonical-window coverage from the ACTUAL opens present in the source.

    Computed independently of series validity: an invalid series still gets
    an accurate ``COMPLETE`` / ``PARTIAL`` / ``NONE`` report — a partial
    source is NEVER relabeled ``NONE``.
    """
    latest = latest_closed_open_ms(evaluation_asof_ms, timeframe_ms)
    window_open = latest - (int(canonical_lookback) - 1) * timeframe_ms
    expected = tuple(
        window_open + i * timeframe_ms for i in range(int(canonical_lookback))
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
        window_close_ms=latest + timeframe_ms,
        dropped_unclosed_count=int(dropped_unclosed_count),
    )


def provide_htf_candles(
    rows: Sequence[Any],
    *,
    timeframe: str,
    evaluation_asof_ms: int,
    source_identity: HtfSourceIdentity,
    canonical_lookback: int = 60,
    min_closed_candles: int = MIN_CLOSED_CANDLES,
    require_latest_closed: bool = True,
) -> HtfCandleSet:
    """Validate raw source rows into CLOSED candles at ``evaluation_asof_ms``.

    Fail-closed chain:

    1. source identity must be a known, self-consistent source with a
       canonical lowercase-hex SHA-256 ``payload_hash`` (``INVALID_SOURCE_
       IDENTITY`` / ``UNKNOWN_SOURCE_KIND``); ``row_count`` and the SHA-256
       are verified UNCONDITIONALLY (``SOURCE_PAYLOAD_MISMATCH``);
    2. ASOF isolation: the trailing future/unclosed suffix is excluded
       BEFORE any OHLCV / grid validation — corrupt or off-grid future
       payloads can never influence a historical classification;
    3. every closed row must parse (``ROW_PARSE_ERROR``);
    4. the Issue #297 validator must pass — its reason codes are
       propagated verbatim.  Missing data NEVER becomes a usable series.

    Coverage diagnostics are computed from the opens actually present in
    the payload for every outcome, valid or not.
    """
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    if not _is_int(evaluation_asof_ms):
        raise ValueError("evaluation_asof_ms must be a real integer")
    if not _is_int(canonical_lookback) or canonical_lookback < 1:
        raise ValueError("canonical_lookback must be a positive integer")

    tf_ms = int(TIMEFRAME_MS[timeframe])
    asof_ms = int(evaluation_asof_ms)
    latest = latest_closed_open_ms(asof_ms, tf_ms)

    def _build(
        *,
        ok: bool,
        reason: str,
        detail: str,
        candles: tuple[Candle, ...] = (),
        dropped: tuple[Candle, ...] = (),
        present_opens: set[int] | None = None,
        dropped_count: int = 0,
    ) -> HtfCandleSet:
        return HtfCandleSet(
            timeframe=timeframe,
            evaluation_asof_ms=asof_ms,
            candles=candles,
            dropped_unclosed=dropped,
            latest_closed_open_ms=latest,
            source_identity=source_identity,
            coverage=_coverage(
                present_opens if present_opens is not None else set(),
                timeframe_ms=tf_ms,
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
            detail=f"source_identity must be HtfSourceIdentity, got {type(source_identity).__name__}",
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
    # before any OHLCV / grid validation (ASOF isolation)
    parse_error: _RowError | None = None
    closed_candles: list[Candle] = []
    present_opens: set[int] = set()
    dropped_raw: list[Any] = []
    in_future_suffix = False

    for index, row in enumerate(rows):
        if in_future_suffix:
            # Future OHLCV is irrelevant at this ASOF, but a CLOSED row
            # appearing after the future suffix is a source-order violation.
            # Otherwise a complete historical prefix could hide disorder.
            try:
                suffix_open_ms = _lenient_open_ms(row, index)
            except _RowError:
                dropped_raw.append(row)
                continue
            if suffix_open_ms + tf_ms <= asof_ms:
                parse_error = parse_error or _RowError(
                    "OUT_OF_ORDER_CANDLE",
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
        if open_ms + tf_ms > asof_ms:
            # unclosed or future at this ASOF — excluded wholesale BEFORE
            # OHLCV validation; a corrupt payload here cannot affect history
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

    # 4. strict causal validation of the CLOSED prefix (Issue #297, unchanged)
    validation: CandleValidationResult = validate_htf_candles(
        closed_candles,
        timeframe=timeframe,
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
