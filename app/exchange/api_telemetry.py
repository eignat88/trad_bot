"""Bybit API call telemetry — BYBIT_API_TELEMETRY_V1 (Issue #228, first slice).

Logs every HTTP request made through ``BybitClient`` (``_public_get`` GET
calls and ``create_order`` POST calls) as exactly one structured record per
HTTP attempt, collects per-cycle statistics, detects duplicate requests, and
reports rate-limit events.

Schema ``BYBIT_API_TELEMETRY_V1`` — every attempt record carries:

* ``logical_request_id`` — one id per logical request (one ``_public_get`` /
  ``create_order`` call); every HTTP attempt of that request (timeout
  retries, rate-limit retries) shares it;
* ``attempt_index`` / ``total_attempts`` — 1-based position of this HTTP
  attempt inside the logical request (strictly ordered) and the attempt
  budget (max HTTP attempts the logical request may make);
* ``service_tag`` — explicit service tag (env ``BYBIT_SERVICE_NAME`` wins,
  otherwise caller-path heuristics) for per-service / per-checkout
  breakdown;
* ``endpoint``, ``method``, ``status_code`` — ``status_code`` is always
  populated: the real HTTP status when a response was received (0 means no
  HTTP response at all, i.e. transport/timeout failure);
* ``ret_code`` / ``ret_msg`` — Bybit API result envelope;
* ``retry_reason`` — why this attempt happened: ``none`` (first attempt),
  ``timeout`` or ``rate_limit`` (retry of the same logical request);
* ``X-Bapi-Limit`` / ``X-Bapi-Limit-Status`` / ``X-Bapi-Limit-Reset-Timestamp``
  values, or an explicit ``headers_absent=true`` marker when the CDN strips
  these headers (or the transport delivered none).

Log format: one ``BYBIT_API_CALL`` line per attempt. The historical prefix
(``service= endpoint= caller= symbol= interval= limit= duration_ms= result=``
plus ``retMsg=``/``status=`` where they existed before) is byte-compatible
with pre-existing analytics; the V1 fields are appended as one uniform
``telemetry_v=1 ...`` suffix on every variant.

Security contract: No API keys, secrets, signatures, authorization headers,
or private request payloads are ever captured or logged — records hold an
explicit allowlist of scalar fields only; response headers are reduced to the
three ``X-Bapi-Limit*`` values before anything is stored.

Telemetry is strictly observational (fail-open): recording errors are
swallowed by the caller-side guard and must never fail or alter a fetch.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("bybit_api_telemetry")

# Explicit service tag env var (wins over caller-path heuristics).
SERVICE_ENV = "BYBIT_SERVICE_NAME"

# Value used in log lines for absent optional scalars.
_NONE = "none"


def _fmt_opt(value: Any) -> str:
    """Render an optional scalar for a journald-friendly key=value line."""
    return _NONE if value is None else str(value)


# ---------------------------------------------------------------------------
# Response metadata extraction (status + X-Bapi-Limit* headers only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResponseMeta:
    """The only response data telemetry is allowed to keep.

    ``status_code`` is ``None`` when the response object exposes no status
    (e.g. a minimal mock); ``headers_absent`` is ``True`` when none of the
    ``X-Bapi-Limit*`` headers were observable.
    """

    status_code: int | None = None
    bapi_limit: int | None = None
    bapi_limit_status: int | None = None
    bapi_limit_reset_ms: int | None = None
    headers_absent: bool = True


EMPTY_RESPONSE_META = ResponseMeta()

_BAPI_LIMIT = "X-Bapi-Limit"
_BAPI_LIMIT_STATUS = "X-Bapi-Limit-Status"
_BAPI_LIMIT_RESET = "X-Bapi-Limit-Reset-Timestamp"


def _header_value(headers: Any, name: str) -> Any:
    """Case-insensitive single-header lookup over header-ish objects."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        try:
            value = getter(name)
        except Exception:
            value = None
        if value is not None:
            return value
        try:
            items = list(headers.items())
        except Exception:
            items = []
        for key, value in items:
            if str(key).lower() == name.lower():
                return value
        return None
    try:
        for key, value in headers:
            if str(key).lower() == name.lower():
                return value
    except Exception:
        return None
    return None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def extract_response_meta(response: Any) -> ResponseMeta:
    """Reduce a response object to the allowlisted telemetry metadata.

    Fail-open and side-effect-free: never raises, never touches request
    data, and keeps only the HTTP status and the three ``X-Bapi-Limit*``
    header values (never raw headers, never auth headers).
    """
    if response is None:
        return EMPTY_RESPONSE_META
    status: Any = None
    try:
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(response, "status", None)
        if status is None:
            getcode = getattr(response, "getcode", None)
            if callable(getcode):
                status = getcode()
    except Exception:
        status = None
    try:
        headers = getattr(response, "headers", None)
        if headers is None:
            getheaders = getattr(response, "getheaders", None)
            if callable(getheaders):
                headers = getheaders()
    except Exception:
        headers = None

    limit = _parse_int(_header_value(headers, _BAPI_LIMIT))
    limit_status = _parse_int(_header_value(headers, _BAPI_LIMIT_STATUS))
    limit_reset = _parse_int(_header_value(headers, _BAPI_LIMIT_RESET))
    headers_absent = limit is None and limit_status is None and limit_reset is None
    return ResponseMeta(
        status_code=_parse_int(status),
        bapi_limit=limit,
        bapi_limit_status=limit_status,
        bapi_limit_reset_ms=limit_reset,
        headers_absent=headers_absent,
    )


