"""Issue #314 / H1-D — immutable provenance snapshot + offline replay
(``H1D_PROVENANCE_REPLAY_V1``).

One snapshot captures, immutably, everything needed to reproduce a
classification BYTE-IDENTICALLY with ZERO HTTP:

* ``source_event_key``, event ``source_identity`` string and ``lineage_hash``
  (preserved VERBATIM from the #305 source event), ``signal_time`` at
  microsecond resolution (the causal ASOF) and ``activation_freeze_ts``;
* per timeframe (4H / 1D): the ``HtfSourceIdentity`` built via the frozen
  H1-C :func:`build_source_identity()`, the source label, ``fetched_at_ms``,
  the row count, the canonical SHA-256 payload hash (deterministic canonical
  serialization of the rows) and the RAW payload rows themselves;
* ``snapshot_sha256`` — the SHA-256 of the canonical document serialization
  (everything except the hash field itself): any tamper — a row value, the
  lineage, the ASOF, the row count — is DETECTED on load
  (:class:`ProvenanceTamperError`), never silently accepted.

:func:`replay_classification` runs the full H1-D classification from a
loaded snapshot with NO client, NO transport and NO clock — replay is
byte-identical for the same snapshot by construction
(:func:`canonical_output_bytes`).

The only I/O in this module is snapshot file load/save (explicit paths,
caller-controlled); there is no network, no database and no clock.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.research.me_entry_quality_h1c_v1.event_adapter import (
    SourceEventRecord,
)
from app.research.me_entry_quality_h1c_v1.htf_provider import (
    HtfSourceIdentity,
    build_source_identity,
)
from app.research.me_entry_quality_h1d_v1.classification import (
    H1DClassification,
    classify_event,
)

H1D_PROVENANCE_VERSION = "H1D_PROVENANCE_REPLAY_V1"
SNAPSHOT_SCHEMA = "H1D_HTF_SNAPSHOT_1"

#: fixture labels — a synthetic payload must NEVER masquerade as real data.
FIXTURE_LABEL_SYNTHETIC = "SYNTHETIC_TEST_FIXTURE"
FIXTURE_LABEL_REAL = "REAL_BYBIT_FETCH"

_ROW_FIELDS = ("open_ms", "open", "high", "low", "close", "volume")


class ProvenanceError(RuntimeError):
    """Base class for snapshot/provenance failures (fail closed)."""


class ProvenanceTamperError(ProvenanceError):
    """Snapshot content does not match its recorded hashes — immutable
    lineage or payload was altered after capture."""


# ─── canonical serialization ────────────────────────────────────────────────


def canonical_json_bytes(obj: Any) -> bytes:
    """Deterministic canonical JSON bytes (sorted keys, no whitespace)."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def canonical_rows_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Deterministic canonical serialization of normalized payload rows.

    Each row becomes ``[open_ms, open, high, low, close, volume]`` in the
    given order; the same rows always produce the same bytes and therefore
    the same SHA-256.
    """
    canonical_rows = [
        [row.get(name) for name in _ROW_FIELDS] for row in rows
    ]
    return canonical_json_bytes(canonical_rows)


def payload_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 of the canonical row serialization (lowercase hex)."""
    return hashlib.sha256(canonical_rows_bytes(rows)).hexdigest()


