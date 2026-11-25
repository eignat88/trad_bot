"""Issue #312 / FR-01 — H2 source reader for the #305 upstream cohort.

READ-ONLY with respect to Issue #305: this module issues SELECT statements
against ``research_me_upstream_coverage.source_event`` and NOTHING else —
it never writes, never updates ``freeze_ts`` and never backfills history
(enforced structurally by :func:`assert_select_only`).

Contract:

* ``source_event_key`` is the deterministic event identity — carried from the
  stored row, never recomputed here;
* the preserved lineage fields (``source_identity``, ``lineage_hash``,
  ``signal_time``, ``activation_freeze_ts``) are carried VERBATIM; the
  original signal time is never recalculated;
* freeze checks at FULL microsecond precision (the #305 DB CHECK protects
  stored rows; this boundary protects caller-supplied / re-read rows):
  ``signal_time > activation_freeze_ts`` STRICTLY and
  ``captured_at >= activation_freeze_ts`` — anything else is an explicit
  rejection, never an admission;
* lineage checks: the stored ``lineage_hash`` must equal the deterministic
  SHA-256 of the canonical ``lineage`` JSON (the #305
  ``me_upstream_source_v1.contracts.lineage_hash`` function, reused
  unchanged); a mismatch is an explicit integrity rejection;
* the immutable source view (``immutable_source_hash``, reused unchanged)
  is computed for every accepted delivery so a redelivery can be compared
  idempotently by the store and a lineage drift becomes an explicit
  ``EVENT_KEY_LINEAGE_CONFLICT`` — never a silent overwrite;
* ``source_reference_price`` is validated STRICTLY and delivered to the H2
  pipeline as an exact :class:`decimal.Decimal` (review P1-Decimal):
  ``NaN`` / ``Infinity`` / zero / negative / arbitrary strings are explicit
  ``INVALID_SOURCE_PRICE`` rejections;
* the delivery cursor is the CAPTURE order ``(captured_at,
  source_event_key)`` — never ``signal_time`` — so late-committed events are
  never lost (review P1-watermark; overlap/reconciliation lives in the
  runtime);
* invalid rows are rejected with deterministic reason codes — never dropped
  silently, never coerced;
* trading scanner filters are deliberately NOT applied: this reader admits
  the raw upstream cohort only.

Pure parts (:func:`check_source_row`, :func:`delivery_from_row`) have no I/O;
the SELECT (:func:`read_source_events_after`) takes an explicit connection and
an explicit capture-order cursor — the caller owns checkpointing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from app.research.me_entry_quality_h1c_v1.event_adapter import parse_utc_us
from app.research.me_entry_quality_h2_prospective_v1.contracts import (
    INGEST_ACCEPTED,
    INGEST_REJECTED_INVALID,
    INGEST_REJECTED_LINEAGE,
    INGEST_REJECTED_PRE_FREEZE,
    REASON_EVENT_KEY_LINEAGE_CONFLICT,
    REASON_INVALID_SOURCE_PRICE,
    REASON_INVALID_SOURCE_TIMING,
    REASON_LINEAGE_HASH_MISMATCH,
    REASON_OK,
    REASON_PRE_FREEZE_CAPTURED_AT,
    REASON_PRE_FREEZE_SIGNAL_TIME,
    REASON_SOURCE_ROW_PARSE_ERROR,
    parse_source_price,
)
from app.research.me_upstream_source_v1.contracts import (
    immutable_source_hash,
    lineage_hash as compute_lineage_hash,
)

H2P_SOURCE_READER_VERSION = "H2P_SOURCE_READER_1.0.0"

#: The ONLY statement shape this module may ever run against #305.
SOURCE_EVENT_SELECT = "SELECT"

#: Columns carried from ``research_me_upstream_coverage.source_event`` into
#: the H2 pipeline mapping (verbatim; ``lineage``/``features`` as JSONB).
SOURCE_EVENT_COLUMNS: tuple[str, ...] = (
    "source_event_key",
    "source_identity",
    "symbol",
    "signal_time",
    "captured_at",
    "activation_freeze_ts",
    "signal_candle_open_time",
    "entry_timeframe",
    "source_fingerprint",
    "source_reference_price",
    "features",
    "lineage",
    "lineage_hash",
)

_SELECT_SQL = f"""
    SELECT {", ".join(SOURCE_EVENT_COLUMNS)}
    FROM research_me_upstream_coverage.source_event
    WHERE captured_at > %s
       OR (captured_at = %s AND source_event_key > %s)
    ORDER BY captured_at, source_event_key
    LIMIT %s
