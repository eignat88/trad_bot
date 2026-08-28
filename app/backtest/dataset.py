"""Versioned historical dataset contracts shared by scanner backtests."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class DatasetMetadata:
    dataset_version: str
    source: str
    start_date: str
    end_date: str
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def validate(self) -> None:
        if not self.dataset_version.strip() or not self.source.strip():
            raise ValueError("dataset_version and source are required")
        if not self.symbols or not self.timeframes:
            raise ValueError("dataset symbols and timeframes cannot be empty")
        if self.start_date > self.end_date:
            raise ValueError("dataset start_date must not exceed end_date")


@dataclass(frozen=True)
class HistoricalMarketRecord:
    symbol: str
    timestamp: int
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float
    funding_rate: float
    universe_member: bool
    spread: float | None = None
    turnover: float | None = None
    mark_price: float | None = None
    index_price: float | None = None

    def validate(self) -> None:
        if not self.symbol or self.timestamp <= 0:
            raise ValueError("historical record requires symbol and positive timestamp")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC geometry")
        if self.volume < 0 or self.open_interest < 0:
            raise ValueError("volume and open interest cannot be negative")


@dataclass
class VersionedHistoricalDataset:
    metadata: DatasetMetadata
    records: list[HistoricalMarketRecord]

    def validate(self) -> None:
        self.metadata.validate()
        symbols = set(self.metadata.symbols)
        timeframes = set(self.metadata.timeframes)
        previous: dict[tuple[str, str], int] = {}
        for record in self.records:
            record.validate()
            if record.symbol not in symbols or record.timeframe not in timeframes:
                raise ValueError("record falls outside dataset metadata")
            key = (record.symbol, record.timeframe)
            if record.timestamp <= previous.get(key, 0):
                raise ValueError("records must be strictly ordered per symbol/timeframe")
            previous[key] = record.timestamp

    def to_dict(self) -> dict[str, Any]:
        return {"metadata": asdict(self.metadata), "records": [asdict(r) for r in self.records]}
