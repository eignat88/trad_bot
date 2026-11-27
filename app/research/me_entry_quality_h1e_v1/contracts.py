"""Issue #324 / H1-E — immutable contracts for the prospective observer.

Everything here is pure: no I/O, no network, no database, no clock.

Preserved identity contract (spec §"Source and temporal authority"):

* ``source_event_key``, ``source_identity``, ``signal_time`` (exact
  microseconds) and ``lineage_hash`` are carried VERBATIM from the #305
  ``research_me_upstream_coverage.source_event`` row — never rebuilt from
  mutable presentation fields;
* the classification ASOF is the ORIGINAL ``signal_time`` — never
  ``captured_at``, never delivery time;
* the H1-E ledger identity is the pair
  ``(source_event_key, h1e protocol fingerprint)`` — one event per protocol
  fingerprint, isolated from every other ledger namespace.

Freeze contract:

* :class:`H1EFreeze` can only be built from a separately owner-approved
  activation record (:meth:`H1EFreeze.from_owner_approval`) or as an
  explicitly labelled SYNTHETIC TEST FIXTURE
  (:meth:`H1EFreeze.synthetic_fixture`) that the capture worker refuses
  outside offline tests.  Phase A never mints a real freeze.

Idempotency / conflict contract:

* :func:`preserved_identity_hash` fingerprints exactly the preserved
  source facts; a redelivery with the SAME hash is an idempotent
  ``DUPLICATE`` (first-capture snapshot kept verbatim), a redelivery with a
  DIFFERENT hash is a real integrity conflict
  (``H1E_EVENT_KEY_LINEAGE_CONFLICT``) that is isolated — never an
  overwrite.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from app.research.me_entry_quality_h1c_v1.event_adapter import (
    SourceEventRecord,
    parse_utc_us,
)
from app.research.me_entry_quality_h1e_v1.protocol import (
    excluded_window_reason,
    protocol_fingerprint as compute_protocol_fingerprint,
)

H1E_CONTRACTS_VERSION = "H1E_CONTRACTS_1.0.0"

# ─── reason codes (stable, fail-closed) ──────────────────────────────────────

REASON_OK = "H1E_OK"
REASON_ROW_PARSE_ERROR = "H1E_REJECT_ROW_PARSE"
REASON_MISSING_SOURCE_EVENT_KEY = "H1E_REJECT_MISSING_SOURCE_EVENT_KEY"
REASON_MISSING_SOURCE_IDENTITY = "H1E_REJECT_MISSING_SOURCE_IDENTITY"
REASON_MISSING_LINEAGE_HASH = "H1E_REJECT_MISSING_LINEAGE_HASH"
REASON_MISSING_FINGERPRINT = "H1E_REJECT_MISSING_FINGERPRINT"
REASON_INVALID_SIGNAL_TIME = "H1E_REJECT_INVALID_SIGNAL_TIME"
REASON_INVALID_CAPTURED_AT = "H1E_REJECT_INVALID_CAPTURED_AT"
REASON_INVALID_SOURCE_FREEZE = "H1E_REJECT_INVALID_SOURCE_FREEZE"
REASON_PRE_FREEZE = "H1E_REJECT_PRE_FREEZE"
REASON_EXCLUDED_WINDOW = "H1E_REJECT_EXCLUDED_WINDOW"
REASON_IDENTITY_CONFLICT = "H1E_REJECT_SOURCE_EVENT_KEY_IDENTITY_CONFLICT"
REASON_LINEAGE_HASH_MISMATCH = "H1E_REJECT_LINEAGE_HASH_MISMATCH"
REASON_LINEAGE_OBLIGATION = "H1E_REJECT_LINEAGE_OBLIGATION"
REASON_FREEZE_NOT_APPROVED = "H1E_REJECT_FREEZE_NOT_APPROVED"

REASON_DUPLICATE = "H1E_DUPLICATE"
REASON_EVENT_KEY_LINEAGE_CONFLICT = "H1E_EVENT_KEY_LINEAGE_CONFLICT"

FIXTURE_LABEL_SYNTHETIC = "SYNTHETIC_TEST_FIXTURE"
FIXTURE_LABEL_REAL = "REAL_BYBIT_FETCH"


# ─── canonical hashing (mirrors the #305 derivations byte-for-byte) ─────────


def canonical_lineage_hash(lineage: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 of the original JSON lineage.

    Mirrors ``app.research.me_upstream_source_v1.contracts.lineage_hash``
    EXACTLY (canonical JSON: ``sort_keys``, ``separators=(",", ":")``,
    ``ensure_ascii=False``, ``allow_nan=False``).  The H1-E package
    re-implements it locally — instead of importing it — because the #305
    contracts module transitively imports ``app.scanners.models`` and the
    H1-E observer must stay wiring-free (isolation test
    ``test_me_entry_quality_h1e_isolation_purity_v1.py``).  Parity with the
    #305 implementation is pinned by an offline test.
    """
    try:
        payload = json.dumps(
            dict(lineage),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError("H1E_REJECT_LINEAGE_NOT_CANONICAL") from None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expected_source_event_key(source_identity: str) -> str:
    """Deterministic #305 ``source_event_key`` derivation, mirrored locally.

    ``SHA256("ME_UPSTREAM_SOURCE_V1|" + normalized_identity)`` — the frozen
    #305 ``identity_key_from_normalized`` (parity pinned by an offline
    test).  Used ONLY to detect identity conflicts: the stored
    ``source_event_key`` must hash from the stored ``source_identity``.
    """
    if not isinstance(source_identity, str) or not source_identity.strip():
        raise ValueError("H1E_REJECT_MISSING_SOURCE_IDENTITY")
    payload = f"ME_UPSTREAM_SOURCE_V1|{source_identity}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def preserved_identity_hash(
    *,
    source_event_key: str,
    source_identity: str,
    signal_time_us: int,
    lineage_hash: str,
    source_fingerprint: str,
) -> str:
    """Fingerprint of the preserved source facts (redelivery comparison)."""
    document = {
        "source_event_key": source_event_key,
        "source_identity": source_identity,
        "signal_time_us": int(signal_time_us),
        "lineage_hash": lineage_hash,
        "source_fingerprint": source_fingerprint,
    }
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ledger_identity(source_event_key: str, h1e_fingerprint: str | None = None) -> str:
    """Unique ledger identity per ``(source_event_key, H1-E fingerprint)``."""
    fingerprint = h1e_fingerprint or compute_protocol_fingerprint()
    return f"{source_event_key}:{fingerprint}"


