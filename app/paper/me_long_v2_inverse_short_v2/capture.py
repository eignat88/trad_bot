"""Source capture for ME_LONG_V2_INVERSE_SHORT_V2 (requirement 1).

Captures the ORIGINAL ``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` detector
events into the contour's own event ledger, INDEPENDENTLY of the trading
``direction gate`` and the ``expectancy filter`` — and without modifying
either of them.

Capture point: ``ScannerOrchestrator.scan_all_with_stats`` publishes the
PRE-dedup, PRE-geometry / PRE-gate / PRE-expectancy candidates under the
``_me_inverse_short_source_events`` stats key; ``scanner_runner`` forwards
them here.  The pre-dedup publication lets an unconfirmed capture (a
PostgreSQL failure on the first delivery) be re-delivered by a later scan
of the same candidate, while ``event_key`` idempotency guarantees the
replay never creates a second row.  Both layers are fail-open: an exception
here can never break a scanner cycle or change a trading decision.

Safety invariants
-----------------
* No setup state, no gate row, no expectancy record and no production
  ``dds.scanner_setup`` / ``dds.paper_trade`` write is performed here.
* Capture requires BOTH the runtime flag ``ME_INVERSE_SHORT_V2_ENABLED=1``
  and a RUNNING activation row with the CURRENT protocol fingerprint —
  so a code/parameter change (new fingerprint) stops capture automatically.
* Strict ``signal_time > freeze_ts`` plus the database CHECK (migration 087)
  make historical backfill impossible by construction.
* Every write is one idempotent statement: replaying the same source event
  after a restart yields ``DUPLICATE``, never a second row.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

from app.scanners.models import SetupCandidate

from .activation import (
    ActivationIdentity,
    PERMISSION_NEW_ENTRIES,
    REASON_EXIT_ONLY,
    is_post_freeze,
    is_runtime_enabled,
    evaluate_activation,
)
from .identity import event_key, signal_candle_open_ms, source_identity
from .models import InverseShortEvent
from .protocol import (
    CAPTURE_ROUTES,
    PROTOCOL_VERSION,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    protocol_fingerprint,
)

logger = logging.getLogger(__name__)

# Honest capture outcomes.
CAPTURE_DISABLED = "DISABLED"
CAPTURE_INVALID_SCANNER = "INVALID_SCANNER"
CAPTURE_INVALID_ROUTE = "INVALID_ROUTE"
CAPTURE_INVALID_EVENT = "INVALID_EVENT"
CAPTURE_NOT_ACTIVE = "NOT_ACTIVE"
CAPTURE_PRE_FREEZE = "PRE_FREEZE"
CAPTURED = "CAPTURED"
CAPTURE_DUPLICATE = "DUPLICATE"
CAPTURE_ERROR = "ERROR"

CAPTURE_STATUSES = (
    CAPTURE_DISABLED, CAPTURE_INVALID_SCANNER, CAPTURE_INVALID_ROUTE,
    CAPTURE_INVALID_EVENT, CAPTURE_NOT_ACTIVE, CAPTURE_PRE_FREEZE,
    CAPTURED, CAPTURE_DUPLICATE, CAPTURE_ERROR,
)

# Features carried into the lineage for audit (all JSON-safe scalars).
_LINEAGE_FEATURE_KEYS = (
    "exhaustion_magnitude", "body_ratio", "rsi_confirmation", "volume_ratio",
    "rr_ratio", "stop_distance_atr", "rsi_14", "rsi_delta_3",
    "entry_confirmation_rsi_rising",
)


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    return str(value)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def event_from_candidate(
    candidate: SetupCandidate,
    *,
    capture_route: str,
    activation: ActivationIdentity,
    captured_at: datetime | None = None,
) -> InverseShortEvent:
    """Build the contour event for one source candidate (pure, may raise)."""
    if capture_route not in CAPTURE_ROUTES:
        raise ValueError("INVALID_CAPTURE_ROUTE")
    if candidate.scanner_name != SOURCE_SCANNER:
        raise ValueError("INVALID_SOURCE_SCANNER")
    if candidate.direction != SOURCE_DIRECTION:
        raise ValueError("INVALID_SOURCE_DIRECTION")

    detected_at = candidate.detected_at
    if not isinstance(detected_at, datetime):
        raise ValueError("MISSING_SIGNAL_TIME")
    if detected_at.tzinfo is None or detected_at.utcoffset() is None:
        raise ValueError("NAIVE_SIGNAL_TIME")
    signal_time = detected_at.astimezone(timezone.utc)

    key = event_key(candidate)
    now = captured_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("NAIVE_CAPTURED_AT")

    features = candidate.features or {}
    lineage = {
        "source_identity": source_identity(candidate),
        "source_setup_id": str(candidate.setup_id) if candidate.setup_id else "",
        "source_scanner": candidate.scanner_name,
        "source_scanner_version": candidate.scanner_version,
        "source_direction": candidate.direction,
        "source_signal_time": signal_time.isoformat(),
        "source_signal_candle_open_time": int(
            signal_candle_open_ms(candidate)
        ),
        "source_entry_timeframe": candidate.entry_timeframe,
        "source_state": str(getattr(candidate.state, "value", candidate.state)),
        "source_score": candidate.score,
        "source_market_regime": candidate.market_regime,
        "source_reference_price": _opt_float(candidate.reference_price),
        "source_invalidation_price": _opt_float(candidate.invalidation_price),
        "source_target_price": _opt_float(candidate.target_1),
        "source_target_2": _opt_float(candidate.target_2),
        "source_entry_zone": [
            _opt_float(candidate.entry_zone_low),
            _opt_float(candidate.entry_zone_high),
        ],
        "source_reasons": list(candidate.reasons or ()),
        "source_features": {
            key_name: _json_safe(features.get(key_name))
            for key_name in _LINEAGE_FEATURE_KEYS
            if key_name in features
        },
        "capture_route": capture_route,
        "capture_independent_of": ["direction_gate", "expectancy_filter"],
        "source_pipeline_fingerprint": candidate.fingerprint,
        "protocol_version": PROTOCOL_VERSION,
        "protocol_fingerprint": protocol_fingerprint(),
    }

    return InverseShortEvent(
        event_key=key,
        symbol=candidate.symbol,
        signal_time=signal_time,
        captured_at=now.astimezone(timezone.utc),
        activation_freeze_ts=activation.freeze_ts,
        signal_candle_open_time=int(signal_candle_open_ms(candidate)),
        source_scanner=candidate.scanner_name,
        source_scanner_version=str(candidate.scanner_version or ""),
        source_direction=candidate.direction,
        source_setup_id=str(candidate.setup_id) if candidate.setup_id else "",
        entry_timeframe=str(candidate.entry_timeframe or "5m"),
        source_reference_price=_opt_float(candidate.reference_price),
        source_invalidation_price=_opt_float(candidate.invalidation_price),
        source_target_price=_opt_float(candidate.target_1),
        source_target_2=_opt_float(candidate.target_2),
        source_score=float(candidate.score) if candidate.score else None,
        market_regime=candidate.market_regime,
        capture_route=capture_route,
        lineage=lineage,
    )


def capture_with_status(
    store: Any,
    candidate: SetupCandidate,
    *,
    capture_route: str = ROUTE_PRE_FILTER_SOURCE,
    env: dict[str, str] | None = None,
    clock: Any | None = None,
) -> str:
    """Capture one source event; return an honest status, NEVER raise.

    Fail-open towards the scanner: any internal failure becomes ``ERROR``.
    """
    if not is_runtime_enabled(env):
        return CAPTURE_DISABLED
    if candidate is None:
        return CAPTURE_INVALID_EVENT
    if candidate.scanner_name != SOURCE_SCANNER or candidate.direction != SOURCE_DIRECTION:
        return CAPTURE_INVALID_SCANNER
    if capture_route not in CAPTURE_ROUTES:
        logger.error(
            "me inverse short v2 capture refused unknown route=%s scanner=%s symbol=%s",
            capture_route, candidate.scanner_name, candidate.symbol,
        )
        return CAPTURE_INVALID_ROUTE

    try:
        record = store.load_activation()
    except Exception:
        logger.debug("me inverse short v2 activation read failed", exc_info=True)
        return CAPTURE_ERROR
    activation, reason = evaluate_activation(record, enabled=True)
    if activation is None:
        logger.debug(
            "me inverse short v2 capture refused: reason=%s scanner=%s symbol=%s",
            reason, candidate.scanner_name, candidate.symbol,
        )
        return CAPTURE_NOT_ACTIVE
    if not activation.allows(PERMISSION_NEW_ENTRIES):
        # EXIT_ONLY (DRAINING): MANAGE_EXISTING only — new source events are
        # refused here, before any row is written and before any freeze
        # boundary is evaluated.  This path never modifies fingerprint or
        # freeze_ts (they stay byte-identical across drain/restart).
        logger.info(
            "me inverse short v2 capture refused (EXIT_ONLY): new source events "
            "are disabled while DRAINING; managing existing positions only: "
            "scanner=%s symbol=%s reason=%s",
            candidate.scanner_name, candidate.symbol, REASON_EXIT_ONLY,
        )
        return CAPTURE_NOT_ACTIVE

    try:
        now = clock() if clock is not None else datetime.now(timezone.utc)
        event = event_from_candidate(
            candidate, capture_route=capture_route,
            activation=activation, captured_at=now,
        )
    except (ValueError, TypeError, OverflowError):
        logger.debug("me inverse short v2 capture invalid event", exc_info=True)
        return CAPTURE_INVALID_EVENT

    if not is_post_freeze(event.signal_time, activation.freeze_ts):
        logger.info(
            "me inverse short v2 capture refused pre-freeze event: "
            "scanner=%s symbol=%s signal_time=%s freeze_ts=%s",
            SOURCE_SCANNER, event.symbol,
            event.signal_time.isoformat(),
            activation.freeze_ts.isoformat(),
        )
        return CAPTURE_PRE_FREEZE

    try:
        result = store.save_event(event)
    except Exception:
        _safe_rollback(store)
        logger.exception(
            "me inverse short v2 capture failed (scanner unaffected): symbol=%s",
            event.symbol,
        )
        return CAPTURE_ERROR

    if result in (CAPTURED, CAPTURE_DUPLICATE, CAPTURE_NOT_ACTIVE):
        if result == CAPTURED:
            logger.info(
                "me inverse short v2 source event captured: symbol=%s event_key=%s "
                "signal_time=%s route=%s",
                event.symbol, event.event_key[:16],
                event.signal_time.isoformat(), capture_route,
            )
        return result
    return CAPTURE_ERROR


def _safe_rollback(store: Any) -> None:
    conn = getattr(store, "_conn", None)
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:  # pragma: no cover - defensive
        logger.debug("me inverse short v2 rollback after error failed", exc_info=True)
