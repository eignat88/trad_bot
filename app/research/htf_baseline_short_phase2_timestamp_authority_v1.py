"""Issue #197 Phase-2 timestamp authority V1 (operator-approved freeze record).

Problem this module solves
--------------------------
The PostgreSQL activation boundary accepts a freeze timestamp only inside
``[now() - 60 seconds, now() + 5 minutes]`` (server clock). A timestamp baked
into a Git-tracked Python constant would have to survive code review, merge,
CI and deployment inside that window — which cannot be guaranteed and would
either fail activation or pressure operators toward a stale/backdated value.

Design (minimal, versioned change to the protocol-authority model)
------------------------------------------------------------------
The FROZEN protocol module carries NO timestamp (``APPROVED_FREEZE_TS`` stays
None on purpose). Instead the fresh UTC timestamp is chosen at the SEPARATE
operator GO and materialized as an immutable **freeze record**: a small,
self-hashed JSON document that binds

    experiment_id + protocol_fingerprint + freeze_ts + claimed_at + approval ref

together. The record is content-addressed (``record_sha256`` over its canonical
payload), so its exact bytes can be recorded in the change log / issue and
re-verified later without any further Git commit. The three independent freeze
authorities then all compare against the SAME agreed value:

    1. protocol authority  = FROZEN protocol fingerprint pin + this freeze record
    2. database authority  = research.htf_baseline_short_phase2_experiment
    3. runtime authority   = private runtime activation JSON (operator-deployed)

Rules enforced here (pure logic unless documented otherwise; all fail-closed)
--------------------------------------------------------------------------
* strict canonical UTC timestamp format ``YYYY-MM-DDTHH:MM:SSZ`` (no offsets,
  no fractional seconds) in the record — one spelling per value everywhere;
* record self-hash must verify (tamper evidence);
* ``protocol_fingerprint`` must be 64-hex and must equal the fingerprint of the
  code actually running (and, on the operator tool path, the pinned
  ``FROZEN_PROTOCOL_FINGERPRINT``);
* freshness: a record is claimable only while its ``freeze_ts`` lies within the
  PostgreSQL acceptance window ``[now - 60s, now + 300s]`` relative to the
  authoritative clock (DB ``now()`` at claim time); the record must also be
  internally consistent: ``freeze_ts`` within that window relative to its own
  ``claimed_at``. Backdated, far-future, stale and re-used timestamps are
  therefore rejected structurally, not by convention;
* legacy/predecessor freeze boundaries are rejected via
  ``forbidden_freeze_ts_ms`` (the mint/claim tool supplies the complete list,
  including the Point-B boundary); the freshness rules make such reuse
  impossible anyway;
* any missing source or pairwise disagreement between the three authorities
  raises :class:`FreezeRecordError` — the runner must then refuse to start;
* record IDENTITY (PR #200 FIX-4): the record's ``record_sha256`` self-hash is
  TAMPER EVIDENCE ONLY — it proves the file is internally intact, NOT that the
  record belongs to the originally claimed GO. The trusted source of that
  identity is the **claim receipt** (schema ``HTF_PHASE2_CLAIM_RECEIPT_V1``):
  sealed exactly once by the operator tool at the initial claim, before the
  activation transaction commits, and never overwritten. Delayed repair binds
  the presented record to that receipt (``record_id`` AND ``record_sha256``
  must match exactly) and fails closed when the identity cannot be
  established.

Nothing in this module mutates state, opens a database connection or activates
an experiment. ``load_freeze_record`` / ``load_claim_receipt`` only read files;
the single write path is the operator tool
(``tools/research/htf_baseline_short_phase2_freeze_record_v1.py``).
"""
from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

from app.research.htf_baseline_short_phase2_freeze_v1 import parse_ts_ms

FREEZE_RECORD_SCHEMA = "HTF_PHASE2_FREEZE_RECORD_V1"

#: Mirrors research.htf_baseline_short_phase2_activate() in migration 074:
#: freeze_ts >= now() - interval '60 seconds' AND freeze_ts <= now() + '5 minutes'.
ACCEPTANCE_PAST_WINDOW_S = 60
ACCEPTANCE_FUTURE_WINDOW_S = 300

#: One canonical spelling per timestamp: UTC, whole seconds, explicit ``Z``.
_TS_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_FINGERPRINT_RE = re.compile(r"\A[0-9a-f]{64}\Z")

_REQUIRED_FIELDS = (
    "schema",
    "experiment_id",
    "protocol_fingerprint",
    "freeze_ts",
    "claimed_at",
    "record_id",
    "operator_approval_ref",
    "issued_by",
)


class FreezeRecordError(ValueError):
    """Freeze record missing, malformed, tampered, stale or disagreeing."""