# ─── H1-E freeze (separate from the #305 freeze) ────────────────────────────


@dataclass(frozen=True)
class H1EFreeze:
    """The separately approved H1-E activation freeze.

    ``fixture_label`` is :data:`FIXTURE_LABEL_SYNTHETIC` for offline test
    fixtures and :data:`FIXTURE_LABEL_REAL` only for a freeze minted by an
    explicit owner GO (Phase C — NOT part of Phase A).  Any consumer can
    refuse fixtures (:attr:`allow_fixture` on the capture worker).
    """

    freeze_ts_us: int
    approval_ref: str
    approved_by: str
    protocol_fingerprint: str
    fixture_label: str

    @property
    def is_fixture(self) -> bool:
        return self.fixture_label == FIXTURE_LABEL_SYNTHETIC

    @classmethod
    def from_owner_approval(
        cls,
        *,
        freeze_ts_us: int,
        approval_ref: str,
        approved_by: str,
        protocol_fingerprint: str | None = None,
    ) -> "H1EFreeze":
        """Build a REAL freeze — only from a documented owner approval.

        Fail-closed: an empty approval reference / approver is refused.
        Phase A never calls this with real values (no freeze is minted).
        """
        if not isinstance(freeze_ts_us, int) or isinstance(freeze_ts_us, bool):
            raise ValueError("freeze_ts_us must be a real integer")
        if not str(approval_ref).strip() or not str(approved_by).strip():
            raise ValueError("H1E_REJECT_FREEZE_NOT_APPROVED")
        return cls(
            freeze_ts_us=int(freeze_ts_us),
            approval_ref=str(approval_ref),
            approved_by=str(approved_by),
            protocol_fingerprint=protocol_fingerprint or compute_protocol_fingerprint(),
            fixture_label=FIXTURE_LABEL_REAL,
        )

    @classmethod
    def synthetic_fixture(cls, freeze_ts_us: int) -> "H1EFreeze":
        """SYNTHETIC TEST FIXTURE freeze — offline tests ONLY.

        Always labelled :data:`FIXTURE_LABEL_SYNTHETIC`; the capture worker
        refuses fixture freezes unless ``allow_fixture=True``.
        """
        if not isinstance(freeze_ts_us, int) or isinstance(freeze_ts_us, bool):
            raise ValueError("freeze_ts_us must be a real integer")
        return cls(
            freeze_ts_us=int(freeze_ts_us),
            approval_ref="SYNTHETIC_TEST_FIXTURE_NOT_AN_APPROVAL",
            approved_by="SYNTHETIC_TEST_FIXTURE",
            protocol_fingerprint=compute_protocol_fingerprint(),
            fixture_label=FIXTURE_LABEL_SYNTHETIC,
        )


# ─── source event (preserved #305 row) ──────────────────────────────────────


