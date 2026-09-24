"""Bybit API call telemetry — temporary diagnostic instrumentation.

Logs every HTTP request made through BybitClient._public_get(), collects
per-cycle statistics, detects duplicate requests, and reports rate-limit
events.

NOT a production module — remove after the audit is complete.

No API keys, secrets, signatures, or tokens are ever logged.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("bybit_api_telemetry")


# ── Per-call record ──────────────────────────────────────────────

@dataclass
class ApiCallRecord:
    timestamp: float
    endpoint: str
    caller: str
    symbol: str
    interval: str
    limit: int
    duration_ms: float
    result: str          # OK | RATE_LIMIT | ERROR | TIMEOUT
    ret_msg: str = ""
    status_code: int = 0


# ── Per-key aggregate ────────────────────────────────────────────

@dataclass
class EndpointStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    rate_limited: int = 0
    total_duration_ms: float = 0.0
    min_duration_ms: float = float("inf")
    max_duration_ms: float = 0.0


# ── Duplicate detection key ──────────────────────────────────────

@dataclass(frozen=True)
class DuplicateKey:
    symbol: str
    interval: str
    limit: int


# ── Cycle-level summary ──────────────────────────────────────────

@dataclass
class CycleSummary:
    cycle_id: int
    start_time: float
    symbols: list[str] = field(default_factory=list)
    total_calls: int = 0
    success: int = 0
    failed: int = 0
    rate_limited: int = 0
    per_endpoint: dict[str, EndpointStats] = field(default_factory=dict)
    per_caller: dict[str, EndpointStats] = field(default_factory=dict)
    per_symbol: dict[str, EndpointStats] = field(default_factory=dict)
    per_interval: dict[str, EndpointStats] = field(default_factory=dict)
    duplicate_keys: dict[DuplicateKey, int] = field(default_factory=dict)
    peak_rps: float = 0.0
    peak_rps_window_ms: float = 0.0
    duration_sec: float = 0.0


# ── Global telemetry singleton ───────────────────────────────────

class ApiTelemetry:
    """Thread-safe telemetry collector for BybitClient API calls.

    Usage:
        telemetry = ApiTelemetry.get_instance()
        telemetry.begin_cycle(symbols=[...])
        # ... each _public_get() call auto-reports via record_call() ...
        summary = telemetry.end_cycle()
    """

    _instance: ApiTelemetry | None = None
    _lock_class = threading.Lock()

    @classmethod
    def get_instance(cls) -> ApiTelemetry:
        if cls._instance is None:
            with cls._lock_class:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = True

        # Cycle state
        self._cycle_id = 0
        self._cycle_active = False
        self._cycle_start = 0.0
        self._cycle_symbols: list[str] = []

        # Per-cycle accumulators
        self._calls: list[ApiCallRecord] = []
        self._by_endpoint: dict[str, EndpointStats] = defaultdict(EndpointStats)
        self._by_caller: dict[str, EndpointStats] = defaultdict(EndpointStats)
        self._by_symbol: dict[str, EndpointStats] = defaultdict(EndpointStats)
        self._by_interval: dict[str, EndpointStats] = defaultdict(EndpointStats)
        self._duplicate_counts: dict[DuplicateKey, int] = defaultdict(int)

        # RPS tracking (sliding window)
        self._call_timestamps: list[float] = []
        self._peak_rps = 0.0
        self._peak_rps_window_ms = 0.0

        # Lifetime counters (never reset)
        self._lifetime_total = 0
        self._lifetime_success = 0
        self._lifetime_rate_limited = 0
        self._lifetime_errors = 0

    # ── Public API ───────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def begin_cycle(self, symbols: list[str] | None = None) -> None:
        with self._lock:
            self._cycle_id += 1
            self._cycle_active = True
            self._cycle_start = time.monotonic()
            self._cycle_symbols = list(symbols) if symbols else []
            self._calls.clear()
            self._by_endpoint.clear()
            self._by_caller.clear()
            self._by_symbol.clear()
            self._by_interval.clear()
            self._duplicate_counts.clear()
            self._call_timestamps.clear()
            self._peak_rps = 0.0
            self._peak_rps_window_ms = 0.0
        logger.info("CYCLE #%d BEGIN: symbols=%d", self._cycle_id, len(self._cycle_symbols))

    def record_call(
        self,
        endpoint: str,
        caller: str,
        symbol: str,
        interval: str,
        limit: int,
        duration_ms: float,
        result: str,
        ret_msg: str = "",
        status_code: int = 0,
    ) -> None:
        now = time.monotonic()
        record = ApiCallRecord(
            timestamp=now,
            endpoint=endpoint,
            caller=caller,
            symbol=symbol,
            interval=interval,
            limit=limit,
            duration_ms=duration_ms,
            result=result,
            ret_msg=ret_msg,
            status_code=status_code,
        )

        with self._lock:
            self._calls.append(record)

            # Aggregates
            for stats_dict, key in [
                (self._by_endpoint, endpoint),
                (self._by_caller, caller),
                (self._by_symbol, symbol),
                (self._by_interval, interval),
            ]:
                s = stats_dict[key]
                s.total += 1
                s.total_duration_ms += duration_ms
                s.min_duration_ms = min(s.min_duration_ms, duration_ms)
                s.max_duration_ms = max(s.max_duration_ms, duration_ms)
                if result == "OK":
                    s.success += 1
                elif result == "RATE_LIMIT":
                    s.rate_limited += 1
                else:
                    s.failed += 1

            # Duplicate detection
            dup_key = DuplicateKey(symbol=symbol, interval=interval, limit=limit)
            self._duplicate_counts[dup_key] += 1

            # Lifetime
            self._lifetime_total += 1
            if result == "OK":
                self._lifetime_success += 1
            elif result == "RATE_LIMIT":
                self._lifetime_rate_limited += 1
            else:
                self._lifetime_errors += 1

            # RPS tracking
            self._call_timestamps.append(now)
            self._update_rps(now)

        # Log every call at INFO level
        if result == "RATE_LIMIT":
            logger.warning(
                "BYBIT_API_CALL service=%s endpoint=%s caller=%s "
                "symbol=%s interval=%s limit=%d duration_ms=%.0f "
                "result=%s retMsg=%s",
                self._detect_service(caller), endpoint, caller,
                symbol, interval, limit, duration_ms,
                result, ret_msg,
            )
        elif result != "OK":
            logger.warning(
                "BYBIT_API_CALL service=%s endpoint=%s caller=%s "
                "symbol=%s interval=%s limit=%d duration_ms=%.0f "
                "result=%s retMsg=%s status=%d",
                self._detect_service(caller), endpoint, caller,
                symbol, interval, limit, duration_ms,
                result, ret_msg, status_code,
            )
        else:
            logger.info(
                "BYBIT_API_CALL service=%s endpoint=%s caller=%s "
                "symbol=%s interval=%s limit=%d duration_ms=%.0f "
                "result=%s",
                self._detect_service(caller), endpoint, caller,
                symbol, interval, limit, duration_ms,
                result,
            )

    def end_cycle(self) -> CycleSummary:
        with self._lock:
            elapsed = time.monotonic() - self._cycle_start
            self._cycle_active = False

            summary = CycleSummary(
                cycle_id=self._cycle_id,
                start_time=self._cycle_start,
                symbols=list(self._cycle_symbols),
                total_calls=len(self._calls),
                success=sum(1 for c in self._calls if c.result == "OK"),
                failed=sum(1 for c in self._calls if c.result not in ("OK", "RATE_LIMIT")),
                rate_limited=sum(1 for c in self._calls if c.result == "RATE_LIMIT"),
                per_endpoint=dict(self._by_endpoint),
                per_caller=dict(self._by_caller),
                per_symbol=dict(self._by_symbol),
                per_interval=dict(self._by_interval),
                duplicate_keys=dict(self._duplicate_counts),
                peak_rps=self._peak_rps,
                peak_rps_window_ms=self._peak_rps_window_ms,
                duration_sec=elapsed,
            )

        # Log summary
        logger.info("=" * 80)
        logger.info("CYCLE #%d SUMMARY", summary.cycle_id)
        logger.info("  Symbols scanned:   %d", len(summary.symbols))
        logger.info("  Total API calls:   %d", summary.total_calls)
        logger.info("  Success:           %d", summary.success)
        logger.info("  Failed:            %d", summary.failed)
        logger.info("  Rate limited:      %d", summary.rate_limited)
        logger.info("  Cycle duration:    %.1fs", summary.duration_sec)
        if summary.total_calls > 0:
            logger.info(
                "  Avg calls/symbol:  %.1f",
                summary.total_calls / max(1, len(summary.symbols)),
            )
            logger.info(
                "  Avg calls/second:  %.1f",
                summary.total_calls / max(0.001, summary.duration_sec),
            )
        logger.info("  Peak requests/sec: %.1f (window=%.0fms)",
                     summary.peak_rps, summary.peak_rps_window_ms)

        # Top endpoints
        if summary.per_endpoint:
            logger.info("  --- By Endpoint ---")
            for ep, stats in sorted(summary.per_endpoint.items(), key=lambda x: -x[1].total):
                logger.info(
                    "    %-40s total=%d ok=%d rate_limit=%d avg=%.0fms",
                    ep, stats.total, stats.success, stats.rate_limited,
                    stats.total_duration_ms / max(1, stats.total),
                )

        # Top callers
        if summary.per_caller:
            logger.info("  --- By Caller ---")
            for caller, stats in sorted(summary.per_caller.items(), key=lambda x: -x[1].total):
                logger.info(
                    "    %-50s total=%d ok=%d rate_limit=%d",
                    caller, stats.total, stats.success, stats.rate_limited,
                )

        # Intervals
        if summary.per_interval:
            logger.info("  --- By Interval ---")
            for iv, stats in sorted(summary.per_interval.items(), key=lambda x: -x[1].total):
                logger.info(
                    "    interval=%-6s total=%d rate_limit=%d",
                    iv, stats.total, stats.rate_limited,
                )

        # Duplicates (only those with count > 1)
        duplicates = {k: v for k, v in summary.duplicate_keys.items() if v > 1}
        if duplicates:
            logger.info("  --- Duplicate Requests (within cycle) ---")
            for dk, count in sorted(duplicates.items(), key=lambda x: -x[1]):
                logger.info(
                    "    symbol=%-14s interval=%-6s limit=%-4d count=%d",
                    dk.symbol, dk.interval, dk.limit, count,
                )

        logger.info("=" * 80)
        return summary

    @property
    def lifetime_totals(self) -> dict[str, int]:
        return {
            "total": self._lifetime_total,
            "success": self._lifetime_success,
            "rate_limited": self._lifetime_rate_limited,
            "errors": self._lifetime_errors,
        }

    # ── Internal ─────────────────────────────────────────────────

    def _update_rps(self, now: float) -> None:
        """Update peak RPS using a 1-second sliding window."""
        cutoff = now - 1.0
        self._call_timestamps = [t for t in self._call_timestamps if t > cutoff]
        current_rps = len(self._call_timestamps)
        if current_rps > self._peak_rps:
            self._peak_rps = current_rps
            self._peak_rps_window_ms = (now - self._call_timestamps[0]) * 1000 if self._call_timestamps else 0

    @staticmethod
    def _detect_service(caller: str) -> str:
        """Best-effort service name from caller path."""
        if "scanner_runner" in caller or "context_builder" in caller or "orchestrator" in caller:
            return "scanner"
        if "paper_runner" in caller or "paper/cli" in caller or "position_monitor" in caller:
            return "paper"
        if "shadow/runner" in caller or "shadow/backfill" in caller or "shadow/evaluator" in caller:
            return "shadow"
        if "analytics" in caller or "candle_sync" in caller:
            return "analytics"
        if "outcome_cli" in caller or "outcome" in caller:
            return "outcome"
        if "market_data" in caller:
            return "market_data"
        if "scanner/cli" in caller:
            return "scanner_cli"
        return "other"
