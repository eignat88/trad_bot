"""Paper Trading Engine — Phase 5: simulate trades from filtered scanner setups.

Lifecycle:
  1. Polls dds.scanner_setup for READY_TO_TRADE setups.
  2. When price enters the entry zone → opens a paper position (dds.paper_trade).
  3. Monitors open positions: TP1/TP2/trailing stop/expiry.
  4. Closes and records P&L.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.scanners.models import SetupCandidate

logger = logging.getLogger(__name__)

# Maximum bars (in entry_timeframe units) to keep a setup alive before expiry.
_ENTRY_TIMEOUT_MAP = {
    "5m": 12,   # 1 hour
    "15m": 8,   # 2 hours
    "1h": 6,    # 6 hours
    "4h": 4,    # 16 hours
}


@dataclass
class PaperTradeRecord:
    """In-memory representation of an open paper trade."""
    trade_id: int | None
    setup_id: str
    symbol: str
    scanner_name: str
    direction: str
    score: float
    entry_price: float
    entry_fee: float
    stop_price: float
    target_1: float | None
    target_2: float | None
    position_size: float
    risk_usdt: float
    balance_before: float
    market_regime: str | None
    entered_at: datetime
    status: str = "OPEN"
    entry_timeframe: str = "5m"
    # trailing / management
    trail_stop: float | None = None
    highest_since_entry: float = 0.0
    lowest_since_entry: float = math.inf
    partial_tp_hit: bool = False
    funding_paid: float = 0.0
    funding_periods_charged: int = 0


class PaperTradingEngine:
    """Simulates paper trades from scanner setups.

    Does NOT call any API — it only needs the current price feed passed in.
    """

    def __init__(
        self,
        settings: Settings,
        repository: Any,  # ScannerRepository
    ) -> None:
        self.settings = settings
        self.repo = repository
        self.balance: float = settings.initial_balance
        self.open_trades: dict[str, PaperTradeRecord] = {}  # symbol → trade
        self._peak_balance: float = settings.initial_balance
        self._max_drawdown: float = 0.0
        self._loaded_account_snapshot = False
        self._daily_loss_usdt: float = 0.0
        self._consecutive_losses: int = 0

        # Restore account/risk state before rebuilding any open positions.
        self._load_account_state()
        self._load_open_trades()
        self._load_risk_state()

    # ------------------------------------------------------------------
    # ENTRY: scan READY_TO_TRADE setups → open positions
    # ------------------------------------------------------------------
    def check_entries(
        self,
        candidates: list[SetupCandidate],
        prices: dict[str, float],
    ) -> list[PaperTradeRecord]:
        """Attempt to open paper trades for setups whose entry zone is touched.

        Args:
            candidates: filtered scanner setups (already passed expectancy + geometry).
            prices: current mid price for each symbol.
        Returns:
            List of newly opened trades.
        """
        opened: list[PaperTradeRecord] = []
        for c in candidates:
            price = prices.get(c.symbol)
            if price is None:
                continue

            # Already have an open position for this symbol? Skip.
            if c.symbol in self.open_trades:
                continue

            # Risk limits check
            if len(self.open_trades) >= self.settings.max_open_positions:
                logger.info("paper gate: max open positions reached (%d), skipping %s",
                            self.settings.max_open_positions, c.symbol)
                break

            if self._daily_loss_usdt >= self.settings.initial_balance * self.settings.max_daily_loss:
                logger.warning("paper gate: daily loss limit reached, skipping new entries")
                break
            if self._consecutive_losses >= self.settings.max_consecutive_losses:
                logger.warning("paper gate: consecutive loss limit reached, skipping new entries")
                break

            # Entry zone check: price must be within [entry_zone_low, entry_zone_high]
            entry_low = c.entry_zone_low
            entry_high = c.entry_zone_high
            if not (entry_low <= price <= entry_high):
                continue

            # Validate risk geometry
            if not self._validate_geometry(c, price):
                continue

            # Size the position
            risk_fraction = self.settings.risk_per_trade
            entry = price
            stop = c.invalidation_price
            distance = abs(entry - stop)
            if distance <= 0 or entry <= 0:
                continue

            risk_usdt = self.balance * risk_fraction
            quantity = risk_usdt / distance
            # Cap by max_symbol_exposure
            exposure_cap = self.balance * self.settings.max_symbol_exposure / entry
            quantity = min(quantity, exposure_cap)

            if quantity <= 0:
                continue

            entry_fee = entry * quantity * self.settings.taker_fee

            # Check daily loss / consecutive losses
            if self.balance - entry_fee <= 0:
                continue

            now = datetime.now(timezone.utc)
            trade = PaperTradeRecord(
                trade_id=None,
                setup_id=str(c.setup_id),
                symbol=c.symbol,
                scanner_name=c.scanner_name,
                direction=c.direction,
                score=c.score,
                entry_price=round(entry, 6),
                entry_fee=round(entry_fee, 6),
                stop_price=round(stop, 6),
                target_1=c.target_1,
                target_2=c.target_2,
                entry_timeframe=c.entry_timeframe,
                position_size=round(quantity, 6),
                risk_usdt=round(risk_usdt, 6),
                balance_before=round(self.balance, 2),
                market_regime=c.market_regime,
                entered_at=now,
                highest_since_entry=price,
                lowest_since_entry=price,
            )

            # Persist to DB
            trade_id = self.repo.save_paper_trade(trade)
            trade.trade_id = trade_id

            self.balance -= entry_fee
            self.open_trades[c.symbol] = trade
            opened.append(trade)

            logger.info(
                "paper ENTRY: %s %s %s entry=%.4f stop=%.4f size=%.4f risk=$%.2f",
                c.symbol, c.direction, c.scanner_name,
                entry, stop, quantity, risk_usdt,
            )

        return opened

    # ------------------------------------------------------------------
    # EXIT: check open positions against current prices
    # ------------------------------------------------------------------
    def check_exits(
        self,
        prices: dict[str, float],
        funding_rates_percent: dict[str, float] | None = None,
    ) -> list[PaperTradeRecord]:
        """Check all open paper trades against current prices and funding.

        Returns list of trades that were closed in this cycle.
        """
        closed: list[PaperTradeRecord] = []
        to_remove: list[str] = []

        for symbol, trade in self.open_trades.items():
            price = prices.get(symbol)
            if price is None:
                continue

            # Expiry has priority over price-based exits.  In particular, a runner
            # restart must not classify a position that was already overdue as a
            # fresh stop/target event observed hours after its allowed lifetime.
            if self._is_expired(trade):
                closed.append(self._close_trade(trade, price, "EXPIRED"))
                to_remove.append(symbol)
                continue

            funding_rate = (funding_rates_percent or {}).get(symbol, 0.0)
            self._apply_funding(trade, funding_rate)

            is_long = trade.direction == "LONG"

            # Update high/low watermarks
            trade.highest_since_entry = max(trade.highest_since_entry, price)
            trade.lowest_since_entry = min(trade.lowest_since_entry, price)

            result = None

            # 1. Stop loss check
            if is_long and price <= trade.stop_price:
                result = self._close_trade(trade, trade.stop_price, "STOP_LOSS")
            elif not is_long and price >= trade.stop_price:
                result = self._close_trade(trade, trade.stop_price, "STOP_LOSS")

            # 2. Take profit 1 check (full close)
            if result is None and trade.target_1 is not None:
                if is_long and price >= trade.target_1:
                    result = self._close_trade(trade, trade.target_1, "TAKE_PROFIT_1")
                elif not is_long and price <= trade.target_1:
                    result = self._close_trade(trade, trade.target_1, "TAKE_PROFIT_1")

            # 3. Take profit 2 check
            if result is None and trade.target_2 is not None:
                if is_long and price >= trade.target_2:
                    result = self._close_trade(trade, trade.target_2, "TAKE_PROFIT_2")
                elif not is_long and price <= trade.target_2:
                    result = self._close_trade(trade, trade.target_2, "TAKE_PROFIT_2")

            # 4. Trailing stop logic
            if result is None:
                result = self._check_trailing_stop(trade, price)

            # 5. Timeout check (setup expired)
            if result is None and self._is_expired(trade):
                result = self._close_trade(trade, price, "EXPIRED")

            if result is not None:
                closed.append(result)
                to_remove.append(symbol)

        for symbol in to_remove:
            self.open_trades.pop(symbol, None)

        return closed

    # ------------------------------------------------------------------
    # INTERNAL HELPERS
    # ------------------------------------------------------------------
    def _validate_geometry(self, c: SetupCandidate, price: float) -> bool:
        """Quick sanity check: stop must be on the correct side of entry."""
        if c.direction == "LONG":
            return c.invalidation_price < price
        else:
            return c.invalidation_price > price

    def _check_trailing_stop(self, trade: PaperTradeRecord, price: float) -> PaperTradeRecord | None:
        """Move stop to breakeven after 1R, then trail at ATR distance."""
        is_long = trade.direction == "LONG"
        risk_distance = abs(trade.entry_price - trade.stop_price)
        if risk_distance <= 0:
            return None

        if is_long:
            favorable = price - trade.entry_price
        else:
            favorable = trade.entry_price - price

        # After 1R of favorable move → move stop to breakeven
        if favorable >= risk_distance:
            new_stop = trade.entry_price
            if is_long:
                trade.stop_price = max(trade.stop_price, new_stop)
            else:
                trade.stop_price = min(trade.stop_price, new_stop)

        # After 1.5R → trail at atr_stop_multiple * ATR from high/low
        # We approximate ATR from risk_distance (entry - stop) as baseline
        if favorable >= 1.5 * risk_distance:
            atr_approx = risk_distance / self.settings.atr_stop_multiple if self.settings.atr_stop_multiple > 0 else risk_distance
            if is_long:
                trail = price - self.settings.atr_stop_multiple * atr_approx
                trade.stop_price = max(trade.stop_price, trail)
            else:
                trail = price + self.settings.atr_stop_multiple * atr_approx
                trade.stop_price = min(trade.stop_price, trail)

        # Did the trailing stop get hit?
        if is_long and price <= trade.stop_price:
            return self._close_trade(trade, trade.stop_price, "TRAILING_STOP")
        elif not is_long and price >= trade.stop_price:
            return self._close_trade(trade, trade.stop_price, "TRAILING_STOP")

        return None

    def _apply_funding(self, trade: PaperTradeRecord, funding_rate_percent: float) -> None:
        """Settle completed funding intervals at the current observed funding rate.

        Positive funding means longs pay shorts. The periodic runner supplies the
        latest rate; settlement is deliberately conservative and only charges
        whole configured intervals elapsed since entry.
        """
        interval_seconds = self.settings.paper_funding_interval_hours * 3600
        elapsed = (datetime.now(timezone.utc) - trade.entered_at).total_seconds()
        due_periods = max(0, int(elapsed // interval_seconds))
        new_periods = due_periods - trade.funding_periods_charged
        if new_periods <= 0:
            return

        notional = trade.entry_price * trade.position_size
        signed_rate = float(funding_rate_percent) / 100.0
        cost = notional * signed_rate * new_periods
        if trade.direction == "SHORT":
            cost = -cost
        trade.funding_paid += cost
        trade.funding_periods_charged = due_periods
        self.balance -= cost

        update_funding = getattr(self.repo, "update_paper_trade_funding", None)
        if update_funding is not None:
            update_funding(
                trade.trade_id,
                round(trade.funding_paid, 6),
                trade.funding_periods_charged,
            )
        logger.info(
            "paper funding: %s periods=%d rate=%.6f%% cost=$%.4f",
            trade.symbol, new_periods, funding_rate_percent, cost,
        )

    def _close_trade(
        self,
        trade: PaperTradeRecord,
        exit_price: float,
        reason: str,
    ) -> PaperTradeRecord:
        """Close a paper trade and calculate P&L."""
        slip = self.settings.slippage_percent
        adjusted_exit = exit_price * (1 - slip if trade.direction == "LONG" else 1 + slip)

        if trade.direction == "LONG":
            signed_move = adjusted_exit - trade.entry_price
        else:
            signed_move = trade.entry_price - adjusted_exit

        gross_pnl = signed_move * trade.position_size
        exit_fee = adjusted_exit * trade.position_size * self.settings.taker_fee
        net_pnl = gross_pnl - exit_fee - trade.funding_paid

        # Funding was debited/credited at each settlement, so do not apply it
        # a second time to the cash balance at exit.
        self.balance += gross_pnl - exit_fee
        net_after_entry_fee = net_pnl - trade.entry_fee
        if net_after_entry_fee < 0:
            self._daily_loss_usdt += -net_after_entry_fee
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # Update drawdown
        if self.balance > self._peak_balance:
            self._peak_balance = self.balance
        dd = (self._peak_balance - self.balance) / self._peak_balance if self._peak_balance > 0 else 0
        self._max_drawdown = max(self._max_drawdown, dd)

        r_multiple = net_pnl / trade.risk_usdt if trade.risk_usdt > 0 else 0
        pnl_pct = net_pnl / (trade.entry_price * trade.position_size) * 100 if trade.entry_price * trade.position_size > 0 else 0
        duration = (datetime.now(timezone.utc) - trade.entered_at).total_seconds()

        # Persist to DB
        self.repo.close_paper_trade(
            trade_id=trade.trade_id,
            exit_price=round(adjusted_exit, 6),
            exit_reason=reason,
            exit_fee=round(exit_fee, 6),
            pnl_usdt=round(net_pnl, 2),
            pnl_r=round(r_multiple, 4),
            pnl_percent=round(pnl_pct, 2),
            slippage=round(abs(adjusted_exit - exit_price) * trade.position_size, 6),
            funding_paid=round(trade.funding_paid, 6),
            balance_after=round(self.balance, 2),
            duration_sec=round(duration, 1),
        )

        logger.info(
            "paper EXIT: %s %s %s reason=%s entry=%.4f exit=%.4f pnl=$%.2f R=%.2f balance=$%.2f",
            trade.symbol, trade.direction, trade.scanner_name, reason,
            trade.entry_price, adjusted_exit, net_pnl, r_multiple, self.balance,
        )

        return PaperTradeRecord(
            trade_id=trade.trade_id,
            setup_id=trade.setup_id,
            symbol=trade.symbol,
            scanner_name=trade.scanner_name,
            direction=trade.direction,
            score=trade.score,
            entry_price=trade.entry_price,
            entry_fee=trade.entry_fee,
            stop_price=trade.stop_price,
            target_1=trade.target_1,
            target_2=trade.target_2,
            position_size=trade.position_size,
            risk_usdt=trade.risk_usdt,
            balance_before=trade.balance_before,
            market_regime=trade.market_regime,
            entered_at=trade.entered_at,
            status="CLOSED",
            entry_timeframe=trade.entry_timeframe,
            trail_stop=trade.trail_stop,
            highest_since_entry=trade.highest_since_entry,
            lowest_since_entry=trade.lowest_since_entry,
            funding_paid=trade.funding_paid,
            funding_periods_charged=trade.funding_periods_charged,
        )

    def _load_account_state(self) -> None:
        """Restore balance and drawdown from the most recent account snapshot."""
        get_snapshot = getattr(self.repo, "get_latest_paper_account_snapshot", None)
        if get_snapshot is None:
            return
        snapshot = get_snapshot()
        if snapshot is None:
            return
        self.balance = float(snapshot["balance"])
        self._max_drawdown = float(snapshot.get("max_drawdown", 0.0))
        self._peak_balance = self.balance / (1 - self._max_drawdown) if self._max_drawdown < 1 else self.balance
        self._loaded_account_snapshot = True

    def _load_risk_state(self) -> None:
        """Restore today's realized loss and current loss streak after restart."""
        get_state = getattr(self.repo, "get_paper_risk_state", None)
        if get_state is None:
            return
        state = get_state()
        self._daily_loss_usdt = float(state.get("daily_loss_usdt", 0.0))
        self._consecutive_losses = int(state.get("consecutive_losses", 0))

    def _load_open_trades(self) -> None:
        """Load any existing OPEN paper trades from the DB on startup.

        When no account snapshot exists yet, reconstruct balance from the
        earliest open trade and its entry fees.
        """
        rows = self.repo.get_open_paper_trades()
        for row in rows:
            trade = PaperTradeRecord(
                trade_id=row["trade_id"],
                setup_id=row["setup_id"],
                symbol=row["symbol"],
                scanner_name=row["scanner_name"],
                direction=row["direction"],
                score=float(row["score"]),
                entry_price=float(row["entry_price"]),
                entry_fee=float(row["entry_fee"]),
                stop_price=float(row["stop_price"]),
                target_1=float(row["target_1"]) if row["target_1"] is not None else None,
                target_2=float(row["target_2"]) if row["target_2"] is not None else None,
                position_size=float(row["position_size"]),
                risk_usdt=float(row["risk_usdt"]),
                balance_before=float(row["balance_before"]),
                market_regime=row.get("market_regime"),
                entered_at=row["entered_at"],
                status="OPEN",
                entry_timeframe=str(row.get("entry_timeframe", "5m")),
                funding_paid=float(row.get("funding_paid", 0.0)),
                funding_periods_charged=int(row.get("funding_periods", 0)),
            )
            self.open_trades[trade.symbol] = trade

        if rows and not self._loaded_account_snapshot:
            # Legacy recovery: use earliest trade's balance_before minus entry fees.
            earliest = min(rows, key=lambda r: r["entered_at"])
            base_balance = float(earliest["balance_before"])
            total_entry_fees = sum(float(r["entry_fee"]) for r in rows)
            self.balance = base_balance - total_entry_fees
            logger.info(
                "paper: loaded %d open trades, reconstructed balance=$%.2f",
                len(rows), self.balance,
            )

    def _is_expired(self, trade: PaperTradeRecord) -> bool:
        """Return whether a trade has exceeded its entry-timeframe lifetime."""
        tf_minutes = self._parse_timeframe(trade)
        max_bars = _ENTRY_TIMEOUT_MAP.get(trade.entry_timeframe, 12)
        age_minutes = (datetime.now(timezone.utc) - trade.entered_at).total_seconds() / 60
        return age_minutes > max_bars * tf_minutes

    def _parse_timeframe(self, trade: PaperTradeRecord) -> int:
        """Return the number of minutes for a trade's persisted entry timeframe."""
        return {
            "5m": 5,
            "15m": 15,
            "1h": 60,
            "4h": 240,
        }.get(trade.entry_timeframe, 5)

    # ------------------------------------------------------------------
    # SNAPSHOT
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return current account state."""
        total_pnl = self.balance - self.settings.initial_balance
        return {
            "balance": round(self.balance, 2),
            "starting_balance": self.settings.initial_balance,
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / self.settings.initial_balance * 100, 2),
            "open_positions": len(self.open_trades),
            "max_drawdown_pct": round(self._max_drawdown * 100, 2),
            "open_trades": [
                {
                    "symbol": t.symbol,
                    "direction": t.direction,
                    "scanner": t.scanner_name,
                    "entry": t.entry_price,
                    "stop": t.stop_price,
                    "tp1": t.target_1,
                    "size": t.position_size,
                }
                for t in self.open_trades.values()
            ],
        }
