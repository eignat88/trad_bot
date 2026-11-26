"""Issue #304: immutable *offline-only* funding observation contract.

This module does not fetch Bybit data, share runtime state, alter engine inputs,
or perform accounting. A sample is provenance evidence, NOT permission to
reuse a funding rate in V3/OOS2. Explicit consumer admission is intentionally
absent until replay equivalence has been established.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math


class FundingStatus(str, Enum):
    OK = "OK"
    MISSING = "MISSING"
    INVALID = "INVALID"
    API_ERROR = "API_ERROR"


@dataclass(frozen=True)
class FundingObservation:
    symbol: str
    status: FundingStatus
    observed_at: datetime
    # Percent, not fraction. A confirmed zero must remain 0.0.
    rate_percent: float | None
    # Exact provenance: Bybit fundingRateTimestamp / period boundary, only
    # when actually present and parsed by a future source adapter.
    funding_event_ms: int | None = None
    source: str = "bybit_funding_history"
    category: str = "linear"
    request_limit: int = 1

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.isascii() or not self.symbol.isalnum():
            raise ValueError("invalid symbol")
        if not isinstance(self.status, FundingStatus):
            raise ValueError("invalid funding status")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observation must have timezone")
        if self.observed_at.utcoffset().total_seconds() != 0:
            raise ValueError("observation time must be UTC")
        if self.source != "bybit_funding_history" or self.category != "linear":
            raise ValueError("unsupported provenance")
        if self.request_limit != 1:
            raise ValueError("unexpected request parameters")
        if self.funding_event_ms is not None and (
            type(self.funding_event_ms) is not int or self.funding_event_ms < 0
        ):
            raise ValueError("invalid funding event timestamp")
        if self.status == FundingStatus.OK:
            if self.rate_percent is None or not isinstance(self.rate_percent, (int, float)):
                raise ValueError("OK requires observed rate")
            if not math.isfinite(float(self.rate_percent)):
                raise ValueError("non-finite rate")
        elif self.rate_percent is not None:
            raise ValueError("non-OK must have UNKNOWN rate (None)")

    @property
    def identity(self) -> tuple[str, str, int, int | None]:
        """Provenance identity; NOT an authorization to replace observations."""
        return (self.category, self.symbol, self.request_limit, self.funding_event_ms)


def parse_funding_history_observation(
    *,
    symbol: str,
    payload: object,
    observed_at: datetime,
) -> FundingObservation:
    """Parse a successful raw Bybit response. Transport failures must be
    classified by the caller as API_ERROR, never disguised as empty history.
    Raw API returns a fraction; consumers historically receive percent.
    """
    rows: object = None
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict):
            rows = result.get("list")
    if not isinstance(rows, list):
        return FundingObservation(symbol, FundingStatus.INVALID, observed_at, None)
    if not rows:
        return FundingObservation(symbol, FundingStatus.MISSING, observed_at, None)
    row = rows[0]
    if not isinstance(row, dict):
        return FundingObservation(symbol, FundingStatus.INVALID, observed_at, None)
    raw_rate = row.get("fundingRate")
    if isinstance(raw_rate, bool):
        return FundingObservation(symbol, FundingStatus.INVALID, observed_at, None)
    try:
        fraction = float(raw_rate)
    except (TypeError, ValueError, OverflowError):
        return FundingObservation(symbol, FundingStatus.INVALID, observed_at, None)
    if not math.isfinite(fraction):
        return FundingObservation(symbol, FundingStatus.INVALID, observed_at, None)
    # Reject malformed event timestamps rather than pretending to know which
    # funding period an observation belongs to.
    timestamp = row.get("fundingRateTimestamp")
    event_ms: int | None = None
    if timestamp is not None:
        try:
            if isinstance(timestamp, bool) or str(timestamp).strip() != str(int(timestamp)):
                raise ValueError("nonintegral timestamp")
            event_ms = int(timestamp)
            if event_ms < 0:
                raise ValueError("negative timestamp")
        except (TypeError, ValueError, OverflowError):
            return FundingObservation(symbol, FundingStatus.INVALID, observed_at, None)
    return FundingObservation(
        symbol, FundingStatus.OK, observed_at, fraction * 100.0,
        funding_event_ms=event_ms,
    )


def unobserved_funding(
    symbol: str, status: FundingStatus, observed_at: datetime,
) -> FundingObservation:
    """Record absence/failure only; never turn it into a confirmed 0.0."""
    if status == FundingStatus.OK:
        raise ValueError("OK must come from a parsed observed history row")
    return FundingObservation(symbol, status, observed_at, None)
