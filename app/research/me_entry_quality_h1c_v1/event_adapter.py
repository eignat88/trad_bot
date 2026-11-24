"""Issue #308 / H1-C.3 — event adapter for the #305 upstream source.

Consumes source events ONLY from the independent prospective source
``research_me_upstream_coverage.source_event`` (Issue #305
``ME_UPSTREAM_SOURCE_V1``) and normalizes them into immutable
:class:`SourceEventRecord` values.

Hard contract:

* the preserved identity fields — ``source_event_key``,
  ``source_identity``, ``signal_time``, ``activation_freeze_ts``,
  ``lineage_hash`` — are carried VERBATIM and never recomputed;
* the classification ASOF is **``signal_time``** — NEVER ``captured_at``
  (:func:`classification_asof_ms`).  Delivery time must not leak into the
  causal evaluation;
* duplicate ``source_event_key`` deliveries are rejected explicitly
  (``DUPLICATE_SOURCE_EVENT``) — one result per event key;
* invalid rows are rejected with explicit reason codes — never dropped
  silently, never coerced;
* immutable lineage: records are frozen dataclasses and
  :meth:`SourceEventRecord.lineage_payload` returns exactly the five
  preserved fields.

READ-ONLY / pure: no database, no network, no clock, no I/O.  Row decoding
from PostgreSQL ``timestamptz``/``bigint`` columns happens at the boundary:
``timestamptz`` values should be passed as timezone-aware ``datetime``
objects (or exact UTC epoch-millisecond integers / ISO-8601 strings with an
explicit offset); naive timestamps are rejected because they cannot be
anchored to UTC exactly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

H1C_EVENT_ADAPTER_VERSION = "H1C_EVENT_ADAPTER_1.1.0"

#: Explicit timestamp precision policy (review remediation, Issue #308):
#:
#: * the exact instant of ``signal_time`` is preserved losslessly at
#:   microsecond resolution (``SourceEventRecord.signal_time_us``);
#: * the UTC epoch-millisecond views used as the classification ASOF are
#:   derived by FLOORING the sub-millisecond remainder toward the past
#:   (``us // 1000``) — never float rounding, never implicit truncation.
#:   Flooring is causal-safe: the ASOF never extends beyond the true
#:   instant, so no data that became available after the true ``signal_time``
#:   can enter the classification.
#: * integer inputs are interpreted as UTC epoch MILLISECONDS; converting
#:   them to microseconds is exact (``us = ms * 1000``).
TIMESTAMP_PRECISION_POLICY = "US_PRESERVED__MS_FLOOR_TOWARD_PAST"

# ─── reason codes ─────────────────────────────────────────────────────────────
REASON_OK = "OK"
REASON_ROW_PARSE_ERROR = "EVENT_ROW_PARSE_ERROR"
REASON_MISSING_SOURCE_EVENT_KEY = "MISSING_SOURCE_EVENT_KEY"
REASON_MISSING_SOURCE_IDENTITY = "MISSING_SOURCE_IDENTITY"
REASON_MISSING_LINEAGE_HASH = "MISSING_LINEAGE_HASH"
REASON_MISSING_SYMBOL = "MISSING_SYMBOL"
REASON_INVALID_SIGNAL_TIME = "INVALID_SIGNAL_TIME"
REASON_INVALID_CAPTURED_AT = "INVALID_CAPTURED_AT"
REASON_INVALID_FREEZE_TS = "INVALID_FREEZE_TS"
REASON_DUPLICATE_SOURCE_EVENT = "DUPLICATE_SOURCE_EVENT"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)


def _dt_to_us(value: datetime, field_name: str) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}: naive datetime is not UTC-anchored")
    return int((value - _EPOCH) // _US)


def parse_utc_us(value: Any, *, field_name: str = "timestamp") -> int:
    """Exact UTC epoch-MICROSECOND conversion (lossless, fail-closed).

    Accepts:

    * real integer epoch-MILLISECONDS (converted exactly, ``us = ms*1000``)
      and integral millisecond floats;
    * timezone-aware ``datetime`` values (any offset) at full microsecond
      resolution;
    * ISO-8601 strings with an explicit offset (``...Z`` or ``±HH:MM``) at
      full microsecond resolution.

    Rejects naive datetimes/strings (no UTC anchor), non-integral
    millisecond floats and anything else — never guesses a timezone.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field_name}: bool is not a timestamp")
    if isinstance(value, int):
        return int(value) * 1000
    if isinstance(value, float):
        if not float(value).is_integer():
            raise ValueError(f"{field_name}: non-integral float {value!r}")
        return int(value) * 1000
    if isinstance(value, datetime):
        return _dt_to_us(value, field_name)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name}: empty timestamp string")
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        # require an explicit UTC offset — no naive local time
        if "+" not in text[10:] and "-" not in text[10:]:
            raise ValueError(
                f"{field_name}: timestamp string lacks an explicit UTC offset"
            )
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field_name}: unparseable timestamp {value!r}") from exc
        return _dt_to_us(parsed, field_name)
    raise ValueError(f"{field_name}: unsupported timestamp type {type(value).__name__}")


