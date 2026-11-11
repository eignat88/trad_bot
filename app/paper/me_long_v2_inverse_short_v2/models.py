"""Data model for the ME_LONG_V2_INVERSE_SHORT_V2 prospective paper contour.

Two entities only:

``InverseShortEvent``
    One captured ORIGINAL ``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` detector
    event with its complete lineage back to the source signal.  Events are
    append-only and idempotent on ``event_key``.  V2 carries a FOUR-state
    disposition: ``CAPTURED`` (eligible, age < entry TTL) → ``ENTERED``, or
    terminal ``EXPIRED`` / ``CAPACITY_REJECTED`` (age >= entry TTL, with the
    refusal ``status_reason`` and the ``status_at`` decision timestamp).

``InverseShortPosition``
    One SHORT paper position opened from an event.  ``entered_at`` and
    ``deadline_at`` are written once at entry and are IMMUTABLE afterwards
    (the store never includes them in an UPDATE; migration 087 additionally
    enforces it with a trigger).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .protocol import (
    EXPERIMENT_ID,
    PROTOCOL_VERSION,
    STRATEGY_ID,
    TRADE_DIRECTION,
    protocol_fingerprint,
)

# V2 four-state disposition (CHECK constraint, migration 087):
#   CAPTURED         — captured, not yet entered, age < entry TTL (eligible)
#   ENTERED          — a position row exists for this event_key
#   EXPIRED          — TERMINAL: age >= entry TTL (generic cause)
#   CAPACITY_REJECTED— TERMINAL: age >= entry TTL while max_open_positions
#                      was the binding refusal (the signal died to capacity)
# Terminal states are written exactly once (UPDATE ... WHERE status=
# 'CAPTURED') and can never become ENTERED afterwards.
EVENT_STATUS_CAPTURED = "CAPTURED"
EVENT_STATUS_ENTERED = "ENTERED"
EVENT_STATUS_EXPIRED = "EXPIRED"
EVENT_STATUS_CAPACITY_REJECTED = "CAPACITY_REJECTED"
EVENT_STATUSES = (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_ENTERED,
    EVENT_STATUS_EXPIRED,
    EVENT_STATUS_CAPACITY_REJECTED,
)
EVENT_TERMINAL_STATUSES = (EVENT_STATUS_ENTERED, EVENT_STATUS_EXPIRED,
                           EVENT_STATUS_CAPACITY_REJECTED)

POSITION_STATUS_OPEN = "OPEN"
POSITION_STATUS_CLOSED = "CLOSED"
POSITION_STATUSES = (POSITION_STATUS_OPEN, POSITION_STATUS_CLOSED)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class InverseShortEvent:
    """One captured source event (append-only, idempotent on event_key)."""

    event_key: str
    symbol: str
    signal_time: datetime
    captured_at: datetime
    activation_freeze_ts: datetime
    signal_candle_open_time: int
    source_scanner: str
    source_scanner_version: str
    source_direction: str
    source_setup_id: str
    strategy_id: str = STRATEGY_ID
    protocol_version: str = PROTOCOL_VERSION
    protocol_hash: str = field(default_factory=protocol_fingerprint)
    direction: str = TRADE_DIRECTION
    entry_timeframe: str = "5m"
    source_reference_price: float | None = None
    source_invalidation_price: float | None = None
    source_target_price: float | None = None
    source_target_2: float | None = None
    source_score: float | None = None
    market_regime: str | None = None
    capture_route: str = ""
    status: str = EVENT_STATUS_CAPTURED
    position_key: str | None = None
    # V2 disposition audit: WHY and WHEN the status became terminal
    # (e.g. reason='ENTRY_TTL' | 'ENTRY_CAP'; None while CAPTURED).
    status_reason: str | None = None
    status_at: datetime | None = None
    lineage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "strategy_id": self.strategy_id,
            "protocol_version": self.protocol_version,
            "protocol_hash": self.protocol_hash,
            "source_scanner": self.source_scanner,
            "source_scanner_version": self.source_scanner_version,
            "source_direction": self.source_direction,
            "source_setup_id": self.source_setup_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_timeframe": self.entry_timeframe,
            "signal_time": _iso(self.signal_time),
            "captured_at": _iso(self.captured_at),
            "activation_freeze_ts": _iso(self.activation_freeze_ts),
            "signal_candle_open_time": int(self.signal_candle_open_time),
            "source_reference_price": self.source_reference_price,
            "source_invalidation_price": self.source_invalidation_price,
            "source_target_price": self.source_target_price,
            "source_target_2": self.source_target_2,
            "source_score": self.source_score,
            "market_regime": self.market_regime,
            "capture_route": self.capture_route,
            "status": self.status,
            "position_key": self.position_key,
            "status_reason": self.status_reason,
            "status_at": _iso(self.status_at),
            "lineage": dict(self.lineage),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "InverseShortEvent":
        return cls(
            event_key=row["event_key"],
            symbol=row["symbol"],
            signal_time=_dt(row["signal_time"]),
            captured_at=_dt(row["captured_at"]),
            activation_freeze_ts=_dt(row["activation_freeze_ts"]),
            signal_candle_open_time=int(row["signal_candle_open_time"]),
            source_scanner=row["source_scanner"],
            source_scanner_version=row.get("source_scanner_version") or "",
            source_direction=row["source_direction"],
            source_setup_id=row.get("source_setup_id") or "",
            strategy_id=row.get("strategy_id") or STRATEGY_ID,
            protocol_version=row.get("protocol_version") or PROTOCOL_VERSION,
            protocol_hash=row.get("protocol_hash") or protocol_fingerprint(),
            direction=row.get("direction") or TRADE_DIRECTION,
            entry_timeframe=row.get("entry_timeframe") or "5m",
            source_reference_price=_opt_float(row.get("source_reference_price")),
            source_invalidation_price=_opt_float(row.get("source_invalidation_price")),
            source_target_price=_opt_float(row.get("source_target_price")),
            source_target_2=_opt_float(row.get("source_target_2")),
            source_score=_opt_float(row.get("source_score")),
            market_regime=row.get("market_regime"),
            capture_route=row.get("capture_route") or "",
            status=row.get("status") or EVENT_STATUS_CAPTURED,
            position_key=row.get("position_key"),
            status_reason=row.get("status_reason"),
            status_at=_dt(row.get("status_at")),
            lineage=row.get("lineage") or {},
        )


@dataclass
class InverseShortPosition:
    """One isolated SHORT paper position of this contour."""

    position_key: str
    event_key: str
    symbol: str
    direction: str
    entered_at: datetime
    deadline_at: datetime
    entry_market_price: float
    entry_price: float
    entry_fee: float
    entry_slippage_cost: float
    stop_price: float
    target_price: float
    position_size: float
    risk_usdt: float
    balance_before: float
    strategy_id: str = STRATEGY_ID
    source_scanner: str = ""
    source_setup_id: str = ""
    market_regime: str | None = None
    source_score: float | None = None
    status: str = POSITION_STATUS_OPEN
    funding_paid: float = 0.0
    funding_periods_charged: int = 0
    highest_since_entry: float = 0.0
    lowest_since_entry: float = float("inf")
    mfe: float = 0.0
    mae: float = 0.0
    exit_price: float | None = None
    exit_reason: str | None = None
    exit_fee: float = 0.0
    slippage_cost: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    closed_at: datetime | None = None
    duration_sec: float | None = None

    # -- entry costs (never recomputed, never updated) -------------------
    @property
    def open_entry_cost(self) -> float:
        """Costs already booked against the contour balance at entry."""
        return self.entry_fee + self.entry_slippage_cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_key": self.position_key,
            "event_key": self.event_key,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "source_scanner": self.source_scanner,
            "source_setup_id": self.source_setup_id,
            "market_regime": self.market_regime,
            "source_score": self.source_score,
            "entry_market_price": self.entry_market_price,
            "entry_price": self.entry_price,
            "entry_fee": self.entry_fee,
            "entry_slippage": self.entry_slippage_cost,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "position_size": self.position_size,
            "risk_usdt": self.risk_usdt,
            "balance_before": self.balance_before,
            "entered_at": _iso(self.entered_at),
            "deadline_at": _iso(self.deadline_at),
            "status": self.status,
            "funding_paid": self.funding_paid,
            "funding_periods": int(self.funding_periods_charged),
            "highest_since_entry": _finite(self.highest_since_entry),
            "lowest_since_entry": _finite(self.lowest_since_entry),
            "mfe": self.mfe,
            "mae": self.mae,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "exit_fee": self.exit_fee,
            "slippage": self.slippage_cost,
            "gross_pnl": self.gross_pnl,
            "net_pnl": self.net_pnl,
            "closed_at": _iso(self.closed_at),
            "duration_sec": self.duration_sec,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "InverseShortPosition":
        entered_at = _dt(row["entered_at"])
        deadline_at = _dt(row["deadline_at"])
        return cls(
            position_key=row["position_key"],
            event_key=row["event_key"],
            symbol=row["symbol"],
            direction=row["direction"],
            entered_at=entered_at,
            deadline_at=deadline_at,
            entry_market_price=float(row["entry_market_price"]),
            entry_price=float(row["entry_price"]),
            entry_fee=float(row["entry_fee"]),
            entry_slippage_cost=float(row.get("entry_slippage") or 0.0),
            stop_price=float(row["stop_price"]),
            target_price=float(row["target_price"]),
            position_size=float(row["position_size"]),
            risk_usdt=float(row["risk_usdt"]),
            balance_before=float(row["balance_before"]),
            strategy_id=row.get("strategy_id") or STRATEGY_ID,
            source_scanner=row.get("source_scanner") or "",
            source_setup_id=row.get("source_setup_id") or "",
            market_regime=row.get("market_regime"),
            source_score=_opt_float(row.get("source_score")),
            status=row.get("status") or POSITION_STATUS_OPEN,
            funding_paid=float(row.get("funding_paid") or 0.0),
            funding_periods_charged=int(row.get("funding_periods") or 0),
            highest_since_entry=float(row.get("highest_since_entry") or 0.0),
            lowest_since_entry=_opt_float(row.get("lowest_since_entry"))
            if row.get("lowest_since_entry") is not None else float("inf"),
            mfe=float(row.get("mfe") or 0.0),
            mae=float(row.get("mae") or 0.0),
            exit_price=_opt_float(row.get("exit_price")),
            exit_reason=row.get("exit_reason"),
            exit_fee=float(row.get("exit_fee") or 0.0),
            slippage_cost=float(row.get("slippage") or 0.0),
            gross_pnl=float(row.get("gross_pnl") or 0.0),
            net_pnl=float(row.get("net_pnl") or 0.0),
            closed_at=_dt(row.get("closed_at")),
            duration_sec=_opt_float(row.get("duration_sec")),
        )

    def closed_copy(self) -> "InverseShortPosition":
        """Detached CLOSED snapshot (the open dict entry is removed)."""
        return replace(self, status=POSITION_STATUS_CLOSED)


@dataclass(frozen=True)
class ActivationRecord:
    """Activation row of the contour (status + frozen fingerprint + freeze)."""

    experiment_id: str
    status: str
    protocol_fingerprint: str
    freeze_ts: datetime | None

    @property
    def is_running(self) -> bool:
        return self.status == "RUNNING"

    @property
    def is_draining(self) -> bool:
        """RUNNING -> DRAINING: valid identity, EXIT_ONLY permissions."""
        return self.status == "DRAINING"

    @property
    def has_freeze(self) -> bool:
        return self.freeze_ts is not None


def current_experiment_id() -> str:
    return EXPERIMENT_ID


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _finite(value: float) -> float | None:
    if value is None:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)