@dataclass(frozen=True)
class H1ESourceEvent:
    """Immutable normalized copy of ONE #305 ``source_event`` row.

    The preserved fields are carried VERBATIM.  ``signal_time_us`` is the
    lossless microsecond instant of the original signal — the causal ASOF
    of every downstream classification.
    """

    source_event_key: str
    source_identity: str
    symbol: str
    signal_time_us: int
    captured_at_us: int
    source_freeze_ts_us: int
    lineage_hash: str
    source_fingerprint: str
    lineage: Mapping[str, Any]
    preserved_hash: str

    @property
    def signal_time_ms(self) -> int:
        return int(self.signal_time_us) // 1000

    def to_source_event_record(self) -> SourceEventRecord:
        """Adapt to the frozen H1-C record (reused UNCHANGED downstream).

        ``captured_at`` is carried for the ledger timing columns only — it
        NEVER becomes the classification ASOF.
        """
        return SourceEventRecord(
            source_event_key=self.source_event_key,
            source_identity=self.source_identity,
            symbol=self.symbol,
            signal_time_ms=self.signal_time_ms,
            captured_at_ms=int(self.captured_at_us) // 1000,
            activation_freeze_ts_ms=int(self.source_freeze_ts_us) // 1000,
            lineage_hash=self.lineage_hash,
            source_fingerprint=self.source_fingerprint,
            signal_time_us=int(self.signal_time_us),
        )

    def lineage_payload(self) -> dict[str, Any]:
        """Exactly the preserved lineage fields (verbatim)."""
        return {
            "source_event_key": self.source_event_key,
            "source_identity": self.source_identity,
            "signal_time_ms": self.signal_time_ms,
            "signal_time_us": int(self.signal_time_us),
            "activation_freeze_ts_ms": int(self.source_freeze_ts_us) // 1000,
            "lineage_hash": self.lineage_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.lineage_payload(),
            "symbol": self.symbol,
            "captured_at_us": int(self.captured_at_us),
            "source_fingerprint": self.source_fingerprint,
            "preserved_hash": self.preserved_hash,
        }


@dataclass(frozen=True)
class AdmissionDecision:
    """Deterministic admission outcome for ONE source row (pure)."""

    accepted: bool
    reason: str
    detail: str = ""
    event: H1ESourceEvent | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "detail": self.detail,
            "source_event_key": (
                "" if self.event is None else self.event.source_event_key
            ),
        }


def _reject(key: str, reason: str, detail: str) -> AdmissionDecision:
    return AdmissionDecision(accepted=False, reason=reason, detail=detail)