"""


def assert_select_only(sql: str) -> None:
    """Fail closed unless ``sql`` is a plain SELECT (read-only guard)."""
    head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    if head != SOURCE_EVENT_SELECT:
        raise ValueError("H2 source reader is READ-ONLY: only SELECT is allowed")


def _maybe_json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def row_mapping(row: Sequence[Any]) -> dict[str, Any]:
    """Map a raw DB-API row tuple onto the named column mapping."""
    if len(row) != len(SOURCE_EVENT_COLUMNS):
        raise ValueError(
            f"expected {len(SOURCE_EVENT_COLUMNS)} columns, got {len(row)}"
        )
    mapped: dict[str, Any] = dict(zip(SOURCE_EVENT_COLUMNS, row))
    mapped["features"] = _maybe_json(mapped.get("features"))
    mapped["lineage"] = _maybe_json(mapped.get("lineage"))
    return mapped


@dataclass(frozen=True)
class SourceDelivery:
    """Immutable outcome of the ingest-time checks for ONE #305 row.

    ``row`` is the VERBATIM column mapping handed to the H2 pipeline (only
    for accepted deliveries).  ``immutable_hash`` is the preserved immutable
    source view used for idempotent-redelivery conflict detection.
    """

    source_event_key: str
    status: str
    reason: str = REASON_OK
    detail: str = ""
    row: Mapping[str, Any] | None = None
    immutable_hash: str = ""
    signal_time_us: int = 0
    source_identity: str = ""
    lineage_hash: str = ""

    @property
    def accepted(self) -> bool:
        return self.status == INGEST_ACCEPTED


def delivery_from_row(row: Mapping[str, Any]) -> SourceDelivery:
    """Ingest-time checks for ONE ``source_event`` mapping (pure, fail closed).

    Freeze boundary (FR-01 / criterion 3), lineage integrity (criterion 15)
    and strict source-price validation (review P1-Decimal); deeper
    feature/price evaluation belongs to the H2 pipeline, which reports it as
    the ``INVALID_SOURCE`` lifecycle state.
    """
    if not isinstance(row, Mapping):
        return SourceDelivery(
            source_event_key="",
            status=INGEST_REJECTED_INVALID,
            reason=REASON_SOURCE_ROW_PARSE_ERROR,
            detail=f"row must be a mapping, got {type(row).__name__}",
        )
    key = row.get("source_event_key")
    key_text = key if isinstance(key, str) else ""
    try:
        if not key_text:
            raise ValueError("source_event_key must be a non-empty string")
        identity = row.get("source_identity")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("source_identity must be a non-empty string")
        stored_hash = row.get("lineage_hash")
        if not isinstance(stored_hash, str) or not stored_hash.strip():
            raise ValueError("lineage_hash must be a non-empty string")
        signal_time_us = parse_utc_us(row.get("signal_time"), field_name="signal_time")
        captured_us = parse_utc_us(row.get("captured_at"), field_name="captured_at")
        freeze_us = parse_utc_us(
            row.get("activation_freeze_ts"), field_name="activation_freeze_ts"
        )
        lineage = row.get("lineage")
        if not isinstance(lineage, Mapping):
            raise ValueError("lineage must be a JSON object")
    except ValueError as exc:
        return SourceDelivery(
            source_event_key=key_text,
            status=INGEST_REJECTED_INVALID,
            reason=REASON_SOURCE_ROW_PARSE_ERROR,
            detail=str(exc),
        )

    # ── freeze boundary (strict, microsecond precision) ────────────────────
    if signal_time_us <= freeze_us:
        return SourceDelivery(
            source_event_key=key_text,
            status=INGEST_REJECTED_PRE_FREEZE,
            reason=REASON_PRE_FREEZE_SIGNAL_TIME,
            detail=(
                f"signal_time_us={signal_time_us} <= "
                f"activation_freeze_ts_us={freeze_us}"
            ),
            signal_time_us=signal_time_us,
        )
    if captured_us < freeze_us:
        return SourceDelivery(
            source_event_key=key_text,
            status=INGEST_REJECTED_PRE_FREEZE,
            reason=REASON_PRE_FREEZE_CAPTURED_AT,
            detail=(
                f"captured_at_us={captured_us} < "
                f"activation_freeze_ts_us={freeze_us}"
            ),
            signal_time_us=signal_time_us,
        )

    # ── lineage integrity: stored hash must match the canonical lineage ───
    try:
        recomputed = compute_lineage_hash(dict(lineage))
    except Exception as exc:  # noqa: BLE001 - deterministic rejection code
        return SourceDelivery(
            source_event_key=key_text,
            status=INGEST_REJECTED_LINEAGE,
            reason=REASON_LINEAGE_HASH_MISMATCH,
            detail=f"lineage not canonicalizable: {exc}",
            signal_time_us=signal_time_us,
        )
    if recomputed != stored_hash:
        return SourceDelivery(
            source_event_key=key_text,
            status=INGEST_REJECTED_LINEAGE,
            reason=REASON_LINEAGE_HASH_MISMATCH,
            detail=(
                f"stored lineage_hash={stored_hash} != recomputed={recomputed}"
            ),
            signal_time_us=signal_time_us,
            lineage_hash=stored_hash,
        )

    # ── source price: strict, exact, type-safe (review P1-Decimal) ─────────
    try:
        price_decimal = parse_source_price(row.get("source_reference_price"))
    except ValueError as exc:
        return SourceDelivery(
            source_event_key=key_text,
            status=INGEST_REJECTED_INVALID,
            reason=REASON_INVALID_SOURCE_PRICE,
            detail=str(exc),
            signal_time_us=signal_time_us,
        )

    # The delivered row carries the CANONICAL Decimal price so the H2 engine
    # (int/float/Decimal contract) never sees a str and never sees a value
    # rounded through float.
    delivered_row = dict(row)
    delivered_row["source_reference_price"] = price_decimal

    return SourceDelivery(
        source_event_key=key_text,
        status=INGEST_ACCEPTED,
        reason=REASON_OK,
        row=delivered_row,
        immutable_hash=immutable_source_hash(dict(lineage)),
        signal_time_us=signal_time_us,
        source_identity=identity,
        lineage_hash=stored_hash,
    )


def read_source_events_after(
    connection: Any,
    *,
    after_captured_us: int | None = None,
    after_key: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read #305 rows AFTER the capture-order cursor (SELECT-only, bounded).

    Review P1-watermark: the delivery cursor is ``(captured_at,
    source_event_key)`` — the CAPTURE/COMMIT order of the #305 store — never
    ``signal_time``.  A late-committed event with an EARLIER ``signal_time``
    is therefore still delivered: ``signal_time`` ordering is a property of
    the data, never of the delivery sequence.

    ``captured_at`` ties are broken by ``source_event_key`` so equal
    timestamps can never drop a row at a batch boundary.  Residual
    capture-to-commit disorder is covered by the caller's overlap rewind
    (see ``H2ProspectiveRuntime._ingest_phase``); the store's idempotent
    ingest makes re-reads safe.
    """
    assert_select_only(_SELECT_SQL)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if after_captured_us is None:
        since_ts: datetime = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        # exact integer conversion — never float seconds (µs precision)
        since_ts = datetime.fromtimestamp(
            after_captured_us // 1_000_000, tz=timezone.utc
        ) + timedelta(microseconds=after_captured_us % 1_000_000)
    cur = connection.cursor()
    cur.execute(_SELECT_SQL, (since_ts, since_ts, after_key, int(limit)))
    return [row_mapping(row) for row in cur.fetchall()]


def cursor_of(rows: Sequence[Mapping[str, Any]]) -> tuple[int, str]:
    """Exclusive capture-order cursor ``(captured_at_us, source_event_key)``
    of a batch (the last row in deterministic (captured_at, key) order)."""
    if not rows:
        return 0, ""
    last = rows[-1]
    captured_us = parse_utc_us(last.get("captured_at"), field_name="captured_at")
    key = last.get("source_event_key")
    return captured_us, key if isinstance(key, str) else ""