def parse_utc_ms(value: Any, *, field_name: str = "timestamp") -> int:
    """UTC epoch-millisecond view (explicit policy, see above).

    ``parse_utc_us(value) // 1000`` — deterministic FLOOR toward the past;
    sub-millisecond precision is preserved in ``parse_utc_us`` and never
    silently discarded here.
    """
    return parse_utc_us(value, field_name=field_name) // 1000


@dataclass(frozen=True)
class SourceEventRecord:
    """Immutable normalized copy of ONE #305 ``source_event`` row.

    ``signal_time_us`` preserves the instant losslessly at microsecond
    resolution; ``signal_time_ms`` is the derived classification-ASOF view
    (explicit ``MS_FLOOR_TOWARD_PAST`` policy) and is always exactly
    ``signal_time_us // 1000``.
    """

    source_event_key: str
    source_identity: str
    symbol: str
    signal_time_ms: int
    captured_at_ms: int
    activation_freeze_ts_ms: int
    lineage_hash: str
    source_fingerprint: str = ""
    signal_candle_open_time_ms: int | None = None
    entry_timeframe: str | None = None
    signal_time_us: int = 0

    def __post_init__(self) -> None:
        if self.signal_time_us and self.signal_time_ms != self.signal_time_us // 1000:
            raise ValueError(
                "signal_time_ms must equal signal_time_us // 1000 "
                "(explicit floor policy)"
            )

    def lineage_payload(self) -> dict[str, Any]:
        """Exactly the preserved lineage fields (verbatim)."""
        return {
            "source_event_key": self.source_event_key,
            "source_identity": self.source_identity,
            "signal_time_ms": self.signal_time_ms,
            "signal_time_us": self.signal_time_us,
            "activation_freeze_ts_ms": self.activation_freeze_ts_ms,
            "lineage_hash": self.lineage_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.lineage_payload(),
            "symbol": self.symbol,
            "captured_at_ms": self.captured_at_ms,
            "source_fingerprint": self.source_fingerprint,
            "signal_candle_open_time_ms": self.signal_candle_open_time_ms,
            "entry_timeframe": self.entry_timeframe,
        }


def classification_asof_ms(record: SourceEventRecord) -> int:
    """The causal evaluation instant of an event = its ``signal_time``.

    NEVER ``captured_at``: classification must reproduce what was knowable
    at the original signal time, not at delivery time.  Millisecond view of
    ``classification_asof_us`` (explicit floor policy).
    """
    return int(record.signal_time_ms)


def classification_asof_us(record: SourceEventRecord) -> int:
    """Exact ``signal_time`` instant at microsecond resolution."""
    return int(record.signal_time_us)


@dataclass(frozen=True)
class EventRejection:
    row_index: int
    source_event_key: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "source_event_key": self.source_event_key,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EventBatch:
    """Deterministic outcome of :func:`adapt_source_events`."""

    records: tuple[SourceEventRecord, ...]
    rejections: tuple[EventRejection, ...]
    source_row_count: int
    adapter_version: str = H1C_EVENT_ADAPTER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_version": self.adapter_version,
            "source_row_count": self.source_row_count,
            "record_count": len(self.records),
            "records": [r.to_dict() for r in self.records],
            "rejections": [r.to_dict() for r in self.rejections],
        }


