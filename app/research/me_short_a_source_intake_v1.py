"""Issue #208 corrective — ``ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1``.

Forward-only, **research-only** intake for the ORIGINAL
``MOMENTUM_EXHAUSTION`` ``SHORT`` events, captured at signal time
independently of every trading filter (direction gate, regime filter,
expectancy filter).

Why this module exists
----------------------
The Issue #208 forward outcome runtime discovers its source candidates in
``dds.scanner_setup``.  Candidates that the trading ``regime filter``
drops (the confirmed incident: ``SHORT`` in ``TREND_UP``) never reach that
table, so Phase 1 snapshots
(``research.me_short_a_fill_feasibility_v1``) end up with an empty
lineage/outcome chain.  This writer is the research-only capture line that
survives every trading filter decision; the forward outcome runtime reads
the resulting rows as an additional candidate source and feeds its frozen
``capture_source_signal -> record_event_lineage -> finalize_outcome``
pipeline from them.

Safety invariants
-----------------
* No order submission is possible in this module (no order, paper or live
  surface at all). It never returns a candidate to the trading flow.
* Capture requires BOTH the runtime feature flag
  ``ME_SHORT_A_SOURCE_INTAKE_ENABLED=1`` and a RUNNING activation row
  (``research.me_short_a_source_intake_activation_v1``) with an explicit
  immutable ``intake_freeze_ts``. Strict ``signal_time > intake_freeze_ts``
  plus the DB ``CHECK`` make a historical backfill impossible by
  construction — the confirmed incident event and every other pre-activation
  observation can never enter the new cohort.
* Every write is ONE atomic ``INSERT ... ON CONFLICT (event_key) DO NOTHING``
  statement: idempotent under retry and safe to replay after a restart.
* The deterministic ``event_key`` is byte-identical to the identity frozen
  in migrations 077/078 (delegated to
  :func:`app.research.me_short_a_forward_outcome_lineage_v1.event_key`), so
  ``intake -> Phase 1 -> lineage -> outcome`` joins on one key.
* This module never writes to Phase 1, never writes to the four Issue #208
  capture tables (their only writer is the forward outcome runtime), never
  rewrites any snapshot, and never touches the Issue #208 ``freeze_ts`` or
  its frozen protocol hash.
* All failures fail OPEN towards the scanner: :func:`capture` catches
  everything and returns ``False``; the caller must never let a research
  capture error affect scanner execution.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from app.research.me_short_a_forward_outcome_lineage_v1 import (
    DIRECTION,
    PROTOCOL_HASH,
    PROTOCOL_VERSION,
    event_key as _lineage_event_key,
)
from app.research.me_short_a_forward_outcome_runtime_v1 import (
    # The SAME normalization the frozen runtime uses when it recomputes an
    # event_key from a ``numeric`` column. One function, one representation.
    canonical_price as _identity_price,
)

LOG = logging.getLogger(__name__)

EXPERIMENT = "ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1"
ACTIVATION_FLAG = "ME_SHORT_A_SOURCE_INTAKE_ENABLED"

SCANNER_NAME = "MOMENTUM_EXHAUSTION"
DIRECTION_SHORT = DIRECTION

# Capture routes: how the candidate reached research capture. The route is
# evidence about the TRADING pipeline's decision, never an input to it.
ROUTE_TRADEABLE_FLOW = "TRADEABLE_FLOW"
ROUTE_SHADOW_CONTROL = "SHADOW_CONTROL"
ROUTE_OBSERVE_ONLY = "OBSERVE_ONLY"
ROUTE_DIRECTION_GATE_BLOCKED = "DIRECTION_GATE_BLOCKED"
ROUTE_EXPECTANCY_FILTER_REJECTED = "EXPECTANCY_FILTER_REJECTED"
ROUTE_REGIME_FILTER_REJECTED = "REGIME_FILTER_REJECTED"

CAPTURE_ROUTES = (
    ROUTE_TRADEABLE_FLOW,
    ROUTE_SHADOW_CONTROL,
    ROUTE_OBSERVE_ONLY,
    ROUTE_DIRECTION_GATE_BLOCKED,
    ROUTE_EXPECTANCY_FILTER_REJECTED,
    ROUTE_REGIME_FILTER_REJECTED,
)

INSERT_SQL = """
    INSERT INTO research.me_short_a_source_intake_v1 (
        event_key, protocol_version, protocol_hash,
        scanner_name, direction, symbol,
        signal_time, captured_at, intake_freeze_ts,
        reference_price, stop_price, target_price, structural_r,
        target_2, trigger_5m_close, entry_zone_low, entry_zone_high,
        signal_candle_open_time,
        recent_high_window, recent_high_window_candles,
        recent_high_window_start, recent_high_window_end,
        market_context, signal_time_quote,
        capture_route, blocked_stage, blocked_reason,
        market_regime, setup_id,
        missing_fields, source_quality_flags, no_lookahead_flags,
        source_observation_ids
    )
    SELECT %s, %s, %s,
           %s, %s, %s,
           %s, NOW(), %s,
           %s, %s, %s, %s,
           %s, %s, %s, %s,
           %s,
           %s::jsonb, %s, %s, %s,
           %s::jsonb, %s::jsonb,
           %s, %s, %s,
           %s, %s,
           %s::jsonb, %s::jsonb, %s::jsonb,
           %s::jsonb
    WHERE EXISTS (
        SELECT 1 FROM research.me_short_a_source_intake_activation_v1
        WHERE experiment_id = %s
          AND status = 'RUNNING'
          AND intake_freeze_ts IS NOT NULL
    )
    ON CONFLICT (event_key) DO NOTHING