# ---------------------------------------------------------------------------
# Service tagging (explicit tag first, heuristics as fallback)
# ---------------------------------------------------------------------------

# Ordered: first match wins. The legacy heuristics keep their historical
# order and tags so existing BYBIT_API_CALL analytics stay stable; the V1
# entries only cover callers that previously fell through to "other"
# (ME R V3, SRR evaluator, v2d shadow, prospective/HTF research runners).
_SERVICE_PATTERNS: tuple[tuple[str, str], ...] = (
    # --- legacy heuristics (order preserved) ---
    ("scanner_runner", "scanner"),
    ("context_builder", "scanner"),
    ("orchestrator", "scanner"),
    ("paper_runner", "paper"),
    ("paper/cli", "paper"),
    ("position_monitor", "paper"),
    ("shadow/runner", "shadow"),
    ("shadow/backfill", "shadow"),
    ("shadow/evaluator", "shadow"),
    ("analytics", "analytics"),
    ("candle_sync", "analytics"),
    ("outcome_cli", "outcome"),
    ("outcome", "outcome"),
    ("market_data", "market_data"),
    ("scanner/cli", "scanner_cli"),
    # --- BYBIT_API_TELEMETRY_V1: explicit tags for former "other" consumers ---
    ("me_r_long_early_mae_v3", "me_r_v3"),
    ("isolated_oos", "me_r_v3"),
    ("me_r_v3", "me_r_v3"),
    ("srr_", "srr"),
    ("me_short", "me_short_a"),
    ("v2d_", "shadow_v2d"),
    ("me_r_long_close_location", "me_r_close_location"),
    ("prospective_evaluator", "prospective_eval"),
    ("evaluator_runner", "prospective_eval"),
    ("research/evaluator", "prospective_eval"),
    ("htf_baseline", "htf_baseline"),
    ("htf_keylevel", "htf_baseline"),
    ("scanners/cli", "scanner_cli"),
    ("live_exchange", "live"),
    ("app/main.py", "live"),
    ("bot.py", "live"),
    ("backtest", "backtest"),
)


def detect_service_tag(caller: str) -> str:
    """Map a caller path to a service tag via the ordered heuristics."""
    normalized = (caller or "").replace("\\", "/")
    for pattern, tag in _SERVICE_PATTERNS:
        if pattern in normalized:
            return tag
    return "other"