# --------------------------------------------------------------- serialization
def canonical_record_json(record: dict) -> str:
    """Deterministic canonical serialization of the record payload (no hash)."""
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def freeze_record_hash(record: dict) -> str:
    """SHA-256 over the canonical payload WITHOUT the ``record_sha256`` field."""
    return sha256(canonical_record_json(record).encode("utf-8")).hexdigest()


def build_freeze_record(
    *,
    experiment_id: str,
    freeze_ts: str,
    protocol_fingerprint: str,
    claimed_at: str,
    operator_approval_ref: str,
    issued_by: str,
    record_id: str,
    note: str | None = None,
) -> dict:
    """Compose a complete, self-hashed freeze record (no validation of policy)."""
    record = {
        "schema": FREEZE_RECORD_SCHEMA,
        "experiment_id": experiment_id,
        "protocol_fingerprint": protocol_fingerprint,
        "freeze_ts": freeze_ts,
        "claimed_at": claimed_at,
        "record_id": record_id,
        "operator_approval_ref": operator_approval_ref,
        "issued_by": issued_by,
    }
    if note:
        record["note"] = note
    record["record_sha256"] = freeze_record_hash(record)
    return record


# -------------------------------------------------------------------- validation
def _require_canonical_ts(value, field: str) -> int:
    if not isinstance(value, str) or not _TS_RE.match(value):
        raise FreezeRecordError(
            f"freeze record field {field!r} must be a canonical UTC timestamp "
            f"YYYY-MM-DDTHH:MM:SSZ, got {value!r}"
        )
    return parse_ts_ms(value)


def validate_freeze_record(
    record: dict,
    *,
    expected_experiment_id: str | None = None,
    expected_protocol_fingerprint: str | None = None,
    now_ms: int | None = None,
    forbidden_freeze_ts_ms: frozenset[int] | set[int] = frozenset(),
    require_pinned_fingerprint: bool = False,
) -> dict:
    """Validate one freeze record fully; return the normalized record.

    ``now_ms`` is the authoritative clock at claim time (prefer the PostgreSQL
    server clock). When provided, the freshness window is enforced against it;
    the internal consistency window against ``claimed_at`` is always enforced.
    ``require_pinned_fingerprint`` additionally demands the reviewed
    ``FROZEN_PROTOCOL_FINGERPRINT`` (operator tool path).
    """
    from app.research.htf_baseline_short_phase2_protocol_v1 import (
        FROZEN_PROTOCOL_FINGERPRINT,
    )

    if not isinstance(record, dict):
        raise FreezeRecordError("freeze record must be a JSON object")
    missing = [key for key in _REQUIRED_FIELDS if not record.get(key)]
    if missing:
        raise FreezeRecordError(f"freeze record missing field(s): {missing}")
    if record.get("schema") != FREEZE_RECORD_SCHEMA:
        raise FreezeRecordError(
            f"unsupported freeze record schema {record.get('schema')!r}"
        )
    # tamper evidence FIRST: nothing else is trustworthy until the hash verifies
    expected_hash = record.get("record_sha256")
    if not isinstance(expected_hash, str) or not _FINGERPRINT_RE.match(expected_hash):
        raise FreezeRecordError("freeze record record_sha256 must be 64 hex chars")
    if freeze_record_hash(record) != expected_hash:
        raise FreezeRecordError("freeze record hash mismatch (tampered or truncated)")

    fingerprint = record["protocol_fingerprint"]
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
        raise FreezeRecordError("freeze record protocol_fingerprint must be 64 hex chars")
    if expected_protocol_fingerprint is not None and fingerprint != expected_protocol_fingerprint:
        raise FreezeRecordError(
            "freeze record protocol_fingerprint does not match the running code "
            "(fail closed)"
        )
    if require_pinned_fingerprint and fingerprint != FROZEN_PROTOCOL_FINGERPRINT:
        raise FreezeRecordError(
            "freeze record protocol_fingerprint is not the pinned FROZEN "
            "protocol fingerprint (fail closed)"
        )
    if expected_experiment_id is not None and record["experiment_id"] != expected_experiment_id:
        raise FreezeRecordError(
            f"freeze record experiment_id {record['experiment_id']!r} != "
            f"{expected_experiment_id!r}"
        )

    freeze_ms = _require_canonical_ts(record["freeze_ts"], "freeze_ts")
    claimed_ms = _require_canonical_ts(record["claimed_at"], "claimed_at")
    if freeze_ms in forbidden_freeze_ts_ms:
        raise FreezeRecordError(
            "freeze record freeze_ts is a forbidden legacy boundary timestamp "
            "(never reusable)"
        )

    # internal consistency: the record may only bind a timestamp inside the DB
    # acceptance window around the moment the operator claimed it; this rejects
    # backdated, stale and far-future records without consulting any clock.
    window_ms = ACCEPTANCE_FUTURE_WINDOW_S * 1000
    past_ms = ACCEPTANCE_PAST_WINDOW_S * 1000
    if freeze_ms < claimed_ms - past_ms:
        raise FreezeRecordError(
            "freeze record freeze_ts is backdated relative to claimed_at "
            "(stale record; fail closed)"
        )
    if freeze_ms > claimed_ms + window_ms:
        raise FreezeRecordError(
            "freeze record freeze_ts is far future relative to claimed_at "
            "(fail closed)"
        )

    # external freshness: at claim time the same window must hold vs the
    # authoritative clock (mirrors the PostgreSQL activation guard exactly).
    if now_ms is not None:
        if now_ms < freeze_ms - window_ms or now_ms > freeze_ms + past_ms:
            raise FreezeRecordError(
                "freeze record freeze_ts is outside the PostgreSQL acceptance "
                f"window at claim time (now- {ACCEPTANCE_PAST_WINDOW_S}s .. "
                f"now+ {ACCEPTANCE_FUTURE_WINDOW_S}s); activation would be "
                "rejected (fail closed)"
            )
    return dict(record)