def compute_snapshot_hash(document: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical document WITHOUT its hash field."""
    body = {key: value for key, value in document.items()
            if key != "snapshot_sha256"}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def canonical_output_bytes(classification: H1DClassification) -> bytes:
    """Byte-identical classification output (canonical JSON)."""
    return canonical_json_bytes(classification.to_dict())


# ─── snapshot model ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimeframePayload:
    """Immutable provenance + raw rows for ONE timeframe payload."""

    timeframe: str
    interval: str
    source_label: str
    fetched_at_ms: int | None
    row_count: int
    payload_sha256: str
    source_identity: dict[str, Any]
    rows: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "interval": self.interval,
            "source_label": self.source_label,
            "fetched_at_ms": self.fetched_at_ms,
            "row_count": self.row_count,
            "payload_sha256": self.payload_sha256,
            "source_identity": dict(self.source_identity),
            "rows": [dict(row) for row in self.rows],
        }


@dataclass(frozen=True)
class Snapshot:
    """Immutable H1-D provenance snapshot for ONE #305 source event."""

    schema: str
    provenance_version: str
    fixture_label: str
    source_event_key: str
    source_identity: str
    lineage_hash: str
    signal_time_us: int
    signal_time_ms: int
    activation_freeze_ts_ms: int
    symbol: str
    asof_us: int
    payload_4h: TimeframePayload
    payload_1d: TimeframePayload
    snapshot_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "provenance_version": self.provenance_version,
            "fixture_label": self.fixture_label,
            "source_event_key": self.source_event_key,
            "source_identity": self.source_identity,
            "lineage_hash": self.lineage_hash,
            "signal_time_us": self.signal_time_us,
            "signal_time_ms": self.signal_time_ms,
            "activation_freeze_ts_ms": self.activation_freeze_ts_ms,
            "symbol": self.symbol,
            "asof_us": self.asof_us,
            "payload_4h": self.payload_4h.to_dict(),
            "payload_1d": self.payload_1d.to_dict(),
            "snapshot_sha256": self.snapshot_sha256,
        }

    def lineage_payload(self) -> dict[str, Any]:
        """Exactly the preserved source lineage fields (verbatim)."""
        return {
            "source_event_key": self.source_event_key,
            "source_identity": self.source_identity,
            "signal_time_ms": self.signal_time_ms,
            "signal_time_us": self.signal_time_us,
            "activation_freeze_ts_ms": self.activation_freeze_ts_ms,
            "lineage_hash": self.lineage_hash,
        }


# ─── build / load ───────────────────────────────────────────────────────────


def _normalize_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProvenanceError(
                f"payload rows must be mappings, got {type(row).__name__}")
        missing = [name for name in _ROW_FIELDS if name not in row]
        if missing:
            raise ProvenanceError(f"payload row missing fields: {missing}")
        normalized.append({name: row[name] for name in _ROW_FIELDS})
    return tuple(normalized)


def _build_payload(
    *,
    timeframe: str,
    interval: str,
    source_kind: str,
    source_label: str,
    rows: Sequence[Mapping[str, Any]],
    fetched_at_ms: int | None,
) -> TimeframePayload:
    normalized = _normalize_rows(rows)
    identity: HtfSourceIdentity = build_source_identity(
        source_kind, source_label, normalized, fetched_at_ms=fetched_at_ms)
    return TimeframePayload(
        timeframe=timeframe,
        interval=interval,
        source_label=source_label,
        fetched_at_ms=fetched_at_ms,
        row_count=len(normalized),
        payload_sha256=payload_sha256(normalized),
        source_identity=identity.to_dict(),
        rows=normalized,
    )


