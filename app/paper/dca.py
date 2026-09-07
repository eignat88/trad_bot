"""DCA (Dollar Cost Averaging) Breakeven — Position Management Policy.

Implements a DCA state machine that operates on top of all active scanner
signals at the paper/execution level.  Scanners remain responsible only for
finding entry points; this module manages DCA lifecycle independently.

State Machine
-------------
    INITIAL_PENDING
        ↓ (initial fill confirmed)
    INITIAL_FILLED
        ↓ (price hits DCA level)
    DCA_PENDING
        ↓ (DCA fill confirmed)
    DCA_FILLED
        ↓ (price returns to avg_entry)
    CLOSED_BREAKEVEN
    -- or --
    CLOSED_STOP  (at any point before DCA fill or after)
    CLOSED_NO_DCA  (position closed by scanner TP/trailing before DCA)
    CANCELLED  (setup expired before initial fill)

Event Priority Within a Single Candle
--------------------------------------
When OHLC data suggests multiple levels may have been touched in the same
candle, the engine applies the following **conservative** ordering:

    1. Stop Loss (if SL is touched → close immediately, no DCA fill)
    2. DCA Fill (only if SL was NOT touched in this candle)
    3. Breakeven TP (only after DCA has been filled)
    4. Scanner TP (only before DCA fill)
    5. Trailing Stop
    6. Expiry

Rationale: If a candle's range crosses both SL and DCA, we assume the SL
was hit first (worst-case for DCA).  This prevents optimistic fill
assumptions from creating artificial edge.

Risk Normalization
------------------
The total position size is calculated BEFORE any DCA split:

    target_qty = risk_usdt / |entry - stop|

Then split:

    initial_qty = target_qty * initial_entry_pct  (default 50%)
    dca_qty     = target_qty * dca_entry_pct      (default 50%)

Invariant: initial_qty + dca_qty <= target_qty (enforced with exchange
precision rounding).  DCA does NOT increase total planned risk.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.config.settings import DCASettings

logger = logging.getLogger(__name__)


class DCAState(str, Enum):
    """Position DCA lifecycle states."""
    INITIAL_PENDING = "INITIAL_PENDING"
    INITIAL_FILLED = "INITIAL_FILLED"
    DCA_PENDING = "DCA_PENDING"
    DCA_FILLED = "DCA_FILLED"
    CLOSED_BREAKEVEN = "CLOSED_BREAKEVEN"
    CLOSED_STOP = "CLOSED_STOP"
    CLOSED_NO_DCA = "CLOSED_NO_DCA"
    CANCELLED = "CANCELLED"


# Terminal states — no further DCA transitions possible
TERMINAL_STATES: frozenset[DCAState] = frozenset({
    DCAState.CLOSED_BREAKEVEN,
    DCAState.CLOSED_STOP,
    DCAState.CLOSED_NO_DCA,
    DCAState.CANCELLED,
})


@dataclass
class DCAPositionState:
    """Persisted DCA state for a single paper position.

    All fields are serialisable to JSONB for PostgreSQL storage.
    """
    position_id: int | None = None
    dca_enabled: bool = False
    state: DCAState = DCAState.INITIAL_PENDING

    # ATR fixed at entry
    atr_at_entry: float = 0.0
    dca_level_atr: float = 0.75

    # DCA price level (pre-calculated at entry)
    dca_price: float = 0.0

    # Position split
    initial_entry_pct: float = 0.50
    dca_target_pct: float = 0.50
    initial_fill_price: float = 0.0
    initial_fill_qty: float = 0.0
    dca_fill_price: float = 0.0
    dca_fill_qty: float = 0.0
    dca_filled_at: datetime | None = None

    # Weighted average after DCA
    avg_entry_price: float = 0.0

    # TP management
    original_tp: float = 0.0   # scanner's original TP
    active_tp: float = 0.0     # currently active TP (scanner or breakeven)
    tp_mode: str = "scanner"   # "scanner" or "breakeven"

    # Stop (remains at initial entry level, never moves for DCA)
    stop_price: float = 0.0

    # Execution cost tracking per leg
    initial_fee: float = 0.0
    initial_slippage: float = 0.0
    dca_fee: float = 0.0
    dca_slippage: float = 0.0

    # DCA fill tracking
    dca_fill_count: int = 0
    dca_order_active: bool = False  # virtual limit order exists

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to dict for JSONB storage."""
        return {
            "dca_enabled": self.dca_enabled,
            "state": self.state.value,
            "atr_at_entry": self.atr_at_entry,
            "dca_level_atr": self.dca_level_atr,
            "dca_price": self.dca_price,
            "initial_entry_pct": self.initial_entry_pct,
            "dca_target_pct": self.dca_target_pct,
            "initial_fill_price": self.initial_fill_price,
            "initial_fill_qty": self.initial_fill_qty,
            "dca_fill_price": self.dca_fill_price,
            "dca_fill_qty": self.dca_fill_qty,
            "dca_filled_at": self.dca_filled_at.isoformat() if self.dca_filled_at else None,
            "avg_entry_price": self.avg_entry_price,
            "original_tp": self.original_tp,
            "active_tp": self.active_tp,
            "tp_mode": self.tp_mode,
            "stop_price": self.stop_price,
            "initial_fee": self.initial_fee,
            "initial_slippage": self.initial_slippage,
            "dca_fee": self.dca_fee,
            "dca_slippage": self.dca_slippage,
            "dca_fill_count": self.dca_fill_count,
            "dca_order_active": self.dca_order_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DCAPositionState:
        """Deserialise from dict (JSONB from PostgreSQL)."""
        if not data:
            return cls()
        dca_filled_at = data.get("dca_filled_at")
        if isinstance(dca_filled_at, str):
            dca_filled_at = datetime.fromisoformat(dca_filled_at)
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        return cls(
            dca_enabled=data.get("dca_enabled", False),
            state=DCAState(data.get("state", "INITIAL_PENDING")),
            atr_at_entry=float(data.get("atr_at_entry", 0.0)),
            dca_level_atr=float(data.get("dca_level_atr", 0.75)),
            dca_price=float(data.get("dca_price", 0.0)),
            initial_entry_pct=float(data.get("initial_entry_pct", 0.50)),
            dca_target_pct=float(data.get("dca_target_pct", 0.50)),
            initial_fill_price=float(data.get("initial_fill_price", 0.0)),
            initial_fill_qty=float(data.get("initial_fill_qty", 0.0)),
            dca_fill_price=float(data.get("dca_fill_price", 0.0)),
            dca_fill_qty=float(data.get("dca_fill_qty", 0.0)),
            dca_filled_at=dca_filled_at,
            avg_entry_price=float(data.get("avg_entry_price", 0.0)),
            original_tp=float(data.get("original_tp", 0.0)),
            active_tp=float(data.get("active_tp", 0.0)),
            tp_mode=data.get("tp_mode", "scanner"),
            stop_price=float(data.get("stop_price", 0.0)),
            initial_fee=float(data.get("initial_fee", 0.0)),
            initial_slippage=float(data.get("initial_slippage", 0.0)),
            dca_fee=float(data.get("dca_fee", 0.0)),
            dca_slippage=float(data.get("dca_slippage", 0.0)),
            dca_fill_count=int(data.get("dca_fill_count", 0)),
            dca_order_active=data.get("dca_order_active", False),
            created_at=created_at or datetime.now(timezone.utc),
            updated_at=updated_at or datetime.now(timezone.utc),
        )


class DCAPolicy:
    """Determines whether and how DCA applies to a position."""

    @staticmethod
    def should_apply(dca_settings: DCASettings) -> bool:
        """Check if DCA is globally enabled."""
        return dca_settings.enabled

    @staticmethod
    def calculate_dca_price(
        initial_entry_price: float,
        atr: float,
        dca_level_atr: float,
        direction: str,
    ) -> float:
        """Calculate the DCA price level.

        LONG: dca_price = initial_entry - dca_level_atr * ATR
        SHORT: dca_price = initial_entry + dca_level_atr * ATR
        """
        if direction == "LONG":
            return initial_entry_price - dca_level_atr * atr
        else:
            return initial_entry_price + dca_level_atr * atr

    @staticmethod
    def calculate_stop_price(
        initial_entry_price: float,
        atr: float,
        stop_loss_atr: float,
        direction: str,
    ) -> float:
        """Calculate SL from initial entry (never changes after DCA).

        LONG: SL = initial_entry - stop_loss_atr * ATR
        SHORT: SL = initial_entry + stop_loss_atr * ATR
        """
        if direction == "LONG":
            return initial_entry_price - stop_loss_atr * atr
        else:
            return initial_entry_price + stop_loss_atr * atr

    @staticmethod
    def calculate_position_split(
        target_qty: float,
        initial_entry_pct: float,
        dca_entry_pct: float,
        price_precision: int = 8,
    ) -> tuple[float, float]:
        """Split target position into initial and DCA quantities.

        Returns (initial_qty, dca_qty) ensuring:
        - initial_qty + dca_qty <= target_qty
        - Both respect exchange precision (rounded down)
        """
        initial_qty = DCAPolicy._round_down(target_qty * initial_entry_pct, price_precision)
        dca_qty = DCAPolicy._round_down(target_qty * dca_entry_pct, price_precision)
        # Safety: ensure sum does not exceed target
        if initial_qty + dca_qty > target_qty:
            dca_qty = DCAPolicy._round_down(target_qty - initial_qty, price_precision)
        return initial_qty, dca_qty

    @staticmethod
    def _round_down(value: float, precision: int) -> float:
        """Round down to exchange precision (truncate, never round up)."""
        if precision <= 0:
            return float(int(value))
        factor = 10 ** precision
        return math.floor(value * factor) / factor

    @staticmethod
    def validate_invariants(
        initial_entry: float,
        dca_price: float,
        stop_price: float,
        direction: str,
        atr: float,
    ) -> bool:
        """Validate geometric invariants for DCA positioning.

        LONG:  SL < DCA < initial_entry
        SHORT: initial_entry < DCA < SL
        ATR must be positive.
        """
        if atr <= 0:
            logger.warning("DCA invariant violation: ATR=%f is not positive", atr)
            return False
        if direction == "LONG":
            if not (stop_price < dca_price < initial_entry):
                logger.warning(
                    "DCA invariant violation (LONG): SL=%.4f < DCA=%.4f < Entry=%.4f",
                    stop_price, dca_price, initial_entry,
                )
                return False
        else:  # SHORT
            if not (initial_entry < dca_price < stop_price):
                logger.warning(
                    "DCA invariant violation (SHORT): Entry=%.4f < DCA=%.4f < SL=%.4f",
                    initial_entry, dca_price, stop_price,
                )
                return False
        return True

    @staticmethod
    def calculate_avg_entry(
        initial_fill_price: float,
        initial_fill_qty: float,
        dca_fill_price: float,
        dca_fill_qty: float,
    ) -> float:
        """Calculate weighted average entry from actual fill prices and quantities.

        Uses real simulated fill prices, NOT theoretical order prices.
        """
        total_qty = initial_fill_qty + dca_fill_qty
        if total_qty <= 0:
            return 0.0
        return (
            initial_fill_price * initial_fill_qty
            + dca_fill_price * dca_fill_qty
        ) / total_qty

    @staticmethod
    def should_replace_tp_with_breakeven(dca_state: DCAPositionState) -> bool:
        """After DCA fill, the original TP is replaced with breakeven exit."""
        return dca_state.state == DCAState.DCA_FILLED


class DCATransitionError(Exception):
    """Raised when an invalid DCA state transition is attempted."""
    pass


class DCAStateManager:
    """Manages DCA state transitions for a position.

    All transitions are idempotent — reprocessing the same event does not
    change state again.
    """

    # Valid transitions: (from_state, event) → to_state
    _TRANSITIONS: dict[tuple[DCAState, str], DCAState] = {
        (DCAState.INITIAL_PENDING, "initial_filled"): DCAState.INITIAL_FILLED,
        (DCAState.INITIAL_FILLED, "dca_filled"): DCAState.DCA_FILLED,
        (DCAState.INITIAL_FILLED, "position_closed"): DCAState.CLOSED_NO_DCA,
        (DCAState.INITIAL_FILLED, "cancelled"): DCAState.CANCELLED,
        (DCAState.DCA_FILLED, "breakeven_exit"): DCAState.CLOSED_BREAKEVEN,
        (DCAState.DCA_FILLED, "stop_exit"): DCAState.CLOSED_STOP,
        (DCAState.INITIAL_FILLED, "stop_exit"): DCAState.CLOSED_STOP,
    }

    @classmethod
    def transition(cls, state: DCAPositionState, event: str) -> DCAState:
        """Apply a state transition. Returns the new state.

        Raises DCATransitionError if the transition is invalid.
        Idempotent: if already in terminal state, returns current state.
        """
        if state.state in TERMINAL_STATES:
            return state.state

        key = (state.state, event)
        new_state = cls._TRANSITIONS.get(key)
        if new_state is None:
            raise DCATransitionError(
                f"Invalid DCA transition: {state.state.value} + {event}"
            )
        return new_state

    @classmethod
    def create_initial_state(
        cls,
        position_id: int,
        initial_fill_price: float,
        initial_fill_qty: float,
        atr_at_entry: float,
        dca_settings: DCASettings,
        direction: str,
        original_tp: float,
        entry_price_for_dca: float,
        price_precision: int = 8,
    ) -> DCAPositionState:
        """Create DCA state when initial entry is filled.

        Called after the initial entry is confirmed. Calculates DCA level,
        validates invariants, and creates the virtual DCA order.
        """
        dca_price = DCAPolicy.calculate_dca_price(
            entry_price_for_dca, atr_at_entry, dca_settings.level_atr, direction,
        )
        stop_price = DCAPolicy.calculate_stop_price(
            entry_price_for_dca, atr_at_entry, dca_settings.stop_loss_atr, direction,
        )

        # Validate invariants
        if not DCAPolicy.validate_invariants(
            entry_price_for_dca, dca_price, stop_price, direction, atr_at_entry,
        ):
            logger.error(
                "DCA invariants violated for position_id=%d — DCA disabled for this trade",
                position_id,
            )
            return DCAPositionState(
                position_id=position_id,
                dca_enabled=False,
                state=DCAState.CANCELLED,
                atr_at_entry=atr_at_entry,
                dca_level_atr=dca_settings.level_atr,
                dca_price=dca_price,
                stop_price=stop_price,
                initial_fill_price=initial_fill_price,
                initial_fill_qty=initial_fill_qty,
                original_tp=original_tp,
                active_tp=original_tp,
            )

        state = DCAPositionState(
            position_id=position_id,
            dca_enabled=True,
            state=DCAState.INITIAL_FILLED,
            atr_at_entry=atr_at_entry,
            dca_level_atr=dca_settings.level_atr,
            dca_price=dca_price,
            initial_entry_pct=dca_settings.initial_entry_pct,
            dca_target_pct=dca_settings.dca_entry_pct,
            initial_fill_price=initial_fill_price,
            initial_fill_qty=initial_fill_qty,
            avg_entry_price=initial_fill_price,  # only one fill so far
            original_tp=original_tp,
            active_tp=original_tp,
            tp_mode="scanner",
            stop_price=stop_price,
            dca_order_active=True,
            dca_fill_count=0,
        )

        logger.info(
            "DCA_INIT position_id=%d scanner=N/A direction=%s "
            "initial_qty=%.6f dca_qty=%.6f atr=%.4f dca_price=%.4f "
            "sl=%.4f original_tp=%.4f",
            position_id, direction,
            initial_fill_qty, dca_settings.dca_entry_pct * (initial_fill_qty / dca_settings.initial_entry_pct),
            atr_at_entry, dca_price, stop_price, original_tp,
        )
        return state

    @classmethod
    def fill_dca(
        cls,
        state: DCAPositionState,
        dca_fill_price: float,
        dca_fill_qty: float,
        now: datetime | None = None,
    ) -> DCAPositionState:
        """Process a DCA fill event.

        Recalculates weighted average entry, replaces TP with breakeven,
        and deactivates the DCA order.
        """
        if state.state != DCAState.INITIAL_FILLED:
            logger.warning(
                "DCA fill attempted in wrong state=%s position_id=%d — ignoring",
                state.state.value, state.position_id,
            )
            return state

        if state.dca_fill_count >= 1:
            logger.warning(
                "DCA already filled once for position_id=%d — duplicate fill suppressed",
                state.position_id,
            )
            return state

        now = now or datetime.now(timezone.utc)

        # Calculate weighted average entry using ACTUAL fill prices
        avg_entry = DCAPolicy.calculate_avg_entry(
            state.initial_fill_price, state.initial_fill_qty,
            dca_fill_price, dca_fill_qty,
        )

        # Update state
        new_state = DCAPositionState(
            position_id=state.position_id,
            dca_enabled=state.dca_enabled,
            state=DCAState.DCA_FILLED,
            atr_at_entry=state.atr_at_entry,
            dca_level_atr=state.dca_level_atr,
            dca_price=state.dca_price,
            initial_entry_pct=state.initial_entry_pct,
            dca_target_pct=state.dca_target_pct,
            initial_fill_price=state.initial_fill_price,
            initial_fill_qty=state.initial_fill_qty,
            dca_fill_price=dca_fill_price,
            dca_fill_qty=dca_fill_qty,
            dca_filled_at=now,
            avg_entry_price=avg_entry,
            original_tp=state.original_tp,
            active_tp=avg_entry,  # breakeven TP = weighted average
            tp_mode="breakeven",
            stop_price=state.stop_price,  # SL stays at initial entry — never moves
            initial_fee=state.initial_fee,
            initial_slippage=state.initial_slippage,
            dca_fill_count=1,
            dca_order_active=False,
            created_at=state.created_at,
            updated_at=now,
        )

        total_qty = state.initial_fill_qty + dca_fill_qty
        logger.info(
            "DCA_FILLED position_id=%d initial_price=%.4f dca_price=%.4f "
            "avg_entry=%.4f new_tp=%.4f total_qty=%.6f",
            state.position_id, state.initial_fill_price, dca_fill_price,
            avg_entry, avg_entry, total_qty,
        )
        return new_state

    @classmethod
    def check_dca_level(
        cls,
        state: DCAPositionState,
        current_price: float,
        direction: str,
    ) -> bool:
        """Check if current price has reached the DCA fill level.

        LONG: price <= dca_price (price moved against position)
        SHORT: price >= dca_price (price moved against position)
        """
        if state.state != DCAState.INITIAL_FILLED:
            return False
        if not state.dca_order_active:
            return False
        if direction == "LONG":
            return current_price <= state.dca_price
        else:
            return current_price >= state.dca_price

    @classmethod
    def get_active_tp(
        cls,
        state: DCAPositionState,
    ) -> float | None:
        """Return the currently active take-profit level.

        After DCA fill, returns breakeven (avg_entry_price).
        Before DCA fill, returns the original scanner TP.
        """
        if state.state in TERMINAL_STATES:
            return None
        if state.tp_mode == "breakeven":
            return state.avg_entry_price
        return state.original_tp if state.original_tp > 0 else None

    @classmethod
    def check_breakeven_tp(
        cls,
        state: DCAPositionState,
        current_price: float,
        direction: str,
    ) -> bool:
        """Check if price has returned to breakeven (avg entry) after DCA fill.

        LONG: price >= avg_entry (sell to close at breakeven)
        SHORT: price <= avg_entry (buy to close at breakeven)
        """
        if state.state != DCAState.DCA_FILLED:
            return False
        if state.tp_mode != "breakeven":
            return False
        avg = state.avg_entry_price
        if avg <= 0:
            return False
        if direction == "LONG":
            return current_price >= avg
        else:
            return current_price <= avg

    @classmethod
    def close_position(
        cls,
        state: DCAPositionState,
        reason: str,
        now: datetime | None = None,
    ) -> DCAPositionState:
        """Close the DCA state when a position is closed.

        reason: 'DCA_BREAKEVEN', 'DCA_STOP', 'STOP_LOSS', 'TAKE_PROFIT_1',
                'TRAILING_STOP', 'EXPIRED', etc.
        """
        if state.state in TERMINAL_STATES:
            return state  # already terminal — idempotent

        now = now or datetime.now(timezone.utc)

        if reason in ("DCA_BREAKEVEN",):
            new_state_value = DCAState.CLOSED_BREAKEVEN
        elif reason in ("DCA_STOP", "STOP_LOSS", "STOP_LOSS_GAP"):
            new_state_value = DCAState.CLOSED_STOP
        else:
            # Scanner TP/trailing/expiry — closed without DCA fill
            new_state_value = DCAState.CLOSED_NO_DCA

        new_state = DCAPositionState(
            position_id=state.position_id,
            dca_enabled=state.dca_enabled,
            state=new_state_value,
            atr_at_entry=state.atr_at_entry,
            dca_level_atr=state.dca_level_atr,
            dca_price=state.dca_price,
            initial_entry_pct=state.initial_entry_pct,
            dca_target_pct=state.dca_target_pct,
            initial_fill_price=state.initial_fill_price,
            initial_fill_qty=state.initial_fill_qty,
            dca_fill_price=state.dca_fill_price,
            dca_fill_qty=state.dca_fill_qty,
            dca_filled_at=state.dca_filled_at,
            avg_entry_price=state.avg_entry_price,
            original_tp=state.original_tp,
            active_tp=state.active_tp,
            tp_mode=state.tp_mode,
            stop_price=state.stop_price,
            initial_fee=state.initial_fee,
            initial_slippage=state.initial_slippage,
            dca_fee=state.dca_fee,
            dca_slippage=state.dca_slippage,
            dca_fill_count=state.dca_fill_count,
            dca_order_active=False,
            created_at=state.created_at,
            updated_at=now,
        )

        logger.info(
            "DCA_CLOSE position_id=%d reason=%s state=%s dca_filled=%s avg_entry=%.4f",
            state.position_id, reason, new_state_value.value,
            state.dca_fill_count > 0, state.avg_entry_price,
        )
        return new_state
