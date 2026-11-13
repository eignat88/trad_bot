"""Data model for the ME_LONG_V2_INVERSE_SHORT_OOS2_V1 prospective paper contour.

Two entities only:

``InverseShortEvent``
    One captured ORIGINAL ``MOMENTUM_EXHAUSTION_REVERSE_LONG_V2`` detector
    event with its complete lineage back to the source signal.  Events are
    append-only and idempotent on ``event_key``.  OOS2 carries a FOUR-state
    disposition: ``CAPTURED`` (eligible, age < entry TTL) → ``ENTERED``, or
    terminal ``EXPIRED`` / ``CAPACITY_REJECTED`` (age >= entry TTL, with the
    refusal ``status_reason`` and the ``status_at`` decision timestamp).

``InverseShortPosition``
    One SHORT paper position opened from an event.  ``entered_at`` and
    ``deadline_at`` are written once at entry and are IMMUTABLE afterwards
    (the store never includes them in an UPDATE; migration 088 additionally
    enforces it with a trigger).  OOS-2 adds the partial-ladder state:
    ``position_size`` is the IMMUTABLE ORIGINAL quantity, ``remaining_size``
    the live quantity, ``tp1_executed`` / ``tp2_executed`` / ``tp3_executed``
    the once-only stage flags, and ``realized_legs_net`` the cumulative
    ``(gross − exit fee − exit slippage)`` of COMMITTED partial legs.
    A partial exit changes NONE of the entry facts, keeps the row OPEN and
    keeps its capacity slot (Issue #276).

``InverseShortCloseLeg``
    One executed close leg of a position: a PARTIAL take-profit leg (TP1 /
    TP2 — position stays OPEN) or the TERMINAL leg (TP3 / STOP_LOSS /
    STOP_LOSS_GAP / TIMEOUT — position flips CLOSED).  Legs are identified
    by ``(position_key, leg_seq)`` — TP1=1, TP2=2, terminal=3 — the
    exactly-once identity of the durable leg ledger (migration 088): a
    replayed or redelivered commit can never book a leg twice.
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

# OOS2 four-state disposition (CHECK constraint, migration 088):
#   CAPTURED         — captured, not yet entered, age < entry TTL (eligible)
#   ENTERED          — a position row exists for this event_key
#   EXPIRED          — TERMINAL: age >= entry TTL (generic cause)
#   CAPACITY_REJECTED— TERMINAL: age >= entry TTL while the OOS-2-LOCAL
#                      capacity (protocol.MAX_OPEN_POSITIONS = 12) was the
#                      binding refusal (the signal died to capacity)
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

# Durable funding obligation states (round 6, Codex P2-funding): an
# obligation is written when the rate is KNOWN but the close-leg history
# was unreadable at the terminal close, so the observed rate is never lost
# with the process.  SETTLED means the gap was booked exactly once (or
# superseded by a later settlement that already covered it).
FUNDING_OBLIGATION_OPEN = "OPEN"
FUNDING_OBLIGATION_SETTLED = "SETTLED"
FUNDING_OBLIGATION_STATUSES = (
    FUNDING_OBLIGATION_OPEN,
    FUNDING_OBLIGATION_SETTLED,
)

# Durable close-leg identity (migration 088: UNIQUE (position_key, leg_seq)).
# A leg can only ever be booked once — restart, replay and ambiguous COMMIT
# all resolve against this key.
LEG_SEQ_TAKE_PROFIT_1 = 1
LEG_SEQ_TAKE_PROFIT_2 = 2
LEG_SEQ_TERMINAL = 3
LEG_SEQUENCES = (LEG_SEQ_TAKE_PROFIT_1, LEG_SEQ_TAKE_PROFIT_2,
                 LEG_SEQ_TERMINAL)


def leg_seq_for(exit_reason: str) -> int:
    """Deterministic leg sequence for a partial or terminal exit reason."""
    if exit_reason == "TAKE_PROFIT_1":
        return LEG_SEQ_TAKE_PROFIT_1
    if exit_reason == "TAKE_PROFIT_2":
        return LEG_SEQ_TAKE_PROFIT_2
    if exit_reason in ("TAKE_PROFIT_3", "STOP_LOSS", "STOP_LOSS_GAP",
                       "TIMEOUT"):
        return LEG_SEQ_TERMINAL
    raise ValueError(f"UNKNOWN_EXIT_REASON:{exit_reason}")


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
    # OOS2 disposition audit: WHY and WHEN the status became terminal
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
    target_price: float          # TP1 level: entry * 0.95 (Issue #276)
    tp2_price: float             # TP2 level: entry * 0.93
    tp3_price: float             # TP3 level: entry * 0.90
    position_size: float         # IMMUTABLE original quantity
    remaining_size: float        # live quantity (== position_size at entry)
    risk_usdt: float
    balance_before: float
    strategy_id: str = STRATEGY_ID
    source_scanner: str = ""
    source_setup_id: str = ""
    market_regime: str | None = None
    source_score: float | None = None
    status: str = POSITION_STATUS_OPEN
    # -- partial TP ladder state (Issue #276) -----------------------------
    # Each stage flag flips exactly once, atomically with its durable leg
    # row; a partial exit NEVER flips ``status`` and NEVER frees a slot.
    tp1_executed: bool = False
    tp2_executed: bool = False
    tp3_executed: bool = False
    # Cumulative (gross − exit fee − exit slippage) of COMMITTED partial
    # legs — the open-position realized component of the balance ledger.
    realized_legs_net: float = 0.0
    # Cumulative entry-cost (fee + slippage) share already allocated to
    # committed legs; the terminal leg always takes the exact remainder, so
    # Σ leg entry_cost_share == entry_fee + entry_slippage by construction.
    entry_cost_allocated: float = 0.0
    funding_paid: float = 0.0
    funding_periods_charged: int = 0
    highest_since_entry: float = 0.0
    lowest_since_entry: float = float("inf")
    mfe: float = 0.0
    mae: float = 0.0
    # Last observed mark price for this position (Issue #276): persisted
    # once per cycle so the dashboard can compute UNREALIZED PnL in SQL —
    # (entry_price − last_mark_price) × remaining_size for this SHORT.
    last_mark_price: float | None = None
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

    @property
    def executed_stages(self) -> tuple[str, ...]:
        """TP stages that have already fired (ladder progress, Issue #276)."""
        stages: list[str] = []
        if self.tp1_executed:
            stages.append("TAKE_PROFIT_1")
        if self.tp2_executed:
            stages.append("TAKE_PROFIT_2")
        if self.tp3_executed:
            stages.append("TAKE_PROFIT_3")
        return tuple(stages)

    @property
    def closed_quantity(self) -> float:
        """Quantity already closed (original − remaining)."""
        return self.position_size - self.remaining_size

    @property
    def realized_net_to_date(self) -> float:
        """Realized net of the position AT ANY MOMENT (Issue #276).

        Committed partial legs minus all entry costs and funding paid so
        far — for a CLOSED position this equals ``net_pnl`` exactly (the
        terminal leg is included in the cumulative accounting).
        """
        return (
            self.realized_legs_net
            - self.entry_fee
            - self.entry_slippage_cost
            - self.funding_paid
        )

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
            "tp2_price": self.tp2_price,
            "tp3_price": self.tp3_price,
            "position_size": self.position_size,
            "remaining_size": self.remaining_size,
            "risk_usdt": self.risk_usdt,
            "balance_before": self.balance_before,
            "entered_at": _iso(self.entered_at),
            "deadline_at": _iso(self.deadline_at),
            "status": self.status,
            "tp1_executed": bool(self.tp1_executed),
            "tp2_executed": bool(self.tp2_executed),
            "tp3_executed": bool(self.tp3_executed),
            "realized_legs_net": self.realized_legs_net,
            "entry_cost_allocated": self.entry_cost_allocated,
            "funding_paid": self.funding_paid,
            "funding_periods": int(self.funding_periods_charged),
            "highest_since_entry": _finite(self.highest_since_entry),
            "lowest_since_entry": _finite(self.lowest_since_entry),
            "mfe": self.mfe,
            "mae": self.mae,
            "last_mark_price": self.last_mark_price,
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
            tp2_price=_opt_float(row.get("tp2_price"))
            if row.get("tp2_price") is not None
            else float(row["entry_price"]) * 0.93,
            tp3_price=_opt_float(row.get("tp3_price"))
            if row.get("tp3_price") is not None
            else float(row["entry_price"]) * 0.90,
            remaining_size=float(
                row.get("remaining_size")
                if row.get("remaining_size") is not None
                else row["position_size"]
            ),
            risk_usdt=float(row["risk_usdt"]),
            balance_before=float(row["balance_before"]),
            strategy_id=row.get("strategy_id") or STRATEGY_ID,
            source_scanner=row.get("source_scanner") or "",
            source_setup_id=row.get("source_setup_id") or "",
            market_regime=row.get("market_regime"),
            source_score=_opt_float(row.get("source_score")),
            status=row.get("status") or POSITION_STATUS_OPEN,
            tp1_executed=bool(row.get("tp1_executed") or False),
            tp2_executed=bool(row.get("tp2_executed") or False),
            tp3_executed=bool(row.get("tp3_executed") or False),
            realized_legs_net=float(row.get("realized_legs_net") or 0.0),
            entry_cost_allocated=float(row.get("entry_cost_allocated") or 0.0),
            funding_paid=float(row.get("funding_paid") or 0.0),
            funding_periods_charged=int(row.get("funding_periods") or 0),
            highest_since_entry=float(row.get("highest_since_entry") or 0.0),
            lowest_since_entry=_opt_float(row.get("lowest_since_entry"))
            if row.get("lowest_since_entry") is not None else float("inf"),
            mfe=float(row.get("mfe") or 0.0),
            mae=float(row.get("mae") or 0.0),
            last_mark_price=_opt_float(row.get("last_mark_price")),
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


@dataclass
class InverseShortCloseLeg:
    """One executed close leg (partial TP or terminal close), Issue #276.

    Identity: ``(position_key, leg_seq)`` — unique and durable (migration
    088).  ``net_pnl`` is the leg contribution:
    ``gross − exit_fee − exit_slippage − entry_cost_share`` (funding is a
    position-level obligation reported via ``funding_paid_to_date`` and
    settled against the position's terminal accounting — never per leg,
    never twice).
    """

    position_key: str
    leg_seq: int
    exit_reason: str
    is_terminal: bool
    quantity: float
    remaining_after: float
    reference_price: float
    fill_price: float
    gross_pnl: float
    exit_fee: float
    exit_slippage: float
    entry_cost_share: float
    funding_paid_to_date: float
    net_pnl: float
    cumulative_realized_net: float
    executed_at: datetime
    status_after: str
    event_key: str = ""
    symbol: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_key": self.position_key,
            "leg_seq": int(self.leg_seq),
            "event_key": self.event_key,
            "symbol": self.symbol,
            "exit_reason": self.exit_reason,
            "is_terminal": bool(self.is_terminal),
            "quantity": self.quantity,
            "remaining_after": self.remaining_after,
            "reference_price": self.reference_price,
            "fill_price": self.fill_price,
            "gross_pnl": self.gross_pnl,
            "exit_fee": self.exit_fee,
            "exit_slippage": self.exit_slippage,
            "entry_cost_share": self.entry_cost_share,
            "funding_paid_to_date": self.funding_paid_to_date,
            "net_pnl": self.net_pnl,
            "cumulative_realized_net": self.cumulative_realized_net,
            "executed_at": _iso(self.executed_at),
            "status_after": self.status_after,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "InverseShortCloseLeg":
        return cls(
            position_key=row["position_key"],
            leg_seq=int(row["leg_seq"]),
            event_key=row.get("event_key") or "",
            symbol=row.get("symbol") or "",
            exit_reason=row["exit_reason"],
            is_terminal=bool(row.get("is_terminal") or False),
            quantity=float(row["quantity"]),
            remaining_after=float(row["remaining_after"]),
            reference_price=float(row["reference_price"]),
            fill_price=float(row["fill_price"]),
            gross_pnl=float(row.get("gross_pnl") or 0.0),
            exit_fee=float(row.get("exit_fee") or 0.0),
            exit_slippage=float(row.get("exit_slippage") or 0.0),
            entry_cost_share=float(row.get("entry_cost_share") or 0.0),
            funding_paid_to_date=float(row.get("funding_paid_to_date") or 0.0),
            net_pnl=float(row.get("net_pnl") or 0.0),
            cumulative_realized_net=float(
                row.get("cumulative_realized_net") or 0.0
            ),
            executed_at=_dt(row["executed_at"]),
            status_after=row.get("status_after") or POSITION_STATUS_OPEN,
        )


@dataclass
class InverseShortFundingObligation:
    """Durable funding obligation of a terminal close (round 6, P2).

    Written by ``_settle_funding_for_close`` when the OBSERVED funding
    rate is known but the close-leg history was unreadable at the moment
    of the terminal close (``HISTORY_UNAVAILABLE``): the protective close
    still commits, but the known rate is persisted instead of being lost
    with the process.  A later recovery pass settles the gap exactly once
    from the durable leg ledger.

    Identity: ``obligation_key`` is deterministic — position + observation
    window — so a replayed close attempt upserts the SAME obligation and
    can never create a second one.  ``periods_due`` may only ADVANCE
    (a later retry observes more elapsed periods); the FIRST observed rate
    is kept (the frozen substitution policy: the first observed rate after
    recovery applies to the whole backlog, exactly once).
    """

    position_key: str
    obligation_key: str
    symbol: str
    event_key: str
    observed_rate_percent: float
    observed_at: datetime
    entered_at: datetime
    periods_due: int
    periods_charged_at_observation: int
    legs_ref_max_seq: int
    status: str = FUNDING_OBLIGATION_OPEN
    settled_at: datetime | None = None
    settled_cost: float | None = None

    @property
    def outstanding_periods(self) -> int:
        """Periods this obligation still aims to cover at observation."""
        return max(0, int(self.periods_due) - int(self.periods_charged_at_observation))

    @staticmethod
    def make_obligation_key(
        position_key: str,
        entered_at: datetime,
        periods_due: int,
        periods_charged: int,
    ) -> str:
        """Deterministic identity of one observation window."""
        return (
            f"{position_key}|{_iso(entered_at)}|{int(periods_due)}"
            f"|{int(periods_charged)}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_key": self.position_key,
            "obligation_key": self.obligation_key,
            "symbol": self.symbol,
            "event_key": self.event_key,
            "observed_rate_percent": self.observed_rate_percent,
            "observed_at": _iso(self.observed_at),
            "entered_at": _iso(self.entered_at),
            "periods_due": int(self.periods_due),
            "periods_charged_at_observation": int(
                self.periods_charged_at_observation
            ),
            "legs_ref_max_seq": int(self.legs_ref_max_seq),
            "status": self.status,
            "settled_at": _iso(self.settled_at) if self.settled_at else None,
            "settled_cost": self.settled_cost,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "InverseShortFundingObligation":
        settled = row.get("settled_at")
        return cls(
            position_key=row["position_key"],
            obligation_key=row["obligation_key"],
            symbol=row.get("symbol") or "",
            event_key=row.get("event_key") or "",
            observed_rate_percent=float(row["observed_rate_percent"]),
            observed_at=_dt(row["observed_at"]),
            entered_at=_dt(row["entered_at"]),
            periods_due=int(row["periods_due"]),
            periods_charged_at_observation=int(
                row.get("periods_charged_at_observation") or 0
            ),
            legs_ref_max_seq=int(row.get("legs_ref_max_seq") or -1),
            status=row.get("status") or FUNDING_OBLIGATION_OPEN,
            settled_at=_dt(settled) if settled else None,
            settled_cost=(
                float(row["settled_cost"])
                if row.get("settled_cost") is not None
                else None
            ),
        )


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