def _text(row: Mapping[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_text(row: Mapping[str, Any], name: str) -> str | None:
    value = row.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or None")
    return value


def _optional_ms(row: Mapping[str, Any], name: str) -> int | None:
    value = row.get(name)
    if value is None:
        return None
    return parse_utc_ms(value, field_name=name)


def adapt_source_events(rows: Sequence[Mapping[str, Any]]) -> EventBatch:
    """Normalize #305 ``source_event`` rows into immutable records.

    * input order is preserved (deterministic);
    * the FIRST delivery of a ``source_event_key`` wins; later duplicates are
      rejected explicitly with ``DUPLICATE_SOURCE_EVENT``;
    * any invalid row is rejected with its specific reason code — such rows
      never reach classification.
    """
    records: list[SourceEventRecord] = []
    rejections: list[EventRejection] = []
    seen: set[str] = set()

    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            rejections.append(
                EventRejection(
                    row_index=index,
                    source_event_key="",
                    reason=REASON_ROW_PARSE_ERROR,
                    detail=f"row must be a mapping, got {type(row).__name__}",
                )
            )
            continue
        key = row.get("source_event_key")
        key_text = key if isinstance(key, str) else ""
        try:
            if key_text in seen:
                raise _Duplicate()
            key_text = _text(row, "source_event_key")
            identity = _text(row, "source_identity")
            symbol = _text(row, "symbol")
            lineage_hash = _text(row, "lineage_hash")
            signal_time_us = parse_utc_us(
                row.get("signal_time"), field_name="signal_time"
            )
            signal_time_ms = signal_time_us // 1000  # explicit floor policy
            captured_at_ms = parse_utc_ms(
                row.get("captured_at"), field_name="captured_at"
            )
            freeze_ms = parse_utc_ms(
                row.get("activation_freeze_ts"), field_name="activation_freeze_ts"
            )
            fingerprint = row.get("source_fingerprint") or ""
            if not isinstance(fingerprint, str):
                raise ValueError("source_fingerprint must be a string")
            candle_open = _optional_ms(row, "signal_candle_open_time")
            entry_tf = _optional_text(row, "entry_timeframe")
        except _Duplicate:
            rejections.append(
                EventRejection(
                    row_index=index,
                    source_event_key=key_text,
                    reason=REASON_DUPLICATE_SOURCE_EVENT,
                    detail="later delivery of an already adapted source_event_key",
                )
            )
            continue
        except ValueError as exc:
            text = str(exc)
            if "source_event_key" in text:
                reason = REASON_MISSING_SOURCE_EVENT_KEY
            elif "source_identity" in text:
                reason = REASON_MISSING_SOURCE_IDENTITY
            elif "lineage_hash" in text:
                reason = REASON_MISSING_LINEAGE_HASH
            elif "symbol" in text:
                reason = REASON_MISSING_SYMBOL
            elif "signal_time" in text:
                reason = REASON_INVALID_SIGNAL_TIME
            elif "captured_at" in text:
                reason = REASON_INVALID_CAPTURED_AT
            elif "activation_freeze_ts" in text:
                reason = REASON_INVALID_FREEZE_TS
            else:
                reason = REASON_ROW_PARSE_ERROR
            rejections.append(
                EventRejection(
                    row_index=index,
                    source_event_key=key_text,
                    reason=reason,
                    detail=text,
                )
            )
            continue

        seen.add(key_text)
        records.append(
            SourceEventRecord(
                source_event_key=key_text,
                source_identity=identity,
                symbol=symbol,
                signal_time_ms=signal_time_ms,
                captured_at_ms=captured_at_ms,
                activation_freeze_ts_ms=freeze_ms,
                lineage_hash=lineage_hash,
                source_fingerprint=fingerprint,
                signal_candle_open_time_ms=candle_open,
                entry_timeframe=entry_tf,
                signal_time_us=signal_time_us,
            )
        )

    return EventBatch(
        records=tuple(records),
        rejections=tuple(rejections),
        source_row_count=len(rows),
    )


class _Duplicate(Exception):
    pass