def admit_source_row(
    row: Mapping[str, Any],
    *,
    freeze: H1EFreeze,
    expected_fingerprint: str = "",
) -> AdmissionDecision:
    """Ingest-time admission for ONE #305 ``source_event`` mapping (pure).

    Fail-closed chain — every failure is an explicit reason code, never a
    silent drop and never a coercion:

    1. strict row parse (identity fields non-empty, timestamps UTC-anchored);
    2. identity integrity — ``source_event_key`` must hash from
       ``source_identity`` (``…_SOURCE_EVENT_KEY_IDENTITY_CONFLICT``);
    3. lineage integrity — stored ``lineage_hash`` must equal the canonical
       SHA-256 of the stored ``lineage`` JSON;
    4. H1-E freeze boundary — ``signal_time_us > freeze.freeze_ts_us``
       STRICTLY (``…_PRE_FREEZE``);
    5. preserved exclusions — the #305 UNVERIFIED window and the SOXSUSDT
       QA example (``…_EXCLUDED_WINDOW``);
    6. source fingerprint — non-empty and (when an expected fingerprint is
       supplied) matching it verbatim.
    """
    if not isinstance(row, Mapping):
        return _reject("", REASON_ROW_PARSE_ERROR, "row must be a mapping")
    key = row.get("source_event_key")
    key_text = key if isinstance(key, str) else ""
    try:
        if not key_text:
            raise ValueError("source_event_key must be a non-empty string")
        identity = row.get("source_identity")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("source_identity must be a non-empty string")
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        stored_lineage_hash = row.get("lineage_hash")
        if not isinstance(stored_lineage_hash, str) or not stored_lineage_hash.strip():
            raise ValueError("lineage_hash must be a non-empty string")
        fingerprint = row.get("source_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("source_fingerprint must be a non-empty string")
        lineage = row.get("lineage")
        if not isinstance(lineage, Mapping):
            raise ValueError("lineage must be a JSON object")
        signal_time_us = parse_utc_us(row.get("signal_time"), field_name="signal_time")
        captured_at_us = parse_utc_us(row.get("captured_at"), field_name="captured_at")
        source_freeze_us = parse_utc_us(
            row.get("activation_freeze_ts"), field_name="activation_freeze_ts"
        )
    except ValueError as exc:
        text = str(exc)
        if "source_event_key" in text:
            reason = REASON_MISSING_SOURCE_EVENT_KEY
        elif "source_identity" in text:
            reason = REASON_MISSING_SOURCE_IDENTITY
        elif "lineage_hash" in text:
            reason = REASON_MISSING_LINEAGE_HASH
        elif "source_fingerprint" in text:
            reason = REASON_MISSING_FINGERPRINT
        elif "signal_time" in text:
            reason = REASON_INVALID_SIGNAL_TIME
        elif "captured_at" in text:
            reason = REASON_INVALID_CAPTURED_AT
        elif "activation_freeze_ts" in text:
            reason = REASON_INVALID_SOURCE_FREEZE
        else:
            reason = REASON_ROW_PARSE_ERROR
        return _reject(key_text, reason, text)

    # 2. identity integrity — the key must hash from the normalized identity
    try:
        derived_key = expected_source_event_key(identity)
    except ValueError as exc:
        return _reject(key_text, REASON_IDENTITY_CONFLICT, str(exc))
    if derived_key != key_text:
        return _reject(
            key_text,
            REASON_IDENTITY_CONFLICT,
            f"source_event_key {key_text} != SHA256(ME_UPSTREAM_SOURCE_V1|"
            f"{identity}) = {derived_key}",
        )

    # 3. lineage integrity
    try:
        recomputed = canonical_lineage_hash(dict(lineage))
    except ValueError as exc:
        return _reject(key_text, REASON_LINEAGE_HASH_MISMATCH, str(exc))
    if recomputed != stored_lineage_hash:
        return _reject(
            key_text,
            REASON_LINEAGE_HASH_MISMATCH,
            f"stored lineage_hash {stored_lineage_hash} != recomputed {recomputed}",
        )

    # 3b. #296 lineage obligations — the preserved lineage must agree with
    # the preserved row identity (verbatim, never rebuilt).
    lineage_identity = lineage.get("source_identity")
    if lineage_identity != identity:
        return _reject(
            key_text,
            REASON_LINEAGE_OBLIGATION,
            f"lineage.source_identity {lineage_identity!r} != row "
            f"source_identity {identity!r}",
        )
    lineage_fingerprint = lineage.get("source_pipeline_fingerprint")
    if not isinstance(lineage_fingerprint, str) or not lineage_fingerprint.strip():
        return _reject(
            key_text,
            REASON_LINEAGE_OBLIGATION,
            "lineage.source_pipeline_fingerprint must be a non-empty string",
        )
    if lineage_fingerprint != fingerprint:
        return _reject(
            key_text,
            REASON_LINEAGE_OBLIGATION,
            "lineage.source_pipeline_fingerprint != row source_fingerprint",
        )

    # 4. H1-E freeze boundary (strict, microsecond precision)
    if not isinstance(freeze, H1EFreeze):
        return _reject(key_text, REASON_FREEZE_NOT_APPROVED, "freeze not provided")
    if signal_time_us <= freeze.freeze_ts_us:
        return _reject(
            key_text,
            REASON_PRE_FREEZE,
            f"signal_time_us={signal_time_us} <= h1e_freeze_ts_us="
            f"{freeze.freeze_ts_us}",
        )

    # 5. preserved exclusions
    exclusion = excluded_window_reason(signal_time_us, symbol)
    if exclusion:
        return _reject(
            key_text,
            REASON_EXCLUDED_WINDOW,
            f"{exclusion}: signal_time_us={signal_time_us} symbol={symbol}",
        )

    # 6. source fingerprint
    if expected_fingerprint and fingerprint != expected_fingerprint:
        return _reject(
            key_text,
            REASON_MISSING_FINGERPRINT,
            "source_fingerprint does not match the expected #305 fingerprint",
        )

    event = H1ESourceEvent(
        source_event_key=key_text,
        source_identity=identity,
        symbol=symbol,
        signal_time_us=int(signal_time_us),
        captured_at_us=int(captured_at_us),
        source_freeze_ts_us=int(source_freeze_us),
        lineage_hash=stored_lineage_hash,
        source_fingerprint=fingerprint,
        lineage=dict(lineage),
        preserved_hash=preserved_identity_hash(
            source_event_key=key_text,
            source_identity=identity,
            signal_time_us=int(signal_time_us),
            lineage_hash=stored_lineage_hash,
            source_fingerprint=fingerprint,
        ),
    )
    return AdmissionDecision(accepted=True, reason=REASON_OK, event=event)
