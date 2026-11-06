"""Isolated SHORT paper engine for ME_LONG_V2_INVERSE_SHORT_V1.

Design constraints (all enforced, all testable):

* **Isolation** — the contour keeps its own ``open_positions`` map keyed by
  the NAMESPACED ``position_key`` (``<strategy>:<event_key>``).  It never
  touches the production ``PaperTradingEngine.open_trades[symbol]`` map, the
  production balance, or any production risk gate.  Several contour positions
  on the SAME symbol are allowed (different source events); the production
  engine still allows only one trade per symbol — and vice versa.
* **Frozen geometry** — TP and SL are exactly 10% of the ACTUAL entry price
  (entry fill includes configured slippage); timeout is exactly
  ``entered_at + 7200 minutes`` computed ONCE at entry.
* **Immutability** — ``entered_at`` and ``deadline_at`` are written at entry
  and never recomputed: restart recovery restores them verbatim from the
  store (the store refuses to update them).
* **Realistic execution model** — taker fees on both sides, configured
  adverse slippage on both fills, funding settled per whole funding interval
  with the SHORT sign convention.
* **Fail-closed entries, fail-safe exits** — entries require a valid
  activation identity; exits (STOP / TP / TIMEOUT) always run so an open
  position can never be stranded.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from app.config import Settings

from .activation import ActivationIdentity, is_post_freeze
from .identity import position_key as build_position_key
from .models import (
    EVENT_STATUS_CAPTURED,
    InverseShortEvent,
    InverseShortPosition,
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from .protocol import (
    EXIT_REASONS,
    TRADE_DIRECTION,
    deadline_for,
    short_geometry,
)
from .store import InverseShortStore

logger = logging.getLogger(__name__)

# Allowed exit reasons of this contour (CHECK constraint, migration 085).
CONTOUR_EXIT_REASONS = frozenset(EXIT_REASONS)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InverseShortPaperEngine:
    """Independent prospective SHORT paper engine (paper-only)."""

    def __init__(
        self,
        settings: Settings,
        store: InverseShortStore,
        *,
        clock: Callable[[], datetime] | None = None,
        activation: ActivationIdentity | None = None,
        require_activation: bool = True,
    ) -> None:
        self.settings = settings
        self.store = store
        self._clock = clock or _utc_now
        self.activation = activation
        self.require_activation = require_activation

        # ISOLATION: never shared with PaperTradingEngine.
        self.open_positions: dict[str, InverseShortPosition] = {}

        components = store.balance_components()
        self.balance = float(components.get("balance", settings.initial_balance))
        self.initial_balance = float(
            components.get("initial_balance", settings.initial_balance)
        )
        self._load_open_positions()

    # ------------------------------------------------------------------
    # Restart recovery
    # ------------------------------------------------------------------
    def _load_open_positions(self) -> None:
        """Restore OPEN positions; ``entered_at``/``deadline_at`` verbatim."""
        restored = self.store.load_open_positions()
        for position in restored:
            if position.direction != TRADE_DIRECTION:
                logger.error(
                    "me inverse short recovery refused non-SHORT position: key=%s direction=%s",
                    position.position_key, position.direction,
                )
                continue
            if position.position_key in self.open_positions:
                logger.error(
                    "me inverse short recovery duplicate position_key=%s (first wins)",
                    position.position_key,
                )
                continue
            if position.deadline_at <= position.entered_at:
                logger.error(
                    "me inverse short recovery refused invalid deadline: key=%s "
                    "entered_at=%s deadline_at=%s",
                    position.position_key,
                    position.entered_at.isoformat(),
                    position.deadline_at.isoformat(),
                )
                continue
            self.open_positions[position.position_key] = position
        if restored:
            logger.info(
                "me inverse short engine recovered %d open position(s); balance=$%.2f",
                len(self.open_positions), self.balance,
            )

    # ------------------------------------------------------------------
    # Entries (prospective-only, activation-gated)
    # ------------------------------------------------------------------
    @property
    def activated(self) -> bool:
        return (self.activation is not None) or (not self.require_activation)

    def open_entries(self, prices: dict[str, float]) -> list[InverseShortPosition]:
        """Open SHORT positions for CAPTURED events at the first observed price."""
        opened: list[InverseShortPosition] = []
        if self.require_activation and self.activation is None:
            logger.warning(
                "me inverse short entries refused: contour is not activated "
                "(protocol fingerprint / freeze_ts missing)"
            )
            return opened

        max_positions = int(self.settings.max_open_positions)
        if len(self.open_positions) >= max_positions:
            return opened

        events = self.store.list_events(status=EVENT_STATUS_CAPTURED)
        for event in events:
            if len(self.open_positions) >= max_positions:
                logger.info(
                    "me inverse short entry cap reached (%d)", max_positions,
                )
                break
            position = self._open_event(event, prices)
            if position is not None:
                opened.append(position)
        return opened

    def _open_event(
        self,
        event: InverseShortEvent,
        prices: dict[str, float],
    ) -> InverseShortPosition | None:
        if event.direction != TRADE_DIRECTION:
            logger.error(
                "me inverse short entry refused non-SHORT event: key=%s direction=%s",
                event.event_key[:16], event.direction,
            )
            return None

        position_key = build_position_key(event.event_key)
        if position_key in self.open_positions:
            return None  # duplicate delivery of the same source event
        if self.store.has_position(position_key):
            # Cross-restart / duplicate-delivery guard: already traded.
            return None

        if self.require_activation and self.activation is not None:
            if not is_post_freeze(event.signal_time, self.activation.freeze_ts):
                logger.warning(
                    "me inverse short entry refused pre-freeze event: key=%s signal_time=%s",
                    event.event_key[:16], event.signal_time.isoformat(),
                )
                return None

        market_price = prices.get(event.symbol)
        if market_price is None or market_price <= 0:
            # Missing market data: skip entry, never guess a fill.
            return None

        position = self._build_position(event, position_key, float(market_price))
        if position is None:
            return None

        if not self.store.save_position(position):
            logger.info(
                "me inverse short duplicate position suppressed: key=%s symbol=%s",
                position_key[:48], position.symbol,
            )
            return None

        try:
            self.store.mark_event_entered(event.event_key, position_key)
        except Exception:
            # Fail-open: has_position() keeps delivery idempotent next cycle.
            logger.warning(
                "me inverse short event mark-entered failed (idempotent guard active)",
                exc_info=True,
            )

        self.balance -= position.entry_fee + position.entry_slippage_cost
        self.open_positions[position_key] = position
        logger.info(
            "me inverse short entry: symbol=%s side=SHORT entry=%.6f stop=%.6f "
            "target=%.6f size=%.6f deadline=%s event_key=%s",
            position.symbol, position.entry_price, position.stop_price,
            position.target_price, position.position_size,
            position.deadline_at.isoformat(), event.event_key[:16],
        )
        return position

    def _build_position(
        self,
        event: InverseShortEvent,
        position_key: str,
        market_price: float,
    ) -> InverseShortPosition | None:
        slip = float(self.settings.slippage_percent)
        entry_market = market_price
        entry_price = entry_market * (1.0 - slip)  # SHORT sell: adverse = lower
        stop_price, target_price = short_geometry(entry_price)
        distance = stop_price - entry_price  # = STOP_LOSS_PCT * entry_price

        balance = self.balance
        risk_budget = balance * float(self.settings.risk_per_trade)
        qty = risk_budget / distance
        max_qty_by_exposure = (
            balance * float(self.settings.max_symbol_exposure) / entry_price
        )
        qty = min(qty, max_qty_by_exposure)
        if qty <= 0 or distance <= 0:
            logger.warning(
                "me inverse short entry sizing failed: symbol=%s balance=%.2f",
                event.symbol, balance,
            )
            return None

        entry_fee = entry_price * qty * float(self.settings.taker_fee)
        entry_slippage_cost = (entry_market - entry_price) * qty
        if balance - entry_fee - entry_slippage_cost <= 0:
            logger.warning(
                "me inverse short entry rejected: costs exceed balance: symbol=%s",
                event.symbol,
            )
            return None

        entered_at = self._clock()
        deadline_at = deadline_for(entered_at)  # immutable, computed ONCE

        return InverseShortPosition(
            position_key=position_key,
            event_key=event.event_key,
            symbol=event.symbol,
            direction=TRADE_DIRECTION,
            entered_at=entered_at,
            deadline_at=deadline_at,
            entry_market_price=entry_market,
            entry_price=entry_price,
            entry_fee=entry_fee,
            entry_slippage_cost=entry_slippage_cost,
            stop_price=stop_price,
            target_price=target_price,
            position_size=qty,
            risk_usdt=distance * qty,
            balance_before=balance,
            source_scanner=event.source_scanner,
            source_setup_id=event.source_setup_id,
            market_regime=event.market_regime,
            source_score=event.source_score,
        )

    # ------------------------------------------------------------------
    # Exits: STOP → TP → TIMEOUT (STOP_FIRST on a shared bar)
    # ------------------------------------------------------------------
    def check_exits(
        self,
        prices: dict[str, float],
        funding_rates_percent: dict[str, float] | None = None,
        bars: dict[str, dict[str, float]] | None = None,
    ) -> list[InverseShortPosition]:
        """Evaluate every open position; return positions closed this cycle."""
        closed: list[InverseShortPosition] = []

        for position_key, position in list(self.open_positions.items()):
            price = prices.get(position.symbol)
            if price is None or price <= 0:
                # Missing market data: hold, never guess an exit fill.
                continue

            funding_rate = (funding_rates_percent or {}).get(position.symbol, 0.0)
            self._apply_funding(position, funding_rate)

            price = float(price)
            position.highest_since_entry = max(position.highest_since_entry, price)
            position.lowest_since_entry = min(position.lowest_since_entry, price)
            # MFE/MAE measured on the market mid (the gross-PnL reference).
            position.mfe = max(
                position.mfe, position.entry_market_price - position.lowest_since_entry,
            )
            position.mae = max(
                position.mae, position.highest_since_entry - position.entry_market_price,
            )

            bar = (bars or {}).get(position.symbol) or {}
            high = float(bar.get("high", price))
            low = float(bar.get("low", price))

            stop_touched = high >= position.stop_price
            tp_touched = low <= position.target_price

            if stop_touched:
                # STOP_FIRST: a protective stop always wins over TP/TIMEOUT.
                gap = price > position.stop_price
                reason = "STOP_LOSS_GAP" if gap else "STOP_LOSS"
                result = self._close(position, position.stop_price, reason)
            elif tp_touched:
                result = self._close(position, position.target_price, "TAKE_PROFIT_1")
            elif self._clock() >= position.deadline_at:
                result = self._close(position, price, "TIMEOUT")
            else:
                continue

            self.open_positions.pop(position_key, None)
            closed.append(result)

        return closed

    def _apply_funding(
        self, position: InverseShortPosition, funding_rate_percent: float,
    ) -> None:
        """Settle whole elapsed funding intervals (SHORT sign convention)."""
        interval_seconds = float(self.settings.paper_funding_interval_hours) * 3600
        if interval_seconds <= 0:
            return
        elapsed = (self._clock() - position.entered_at).total_seconds()
        due_periods = max(0, int(elapsed // interval_seconds))
        new_periods = due_periods - position.funding_periods_charged
        if new_periods <= 0:
            return

        notional = position.entry_price * position.position_size
        signed_rate = float(funding_rate_percent) / 100.0
        cost = notional * signed_rate * new_periods
        if position.direction == "SHORT":
            cost = -cost
        position.funding_paid += cost
        position.funding_periods_charged = due_periods
        self.balance -= cost
        try:
            self.store.update_position(position)
        except Exception:
            logger.warning(
                "me inverse short funding persistence failed (state kept in memory)",
                exc_info=True,
            )
        logger.info(
            "me inverse short funding: %s periods=%d rate=%.6f%% cost=$%.4f",
            position.symbol, new_periods, funding_rate_percent, cost,
        )

    def _close(
        self,
        position: InverseShortPosition,
        exit_reference_price: float,
        reason: str,
    ) -> InverseShortPosition:
        """Close a position; PnL model: gross at reference prices, all costs explicit."""
        if reason not in CONTOUR_EXIT_REASONS:
            raise ValueError(f"INVALID_EXIT_REASON:{reason}")

        closed_at = self._clock()
        slip = float(self.settings.slippage_percent)
        # SHORT cover: adverse slippage moves the fill UP.
        fill = exit_reference_price * (1.0 + slip)

        gross = (
            position.entry_market_price - exit_reference_price
        ) * position.position_size
        exit_fee = fill * position.position_size * float(self.settings.taker_fee)
        exit_slippage_cost = (
            fill - exit_reference_price
        ) * position.position_size
        slippage_cost = position.entry_slippage_cost + exit_slippage_cost
        net_pnl = (
            gross
            - position.entry_fee
            - exit_fee
            - position.funding_paid
            - slippage_cost
        )

        self.balance += gross - exit_fee - exit_slippage_cost

        position.status = POSITION_STATUS_CLOSED
        position.exit_price = fill
        position.exit_reason = reason
        position.exit_fee = exit_fee
        position.slippage_cost = slippage_cost
        position.gross_pnl = gross
        position.net_pnl = net_pnl
        position.closed_at = closed_at
        position.duration_sec = (closed_at - position.entered_at).total_seconds()

        try:
            self.store.update_position(position)
        except Exception:
            # Fail-safe: memory state is already terminal; persistence retried
            # by the next update (duplicate UPDATE is idempotent).
            logger.exception(
                "me inverse short close persistence failed: key=%s reason=%s",
                position.position_key[:48], reason,
            )

        logger.info(
            "me inverse short exit: symbol=%s reason=%s ref=%.6f fill=%.6f "
            "gross=%.4f fees=%.4f slip=%.4f funding=%.4f net=%.4f",
            position.symbol, reason, exit_reference_price, fill,
            gross, position.entry_fee + exit_fee, slippage_cost,
            position.funding_paid, net_pnl,
        )
        return position

    # ------------------------------------------------------------------
    # Reporting (isolated accounting — nothing shared with Paper)
    # ------------------------------------------------------------------
    def mark_to_market(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        unrealized = 0.0
        for position in self.open_positions.values():
            if not prices:
                continue
            price = prices.get(position.symbol)
            if price is None:
                continue
            move = position.entry_market_price - float(price)  # SHORT
            unrealized += move * position.position_size
        return {
            "balance": self.balance,
            "unrealized_pnl": unrealized,
            "equity": self.balance + unrealized,
        }

    def snapshot(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        account = self.mark_to_market(prices)
        summary = self.store.net_pnl_summary()
        gross_exposure = sum(
            p.entry_price * p.position_size
            for p in self.open_positions.values()
        )
        equity = account["equity"]
        total_pnl = equity - self.initial_balance
        return {
            "strategy_id": "ME_LONG_V2_INVERSE_SHORT_V1",
            "direction": TRADE_DIRECTION,
            "balance": round(account["balance"], 2),
            "unrealized_pnl": round(account["unrealized_pnl"], 2),
            "equity": round(equity, 2),
            "starting_balance": round(self.initial_balance, 2),
            "net_pnl": round(total_pnl, 2),
            "net_pnl_pct": round(
                (total_pnl / self.initial_balance * 100.0) if self.initial_balance else 0.0, 4,
            ),
            "closed_trades": int(summary["closed_trades"]),
            "closed_net_pnl": round(summary["net_pnl"], 4),
            "open_positions": len(self.open_positions),
            "gross_exposure_pct": round(
                (gross_exposure / equity * 100.0) if equity else 0.0, 4,
            ),
            "activated": self.activated,
            "positions": [
                {
                    "symbol": p.symbol,
                    "direction": p.direction,
                    "entry": p.entry_price,
                    "stop": p.stop_price,
                    "target": p.target_price,
                    "entered_at": p.entered_at.isoformat(),
                    "deadline_at": p.deadline_at.isoformat(),
                    "event_key": p.event_key[:16],
                }
                for p in sorted(
                    self.open_positions.values(), key=lambda p: p.entered_at,
                )
            ],
        }

    def pending_events(self) -> list[InverseShortEvent]:
        return self.store.list_events(status=EVENT_STATUS_CAPTURED)

    def closed_positions(self) -> list[InverseShortPosition]:
        return self.store.list_positions(status=POSITION_STATUS_CLOSED)
