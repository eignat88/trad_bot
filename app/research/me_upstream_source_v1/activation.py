"""Issue #305 — fail-closed activation for the upstream source capture.

Prospective-only launch contract (requirement 4.3).  Capture is allowed
ONLY when BOTH gates hold:

  1. the runtime feature flag ``ME_UPSTREAM_SOURCE_CAPTURE_V1_ENABLED=1``, and
  2. an activation row (``research_me_upstream_coverage.activation``) with
     ``status = 'RUNNING'``, a non-null timezone-aware ``freeze_ts`` and a
     ``protocol_fingerprint`` byte-identical to the fingerprint of the code
     running NOW.

Everything else — missing row, ``DISABLED`` / ``READY`` / ``PAUSED`` /
``BLOCKED`` / unknown status, foreign fingerprint, missing or naive freeze
timestamp, disabled flag — resolves to ``None`` with an honest reason.
Capture fails closed on ``None``.

The strict prospective boundary is ``signal_time > freeze_ts``: historical
backfill is impossible by construction (refused here AND by the database
CHECK in migration 094), and ``freeze_ts`` is never rewritten by any
restore/restart path (migration 094 trigger).  Nothing in this module ever
writes the activation row — activation is administrative
(``scripts/me_upstream_source_v1_admin.py``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .protocol import (
    ACTIVATION_FLAG,
    EXPERIMENT_ID,
    protocol_fingerprint,
)

# Activation statuses.  Only RUNNING grants capture (prospective-only);
# everything else fails closed.
STATUS_DISABLED = "DISABLED"
STATUS_READY = "READY"
STATUS_RUNNING = "RUNNING"
STATUS_PAUSED = "PAUSED"
STATUS_BLOCKED = "BLOCKED"
VALID_STATUSES = frozenset({
    STATUS_DISABLED, STATUS_READY, STATUS_RUNNING, STATUS_PAUSED,
    STATUS_BLOCKED,
})

# Honest diagnostics (logged / asserted, never used as control flow alone).
REASON_ACTIVE = "ACTIVE"
REASON_DISABLED = "DISABLED_FLAG"
REASON_NOT_INSTALLED = "ACTIVATION_ROW_MISSING"
REASON_NOT_RUNNING = "ACTIVATION_NOT_RUNNING"
REASON_FINGERPRINT_MISMATCH = "PROTOCOL_FINGERPRINT_MISMATCH"
REASON_MISSING_FREEZE = "FREEZE_TS_MISSING"
REASON_INVALID_FREEZE = "FREEZE_TS_INVALID"
REASON_STORE_ERROR = "ACTIVATION_READ_ERROR"
REASON_PRE_FREEZE = "PRE_FREEZE"


@dataclass(frozen=True)
class UpstreamActivation:
    """The one activation identity this process may capture under."""

    experiment_id: str
    status: str
    protocol_version: str
    protocol_fingerprint: str
    freeze_ts: datetime


def is_runtime_enabled(env: dict[str, str] | None = None) -> bool:
    """Runtime feature flag (first of the two activation gates).

    OFF by default: with the flag unset no DB connection is ever attempted.
    """
    source = os.environ if env is None else env
    return source.get(ACTIVATION_FLAG) == "1"


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def evaluate_activation(
    record: Any,
    *,
    enabled: bool | None = None,
    expected_fingerprint: str | None = None,
) -> tuple[UpstreamActivation | None, str]:
    """Validate an activation row against the RUNNING protocol contract."""
    if enabled is None:
        enabled = is_runtime_enabled()
    if not enabled:
        return None, REASON_DISABLED

    if record is None:
        return None, REASON_NOT_INSTALLED

    experiment_id = str(
        record.get("experiment_id") if isinstance(record, dict)
        else getattr(record, "experiment_id", "")
    )
    if experiment_id != EXPERIMENT_ID:
        return None, REASON_NOT_INSTALLED

    status = str(
        record.get("status") if isinstance(record, dict)
        else getattr(record, "status", "")
    )
    if status != STATUS_RUNNING:
        return None, REASON_NOT_RUNNING

    stored_fingerprint = str(
        record.get("protocol_fingerprint") if isinstance(record, dict)
        else getattr(record, "protocol_fingerprint", "")
    )
    expected = expected_fingerprint or protocol_fingerprint()
    if stored_fingerprint != expected:
        return None, REASON_FINGERPRINT_MISMATCH

    raw_freeze = (
        record.get("freeze_ts") if isinstance(record, dict)
        else getattr(record, "freeze_ts", None)
    )
    if raw_freeze is None:
        return None, REASON_MISSING_FREEZE
    freeze = _as_utc(raw_freeze)
    if freeze is None:
        return None, REASON_INVALID_FREEZE

    protocol_version = str(
        record.get("protocol_version") if isinstance(record, dict)
        else getattr(record, "protocol_version", "")
    )

    return UpstreamActivation(
        experiment_id=experiment_id,
        status=status,
        protocol_version=protocol_version,
        protocol_fingerprint=stored_fingerprint,
        freeze_ts=freeze,
    ), REASON_ACTIVE


def is_post_freeze(signal_time: datetime, freeze_ts: datetime) -> bool:
    """Strict prospective boundary: ``signal_time > freeze_ts``.

    Naive timestamps on either side fail closed (``False``).
    """
    signal = _as_utc(signal_time)
    freeze = _as_utc(freeze_ts)
    if signal is None or freeze is None:
        return False
    return signal > freeze
