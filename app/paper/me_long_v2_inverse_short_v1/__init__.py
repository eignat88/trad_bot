"""ME_LONG_V2_INVERSE_SHORT_V1 — independent prospective SHORT paper contour.

Derived from the ORIGINAL ``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` detector
events (source events are LONG; the contour trades the inverse SHORT side)
under a frozen, fingerprint-fixed protocol:

  SHORT · TP 10% · SL 10% · max hold 7200 minutes · DCA/trailing/BE OFF ·
  prospective-only · paper-only · no historical backfill.

Status: IMPLEMENTATION_REQUIRED / NOT_DEPLOYED until the protocol
fingerprint + ``freeze_ts`` are separately fixed (see
``app.paper.me_long_v2_inverse_short_v1.activation`` and
``scripts/me_inverse_short_v1_activate.py``).
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
from .models import ActivationRecord, InverseShortEvent, InverseShortPosition
from .protocol import (
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
    "ActivationIdentity",
    "ActivationRecord",
    "InverseShortContour",
    "InverseShortEvent",
    "InverseShortPaperEngine",
    "InverseShortPosition",
    "InverseShortStore",
    "InMemoryInverseShortStore",
    "PgInverseShortStore",
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
