"""Fail-closed activation for ME_LONG_V2_INVERSE_SHORT_V1.

Launch is allowed ONLY after both gates hold (requirement: separate fixation
of the protocol fingerprint / ``freeze_ts`` before start):

  1. the runtime feature flag ``ME_INVERSE_SHORT_V1_ENABLED=1``, and
  2. an activation row with ``status = 'RUNNING'`` whose stored
     ``protocol_fingerprint`` is byte-identical to the fingerprint of the
     code that is running NOW, with a non-null, timezone-aware ``freeze_ts``.

Anything else — missing row, ``PENDING``, stale/foreign fingerprint, missing
or naive freeze timestamp, disabled flag — resolves to ``None`` with an
honest reason.  Capture and entries both fail closed on ``None``.

The strict prospective boundary is ``signal_time > freeze_ts``: historical
backfill is impossible by construction (an event at or before the freeze is
refused here AND by the database CHECK in migration 085).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .models import ActivationRecord
from .protocol import EXPERIMENT_ID, protocol_fingerprint

ACTIVATION_FLAG = "ME_INVERSE_SHORT_V1_ENABLED"

# Honest diagnostics (logged / asserted, never used as control flow alone).
REASON_ACTIVE = "ACTIVE"
REASON_DISABLED = "DISABLED_FLAG"
REASON_NOT_INSTALLED = "ACTIVATION_ROW_MISSING"
REASON_NOT_RUNNING = "ACTIVATION_NOT_RUNNING"
REASON_FINGERPRINT_MISMATCH = "PROTOCOL_FINGERPRINT_MISMATCH"
REASON_MISSING_FREEZE = "FREEZE_TS_MISSING"
REASON_INVALID_FREEZE = "FREEZE_TS_INVALID"
REASON_STORE_ERROR = "ACTIVATION_READ_ERROR"


@dataclass(frozen=True)
class ActivationIdentity:
    """The one activation identity this process may operate under."""

    experiment_id: str
    status: str
    protocol_fingerprint: str
    freeze_ts: datetime

    @property
    def freeze_ns(self) -> int:
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = self.freeze_ts - epoch
        return (
            delta.days * 86_400_000_000_000
            + delta.seconds * 1_000_000_000
            + delta.microseconds * 1_000
        )


def is_runtime_enabled(env: dict[str, str] | None = None) -> bool:
    """Runtime feature flag (first of the two activation gates)."""
    source = os.environ if env is None else env
    return source.get(ACTIVATION_FLAG) == "1"


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def evaluate_activation(
    record: ActivationRecord | None,
    *,
    enabled: bool | None = None,
    expected_fingerprint: str | None = None,
) -> tuple[ActivationIdentity | None, str]:
    """Validate an activation row against the RUNNING protocol contract."""
    if enabled is None:
        enabled = is_runtime_enabled()
    if not enabled:
        return None, REASON_DISABLED
    if record is None:
        return None, REASON_NOT_INSTALLED
    if record.experiment_id != EXPERIMENT_ID:
        return None, REASON_NOT_INSTALLED
    if not record.is_running:
        return None, REASON_NOT_RUNNING

    expected = expected_fingerprint or protocol_fingerprint()
    if record.protocol_fingerprint != expected:
        return None, REASON_FINGERPRINT_MISMATCH

    freeze = _as_utc(record.freeze_ts)
    if record.freeze_ts is None:
        return None, REASON_MISSING_FREEZE
    if freeze is None:
        return None, REASON_INVALID_FREEZE

    return (
        ActivationIdentity(
            experiment_id=record.experiment_id,
            status=record.status,
            protocol_fingerprint=record.protocol_fingerprint,
            freeze_ts=freeze,
        ),
        REASON_ACTIVE,
    )


def resolve_activation(
    store: Any,
    *,
    enabled: bool | None = None,
    expected_fingerprint: str | None = None,
) -> tuple[ActivationIdentity | None, str]:
    """Read the activation row from ``store`` and validate it (fail closed)."""
    try:
        record = store.load_activation()
    except Exception:
        return None, REASON_STORE_ERROR
    return evaluate_activation(
        record, enabled=enabled, expected_fingerprint=expected_fingerprint,
    )


def is_post_freeze(signal_time: datetime, freeze_ts: datetime) -> bool:
    """Strict prospective boundary: ``signal_time > freeze_ts``.

    Naive timestamps on either side fail closed (``False``).
    """
    signal = _as_utc(signal_time)
    freeze = _as_utc(freeze_ts)
    if signal is None or freeze is None:
        return False
    return signal > freeze