def resolve_service_tag(caller: str = "") -> str:
    """Resolve the service tag: explicit env tag wins, heuristics fallback."""
    explicit = os.environ.get(SERVICE_ENV, "").strip()
    if explicit:
        return explicit
    return detect_service_tag(caller)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


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
    # --- BYBIT_API_TELEMETRY_V1 (Issue #228) ---
    logical_request_id: str = ""
    attempt_index: int = 1
    total_attempts: int = 1
    service_tag: str = ""
    method: str = "GET"
    ret_code: int | None = None
    retry_reason: str = "none"      # none | timeout | rate_limit
    bapi_limit: int | None = None
    bapi_limit_status: int | None = None
    bapi_limit_reset_ms: int | None = None
    headers_absent: bool = True


@dataclass
class EndpointStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    rate_limited: int = 0
    total_duration_ms: float = 0.0
    min_duration_ms: float = float("inf")
    max_duration_ms: float = 0.0


@dataclass(frozen=True)
class DuplicateKey:
    symbol: str
    interval: str
    limit: int


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


# Historical BYBIT_API_CALL prefixes — byte-compatible with pre-existing
# journald analytics. The V1 fields are appended as one uniform suffix.
_OK_FMT = (
    "BYBIT_API_CALL service=%s endpoint=%s caller=%s "
    "symbol=%s interval=%s limit=%d duration_ms=%.0f result=%s"
)
_RL_FMT = _OK_FMT + " retMsg=%s"
_ERR_FMT = _RL_FMT + " status=%d"
_TELEMETRY_SUFFIX = (
    " telemetry_v=1 logical_request_id=%s attempt_index=%d total_attempts=%d"
    " method=%s status_code=%d ret_code=%s retry_reason=%s"
    " bapi_limit=%s bapi_limit_status=%s bapi_limit_reset_ms=%s"
    " headers_absent=%s"
)


