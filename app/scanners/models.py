from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from app.models import Candle


class SetupState(str, Enum):
    SEARCHING = "SEARCHING"
    POI_FOUND = "POI_FOUND"
    LIQUIDITY_SWEPT = "LIQUIDITY_SWEPT"
    CHOCH_CONFIRMED = "CHOCH_CONFIRMED"
    WAITING_RETEST = "WAITING_RETEST"
    SETUP_READY = "SETUP_READY"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class ScannerDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class IndicatorSnapshot:
    atr: float = 0.0
    rsi: float = 0.0
    ema20: float = 0.0
    ema50: float = 0.0
    ema200: float = 0.0
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_width: float = 0.0
    volume_sma: float = 0.0


@dataclass(frozen=True)
class MarketLevels:
    previous_day_high: float = 0.0
    previous_day_low: float = 0.0
    previous_week_high: float = 0.0
    previous_week_low: float = 0.0
    swing_highs: tuple[float, ...] = ()
    swing_lows: tuple[float, ...] = ()
    support_levels: tuple[float, ...] = ()
    resistance_levels: tuple[float, ...] = ()


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    candles_5m: tuple[Candle, ...]
    candles_15m: tuple[Candle, ...]
    candles_1h: tuple[Candle, ...]
    candles_4h: tuple[Candle, ...]
    indicators: IndicatorSnapshot
    market_regime: str | None
    levels: MarketLevels
    evaluated_at: datetime


@dataclass(frozen=True)
class SetupCandidate:
    setup_id: UUID = field(default_factory=uuid4)
    scanner_name: str = ""
    scanner_version: str = "1.0.0"
    symbol: str = ""
    direction: str = "LONG"
    htf_timeframe: str = "1h"
    setup_timeframe: str = "15m"
    entry_timeframe: str = "5m"
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    setup_started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reference_price: float = 0.0
    entry_zone_low: float = 0.0
    entry_zone_high: float = 0.0
    invalidation_price: float = 0.0
    target_1: float | None = None
    target_2: float | None = None
    score: float = 0.0
    market_regime: str | None = None
    reasons: tuple[str, ...] = ()
    features: dict[str, Any] = field(default_factory=dict)
    source_candle_ids: tuple[int, ...] = ()
    state: SetupState = SetupState.SETUP_READY

    @property
    def fingerprint(self) -> str:
        return (
            f"{self.scanner_name}|{self.symbol}|{self.direction}|"
            f"{self.reference_price:.4f}|{self.setup_timeframe}|"
            f"{self.setup_started_at.isoformat()}"
        )


@dataclass
class SwingPoint:
    timestamp: int
    price: float
    direction: str
    index: int = 0
