"""Signal-level prospective observer for ME_LONG_V2_INVERSE_SHORT_V2.

A READ-ONLY observer of SIGNAL-level prospective outcomes — deliberately
independent of the trading engine:

* it never opens, closes or mutates a position, never calls
  ``open_entries`` / ``_open_event`` / ``save_position`` and never writes
  any row: **no trades are ever created by this observer, in particular
  while ``max_open_positions`` is full** — capacity effects are OBSERVED
  (``CAPACITY_REJECTED`` counts, eligible backlog), never acted on;
* it consumes only facts that already exist in the contour's own ledgers
  (the event table + the position table) plus the injected current clock —
  **no look-ahead**: nothing here reads future prices, future candles or
  anything not yet persisted, and every metric is reproducible from a
  database snapshot at the observation time;
* it works in any contour state (FULL, EXIT_ONLY, not activated) because it
  needs no activation identity of its own — it only reports.

Outputs (``observe()``):

* disposition counts over ALL captured events (``CAPTURED`` /
  ``ENTERED`` / ``EXPIRED`` / ``CAPACITY_REJECTED``);
* ``eligible_pending`` — CAPTURED events still inside the frozen entry TTL
  versus ``stale_unswept`` CAPTURED events already at/past it (the sweep
  has not run yet, e.g. while the contour is dormant);
* entry latency (``entered_at − signal_time``) for events that did enter —
  the signal-level counterpart of position-level PnL;
* ``open_positions`` and ``finalized_outcomes`` (CLOSED positions, count +
  net PnL) from the position ledger;
* capacity-loss accounting: ``capacity_rejected`` count/rate — how many
  signals died specifically because the book was full.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_CAPACITY_REJECTED,
    EVENT_STATUS_ENTERED,
    EVENT_STATUS_EXPIRED,
    EVENT_STATUSES,
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from .protocol import (
    ENTRY_MAX_AGE_MINUTES,
    EXPERIMENT_ID,
    STRATEGY_ID,
    protocol_fingerprint,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SignalLevelProspectiveObserver:
    """Read-only, trade-less observer of signal-level prospective outcomes."""

    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock or _utc_now

    # ------------------------------------------------------------------
    def observe(self) -> dict[str, Any]:
        """Compute the signal-level metrics from the durable ledgers only."""
        now = self._clock()
        ttl = timedelta(minutes=ENTRY_MAX_AGE_MINUTES)

        events = self.store.list_events()
        positions = self.store.list_positions()

        by_status: dict[str, int] = {status: 0 for status in EVENT_STATUSES}
        eligible_pending = 0
        stale_unswept = 0
        for event in events:
            by_status[event.status] = by_status.get(event.status, 0) + 1
            if event.status == EVENT_STATUS_CAPTURED:
                if now - event.signal_time < ttl:
                    eligible_pending += 1
                else:
                    stale_unswept += 1

        # Entry latency: ENTERED events joined to their position row.
        positions_by_event = {p.event_key: p for p in positions}
        latencies_min: list[float] = []
        for event in events:
            if event.status != EVENT_STATUS_ENTERED:
                continue
            position = positions_by_event.get(event.event_key)
            if position is None or position.entered_at is None:
                continue
            delta = position.entered_at - event.signal_time
            latencies_min.append(delta.total_seconds() / 60.0)

        open_positions = [
            p for p in positions if p.status == POSITION_STATUS_OPEN
        ]
        finalized = [
            p for p in positions if p.status == POSITION_STATUS_CLOSED
        ]

        capacity_rejected = by_status.get(EVENT_STATUS_CAPACITY_REJECTED, 0)
        disposed = capacity_rejected + by_status.get(EVENT_STATUS_EXPIRED, 0)
        total = len(events)

        return {
            "experiment_id": EXPERIMENT_ID,
            "strategy_id": STRATEGY_ID,
            "protocol_fingerprint": protocol_fingerprint(),
            "observed_at": now.isoformat(),
            "events_total": total,
            "events_by_status": by_status,
            # Eligible backlog (age < TTL) vs already-stale CAPTURED rows.
            "eligible_pending": eligible_pending,
            "stale_unswept": stale_unswept,
            # Terminal dispositions (never re-enterable).
            "expired": by_status.get(EVENT_STATUS_EXPIRED, 0),
            "capacity_rejected": capacity_rejected,
            "capacity_rejected_rate": (
                round(capacity_rejected / disposed, 6) if disposed else 0.0
            ),
            # Signal-level entry timeliness (look-ahead-free: stored facts).
            "entered": by_status.get(EVENT_STATUS_ENTERED, 0),
            "entry_latency_minutes": _latency_stats(latencies_min),
            # Position-level outcomes (reported, never influenced).
            "open_positions": len(open_positions),
            "finalized_outcomes": len(finalized),
            "finalized_net_pnl": round(
                sum(float(p.net_pnl) for p in finalized), 8,
            ),
            "read_only": True,
        }

    def dispositions(self) -> list[dict[str, Any]]:
        """Per-event disposition rows (audit view), oldest signal first."""
        now = self._clock()
        ttl = timedelta(minutes=ENTRY_MAX_AGE_MINUTES)
        rows: list[dict[str, Any]] = []
        for event in self.store.list_events():
            age = now - event.signal_time
            rows.append({
                "event_key": event.event_key,
                "symbol": event.symbol,
                "signal_time": event.signal_time.isoformat(),
                "age_minutes": round(age.total_seconds() / 60.0, 3),
                "status": event.status,
                "status_reason": event.status_reason,
                "status_at": (
                    event.status_at.isoformat() if event.status_at else None
                ),
                "eligible_now": (
                    event.status == EVENT_STATUS_CAPTURED and age < ttl
                ),
            })
        return rows


def _latency_stats(samples: list[float]) -> dict[str, Any] | None:
    """min/avg/max entry latency in minutes (None when nothing entered)."""
    if not samples:
        return None
    return {
        "min": round(min(samples), 6),
        "avg": round(sum(samples) / len(samples), 6),
        "max": round(max(samples), 6),
        "count": len(samples),
    }