"""


def _datetime(value: Any) -> datetime:
    """Parse a timestamp into a UTC datetime.

    Accepts datetime objects, ISO strings and epoch numbers. Numeric values
    are epoch MILLISECONDS when their magnitude says so (``>= 1e11`` — every
    real millisecond timestamp since 1973, and no plausible epoch-seconds
    value) and epoch seconds otherwise. The scanner's candle fields
    (``signal_candle_open_time``, window ``open_time``/``close_time``) are
    epoch milliseconds: reading them as ISO strings used to fail the whole
    capture instead of converting them.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    elif isinstance(value, (int, float)):
        seconds = value / 1000.0 if abs(value) >= 10**11 else float(value)
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _jsonable(value: Any) -> Any:
    """Decimal -> str (exact), datetime -> UTC isoformat, nested kept.

    Mirrors the frozen collector's encoding so one event's payloads are
    comparable across the intake and the Issue #208 capture tables.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _datetime(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _json_object(value: Any) -> dict:
    """Normalize a context blob to a JSON object (never imputes content)."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return _jsonable(dict(value))
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {"text": value}
        return parsed if isinstance(parsed, Mapping) else {"text": value}
    return {"text": str(value)}


def compute_event_key(
    symbol: str,
    signal_time: Any,
    reference_price: Any,
    stop_price: Any,
    target_price: Any,
) -> str:
    """Deterministic event identity, byte-identical to Phase 1 / Issue #208.

    ``sha256(symbol|SHORT|<utc isoformat signal_time>|<ref>|<stop>|<target>)``

    Identity prices are mapped onto the FROZEN representation first. Phase 1
    (``me_short_a_fill_feasibility_v1.capture``) hashes the scanner's
    in-memory ``float`` prices via ``Decimal(str(<float>))`` — the exact
    contract of ``canonical_price`` in the frozen runtime — so every other
    spelling of the same number (``Decimal("342.10")``, ``"342.10"``,
    ``Decimal("342.1")``, ``342.1``) MUST hash as ``342.1``. Without this
    mapping the writer's key would not match the key the runtime recomputes
    from the stored ``numeric`` column after a PostgreSQL round-trip, and one
    event would join nothing in the ``source intake -> Phase 1 -> lineage ->
    outcome`` chain.
    """
    return _lineage_event_key(
        symbol, DIRECTION_SHORT, signal_time,
        _identity_price(reference_price),
        _identity_price(stop_price),
        _identity_price(target_price))