def verify_authority_agreement(
    *,
    record: dict,
    database_freeze_ts,
    database_fingerprint,
    activation: dict,
    expected_experiment_id: str,
    expected_protocol_fingerprint: str,
) -> dict:
    """Require exact agreement of ALL applicable authorities; fail closed.

    Checked equalities (each missing or unequal value raises FreezeRecordError):
      * experiment_id: record / activation / expected;
      * freeze_ts: record / database / activation (exact, millisecond level);
      * protocol_fingerprint: record / database / activation / running code.
    """
    if activation is None:
        raise FreezeRecordError("runtime activation config is missing (fail closed)")
    if not isinstance(activation, dict):
        raise FreezeRecordError("runtime activation config must be a JSON object")

    problems: list[str] = []
    if activation.get("experiment_id") != expected_experiment_id:
        problems.append("activation experiment_id mismatch")
    if record.get("experiment_id") != expected_experiment_id:
        problems.append("record experiment_id mismatch")

    for source_name, value in (
        ("database", database_freeze_ts),
        ("activation", activation.get("freeze_ts")),
        ("record", record.get("freeze_ts")),
    ):
        if value in (None, ""):
            problems.append(f"{source_name} freeze_ts missing")
    if database_fingerprint in (None, ""):
        problems.append("database protocol_fingerprint missing")
    if activation.get("protocol_fingerprint") != expected_protocol_fingerprint:
        problems.append("activation protocol_fingerprint mismatch")
    if record.get("protocol_fingerprint") != expected_protocol_fingerprint:
        problems.append("record protocol_fingerprint mismatch")
    if database_fingerprint not in (None, "") and (
        database_fingerprint != expected_protocol_fingerprint
    ):
        problems.append("database protocol_fingerprint mismatch")
    if problems:
        raise FreezeRecordError("freeze authority disagreement: " + "; ".join(problems))

    parsed = {
        "database": parse_ts_ms(database_freeze_ts),
        "activation": parse_ts_ms(activation.get("freeze_ts")),
        "record": parse_ts_ms(record.get("freeze_ts")),
    }
    if len(set(parsed.values())) != 1:
        raise FreezeRecordError(
            f"freeze authority timestamp disagreement {sorted(parsed.items())} "
            "(fail closed)"
        )
    return {
        "experiment_id": expected_experiment_id,
        "freeze_ts_ms": parsed["record"],
        "protocol_fingerprint": expected_protocol_fingerprint,
    }


