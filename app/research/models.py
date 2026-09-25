"""Data models for the generic research framework."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ResearchObservation:
    """Immutable record of a scanner candidate.

    Captured at detection time.  Both PASS and REJECT candidates
    are recorded.  Features and parameters are frozen snapshots.
    """
    experiment_id: str
    scanner_name: str
    scanner_version: str
    parameter_set_id: str
    symbol: str
    direction: str
    signal_time: datetime
    signal_candle_open_time: int
    reference_price: float
    entry_zone_low: float | None
    entry_zone_high: float | None
    invalidation_price: float | None
    target_1: float | None
    target_2: float | None
    score: float
    status: str = "DETECTED"
    rejection_stage: str | None = None
    rejection_reason: str | None = None
    features: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    market_regime: str | None = None
    htf_timeframe: str = "1h"
    setup_timeframe: str = "15m"
    entry_timeframe: str = "5m"
    setup_id: str | None = None