class ApiTelemetry:
    """Thread-safe telemetry singleton for BybitClient API calls."""

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
        self._cycle_id = 0
        self._cycle_active = False
        self._cycle_start = 0.0
        self._cycle_symbols: list[str] = []
        self._calls: list[ApiCallRecord] = []
        self._by_endpoint: dict[str, EndpointStats] = defaultdict(EndpointStats)
        self._by_caller: dict[str, EndpointStats] = defaultdict(EndpointStats)
        self._by_symbol: dict[str, EndpointStats] = defaultdict(EndpointStats)
        self._by_interval: dict[str, EndpointStats] = defaultdict(EndpointStats)
        self._by_service: dict[str, EndpointStats] = defaultdict(EndpointStats)
        self._duplicate_counts: dict[DuplicateKey, int] = defaultdict(int)
        self._call_timestamps: list[float] = []
        self._peak_rps = 0.0
        self._peak_rps_window_ms = 0.0
        self._lifetime_total = 0
        self._lifetime_success = 0
        self._lifetime_rate_limited = 0
        self._lifetime_errors = 0

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
            self._by_service.clear()
            self._duplicate_counts.clear()
            self._call_timestamps.clear()
            self._peak_rps = 0.0
            self._peak_rps_window_ms = 0.0
        logger.info("CYCLE #%d BEGIN: symbols=%d", self._cycle_id, len(self._cycle_symbols))

    def record_http_attempt(
        self,
        *,
        endpoint: str,
        caller: str,
        symbol: str,
        interval: str,
        limit: int,
        duration_ms: float,
        result: str,
        logical_request_id: str,
        attempt_index: int,
        total_attempts: int,
        method: str = "GET",
        ret_code: int | None = None,
        ret_msg: str = "",
        retry_reason: str = "none",
        response: Any = None,
    ) -> ApiCallRecord:
        """Record exactly one HTTP attempt of a logical request.

        The response is reduced to the allowlisted metadata (HTTP status and
        the ``X-Bapi-Limit*`` headers) via :func:`extract_response_meta`;
        nothing else from the response is ever kept. ``status_code`` is
        always populated: the real status when the response exposes one,
        ``200`` as a best-effort derivation when a response object exists but
        exposes no status, and ``0`` only when no HTTP response was received
        (transport/timeout failure).
        """
        meta = extract_response_meta(response) if response is not None else EMPTY_RESPONSE_META
        if meta.status_code is not None:
            status_code = meta.status_code
        else:
            status_code = 200 if response is not None else 0
        return self.record_call(
            endpoint=endpoint,
            caller=caller,
            symbol=symbol,
            interval=interval,
            limit=limit,
            duration_ms=duration_ms,
            result=result,
            ret_msg=ret_msg,
            status_code=status_code,
            logical_request_id=logical_request_id,
            attempt_index=attempt_index,
            total_attempts=total_attempts,
            service_tag=None,
            method=method,
            ret_code=ret_code,
            retry_reason=retry_reason,
            bapi_limit=meta.bapi_limit,
            bapi_limit_status=meta.bapi_limit_status,
            bapi_limit_reset_ms=meta.bapi_limit_reset_ms,
            headers_absent=meta.headers_absent,
        )

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
        *,
        logical_request_id: str = "",
        attempt_index: int = 1,
        total_attempts: int = 1,
        service_tag: str | None = None,
        method: str = "GET",
        ret_code: int | None = None,
        retry_reason: str = "none",
        bapi_limit: int | None = None,
        bapi_limit_status: int | None = None,
        bapi_limit_reset_ms: int | None = None,
        headers_absent: bool = True,
    ) -> ApiCallRecord:
        """Record one API call (== one HTTP attempt) and emit its log line.

        The first nine parameters are positional-compatible with the
        historical signature; the keyword-only tail carries the
        ``BYBIT_API_TELEMETRY_V1`` fields.
        """
        now = time.monotonic()
        tag = service_tag or resolve_service_tag(caller)
        record = ApiCallRecord(
            timestamp=now, endpoint=endpoint, caller=caller,
            symbol=symbol, interval=interval, limit=limit,
            duration_ms=duration_ms, result=result,
            ret_msg=ret_msg, status_code=status_code,
            logical_request_id=logical_request_id,
            attempt_index=attempt_index,
            total_attempts=total_attempts,
            service_tag=tag,
            method=method,
            ret_code=ret_code,
            retry_reason=retry_reason or "none",
            bapi_limit=bapi_limit,
            bapi_limit_status=bapi_limit_status,
            bapi_limit_reset_ms=bapi_limit_reset_ms,
            headers_absent=headers_absent,
        )

        with self._lock:
            self._calls.append(record)
            for stats_dict, key in [
                (self._by_endpoint, endpoint),
                (self._by_caller, caller),
                (self._by_symbol, symbol),
                (self._by_interval, interval),
                (self._by_service, tag),
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
            dup_key = DuplicateKey(symbol=symbol, interval=interval, limit=limit)
            self._duplicate_counts[dup_key] += 1
            self._lifetime_total += 1
            if result == "OK":
                self._lifetime_success += 1
            elif result == "RATE_LIMIT":
                self._lifetime_rate_limited += 1
            else:
                self._lifetime_errors += 1
            self._call_timestamps.append(now)
            self._update_rps(now)

        self._emit(record)
        return record

    def _emit(self, record: ApiCallRecord) -> None:
        """Emit exactly one log line for one attempt record.

        The legacy ``BYBIT_API_CALL`` prefixes are preserved byte-compatible;
        the V1 fields share one uniform ``telemetry_v=1`` suffix on every
        variant so parsers can rely on it.
        """
        suffix = (
            record.logical_request_id or _NONE,
            record.attempt_index,
            record.total_attempts,
            record.method,
            record.status_code,
            _fmt_opt(record.ret_code),
            record.retry_reason or _NONE,
            _fmt_opt(record.bapi_limit),
            _fmt_opt(record.bapi_limit_status),
            _fmt_opt(record.bapi_limit_reset_ms),
            "true" if record.headers_absent else "false",
        )
        base = (
            record.service_tag or _NONE, record.endpoint, record.caller,
            record.symbol, record.interval, record.limit,
            record.duration_ms, record.result,
        )
        if record.result == "RATE_LIMIT":
            logger.warning(
                _RL_FMT + _TELEMETRY_SUFFIX,
                *base, record.ret_msg, *suffix,
            )
        elif record.result != "OK":
            logger.warning(
                _ERR_FMT + _TELEMETRY_SUFFIX,
                *base, record.ret_msg, record.status_code, *suffix,
            )
        else:
            logger.info(
                _OK_FMT + _TELEMETRY_SUFFIX,
                *base, *suffix,
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

        logger.info("=" * 80)
        logger.info("CYCLE #%d SUMMARY", summary.cycle_id)
        logger.info("  Symbols scanned:   %d", len(summary.symbols))
        logger.info("  Total API calls:   %d", summary.total_calls)
        logger.info("  Success:           %d", summary.success)
        logger.info("  Failed:            %d", summary.failed)
        logger.info("  Rate limited:      %d", summary.rate_limited)
        logger.info("  Cycle duration:    %.1fs", summary.duration_sec)
        if summary.total_calls > 0:
            logger.info("  Avg calls/symbol:  %.1f",
                         summary.total_calls / max(1, len(summary.symbols)))
            logger.info("  Avg calls/second:  %.1f",
                         summary.total_calls / max(0.001, summary.duration_sec))
        logger.info("  Peak requests/sec: %.1f (window=%.0fms)",
                     summary.peak_rps, summary.peak_rps_window_ms)
        if summary.per_endpoint:
            logger.info("  --- By Endpoint ---")
            for ep, st in sorted(summary.per_endpoint.items(), key=lambda x: -x[1].total):
                logger.info("    %-40s total=%d ok=%d rate_limit=%d avg=%.0fms",
                             ep, st.total, st.success, st.rate_limited,
                             st.total_duration_ms / max(1, st.total))
        if summary.per_caller:
            logger.info("  --- By Caller ---")
            for caller, st in sorted(summary.per_caller.items(), key=lambda x: -x[1].total):
                logger.info("    %-50s total=%d ok=%d rate_limit=%d",
                             caller, st.total, st.success, st.rate_limited)
        if summary.per_interval:
            logger.info("  --- By Interval ---")
            for iv, st in sorted(summary.per_interval.items(), key=lambda x: -x[1].total):
                logger.info("    interval=%-6s total=%d rate_limit=%d",
                             iv, st.total, st.rate_limited)
        duplicates = {k: v for k, v in summary.duplicate_keys.items() if v > 1}
        if duplicates:
            logger.info("  --- Duplicate Requests (within cycle) ---")
            for dk, count in sorted(duplicates.items(), key=lambda x: -x[1]):
                logger.info("    symbol=%-14s interval=%-6s limit=%-4d count=%d",
                             dk.symbol, dk.interval, dk.limit, count)
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

    @property
    def service_totals(self) -> dict[str, EndpointStats]:
        """Per-service breakdown keyed by explicit ``service_tag``."""
        with self._lock:
            return dict(self._by_service)

    def _update_rps(self, now: float) -> None:
        cutoff = now - 1.0
        self._call_timestamps = [t for t in self._call_timestamps if t > cutoff]
        current_rps = len(self._call_timestamps)
        if current_rps > self._peak_rps:
            self._peak_rps = current_rps
            self._peak_rps_window_ms = (
                (now - self._call_timestamps[0]) * 1000
                if self._call_timestamps else 0
            )

    @staticmethod
    def _detect_service(caller: str) -> str:
        return detect_service_tag(caller)