# ------------------------------------------------------------------------- I/O
def load_freeze_record(path: str | Path) -> dict:
    """Read the operator freeze record file defensively (read-only).

    Raises :class:`FreezeRecordError` on any missing/unreadable/non-object file.
    """
    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FreezeRecordError(f"freeze record unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise FreezeRecordError("freeze record must be a JSON object")
    return data


# ------------------------------------------------------ claim receipt (FIX-4)
CLAIM_RECEIPT_SCHEMA = "HTF_PHASE2_CLAIM_RECEIPT_V1"

_RECEIPT_REQUIRED_FIELDS = (
    "schema",
    "experiment_id",
    "protocol_fingerprint",
    "freeze_ts",
    "claimed_at",
    "record_id",
    "record_sha256",
)

#: Fields whose EXACT agreement binds a presented freeze record to the record
#: the original claim committed. ``record_id`` and ``record_sha256`` are the
#: identity proper; the rest bind the receipt to the claimed DB boundary.
_IDENTITY_BOUND_FIELDS = (
    "experiment_id",
    "protocol_fingerprint",
    "freeze_ts",
    "claimed_at",
    "record_id",
    "record_sha256",
)


def canonical_receipt_json(receipt: dict) -> str:
    """Deterministic canonical serialization of the receipt payload (no hash)."""
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def claim_receipt_hash(receipt: dict) -> str:
    """SHA-256 over the canonical receipt payload WITHOUT ``receipt_sha256``.

    Integrity only — never provenance (see :func:`verify_claim_identity`).
    """
    return sha256(canonical_receipt_json(receipt).encode("utf-8")).hexdigest()


def build_claim_receipt(record: dict) -> dict:
    """Compose the claim receipt for ``record`` — a DETERMINISTIC pure function.

    The receipt is sealed by the operator tool EXACTLY ONCE at the initial
    claim (strictly before the activation transaction commits) and is never
    overwritten. Because it depends only on the record payload, re-running the
    seal after an interrupted claim reproduces byte-identical content
    (idempotent retry of the SAME record), while any other record — a second
    ``mint`` in the same second has a different ``record_id`` and
    ``record_sha256`` even when ``freeze_ts`` and the protocol fingerprint are
    identical — yields different content and can never replace the seal.
    """
    receipt = {
        "schema": CLAIM_RECEIPT_SCHEMA,
        "experiment_id": record["experiment_id"],
        "protocol_fingerprint": record["protocol_fingerprint"],
        "freeze_ts": record["freeze_ts"],
        "claimed_at": record["claimed_at"],
        "record_id": record["record_id"],
        "record_sha256": record["record_sha256"],
    }
    receipt["receipt_sha256"] = claim_receipt_hash(receipt)
    return receipt


def validate_claim_receipt(receipt: dict) -> dict:
    """Validate one claim receipt fully (structure, canonical form, self-hash).

    Returns a normalized copy; raises :class:`FreezeRecordError` on anything
    missing, malformed or tampered. Note that a valid self-hash proves only
    internal integrity — provenance is established by the one-time seal at the
    original claim, never by this hash.
    """
    if not isinstance(receipt, dict):
        raise FreezeRecordError("claim receipt must be a JSON object")
    missing = [key for key in _RECEIPT_REQUIRED_FIELDS if not receipt.get(key)]
    if missing:
        raise FreezeRecordError(f"claim receipt missing field(s): {missing}")
    if receipt.get("schema") != CLAIM_RECEIPT_SCHEMA:
        raise FreezeRecordError(
            f"unsupported claim receipt schema {receipt.get('schema')!r}"
        )
    expected_hash = receipt.get("receipt_sha256")
    if not isinstance(expected_hash, str) or not _FINGERPRINT_RE.match(expected_hash):
        raise FreezeRecordError("claim receipt receipt_sha256 must be 64 hex chars")
    if claim_receipt_hash(receipt) != expected_hash:
        raise FreezeRecordError("claim receipt hash mismatch (tampered or truncated)")
    _require_canonical_ts(receipt["freeze_ts"], "freeze_ts")
    _require_canonical_ts(receipt["claimed_at"], "claimed_at")
    return dict(receipt)


def verify_claim_identity(receipt: dict, record: dict) -> None:
    """Bind a presented freeze record to the ORIGINAL claimed record identity.

    The trusted source of that identity is the claim receipt sealed at the
    initial claim. The presented record's own ``record_sha256`` self-hash is
    NOT evidence that the record belongs to the original GO — a second record
    minted in the same second carries a perfectly valid self-hash and a fully
    matching DB boundary. Therefore BOTH ``record_id`` and ``record_sha256``
    (plus every boundary field) must equal the receipt EXACTLY; any mismatch
    raises :class:`FreezeRecordError` and the caller must fail closed BEFORE
    any artifact is written.
    """
    problems = [
        field for field in _IDENTITY_BOUND_FIELDS if receipt.get(field) != record.get(field)
    ]
    if problems:
        raise FreezeRecordError(
            "presented freeze record is NOT the record of the original claim: "
            + ", ".join(f"{field} mismatch" for field in problems)
            + f" (claimed record_id={receipt.get('record_id')!r}, "
            f"record_sha256={receipt.get('record_sha256')!r}; presented "
            f"record_id={record.get('record_id')!r}, "
            f"record_sha256={record.get('record_sha256')!r})"
        )


def load_claim_receipt(path: str | Path) -> dict:
    """Read the claim-receipt file defensively (read-only).

    Raises :class:`FreezeRecordError` on any missing/unreadable/non-object file
    — an absent receipt means the original identity cannot be established and
    every repair must fail closed.
    """
    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FreezeRecordError(f"claim receipt unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise FreezeRecordError("claim receipt must be a JSON object")
    return data
