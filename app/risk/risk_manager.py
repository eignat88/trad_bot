from __future__ import annotations

from datetime import date

from app.config import Settings
from app.models import TradeSignal


class PositionSizer:
    @staticmethod
    def calculate(balance: float, risk_fraction: float, entry: float, stop: float,
                  max_exposure: float = 1.0) -> tuple[float, float]:
        distance = abs(entry - stop)
        if balance <= 0 or not 0 < risk_fraction <= 1 or distance <= 0 or entry <= 0:
            raise ValueError("invalid position sizing inputs")
        risk_usdt = balance * risk_fraction
        quantity = risk_usdt / distance
        exposure_cap = balance * max_exposure / entry
        return min(quantity, exposure_cap), risk_usdt


class RiskManager:
    def __init__(self, settings: Settings, starting_balance: float):
        self.settings = settings
        self.starting_balance = starting_balance
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.day = date.today()

    def reset_day_if_needed(self, today: date | None = None) -> None:
        today = today or date.today()
        if today != self.day:
            self.day, self.daily_pnl = today, 0.0

    def validate(self, signal: TradeSignal, balance: float, open_symbols: set[str]) -> str | None:
        self.reset_day_if_needed()
        if signal.symbol in open_symbols:
            return "POSITION_ALREADY_OPEN"
        if len(open_symbols) >= self.settings.max_open_positions:
            return "MAX_OPEN_POSITIONS"
        if self.daily_pnl <= -self.starting_balance * self.settings.max_daily_loss:
            return "MAX_DAILY_LOSS"
        if self.consecutive_losses >= self.settings.max_consecutive_losses:
            return "MAX_CONSECUTIVE_LOSSES"
        return None

    def record_result(self, pnl: float) -> None:
        self.daily_pnl += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
