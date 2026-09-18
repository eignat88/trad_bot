"""Shadow Paper Engine — counterfactual trades for experimental scanners.

Manages shadow/counterfactual paper trades that derive from existing
scanner signals but test alternative hypotheses (e.g. reversed direction,
different stop/target geometry) without affecting the main paper balance
or live trading.

Key constraints:
  - Shadow trades are fully independent from the main PaperTradingEngine.
  - They do NOT consume balance, affect exposure limits, or block symbols.
  - Each shadow trade is linked to a source setup via source_setup_id.
  - Only SL and TP exits are supported (no DCA, no trailing, no BE).
  - Shadow trades persist to the dds.paper_shadow_trade table.

Lifecycle:
  1. When a ME SHORT signal is executed as a paper trade, the shadow engine
     creates a corresponding reverse LONG shadow trade.
  2. The shadow engine monitors prices independently and closes shadow trades
     when SL or TP is hit.
  3. Shadow trades are persisted for paired analytics.
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import ExperimentalScannerConfig, Settings
from app.scanners.models import SetupCandidate

logger = logging.getLogger(__name__)


@dataclass
class ShadowTradeRecord:
    """In-memory representation of an open shadow trade."""
    shadow_trade_id: int | None
    experiment_id: str
    source_trade_id: int | None
    source_setup_id: str
    source_scanner: str
    source_direction: str
    symbol: str
    scanner_name: str
    direction: str
    score: float
    entry_price: float
    entry_fee: float
    stop_price: float
    target_1: float
    position_size: float
    risk_usdt: float
    entered_at: datetime
    entry_market_price: float = 0.0
    entry_slippage_cost: float = 0.0
    status: str = "OPEN"
    mfe: float = 0.0
    mae: float = 0.0
    highest_since_entry: float = 0.0
    lowest_since_entry: float = math.inf
    market_regime: str | None = None
    funding_paid: float = 0.0


class ShadowPaperEngine:
    """Shadow paper engine for counterfactual experimental trades.

    Operates independently from the main PaperTradingEngine:
    - No shared balance or exposure limits
    - No DCA, trailing, breakeven, or expiry
    - Fixed SL and TP geometry only
    - Writes to dds.paper_shadow_trade (separate table)
    """

    SCANNER_NAME = "MOMENTUM_EXHAUSTION_SHORT_REVERSE_LONG_V1"

    def __init__(
        self,
        settings: Settings,
        repo: Any,
        config: ExperimentalScannerConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        # Shadow trades keyed by symbol (same one-per-symbol constraint)
        self.open_trades: dict[str, ShadowTradeRecord] = {}
        self._lock = threading.Lock()

        # Reload existing open shadow trades from DB
        self._load_open_trades()

    @property
    def is_enabled(self) -> bool:
        return self.config.enabled and self.config.mode == "shadow"

    # ------------------------------------------------------------------
    # ENTRY: create shadow trade when source ME SHORT is executed
    # ------------------------------------------------------------------

    def check_shadow_entries(
        self,
        source_trade: Any,  # PaperTradeRecord from main engine
        price: float,
    ) -> ShadowTradeRecord | None:
        """Create a reverse LONG shadow trade when a ME SHORT trade is opened.

        Called by the main paper_runner after a ME SHORT trade is opened.
        The shadow trade mirrors the source entry price but inverts direction
        and applies fixed SL/TP geometry.

        Args:
            source_trade: The PaperTradeRecord of the original ME SHORT trade.
            price: Current market price at the time of source entry.
        Returns:
            The newly opened ShadowTradeRecord, or None if skipped.
        """
        if not self.is_enabled:
            return None

        # Only shadow MOMENTUM_EXHAUSTION SHORT trades
        if (source_trade.scanner_name != self.config.source_scanner
                or source_trade.direction != self.config.source_direction):
            return None

        # Already have a shadow trade for this symbol? Skip.
        if source_trade.symbol in self.open_trades:
            logger.info(
                "shadow entry suppressed: symbol=%s already has open shadow trade",
                source_trade.symbol,
            )
            return None

        # Already created a shadow for this source setup? Skip.
        existing = self._find_shadow_by_setup(source_trade.setup_id)
        if existing is not None:
            logger.info(
                "shadow entry suppressed: source setup_id=%s already has shadow trade",
                source_trade.setup_id,
            )
            return None

        now = self._clock()

        # Calculate reverse LONG entry: use source entry price as base
        # The shadow entry is the source's entry (which was SHORT entry = market price)
        entry_market = source_trade.entry_price
        slip = self.settings.slippage_percent
        entry = entry_market * (1 + slip)  # LONG entry: buy at ask

        # Fixed SL and TP geometry for the reverse LONG
        stop_loss_pct = self.config.stop_loss_pct / 100.0  # 2.5% → 0.025
        take_profit_pct = self.config.take_profit_pct / 100.0  # 3.0% → 0.03

        stop_price = entry * (1 - stop_loss_pct)
        target_1 = entry * (1 + take_profit_pct)

        # Position sizing: risk_per_trade / stop_distance
        risk_fraction = self.settings.risk_per_trade
        distance = abs(entry - stop_price)
        if distance <= 0 or entry <= 0:
            return None

        requested_risk = self.settings.initial_balance * risk_fraction
        quantity = requested_risk / distance

        # Cap by max_symbol_exposure
        exposure_cap = self.settings.initial_balance * self.settings.max_symbol_exposure / entry
        quantity = min(quantity, exposure_cap)

        if quantity <= 0:
            return None

        risk_usdt = distance * quantity
        entry_fee = entry * quantity * self.settings.taker_fee
        entry_slippage_cost = abs(entry - entry_market) * quantity

        trade = ShadowTradeRecord(
            shadow_trade_id=None,
            experiment_id="ME_SHORT_REVERSE_LONG_V1",
            source_trade_id=source_trade.trade_id,
            source_setup_id=source_trade.setup_id,
            source_scanner=source_trade.scanner_name,
            source_direction=source_trade.direction,
            symbol=source_trade.symbol,
            scanner_name=self.SCANNER_NAME,
            direction="LONG",
            score=source_trade.score,
            entry_price=round(entry, 6),
            entry_fee=round(entry_fee, 6),
            stop_price=round(stop_price, 6),
            target_1=round(target_1, 6),
            position_size=round(quantity, 6),
            risk_usdt=round(risk_usdt, 6),
            entered_at=now,
            entry_market_price=round(entry_market, 6),
            entry_slippage_cost=round(entry_slippage_cost, 6),
            highest_since_entry=price,
            lowest_since_entry=price,
            market_regime=source_trade.market_regime,
        )

        # Persist to DB
        trade_id = self.repo.save_shadow_trade(trade)
        if trade_id is None:
            return None
        trade.shadow_trade_id = trade_id

        with self._lock:
            self.open_trades[trade.symbol] = trade

        logger.info(
            "shadow ENTRY: %s %s entry=%.4f stop=%.4f tp=%.4f size=%.4f risk=$%.2f "
            "source_trade_id=%s source_setup_id=%s",
            trade.symbol, trade.direction,
            entry, stop_price, target_1, quantity, risk_usdt,
            source_trade.trade_id, source_trade.setup_id,
        )

        return trade

    # ------------------------------------------------------------------
    # EXIT: check shadow positions against current prices
    # ------------------------------------------------------------------

    def check_shadow_exits(
        self,
        prices: dict[str, float],
    ) -> list[ShadowTradeRecord]:
        """Check all open shadow trades against current prices.

        Only SL and TP exits are supported. No trailing, no DCA, no BE.
        Returns list of trades that were closed in this cycle.
        """
        if not self.is_enabled:
            return []

        closed: list[ShadowTradeRecord] = []
        to_remove: list[str] = []

        with self._lock:
            for symbol, trade in self.open_trades.items():
                price = prices.get(symbol)
                if price is None:
                    continue

                # Update high/low watermarks
                trade.highest_since_entry = max(trade.highest_since_entry, price)
                trade.lowest_since_entry = min(trade.lowest_since_entry, price)

                # LONG position MFE/MAE
                trade.mfe = max(trade.mfe, trade.highest_since_entry - trade.entry_price)
                trade.mae = max(trade.mae, trade.entry_price - trade.lowest_since_entry)

                result = None

                # 1. Stop loss check (LONG: price <= stop)
                if price <= trade.stop_price:
                    gap = price < trade.stop_price
                    slip = self.settings.slippage_percent
                    exit_price = trade.stop_price * (1 - slip)
                    exit_reason = "STOP_LOSS_GAP" if gap else "STOP_LOSS"
                    result = self._close_shadow_trade(trade, exit_price, exit_reason)

                # 2. Take profit check (LONG: price >= target)
                if result is None and trade.target_1 is not None:
                    if price >= trade.target_1:
                        result = self._close_shadow_trade(
                            trade, trade.target_1, "TAKE_PROFIT_1",
                        )

                if result is not None:
                    closed.append(result)
                    to_remove.append(symbol)

            for symbol in to_remove:
                self.open_trades.pop(symbol, None)

        return closed

    def _close_shadow_trade(
        self,
        trade: ShadowTradeRecord,
        exit_price: float,
        reason: str,
    ) -> ShadowTradeRecord:
        """Close a shadow trade and calculate P&L."""
        slip = self.settings.slippage_percent
        adjusted_exit = exit_price * (1 - slip)  # LONG exit: sell at bid

        market_entry = trade.entry_market_price or trade.entry_price
        signed_move = exit_price - market_entry  # LONG
        gross_pnl = signed_move * trade.position_size
        exit_fee = adjusted_exit * trade.position_size * self.settings.taker_fee
        exit_slippage = abs(adjusted_exit - exit_price) * trade.position_size
        slippage_cost = trade.entry_slippage_cost + exit_slippage
        net_pnl = gross_pnl - trade.entry_fee - exit_fee - trade.funding_paid - slippage_cost

        r_multiple = net_pnl / trade.risk_usdt if trade.risk_usdt > 0 else 0
        pnl_pct = (
            net_pnl / (trade.entry_price * trade.position_size) * 100
            if trade.entry_price * trade.position_size > 0 else 0
        )
        duration = (self._clock() - trade.entered_at).total_seconds()

        now = self._clock()

        # Persist to DB
        self.repo.close_shadow_trade(
            shadow_trade_id=trade.shadow_trade_id,
            exit_price=round(adjusted_exit, 6),
            exit_reason=reason,
            exit_fee=round(exit_fee, 6),
            pnl_usdt=round(net_pnl, 2),
            pnl_r=round(r_multiple, 4),
            pnl_percent=round(pnl_pct, 2),
            slippage=round(slippage_cost, 6),
            mfe=round(trade.mfe, 6),
            mae=round(trade.mae, 6),
            mfe_r=round(trade.mfe / (trade.risk_usdt / trade.position_size) if trade.position_size > 0 and trade.risk_usdt > 0 else 0, 6),
            mae_r=round(trade.mae / (trade.risk_usdt / trade.position_size) if trade.position_size > 0 and trade.risk_usdt > 0 else 0, 6),
            duration_sec=round(duration, 1),
        )

        logger.info(
            "shadow EXIT: %s %s reason=%s entry=%.4f exit=%.4f pnl=$%.2f R=%.2f",
            trade.symbol, trade.direction, reason,
            trade.entry_price, adjusted_exit, net_pnl, r_multiple,
        )

        return ShadowTradeRecord(
            shadow_trade_id=trade.shadow_trade_id,
            experiment_id=trade.experiment_id,
            source_trade_id=trade.source_trade_id,
            source_setup_id=trade.source_setup_id,
            source_scanner=trade.source_scanner,
            source_direction=trade.source_direction,
            symbol=trade.symbol,
            scanner_name=trade.scanner_name,
            direction=trade.direction,
            score=trade.score,
            entry_price=trade.entry_price,
            entry_fee=trade.entry_fee,
            stop_price=trade.stop_price,
            target_1=trade.target_1,
            position_size=trade.position_size,
            risk_usdt=trade.risk_usdt,
            entered_at=trade.entered_at,
            entry_market_price=market_entry,
            entry_slippage_cost=trade.entry_slippage_cost,
            status="CLOSED",
            mfe=trade.mfe,
            mae=trade.mae,
            highest_since_entry=trade.highest_since_entry,
            lowest_since_entry=trade.lowest_since_entry,
            market_regime=trade.market_regime,
            funding_paid=trade.funding_paid,
        )

    # ------------------------------------------------------------------
    # INTERNAL HELPERS
    # ------------------------------------------------------------------

    def _find_shadow_by_setup(self, source_setup_id: str) -> ShadowTradeRecord | None:
        """Find an existing shadow trade by its source setup ID."""
        for trade in self.open_trades.values():
            if trade.source_setup_id == source_setup_id:
                return trade
        return None

    def _load_open_trades(self) -> None:
        """Load any existing OPEN shadow trades from the DB on startup."""
        get_open = getattr(self.repo, "get_open_shadow_trades", None)
        if get_open is None:
            return

        rows = get_open()
        for row in rows:
            trade = ShadowTradeRecord(
                shadow_trade_id=row["shadow_trade_id"],
                experiment_id=row["experiment_id"],
                source_trade_id=row.get("source_trade_id"),
                source_setup_id=row["source_setup_id"],
                source_scanner=row["source_scanner"],
                source_direction=row["source_direction"],
                symbol=row["symbol"],
                scanner_name=row["scanner_name"],
                direction=row["direction"],
                score=float(row["score"]),
                entry_price=float(row["entry_price"]),
                entry_fee=float(row["entry_fee"]),
                stop_price=float(row["stop_price"]),
                target_1=float(row["target_1"]),
                position_size=float(row["position_size"]),
                risk_usdt=float(row["risk_usdt"]),
                entered_at=row["entered_at"],
                entry_market_price=float(row.get("entry_market_price", row["entry_price"])),
                entry_slippage_cost=float(row.get("slippage", 0.0)),
                status="OPEN",
                mfe=float(row.get("mfe", 0.0)),
                mae=float(row.get("mae", 0.0)),
                highest_since_entry=float(row.get("entry_market_price", row["entry_price"])),
                lowest_since_entry=float(row.get("entry_market_price", row["entry_price"])),
                market_regime=row.get("market_regime"),
                funding_paid=float(row.get("funding_paid", 0.0)),
            )
            self.open_trades[trade.symbol] = trade

        if rows:
            logger.info(
                "shadow engine: loaded %d open shadow trades",
                len(rows),
            )

    def snapshot(self) -> dict[str, Any]:
        """Return current shadow engine state for diagnostics."""
        return {
            "experiment_id": "ME_SHORT_REVERSE_LONG_V1",
            "enabled": self.is_enabled,
            "open_shadow_trades": len(self.open_trades),
            "trades": [
                {
                    "symbol": t.symbol,
                    "direction": t.direction,
                    "entry": t.entry_price,
                    "stop": t.stop_price,
                    "tp": t.target_1,
                    "source_setup_id": t.source_setup_id,
                }
                for t in self.open_trades.values()
            ],
        }
