"""ME_LONG_V2_INVERSE_SHORT_V3 — independent prospective SHORT paper contour.

Issue #285 (``ME_INVERSE_SHORT_V3_ENTRY_GUARDS_AND_CAPACITY_V1``): the V3
successor of ``ME_LONG_V2_INVERSE_SHORT_V2`` — SAME frozen detector source
and execution geometry, NEW entry guards and capacity:

  SHORT · TP 10% · SL 10% · max hold 7200 minutes · entry signal TTL
  120 minutes · **max 12 OPEN positions across V2+V3** · **single OPEN
  position per symbol across V2+V3** · **12h per-symbol entry cooldown from
  the last committed entered_at** · DCA/trailing/BE OFF · prospective-only ·
  paper-only · no historical backfill.

Status: NOT_DEPLOYED until the V3 protocol fingerprint + ``freeze_ts`` are
separately fixed (see ``app.paper.me_long_v2_inverse_short_v3.activation``
and ``scripts/me_inverse_short_v3_activate.py``).  V2 and OOS-2 are never
modified by this package; no deployment, drain, activation or merge belongs
to the implementation of this issue.
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
    EVENT_STATUS_SYMBOL_REJECTED,
    EVENT_STATUSES,
    ActivationRecord,
    InverseShortEvent,
    InverseShortPosition,
)
from .observer import SignalLevelProspectiveObserver
from .protocol import (
    ENTRY_MAX_AGE_MINUTES,
    MAX_OPEN_POSITIONS_TOTAL,
    SINGLE_ACTIVE_SYMBOL,
    STRATEGY_ID,
    SYMBOL_ENTRY_COOLDOWN_HOURS,
    describe_protocol,
    protocol_fingerprint,
)
from .runner import InverseShortContour, run_dry_run
from .store import (
    AdmissionDecision,
    CrossContourState,
    InMemoryInverseShortStore,
    InMemoryV2Ledger,
    InverseShortStore,
    PgInverseShortStore,
    connect_pg_store,
)

__all__ = [
    "STRATEGY_ID",
    "ENTRY_MAX_AGE_MINUTES",
    "MAX_OPEN_POSITIONS_TOTAL",
    "SINGLE_ACTIVE_SYMBOL",
    "SYMBOL_ENTRY_COOLDOWN_HOURS",
    "EVENT_STATUS_CAPTURED",
    "EVENT_STATUS_CAPACITY_REJECTED",
    "EVENT_STATUS_ENTERED",
    "EVENT_STATUS_EXPIRED",
    "EVENT_STATUS_SYMBOL_REJECTED",
    "EVENT_STATUSES",
    "ActivationIdentity",
    "ActivationRecord",
    "AdmissionDecision",
    "CrossContourState",
    "InverseShortContour",
    "InverseShortEvent",
    "InverseShortPaperEngine",
    "InverseShortPosition",
    "InverseShortStore",
    "InMemoryInverseShortStore",
    "InMemoryV2Ledger",
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
