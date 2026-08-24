from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Setup(str, Enum):
    TREND_START = "TREND_START"
    OI_COMPRESSION = "OI_COMPRESSION"
    CAPITULATION = "CAPITULATION"


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float
    funding_rate: float
    atr: float
    rsi: float
    ma20: float
    price_change_5m: float = 0.0
    price_change_15m: float = 0.0
    price_change_30m: float = 0.0
    price_change_1h: float = 0.0
    oi_change_5m: float = 0.0
    oi_change_15m: float = 0.0
    oi_change_30m: float = 0.0
    oi_change_1h: float = 0.0
    volume_ratio: float = 0.0
    atr_percent: float = 0.0
    local_high: float = 0.0
    local_low: float = 0.0
    previous_high: float = 0.0
    previous_low: float = 0.0

    @property
    def price(self) -> float:
        return self.close


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    setup: Setup
    side: Side
    entry: float
    stop: float
    take_profit: float
    confidence: float
    reason: str


@dataclass(frozen=True)
class StrategyDecision:
    signal: TradeSignal | None = None
    rejected_setup: Setup | None = None
    rejection_reason: str | None = None
    state: str = "NO_SIGNAL"


@dataclass
class Trade:
    trade_id: str
    timestamp: int
    symbol: str
    setup: str
    direction: str
    entry_price: float
    stop_price: float
    take_profit_price: float
    position_size: float
    risk_usdt: float
    risk_percent: float
    price_change_15m: float
    price_change_1h: float
    oi: float
    oi_change_15m: float
    oi_change_1h: float
    volume: float
    volume_ratio: float
    funding_rate: float
    atr: float
    rsi: float
    entry_reason: str
    status: str = "OPEN"
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_usdt: float = 0.0
    pnl_percent: float = 0.0
    pnl_r: float = 0.0
    fee: float = 0.0
    slippage: float = 0.0
    duration: float = 0.0
    timeframe: str = "5"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def month(self) -> str:
        return datetime.fromtimestamp(self.timestamp / 1000, timezone.utc).strftime("%Y-%m")
