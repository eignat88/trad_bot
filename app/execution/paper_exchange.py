from __future__ import annotations

import uuid

from app.config import Settings
from app.models import MarketSnapshot, Side, Trade, TradeSignal
from app.risk import PositionSizer, RiskManager
from app.storage import JsonlRepository


class PaperExchange:
    """Deterministic fill simulator. It never owns or calls an API client."""
    def __init__(self, settings: Settings, repository: JsonlRepository | None = None):
        self.settings, self.balance = settings, settings.initial_balance
        self.positions: dict[str, Trade] = {}
        self.repository = repository
        self.risk = RiskManager(settings, self.balance)

    def open(self, signal: TradeSignal, snapshot: MarketSnapshot) -> tuple[Trade | None, str | None]:
        rejection = self.risk.validate(signal, self.balance, set(self.positions))
        if rejection:
            return None, rejection
        slip = self.settings.slippage_percent
        entry = signal.entry * (1 + slip if signal.side == Side.LONG else 1 - slip)
        quantity, risk_usdt = PositionSizer.calculate(self.balance, self.settings.risk_per_trade,
            entry, signal.stop, self.settings.max_symbol_exposure)
        entry_fee = entry * quantity * self.settings.taker_fee
        trade = Trade(str(uuid.uuid4()), snapshot.timestamp, signal.symbol, signal.setup.value,
            signal.side.value, entry, signal.stop, signal.take_profit, quantity, risk_usdt,
            self.settings.risk_per_trade * 100, snapshot.price_change_15m, snapshot.price_change_1h,
            snapshot.open_interest, snapshot.oi_change_15m, snapshot.oi_change_1h, snapshot.volume,
            snapshot.volume_ratio, snapshot.funding_rate, snapshot.atr, snapshot.rsi, signal.reason,
            fee=entry_fee, slippage=abs(entry - signal.entry) * quantity,
            timeframe=self.settings.timeframe)
        self.balance -= entry_fee
        self.positions[signal.symbol] = trade
        if self.repository:
            self.repository.save_trade(trade)
        return trade, None

    def update(self, snapshot: MarketSnapshot) -> Trade | None:
        trade = self.positions.get(snapshot.symbol)
        if not trade:
            return None
        is_long = trade.direction == Side.LONG.value
        # Conservative same-candle assumption: stop is evaluated before target.
        if (is_long and snapshot.low <= trade.stop_price) or (not is_long and snapshot.high >= trade.stop_price):
            return self.close(snapshot.symbol, trade.stop_price, snapshot.timestamp, "STOP_LOSS")
        if (is_long and snapshot.high >= trade.take_profit_price) or (not is_long and snapshot.low <= trade.take_profit_price):
            return self.close(snapshot.symbol, trade.take_profit_price, snapshot.timestamp, "TAKE_PROFIT")
        risk_distance = abs(trade.entry_price - trade.stop_price)
        favorable = snapshot.high - trade.entry_price if is_long else trade.entry_price - snapshot.low
        if favorable >= risk_distance and ((is_long and trade.stop_price < trade.entry_price) or
                                           (not is_long and trade.stop_price > trade.entry_price)):
            trade.stop_price = trade.entry_price
        if favorable >= 1.5 * risk_distance:
            trail = snapshot.close - self.settings.atr_stop_multiple * snapshot.atr if is_long else snapshot.close + self.settings.atr_stop_multiple * snapshot.atr
            trade.stop_price = max(trade.stop_price, trail) if is_long else min(trade.stop_price, trail)
        return None

    def close(self, symbol: str, raw_price: float, timestamp: int, reason: str) -> Trade:
        trade = self.positions.pop(symbol)
        slip = self.settings.slippage_percent
        exit_price = raw_price * (1 - slip if trade.direction == Side.LONG.value else 1 + slip)
        signed_move = exit_price - trade.entry_price if trade.direction == Side.LONG.value else trade.entry_price - exit_price
        gross = signed_move * trade.position_size
        exit_fee = exit_price * trade.position_size * self.settings.taker_fee
        pnl = gross - exit_fee
        self.balance += pnl
        trade.status, trade.exit_price, trade.exit_reason = "CLOSED", exit_price, reason
        trade.fee += exit_fee
        trade.slippage += abs(exit_price - raw_price) * trade.position_size
        trade.pnl_usdt, trade.pnl_percent = pnl, pnl / (trade.entry_price * trade.position_size) * 100
        trade.pnl_r = pnl / trade.risk_usdt if trade.risk_usdt else 0
        trade.duration = (timestamp - trade.timestamp) / 1000
        self.risk.record_result(pnl)
        if self.repository:
            self.repository.save_trade(trade)
        return trade
