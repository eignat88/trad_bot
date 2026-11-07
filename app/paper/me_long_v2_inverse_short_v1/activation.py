"""Fail-closed activation for ME_LONG_V2_INVERSE_SHORT_V1.

Launch is allowed ONLY after both gates hold (requirement: separate fixation
of the protocol fingerprint / ``freeze_ts`` before start):

  1. the runtime feature flag ``ME_INVERSE_SHORT_V1_ENABLED=1``, and
  2. an activation row with ``status = 'RUNNING'`` whose stored
     ``protocol_fingerprint`` is byte-identical to the fingerprint of the
     code that is running NOW, with a non-null, timezone-aware ``freeze_ts``.

``status = 'DRAINING'`` (set by ``scripts/me_inverse_short_v1_activate.py
--drain``, which PRESERVES fingerprint and freeze_ts) is also a valid source
identity, but grants ONLY ``MANAGE_EXISTING``: the contour runs ``EXIT_ONLY``
— STOP/TP/TIMEOUT exits, funding reconciliation, outbox replay and pending
close recovery continue, while ``NEW_ENTRIES`` (new source capture, new
entries, reuse of CAPTURED events) is refused.

Anything else — missing row, ``PENDING``, ``CANCELLED``, unknown status,
stale/foreign fingerprint, missing or naive freeze timestamp, disabled flag —
resolves to ``None`` with an honest reason.  Capture and entries both fail
closed on ``None``.

The strict prospective boundary is ``signal_time > freeze_ts``: historical
backfill is impossible by construction (an event at or before the freeze is
refused here AND by the database CHECK in migration 085), and ``freeze_ts``
is never rewritten by any restore/restart path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .models import ActivationRecord
from .protocol import EXPERIMENT_ID, protocol_fingerprint

ACTIVATION_FLAG = "ME_INVERSE_SHORT_V1_ENABLED"

# Activation statuses that carry a valid source identity.  Everything else
# (READY_TO_START / PENDING / CANCELLED / unknown) fails closed.
STATUS_RUNNING = "RUNNING"
STATUS_DRAINING = "DRAINING"
VALID_STATUSES = frozenset({STATUS_RUNNING, STATUS_DRAINING})

# Review P2-2: NEW_ENTRIES and MANAGE_EXISTING are SEPARATE grants.
PERMISSION_NEW_ENTRIES = "NEW_ENTRIES"
PERMISSION_MANAGE_EXISTING = "MANAGE_EXISTING"
# RUNNING -> FULL (both grants); DRAINING -> EXIT_ONLY (manage only).
MODE_FULL = "FULL"
MODE_EXIT_ONLY = "EXIT_ONLY"
EXIT_ONLY_STATUSES = frozenset({STATUS_DRAINING})

# Honest diagnostics (logged / asserted, never used as control flow alone).
REASON_ACTIVE = "ACTIVE"
REASON_EXIT_ONLY = "DRAINING_EXIT_ONLY"
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

    # -- separated permissions (review P2-2) ------------------------------
    @property
    def exit_only(self) -> bool:
        """True when the identity grants MANAGE_EXISTING only (DRAINING)."""
        return self.status in EXIT_ONLY_STATUSES

    @property
    def mode(self) -> str:
        """``FULL`` (RUNNING) or ``EXIT_ONLY`` (DRAINING)."""
        return MODE_EXIT_ONLY if self.exit_only else MODE_FULL

    def allows(self, permission: str) -> bool:
        """Separate permission grants — UNKNOWN permissions fail closed."""
        if permission == PERMISSION_MANAGE_EXISTING:
            # Any valid identity may manage existing positions/exits
            # (STOP/TP/TIMEOUT, funding reconciliation, outbox replay).
            return True
        if permission == PERMISSION_NEW_ENTRIES:
            # New source captures / entries require FULL (RUNNING) mode.
            return not self.exit_only
        return False


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
    """Validate an activation row against the RUNNING protocol contract.

    ``RUNNING`` resolves to a FULL identity (NEW_ENTRIES + MANAGE_EXISTING).
    ``DRAINING`` resolves to an EXIT_ONLY identity (MANAGE_EXISTING only) —
    but ONLY with a byte-identical fingerprint and a valid freeze_ts; a
    corrupt identity fails closed exactly like RUNNING would.
    """
    if enabled is None:
        enabled = is_runtime_enabled()
    if not enabled:
        return None, REASON_DISABLED
    if record is None:
        return None, REASON_NOT_INSTALLED
    if record.experiment_id != EXPERIMENT_ID:
        return None, REASON_NOT_INSTALLED
    if record.status not in VALID_STATUSES:
        # CANCELLED, READY_TO_START, PENDING and every unknown status fail
        # closed: no identity, no capture, no entries.
        return None, REASON_NOT_RUNNING

    expected = expected_fingerprint or protocol_fingerprint()
    if record.protocol_fingerprint != expected:
        return None, REASON_FINGERPRINT_MISMATCH

    freeze = _as_utc(record.freeze_ts)
    if record.freeze_ts is None:
        return None, REASON_MISSING_FREEZE
    if freeze is None:
        return None, REASON_INVALID_FREEZE

    identity = ActivationIdentity(
        experiment_id=record.experiment_id,
        status=record.status,
        protocol_fingerprint=record.protocol_fingerprint,
        freeze_ts=freeze,
    )
    if identity.exit_only:
        return identity, REASON_EXIT_ONLY
    return identity, REASON_ACTIVE


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
