"""Signal Funnel Diagnostics V1 — Shadow observability for MOMENTUM_EXHAUSTION_REVERSE_LONG_V2.

Counts every stage of the V2 _scan_long() funnel WITHOUT modifying any
production behavior (no new gates, no SL/TP/entry/sizing/DCA/timeout changes).

Architecture:
  - FunnelCounters: per-scanner atomic counters for pass stages and reject reasons
  - FunnelCollector: manages counters by scanner name, supports flush
  - Global collector instance: get_funnel_collector(name) returns the singleton
  - flush_and_persist_all(): UPSERTs current counters to PostgreSQL (fail-open)

Counter design:
  PASS_* counters are accumulative — each stage adds 1 when reached.
  REJECT_* counters are first-match — exactly one per rejected scan.
  FINAL_SETUP counts successful scan completions.

Usage in scanner:
    from app.scanners.funnel_diagnostics import get_funnel_collector
    funnel = get_funnel_collector("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2")
    funnel.increment("TOTAL_SCANS")

Usage in runner (after scan cycle):
    from app.scanners.funnel_diagnostics import flush_and_persist_all
    flush_and_persist_all(repository)
"""
from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.db.repository import ScannerRepository

logger = logging.getLogger(__name__)


@dataclass
class FunnelCounters:
    """Atomic counters for one scanner's signal funnel.

    Pass counters are accumulative:
      PASS_DATA_LENGTH includes PASS_SWING_HIGHS which includes PASS_BREAK_PREV_HIGH, etc.

    Reject counters are first-match (exactly one per rejected scan).
    """

    # ── Pass stage counters (accumulative) ──────────────────────────────
    TOTAL_SCANS: int = 0
    PASS_DATA_LENGTH: int = 0
    PASS_SWING_HIGHS: int = 0
    PASS_BREAK_PREV_HIGH: int = 0
    PASS_RETURN_NEAR_HIGH: int = 0
    PASS_BEARISH_CANDLE: int = 0
    PASS_BODY_RATIO: int = 0
    PASS_RSI_65: int = 0
    PASS_RSI_DELTA_AVAILABLE: int = 0
    PASS_RSI_DELTA_POSITIVE: int = 0
    FINAL_SETUP: int = 0

    # ── Reject reason counters (first-match, mutually exclusive) ────────
    NO_DATA: int = 0
    NO_SWINGS: int = 0
    NO_BREAKOUT: int = 0
    TOO_FAR_ABOVE_PREV_HIGH: int = 0
    NOT_BEARISH: int = 0
    BODY_TOO_LARGE: int = 0
    RSI_BELOW_65: int = 0
    RSI_DELTA_MISSING: int = 0
    RSI_DELTA_NOT_POSITIVE: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Convert to dictionary for persistence."""
        return {
            "total_scans": self.TOTAL_SCANS,
            "pass_data_length": self.PASS_DATA_LENGTH,
            "pass_swing_highs": self.PASS_SWING_HIGHS,
            "pass_break_prev_high": self.PASS_BREAK_PREV_HIGH,
            "pass_return_near_high": self.PASS_RETURN_NEAR_HIGH,
            "pass_bearish_candle": self.PASS_BEARISH_CANDLE,
            "pass_body_ratio": self.PASS_BODY_RATIO,
            "pass_rsi_65": self.PASS_RSI_65,
            "pass_rsi_delta_available": self.PASS_RSI_DELTA_AVAILABLE,
            "pass_rsi_delta_positive": self.PASS_RSI_DELTA_POSITIVE,
            "final_setup": self.FINAL_SETUP,
            "no_data": self.NO_DATA,
            "no_swings": self.NO_SWINGS,
            "no_breakout": self.NO_BREAKOUT,
            "too_far_above_prev_high": self.TOO_FAR_ABOVE_PREV_HIGH,
            "not_bearish": self.NOT_BEARISH,
            "body_too_large": self.BODY_TOO_LARGE,
            "rsi_below_65": self.RSI_BELOW_65,
            "rsi_delta_missing": self.RSI_DELTA_MISSING,
            "rsi_delta_not_positive": self.RSI_DELTA_NOT_POSITIVE,
        }

    def total_rejects(self) -> int:
        """Total number of rejected scans."""
        return (
            self.NO_DATA + self.NO_SWINGS + self.NO_BREAKOUT
            + self.TOO_FAR_ABOVE_PREV_HIGH + self.NOT_BEARISH
            + self.BODY_TOO_LARGE + self.RSI_BELOW_65
            + self.RSI_DELTA_MISSING + self.RSI_DELTA_NOT_POSITIVE
        )

    def is_empty(self) -> bool:
        """Check if all counters are zero."""
        return self.TOTAL_SCANS == 0


class FunnelCollector:
    """Thread-safe funnel diagnostics collector for a single scanner."""

    VALID_COUNTERS = frozenset({
        "TOTAL_SCANS",
        "PASS_DATA_LENGTH", "PASS_SWING_HIGHS", "PASS_BREAK_PREV_HIGH",
        "PASS_RETURN_NEAR_HIGH", "PASS_BEARISH_CANDLE", "PASS_BODY_RATIO",
        "PASS_RSI_65", "PASS_RSI_DELTA_AVAILABLE", "PASS_RSI_DELTA_POSITIVE",
        "FINAL_SETUP",
        "NO_DATA", "NO_SWINGS", "NO_BREAKOUT", "TOO_FAR_ABOVE_PREV_HIGH",
        "NOT_BEARISH", "BODY_TOO_LARGE", "RSI_BELOW_65",
        "RSI_DELTA_MISSING", "RSI_DELTA_NOT_POSITIVE",
    })

    def __init__(self, scanner_name: str) -> None:
        self.scanner_name = scanner_name
        self._lock = threading.Lock()
        self._current = FunnelCounters()
        self._period_start: datetime = datetime.now(timezone.utc)
        self._total_scans_all_time = 0

    def increment(self, counter_name: str) -> None:
        """Increment a counter by 1. Thread-safe."""
        if counter_name not in self.VALID_COUNTERS:
            raise ValueError(f"Unknown funnel counter: {counter_name}")
        with self._lock:
            current_value = getattr(self._current, counter_name)
            setattr(self._current, counter_name, current_value + 1)
            if counter_name == "TOTAL_SCANS":
                self._total_scans_all_time += 1

    def get_counters(self) -> FunnelCounters:
        """Return a snapshot of current counters (thread-safe copy)."""
        with self._lock:
            return FunnelCounters(**{
                field: getattr(self._current, field)
                for field in self._current.__dataclass_fields__
            })

    def get_all_time_total(self) -> int:
        """Return total scans across all periods."""
        with self._lock:
            return self._total_scans_all_time

    def flush(self) -> tuple[FunnelCounters, datetime]:
        """Flush current counters and start a new period."""
        with self._lock:
            flushed = self._current
            period_start = self._period_start
            self._current = FunnelCounters()
            self._period_start = datetime.now(timezone.utc)
            return flushed, period_start

    def summary_text(self) -> str:
        """Human-readable funnel summary for logging."""
        c = self.get_counters()
        lines = [
            f"--- {self.scanner_name} Funnel ---",
            f"TOTAL_SCANS:          {c.TOTAL_SCANS:>6}",
            f"PASS_DATA_LENGTH:     {c.PASS_DATA_LENGTH:>6}",
            f"PASS_SWING_HIGHS:     {c.PASS_SWING_HIGHS:>6}",
            f"PASS_BREAK_PREV_HIGH: {c.PASS_BREAK_PREV_HIGH:>6}",
            f"PASS_RETURN_NEAR_HIGH:{c.PASS_RETURN_NEAR_HIGH:>6}",
            f"PASS_BEARISH_CANDLE:  {c.PASS_BEARISH_CANDLE:>6}",
            f"PASS_BODY_RATIO:      {c.PASS_BODY_RATIO:>6}",
            f"PASS_RSI_65:          {c.PASS_RSI_65:>6}",
            f"PASS_RSI_DELTA_AVAIL: {c.PASS_RSI_DELTA_AVAILABLE:>6}",
            f"PASS_RSI_DELTA_POS:   {c.PASS_RSI_DELTA_POSITIVE:>6}",
            f"FINAL_SETUP:          {c.FINAL_SETUP:>6}",
            "",
            "Reject reasons:",
            f"  NO_DATA:               {c.NO_DATA:>4}",
            f"  NO_SWINGS:             {c.NO_SWINGS:>4}",
            f"  NO_BREAKOUT:           {c.NO_BREAKOUT:>4}",
            f"  TOO_FAR_ABOVE_PREV:    {c.TOO_FAR_ABOVE_PREV_HIGH:>4}",
            f"  NOT_BEARISH:           {c.NOT_BEARISH:>4}",
            f"  BODY_TOO_LARGE:        {c.BODY_TOO_LARGE:>4}",
            f"  RSI_BELOW_65:          {c.RSI_BELOW_65:>4}",
            f"  RSI_DELTA_MISSING:     {c.RSI_DELTA_MISSING:>4}",
            f"  RSI_DELTA_NOT_POSITIVE:{c.RSI_DELTA_NOT_POSITIVE:>4}",
        ]
        return "\n".join(lines)


# ── Global registry ──────────────────────────────────────────────────────

_collectors: dict[str, FunnelCollector] = {}
_collectors_lock = threading.Lock()


def get_funnel_collector(scanner_name: str) -> FunnelCollector:
    """Get or create the singleton FunnelCollector for a scanner."""
    with _collectors_lock:
        if scanner_name not in _collectors:
            _collectors[scanner_name] = FunnelCollector(scanner_name)
        return _collectors[scanner_name]


def get_all_collectors() -> dict[str, FunnelCollector]:
    """Return all registered collectors."""
    with _collectors_lock:
        return dict(_collectors)


def reset_all_collectors() -> None:
    """Reset all collectors (for testing)."""
    with _collectors_lock:
        _collectors.clear()


def flush_and_persist_all(repository: ScannerRepository | None) -> None:
    """Flush all collectors and persist to PostgreSQL. Fail-open."""
    collectors = get_all_collectors()
    for scanner_name, collector in collectors.items():
        counters, period_start = collector.flush()
        if counters.is_empty():
            continue
        try:
            if repository is not None and repository.is_connected():
                repository.save_funnel_observation(
                    scanner_name=scanner_name,
                    period_start=period_start,
                    counters=counters,
                )
                logger.info(
                    "signal funnel persisted: scanner=%s period_start=%s "
                    "total_scans=%d final_setup=%d",
                    scanner_name,
                    period_start.isoformat(),
                    counters.TOTAL_SCANS,
                    counters.FINAL_SETUP,
                )
            else:
                logger.debug(
                    "signal funnel not persisted (no DB): scanner=%s total_scans=%d",
                    scanner_name, counters.TOTAL_SCANS,
                )
        except Exception:
            logger.warning(
                "signal funnel persistence failed for %s: %s",
                scanner_name, str(sys.exc_info()[1]),
            )
