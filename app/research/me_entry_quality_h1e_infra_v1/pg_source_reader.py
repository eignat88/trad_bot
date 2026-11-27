"""Issue #324 / H1-E — C.1: PostgreSQL source reader for #305 events.

Implements the frozen :class:`H1ESourceReader` contract
(``read_after(cursor, *, limit, ceiling_captured_us)``) over the #305
capture table ``research_me_upstream_coverage.source_event`` (READ-ONLY,
least-privilege ``SELECT``).

Contract guarantees:

* preserved values are carried VERBATIM — ``source_event_key``,
  ``source_identity``, ``signal_time``, ``lineage`` and ``lineage_hash``
  come out exactly as stored (no re-derivation, no normalization);
* continuation is the composite cursor ``(captured_at, source_event_key)``
  — capture order, never ``signal_time`` — with strict ``>`` semantics, so
  a batch boundary can never drop or duplicate a row;
* deterministic ordering (``ORDER BY captured_at, source_event_key``) and
  bounded batches (``LIMIT`` — positive integer, fail-closed validation);
* the optional capture ceiling is honoured (``captured_at <= ceiling``).

This reader NEVER writes to any table and never touches #305, V3,
scanner or existing OOS state.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from app.research.me_entry_quality_h1e_v1.capture import CaptureCursor

H1E_PG_SOURCE_READER_VERSION = "H1E_PG_SOURCE_READER_V1"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: columns carried VERBATIM from the #305 capture table into the row
#: document consumed by ``admit_source_row``
_SOURCE_COLUMNS = (
    "source_event_key",
    "source_identity",
    "symbol",
    "signal_time",
    "captured_at",
    "activation_freeze_ts",
    "source_fingerprint",
    "lineage_hash",
    "lineage",
)


def us_to_datetime(us: int) -> datetime:
    """Exact integer µs -> aware datetime (microsecond resolution)."""
    return _EPOCH + timedelta(microseconds=int(us))


def datetime_to_us(value: datetime) -> int:
    """Exact aware datetime -> integer µs (no float rounding)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (
        delta.days * 86_400 * 10**6
        + delta.seconds * 10**6
        + delta.microseconds
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class PgSourceReader:
    """Bounded, cursor-ordered READ-ONLY reader over the #305 table.

    ``connection_factory`` is a zero-argument callable returning a DB-API
    connection (injected — this module never opens connections itself); a
    connection is taken per call and released when the call ends.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        schema: str = "research_me_upstream_coverage",
        table: str = "source_event",
    ) -> None:
        if not schema.isidentifier() or not table.isidentifier():
            raise ValueError("schema and table must be plain identifiers")
        self._connection_factory = connection_factory
        self._schema = schema
        self._table = table

    def read_after(
        self,
        cursor: CaptureCursor,
        *,
        limit: int,
        ceiling_captured_us: int | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(cursor, CaptureCursor):
            raise ValueError("cursor must be a CaptureCursor")
        if not _is_int(limit) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if ceiling_captured_us is not None and not _is_int(ceiling_captured_us):
            raise ValueError("ceiling_captured_us must be a real integer or None")

        captured_us, source_event_key = cursor.as_tuple()
        params: dict[str, Any] = {
            "captured_us": us_to_datetime(captured_us),
            "source_event_key": source_event_key,
            "limit": int(limit),
        }
        ceiling_sql = ""
        if ceiling_captured_us is not None:
            ceiling_sql = " AND captured_at <= %(ceiling)s"
            params["ceiling"] = us_to_datetime(int(ceiling_captured_us))
        columns = ", ".join(_SOURCE_COLUMNS)
        sql = (
            f"SELECT {columns} FROM {self._schema}.{self._table} "
            "WHERE (captured_at, source_event_key) > (%(captured_us)s, "
            "%(source_event_key)s)"
            f"{ceiling_sql} "
            "ORDER BY captured_at, source_event_key "
            "LIMIT %(limit)s"
        )
        connection = self._connection_factory()
        try:
            with connection.cursor() as statement:
                statement.execute(sql, params)
                columns_seen = [desc[0] for desc in statement.description]
                rows = statement.fetchall()
        finally:
            # caller-owned lifecycle: close what we opened
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        documents: list[dict[str, Any]] = []
        for row in rows:
            record = dict(zip(columns_seen, row))
            documents.append({
                "source_event_key": str(record["source_event_key"]),
                "source_identity": str(record["source_identity"]),
                "symbol": str(record["symbol"]),
                "signal_time": record["signal_time"],
                "captured_at": record["captured_at"],
                "activation_freeze_ts": record["activation_freeze_ts"],
                "source_fingerprint": str(record["source_fingerprint"] or ""),
                "lineage_hash": str(record["lineage_hash"]),
                "lineage": dict(record["lineage"]),
            })
        return documents