def activation_snapshot(conn: Any) -> tuple[str, datetime | None] | None:
    """Read ``(status, intake_freeze_ts)`` of the intake activation row."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT status,intake_freeze_ts "
            "FROM research.me_short_a_source_intake_activation_v1 "
            "WHERE experiment_id=%s",
            (EXPERIMENT,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    if row is None:
        return None
    status = row[0]
    freeze = row[1]
    if freeze is not None:
        freeze = _datetime(freeze)
    return status, freeze


def capture_with_status(
    conn: Any,
    *,
    scanner_name: str,
    direction: str,
    symbol: str,
    signal_time: Any,
    reference_price: Any,
    invalidation_price: Any,
    target_1: Any,
    target_2: Any = None,
    trigger_5m_close: Any = None,
    entry_zone_low: Any = None,
    entry_zone_high: Any = None,
    signal_candle_open_time: Any = None,
    recent_high_window: Sequence[Mapping[str, Any]] | None = None,
    market_context: Any = None,
    signal_time_quote: Any = None,
    market_regime: str | None = None,
    setup_id: Any = None,
    capture_route: str,
    blocked_stage: str | None = None,
    blocked_reason: str | None = None,
    missing_fields: Sequence[str] | None = None,
    source_quality_flags: Sequence[str] | None = None,
    no_lookahead_flags: Sequence[str] | None = None,
    source_observation_ids: Sequence[int] | None = None,
) -> str:
    """Append ONE original ME SHORT event; return an honest outcome status.

    Statuses (for separate source-capture error diagnostics):

    * ``CAPTURED``      — a new evidence row was appended;
    * ``DUPLICATE``     — idempotent replay: the row already exists;
    * ``NOT_ELIGIBLE``  — wrong scanner/direction/route or no connection;
    * ``DISABLED``      — the runtime feature flag is off;
    * ``NOT_ACTIVE``    — intake not installed or not ``RUNNING``;
    * ``PRE_FREEZE``    — refused by the strict forward-only boundary;
    * ``INVALID_EVENT`` — unparseable/invalid frozen-A geometry;
    * ``ERROR``         — a persistence error occurred (fail-open).

    Any failure fails OPEN towards the scanner: nothing here may ever affect
    scanner execution.
    """
    if os.environ.get(ACTIVATION_FLAG) != "1":
        return "DISABLED"
    if conn is None:
        return "NOT_ELIGIBLE"
    if scanner_name != SCANNER_NAME or direction != DIRECTION_SHORT:
        return "NOT_ELIGIBLE"
    if capture_route not in CAPTURE_ROUTES:
        LOG.error("ME SHORT A source intake: unknown capture_route=%r", capture_route)
        return "NOT_ELIGIBLE"

    try:
        event_time = _datetime(signal_time)
        # Identity columns in the FROZEN representation — exactly what Phase 1
        # stores as its identity inputs — so the row is self-consistent with
        # its event_key and one number never produces two rows. Payload
        # columns (window, zones, context) keep the captured values verbatim.
        ref = _identity_price(reference_price)
        stop = _identity_price(invalidation_price)
        target = _identity_price(target_1)
        if ref is None or stop is None or target is None:
            LOG.error(
                "ME SHORT A source intake: unparseable geometry for %s %s",
                symbol, signal_time)
            return "INVALID_EVENT"
        if ref <= 0 or stop <= ref or target <= 0 or target >= ref:
            LOG.error(
                "ME SHORT A source intake: invalid frozen A geometry for %s %s",
                symbol, signal_time)
            return "INVALID_EVENT"

        key = compute_event_key(symbol, event_time, ref, stop, target)

        try:
            state = activation_snapshot(conn)
        except Exception:
            # A failed read aborts the whole transaction in PostgreSQL. The
            # writer connection is a CACHED singleton reused by the scanner,
            # so the rollback here is what lets the NEXT capture succeed
            # without a scanner restart (e.g. the capture attempted before
            # migration 080 is applied).
            try:
                conn.rollback()
            except Exception:
                LOG.debug("ME SHORT A source intake rollback failed", exc_info=True)
            LOG.exception("ME SHORT A source intake activation read failed")
            return "ERROR"
        if state is None:
            LOG.debug("ME SHORT A source intake not installed; capture skipped")
            return "NOT_ACTIVE"
        status, intake_freeze = state
        if status != "RUNNING" or intake_freeze is None:
            LOG.debug(
                "ME SHORT A source intake not active (status=%s); capture skipped",
                status)
            return "NOT_ACTIVE"
        if event_time <= intake_freeze:
            # Forward-only: never a backfill, never the incident event.
            LOG.warning(
                "ME SHORT A source intake refused pre-freeze event %s %s",
                symbol, event_time)
            return "PRE_FREEZE"

        window: list[dict[str, Any]] = []
        window_rejected = False
        for candle in (recent_high_window or []):
            close_time = candle.get("close_time")
            if close_time is not None and _datetime(close_time) > event_time:
                window_rejected = True
                continue
            window.append(_jsonable(dict(candle)))
        window_start = min(
            (_datetime(c["open_time"]) for c in window
             if c.get("open_time") is not None), default=None)
        window_end = max(
            (_datetime(c["close_time"]) for c in window
             if c.get("close_time") is not None), default=None)

        missing = [str(name) for name in (missing_fields or [])]
        flags = [str(name) for name in (source_quality_flags or [])]
        if window_rejected and "recent_high_window_lookahead_rejected" not in flags:
            flags.append("recent_high_window_lookahead_rejected")
        no_lookahead = [str(name) for name in (no_lookahead_flags or [])]
        if window and "recent_high_window_closed_before_signal" not in no_lookahead:
            no_lookahead.append("recent_high_window_closed_before_signal")

        candle_open = None
        if signal_candle_open_time not in (None, 0, "0"):
            try:
                candle_open = _datetime(signal_candle_open_time)
            except Exception:
                candle_open = None

        structural_r = stop - ref
        cursor = conn.cursor()
        try:
            cursor.execute(
                INSERT_SQL,
                (
                    key, PROTOCOL_VERSION, PROTOCOL_HASH,
                    scanner_name, direction, symbol,
                    event_time, intake_freeze,
                    ref, stop, target, structural_r,
                    _decimal(target_2), _decimal(trigger_5m_close),
                    _decimal(entry_zone_low), _decimal(entry_zone_high),
                    candle_open,
                    json.dumps(window),
                    len(window) if window else None,
                    window_start, window_end,
                    json.dumps(_json_object(market_context)),
                    json.dumps(_json_object(signal_time_quote)),
                    capture_route,
                    blocked_stage, blocked_reason,
                    market_regime,
                    str(setup_id) if setup_id is not None else None,
                    json.dumps(missing), json.dumps(flags),
                    json.dumps(no_lookahead),
                    json.dumps([int(i) for i in source_observation_ids])
                    if source_observation_ids else None,
                    # WHERE EXISTS guard parameters:
                    EXPERIMENT,
                ),
            )
            conn.commit()
            # True only for a real insert: idempotent replays and activation
            # races (WHERE EXISTS guard) never claim a capture.
            return "CAPTURED" if cursor.rowcount == 1 else "DUPLICATE"
        finally:
            cursor.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            LOG.debug("ME SHORT A source intake rollback failed", exc_info=True)
        LOG.exception(
            "ME SHORT A source intake capture failed open; scanner unaffected")
        return "ERROR"


def capture(
    conn: Any,
    *,
    scanner_name: str,
    direction: str,
    symbol: str,
    signal_time: Any,
    reference_price: Any,
    invalidation_price: Any,
    target_1: Any,
    target_2: Any = None,
    trigger_5m_close: Any = None,
    entry_zone_low: Any = None,
    entry_zone_high: Any = None,
    signal_candle_open_time: Any = None,
    recent_high_window: Sequence[Mapping[str, Any]] | None = None,
    market_context: Any = None,
    signal_time_quote: Any = None,
    market_regime: str | None = None,
    setup_id: Any = None,
    capture_route: str,
    blocked_stage: str | None = None,
    blocked_reason: str | None = None,
    missing_fields: Sequence[str] | None = None,
    source_quality_flags: Sequence[str] | None = None,
    no_lookahead_flags: Sequence[str] | None = None,
    source_observation_ids: Sequence[int] | None = None,
) -> bool:
    """Append ONE original ME SHORT event to the research source intake.

    Returns ``True`` only for a real insert: idempotent replays, activation
    races and pre-freeze observations never claim a capture. Any failure
    fails OPEN and returns ``False`` — the caller must never let this affect
    scanner execution. Callers that need the honest outcome status (for
    separate source-capture error diagnostics) use
    :func:`capture_with_status`.
    """
    return capture_with_status(
        conn,
        scanner_name=scanner_name,
        direction=direction,
        symbol=symbol,
        signal_time=signal_time,
        reference_price=reference_price,
        invalidation_price=invalidation_price,
        target_1=target_1,
        target_2=target_2,
        trigger_5m_close=trigger_5m_close,
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        signal_candle_open_time=signal_candle_open_time,
        recent_high_window=recent_high_window,
        market_context=market_context,
        signal_time_quote=signal_time_quote,
        market_regime=market_regime,
        setup_id=setup_id,
        capture_route=capture_route,
        blocked_stage=blocked_stage,
        blocked_reason=blocked_reason,
        missing_fields=missing_fields,
        source_quality_flags=source_quality_flags,
        no_lookahead_flags=no_lookahead_flags,
        source_observation_ids=source_observation_ids,
    ) == "CAPTURED"
