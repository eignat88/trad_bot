"""Issue #305 — fail-open upstream source capture.

Captures the ORIGINAL ``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` / ``LONG``
detector events into the capture's own ledger IMMEDIATELY after
``scanner.scan(ctx)`` — before scoring, dedup, risk geometry, direction
gate, regime filter and expectancy filter (all untouched).

Safety invariants (requirements 4.3 / 4.4)
------------------------------------------
* capture is OFF by default; with the feature flag unset this module
  performs NO store access at all (no connection, no write);
* capture requires BOTH the flag and a RUNNING activation row with a frozen
  ``freeze_ts`` and the CURRENT protocol fingerprint;
* strict ``signal_time > freeze_ts`` (plus the migration 094 CHECK) makes
  historical backfill impossible by construction;
* every write is one idempotent statement: re-delivery after a restart
  yields ``DUPLICATE``, never a second row;
* an immutable-lineage conflict is fail-closed for the RESEARCH contour
  (sticky ``BLOCKED`` state) but fail-open for the scanner: no exception
  ever propagates, the scanner cycle and trading decisions are untouched;
* an invalid / pre-freeze event never enters the eligible ``source_event``
  cohort — it lands in the durable ``rejected_event`` ledger with a
  deterministic ``reason_code`` (criterion 12) and is observable in health;
* ``DISABLED`` / ``NOT_ACTIVE`` / ``BLOCKED`` outcomes perform NO writes.

Honest capture outcomes are returned as status strings — never raised.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.scanners.models import SetupCandidate

from .activation import (
    REASON_PRE_FREEZE,
    evaluate_activation,
    is_post_freeze,
    is_runtime_enabled,
)
from .contracts import (
    SourceIntegrityError,
    SourceValidationError,
    build_source_event,
    normalize_reason_code,
)
from .identity import IdentityError
from .protocol import (
    CAPTURE_ROUTES,
    ROUTE_PRE_SCORING_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
)
from .store import (
    SAVE_CAPTURED,
    SAVE_DUPLICATE,
    SAVE_NOT_ACTIVE,
    STATE_BLOCKED,
)

logger = logging.getLogger(__name__)

# Honest capture outcomes.
CAPTURE_DISABLED = "DISABLED"
CAPTURE_INVALID_SCANNER = "INVALID_SCANNER"
CAPTURE_INVALID_EVENT = "INVALID_EVENT"
CAPTURE_NOT_ACTIVE = "NOT_ACTIVE"
CAPTURE_PRE_FREEZE = "PRE_FREEZE"
CAPTURE_BLOCKED = "BLOCKED"
CAPTURE_INTEGRITY_CONFLICT = "INTEGRITY_CONFLICT"
CAPTURED = "CAPTURED"
CAPTURE_DUPLICATE = "DUPLICATE"
CAPTURE_ERROR = "ERROR"

CAPTURE_STATUSES = (
    CAPTURE_DISABLED, CAPTURE_INVALID_SCANNER, CAPTURE_INVALID_EVENT,
    CAPTURE_NOT_ACTIVE, CAPTURE_PRE_FREEZE, CAPTURE_BLOCKED,
    CAPTURE_INTEGRITY_CONFLICT, CAPTURED, CAPTURE_DUPLICATE, CAPTURE_ERROR,
)

BLOCK_REASON_LINEAGE_CONFLICT = "EVENT_KEY_LINEAGE_CONFLICT"


def capture_with_status(
    store: Any,
    candidate: SetupCandidate | None,
    *,
    capture_route: str = ROUTE_PRE_SCORING_SOURCE,
    env: dict[str, str] | None = None,
    clock: Any | None = None,
) -> str:
    """Capture one detector event; return an honest status, NEVER raise.

    Fail-open towards the scanner: any internal failure becomes ``ERROR``;
    the research contour stays fail-closed (BLOCKED) on integrity conflicts.
    """
    # 1. Feature flag FIRST — with capture off there is no store access at
    #    all (no connection attempt, no read, no write).
    if not is_runtime_enabled(env):
        return CAPTURE_DISABLED

    if candidate is None:
        return CAPTURE_INVALID_EVENT

    if (
        candidate.scanner_name != SOURCE_SCANNER
        or candidate.direction != SOURCE_DIRECTION
    ):
        return CAPTURE_INVALID_SCANNER

    if capture_route not in CAPTURE_ROUTES:
        logger.error(
            "me upstream source capture refused unknown route=%s scanner=%s "
            "symbol=%s",
            capture_route, candidate.scanner_name, candidate.symbol,
        )
        return CAPTURE_INVALID_EVENT

    # 2. Sticky BLOCKED state (fail-closed research contour): nothing is
    #    admitted before an explicit administrative RESUMED.
    try:
        state = store.load_capture_state()
    except Exception:
        logger.debug("me upstream source capture state read failed", exc_info=True)
        return CAPTURE_ERROR
    if state is not None and state.get("state") == STATE_BLOCKED:
        logger.warning(
            "me upstream source capture blocked: reason=%s symbol=%s",
            state.get("reason_code"), candidate.symbol,
        )
        return CAPTURE_BLOCKED

    # 3. Activation gate: flag + RUNNING activation + frozen freeze_ts +
    #    matching protocol fingerprint.  Without them nothing is written.
    try:
        record = store.load_activation()
    except Exception:
        logger.debug("me upstream source activation read failed", exc_info=True)
        return CAPTURE_ERROR
    activation, reason = evaluate_activation(record, enabled=True)
    if activation is None:
        logger.debug(
            "me upstream source capture refused: reason=%s scanner=%s symbol=%s",
            reason, candidate.scanner_name, candidate.symbol,
        )
        return CAPTURE_NOT_ACTIVE

    # 4. Build the immutable event (deterministic validation).
    try:
        now = clock() if clock is not None else datetime.now(timezone.utc)
        event = build_source_event(
            candidate,
            activation_freeze_ts=activation.freeze_ts,
            captured_at=now,
        )
    except (SourceValidationError, IdentityError, ValueError, TypeError,
            OverflowError) as exc:
        code = normalize_reason_code(exc)
        logger.warning(
            "me upstream source capture invalid event: reason=%s scanner=%s "
            "symbol=%s signal_time=%s",
            code, candidate.scanner_name, candidate.symbol,
            getattr(candidate, "detected_at", None),
        )
        _safe_reject(
            store,
            source_event_key=_rejection_key(candidate),
            source_identity=None,
            reason_code=code,
            payload={
                "symbol": candidate.symbol,
                "setup_id": str(getattr(candidate, "setup_id", "") or ""),
            },
        )
        _safe_health(
            store, CAPTURE_INVALID_EVENT,
            error_code=code,
            diagnostics={"symbol": candidate.symbol},
        )
        return CAPTURE_INVALID_EVENT

    # 5. Strict prospective boundary (backfill impossible by construction).
    if not is_post_freeze(event.signal_time, activation.freeze_ts):
        logger.info(
            "me upstream source capture refused pre-freeze event: symbol=%s "
            "signal_time=%s freeze_ts=%s",
            event.symbol, event.signal_time.isoformat(),
            activation.freeze_ts.isoformat(),
        )
        _safe_reject(
            store,
            source_event_key=event.source_event_key,
            source_identity=event.source_identity,
            reason_code=REASON_PRE_FREEZE,
            payload={"symbol": event.symbol},
        )
        _safe_health(
            store, CAPTURE_PRE_FREEZE,
            error_code=REASON_PRE_FREEZE,
            diagnostics={"symbol": event.symbol},
        )
        return CAPTURE_PRE_FREEZE

    # 6. Idempotent, activation-guarded, single-statement write.
    try:
        result = store.save_event(event)
    except SourceIntegrityError:
        # Same key with a DIFFERENT immutable lineage: fail-closed for the
        # research contour (sticky BLOCKED), fail-open for the scanner.
        logger.error(
            "me upstream source capture INTEGRITY_CONFLICT: event_key=%s "
            "symbol=%s",
            event.source_event_key[:16], event.symbol,
        )
        _safe_state(
            store,
            STATE_BLOCKED,
            BLOCK_REASON_LINEAGE_CONFLICT,
            {
                "source_event_key": event.source_event_key,
                "source_identity": event.source_identity,
                "symbol": event.symbol,
            },
        )
        _safe_health(
            store, CAPTURE_INTEGRITY_CONFLICT,
            errors=1,
            error_code=BLOCK_REASON_LINEAGE_CONFLICT,
            diagnostics={"source_event_key": event.source_event_key},
        )
        return CAPTURE_INTEGRITY_CONFLICT
    except Exception:
        # Persistence failure: the scanner continues; the loss is observable
        # (health row when reachable + the hook's dds.scanner_error sink).
        _safe_health(
            store, CAPTURE_ERROR,
            errors=1,
            error_code="SAVE_EVENT_ERROR",
            diagnostics={"symbol": event.symbol},
        )
        logger.exception(
            "me upstream source capture failed (scanner unaffected): symbol=%s",
            event.symbol,
        )
        return CAPTURE_ERROR

    latency = (event.captured_at - event.signal_time).total_seconds()
    if result == SAVE_CAPTURED:
        _safe_health(
            store, CAPTURED,
            events_inserted=1,
            delivery_latency_seconds=latency,
            diagnostics={"symbol": event.symbol},
        )
        logger.info(
            "me upstream source event captured: symbol=%s event_key=%s "
            "signal_time=%s route=%s",
            event.symbol, event.source_event_key[:16],
            event.signal_time.isoformat(), capture_route,
        )
        return CAPTURED
    if result == SAVE_DUPLICATE:
        _safe_health(
            store, CAPTURE_DUPLICATE,
            duplicates=1,
            delivery_latency_seconds=latency,
            diagnostics={"symbol": event.symbol},
        )
        return CAPTURE_DUPLICATE
    if result == SAVE_NOT_ACTIVE:
        return CAPTURE_NOT_ACTIVE
    return CAPTURE_ERROR


def _rejection_key(candidate: SetupCandidate) -> str | None:
    """Deterministic event key for the rejection ledger, when derivable.

    An event with an unusable identity is recorded with ``NULL`` key (its
    ``setup_id`` travels in the payload) — the ledger column semantics stay
    honest: it holds a ``source_event_key`` or NULL, never a substitute id.
    """
    try:
        from .identity import source_event_key

        return source_event_key(candidate)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Best-effort diagnostics helpers — fail-open, never raise.
# ---------------------------------------------------------------------------

def _safe_reject(store: Any, **kwargs: Any) -> None:
    try:
        store.reject_event(**kwargs)
    except Exception:
        logger.debug("me upstream source rejection ledger write failed",
                     exc_info=True)


def _safe_health(store: Any, status: str, **kwargs: Any) -> None:
    try:
        store.record_health(status, **kwargs)
    except Exception:
        logger.debug("me upstream source health write failed", exc_info=True)


def _safe_state(store: Any, state: str, reason_code: str, details: Any) -> None:
    try:
        store.record_state(state, reason_code, details)
    except Exception:
        logger.debug("me upstream source state write failed", exc_info=True)
