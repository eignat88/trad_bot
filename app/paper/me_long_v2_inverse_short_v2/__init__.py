"""ME_LONG_V2_INVERSE_SHORT_V2 — independent prospective SHORT paper contour.

Derived from the ORIGINAL ``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` detector
events (source events are LONG; the contour trades the inverse SHORT side)
under a frozen, fingerprint-fixed protocol:

  SHORT · TP 10% · SL 10% · max hold 7200 minutes · entry signal TTL
  120 minutes (V2: events at/past the TTL never open and are terminally
  disposed as EXPIRED / CAPACITY_REJECTED) · DCA/trailing/BE OFF ·
  prospective-only · paper-only · no historical backfill.

Status: IMPLEMENTATION_REQUIRED / NOT_DEPLOYED until the protocol
fingerprint + ``freeze_ts`` are separately fixed (see
``app.paper.me_long_v2_inverse_short_v2.activation`` and
``scripts/me_inverse_short_v2_activate.py``).
"""
from __future__ import annotations

from .activation import (
    ActivationIdentity,
    evaluate_activation,
    is_post_freeze,
    is_runtime_enabled,
    resolve_activation,
)
from .capture import capture_with_status, event_from_candidate
from .engine import InverseShortPaperEngine
from .identity import event_key, position_key
from .models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_CAPACITY_REJECTED,
    EVENT_STATUS_ENTERED,
    EVENT_STATUS_EXPIRED,
    EVENT_STATUSES,
    ActivationRecord,
    InverseShortEvent,
    InverseShortPosition,
)
from .observer import SignalLevelProspectiveObserver
from .protocol import (
    ENTRY_MAX_AGE_MINUTES,
    STRATEGY_ID,
    describe_protocol,
    protocol_fingerprint,
)
from .runner import InverseShortContour, run_dry_run
from .store import (
    InMemoryInverseShortStore,
    InverseShortStore,
    PgInverseShortStore,
    connect_pg_store,
)

__all__ = [
    "STRATEGY_ID",
    "ENTRY_MAX_AGE_MINUTES",
    "EVENT_STATUS_CAPTURED",
    "EVENT_STATUS_CAPACITY_REJECTED",
    "EVENT_STATUS_ENTERED",
    "EVENT_STATUS_EXPIRED",
    "EVENT_STATUSES",
    "ActivationIdentity",
    "ActivationRecord",
    "InverseShortContour",
    "InverseShortEvent",
    "InverseShortPaperEngine",
    "InverseShortPosition",
    "InverseShortStore",
    "InMemoryInverseShortStore",
    "PgInverseShortStore",
    "SignalLevelProspectiveObserver",
    "capture_with_status",
    "connect_pg_store",
    "describe_protocol",
    "event_from_candidate",
    "event_key",
    "evaluate_activation",
    "is_post_freeze",
    "is_runtime_enabled",
    "position_key",
    "protocol_fingerprint",
    "resolve_activation",
    "run_dry_run",
]