def build_snapshot(
    *,
    record: SourceEventRecord,
    rows_4h: Sequence[Mapping[str, Any]],
    rows_1d: Sequence[Mapping[str, Any]],
    interval_4h: str = "240",
    interval_1d: str = "D",
    source_kind: str,
    source_label: str,
    fixture_label: str,
    fetched_at_ms: int | None = None,
    source_label_4h: str | None = None,
    source_label_1d: str | None = None,
) -> Snapshot:
    """Build an immutable snapshot for ONE event and its two HTF payloads.

    ``fixture_label`` MUST be honest: synthetic test payloads are labeled
    :data:`FIXTURE_LABEL_SYNTHETIC` and are never presented as real
    coverage; only a real, separately-approved fetch may use
    :data:`FIXTURE_LABEL_REAL`.
    """
    if fixture_label not in (FIXTURE_LABEL_SYNTHETIC, FIXTURE_LABEL_REAL):
        raise ValueError(
            f"fixture_label must be {FIXTURE_LABEL_SYNTHETIC!r} or "
            f"{FIXTURE_LABEL_REAL!r}, got {fixture_label!r}")
    payload_4h = _build_payload(
        timeframe="4H", interval=interval_4h, source_kind=source_kind,
        source_label=source_label_4h or source_label, rows=rows_4h,
        fetched_at_ms=fetched_at_ms)
    payload_1d = _build_payload(
        timeframe="1D", interval=interval_1d, source_kind=source_kind,
        source_label=source_label_1d or source_label, rows=rows_1d,
        fetched_at_ms=fetched_at_ms)

    document: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "provenance_version": H1D_PROVENANCE_VERSION,
        "fixture_label": fixture_label,
        "source_event_key": record.source_event_key,
        "source_identity": record.source_identity,
        "lineage_hash": record.lineage_hash,
        "signal_time_us": record.signal_time_us,
        "signal_time_ms": record.signal_time_ms,
        "activation_freeze_ts_ms": record.activation_freeze_ts_ms,
        "symbol": record.symbol,
        "asof_us": record.signal_time_us,
        "payload_4h": payload_4h.to_dict(),
        "payload_1d": payload_1d.to_dict(),
    }
    document["snapshot_sha256"] = compute_snapshot_hash(document)
    return Snapshot(
        schema=SNAPSHOT_SCHEMA,
        provenance_version=H1D_PROVENANCE_VERSION,
        fixture_label=fixture_label,
        source_event_key=record.source_event_key,
        source_identity=record.source_identity,
        lineage_hash=record.lineage_hash,
        signal_time_us=record.signal_time_us,
        signal_time_ms=record.signal_time_ms,
        activation_freeze_ts_ms=record.activation_freeze_ts_ms,
        symbol=record.symbol,
        asof_us=record.signal_time_us,
        payload_4h=payload_4h,
        payload_1d=payload_1d,
        snapshot_sha256=document["snapshot_sha256"],
    )


def _payload_from_dict(data: Any, *, field_name: str) -> TimeframePayload:
    if not isinstance(data, Mapping):
        raise ProvenanceTamperError(f"{field_name} must be an object")
    try:
        rows = _normalize_rows(data.get("rows") or ())
        payload = TimeframePayload(
            timeframe=str(data["timeframe"]),
            interval=str(data["interval"]),
            source_label=str(data["source_label"]),
            fetched_at_ms=data.get("fetched_at_ms"),
            row_count=int(data["row_count"]),
            payload_sha256=str(data["payload_sha256"]),
            source_identity=dict(data["source_identity"]),
            rows=rows,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProvenanceTamperError(
            f"{field_name} is malformed: {exc}") from exc

    # immutability checks — recomputed hashes must match the recorded ones
    if payload.row_count != len(payload.rows):
        raise ProvenanceTamperError(
            f"{field_name}: row_count {payload.row_count} != "
            f"{len(payload.rows)} stored rows")
    recomputed = payload_sha256(payload.rows)
    if recomputed != payload.payload_sha256:
        raise ProvenanceTamperError(
            f"{field_name}: payload rows do not hash to the recorded "
            f"payload_sha256 ({recomputed} != {payload.payload_sha256})")
    # the H1-C source identity is rebuilt from the stored rows and must
    # match VERBATIM (covers its own payload_hash and row_count)
    try:
        rebuilt_identity = build_source_identity(
            str(payload.source_identity.get("source_kind")),
            payload.source_label,
            payload.rows,
            fetched_at_ms=payload.fetched_at_ms,
        ).to_dict()
    except (TypeError, ValueError) as exc:
        raise ProvenanceTamperError(
            f"{field_name}: source identity cannot be rebuilt: {exc}") from exc
    if rebuilt_identity != payload.source_identity:
        raise ProvenanceTamperError(
            f"{field_name}: source_identity does not match the rebuilt "
            "identity of the stored rows")
    return payload


def snapshot_from_dict(document: Any) -> Snapshot:
    """Validate a snapshot document in full (tamper → explicit error)."""
    if not isinstance(document, Mapping):
        raise ProvenanceTamperError("snapshot document must be an object")
    recorded_hash = document.get("snapshot_sha256")
    recomputed_hash = compute_snapshot_hash(document)
    if recorded_hash != recomputed_hash:
        raise ProvenanceTamperError(
            f"snapshot_sha256 mismatch: recorded {recorded_hash!r}, "
            f"recomputed {recomputed_hash!r} — the snapshot was altered")
    if document.get("schema") != SNAPSHOT_SCHEMA:
        raise ProvenanceTamperError(
            f"unknown snapshot schema {document.get('schema')!r}")
    if document.get("provenance_version") != H1D_PROVENANCE_VERSION:
        raise ProvenanceTamperError(
            f"unknown provenance_version "
            f"{document.get('provenance_version')!r}")
    fixture_label = document.get("fixture_label")
    if fixture_label not in (FIXTURE_LABEL_SYNTHETIC, FIXTURE_LABEL_REAL):
        raise ProvenanceTamperError(
            f"unknown fixture_label {fixture_label!r}")
    try:
        payload_4h = _payload_from_dict(
            document.get("payload_4h"), field_name="payload_4h")
        payload_1d = _payload_from_dict(
            document.get("payload_1d"), field_name="payload_1d")
        snapshot = Snapshot(
            schema=SNAPSHOT_SCHEMA,
            provenance_version=str(document["provenance_version"]),
            fixture_label=str(fixture_label),
            source_event_key=str(document["source_event_key"]),
            source_identity=str(document["source_identity"]),
            lineage_hash=str(document["lineage_hash"]),
            signal_time_us=int(document["signal_time_us"]),
            signal_time_ms=int(document["signal_time_ms"]),
            activation_freeze_ts_ms=int(document["activation_freeze_ts_ms"]),
            symbol=str(document["symbol"]),
            asof_us=int(document["asof_us"]),
            payload_4h=payload_4h,
            payload_1d=payload_1d,
            snapshot_sha256=str(recorded_hash),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProvenanceTamperError(
            f"snapshot document is malformed: {exc}") from exc
    if snapshot.signal_time_ms != snapshot.signal_time_us // 1000:
        raise ProvenanceTamperError(
            "signal_time_ms must equal signal_time_us // 1000 (floor policy)")
    if snapshot.asof_us != snapshot.signal_time_us:
        raise ProvenanceTamperError(
            "asof_us must equal signal_time_us (ASOF = signal_time)")
    return snapshot


def save_snapshot(snapshot: Snapshot, path: str | Path) -> Path:
    """Write the snapshot as canonical JSON (explicit caller-controlled path)."""
    target = Path(path)
    target.write_bytes(canonical_json_bytes(snapshot.to_dict()))
    return target


def load_snapshot(path: str | Path) -> Snapshot:
    """Load + fully verify a snapshot (zero HTTP)."""
    raw = Path(path).read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"snapshot is not valid JSON: {exc}") from exc
    return snapshot_from_dict(document)


# ─── offline replay (zero HTTP) ─────────────────────────────────────────────


def build_record_from_snapshot(snapshot: Snapshot) -> SourceEventRecord:
    """Rebuild the immutable #305 record from the preserved lineage."""
    return SourceEventRecord(
        source_event_key=snapshot.source_event_key,
        source_identity=snapshot.source_identity,
        symbol=snapshot.symbol,
        signal_time_ms=snapshot.signal_time_ms,
        captured_at_ms=snapshot.signal_time_ms,
        activation_freeze_ts_ms=snapshot.activation_freeze_ts_ms,
        lineage_hash=snapshot.lineage_hash,
        signal_time_us=snapshot.signal_time_us,
    )


def replay_classification(snapshot: Snapshot) -> H1DClassification:
    """Reproduce the classification from a snapshot — ZERO HTTP, ZERO clock.

    Deterministic: the same snapshot always yields byte-identical output
    (:func:`canonical_output_bytes`).
    """
    record = build_record_from_snapshot(snapshot)
    source_4h = HtfSourceIdentity(**{
        name: snapshot.payload_4h.source_identity[name]
        for name in ("source_kind", "source_label", "fetched_at_ms",
                     "row_count", "payload_hash")
    })
    source_1d = HtfSourceIdentity(**{
        name: snapshot.payload_1d.source_identity[name]
        for name in ("source_kind", "source_label", "fetched_at_ms",
                     "row_count", "payload_hash")
    })
    return classify_event(
        record,
        snapshot.payload_4h.rows,
        snapshot.payload_1d.rows,
        source_4h=source_4h,
        source_1d=source_1d,
    )
