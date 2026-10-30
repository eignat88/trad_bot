from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import math
import socket
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from app.config import Settings
from app.exchange import retry_budget
from app.models import Candle


class BybitError(RuntimeError):
    pass


class BybitTimeoutError(BybitError):
    pass


class BybitRateLimitError(BybitError):
    """Fail-closed rate-limit outcome (BYBIT_RETRY_BUDGET_HARDENING_V1).

    Raised when the rate-limit retry budget is exhausted, when an open
    breaker cooldown defers the request (P3), or when the retry-storm guard
    suppresses a retry. Callers must treat it as a counted drop — never as a
    signal to retry at another layer (Issue #229).
    """


class BybitOrderUncertainError(BybitError):
    """Fail-closed double-submit block (BYBIT_RETRY_BUDGET_HARDENING_V1).

    An identical POST /v5/order/create submission has an INDETERMINATE
    outcome (no response / 5xx / unparseable body): the order may exist
    server-side, so re-sending it is blocked instead of risking a double fill.
    """


class _RateLimiter:
    """Thread-safe token-bucket rate limiter.

    Tokens refill at rps tokens per second up to a burst capacity of 1.
    acquire() blocks until a token is available.
    """

    def __init__(self, rps: float = 10.0) -> None:
        self._rps = max(rps, 0.1)
        self._interval = 1.0 / self._rps
        self._lock = threading.Lock()
        # Start with a token ready so the first acquire() is instant.
        self._last_token_time = time.monotonic() - self._interval
        self._stop_event = threading.Event()

    def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_token_time
                if elapsed >= self._interval:
                    self._last_token_time = now
                    return
                wait_time = self._interval - elapsed
            # Use Event.wait() instead of time.sleep() to avoid being
            # captured by monkeypatch in tests.
            self._stop_event.wait(timeout=wait_time)
            self._stop_event.clear()


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telemetry helpers (BYBIT_API_TELEMETRY_V1, Issue #228)
# ---------------------------------------------------------------------------

_telemetry = None


def _get_telemetry():
    """Lazy-load the ApiTelemetry singleton (avoids circular import)."""
    global _telemetry
    if _telemetry is None:
        from app.exchange.api_telemetry import ApiTelemetry
        _telemetry = ApiTelemetry.get_instance()
    return _telemetry


def _new_logical_request_id() -> str:
    """One id per logical request (a full _public_get / create_order call)."""
    return uuid4().hex[:16]


def _record_attempt(
    telemetry: Any,
    *,
    endpoint: str,
    caller: str,
    symbol: Any,
    interval: Any,
    limit: Any,
    duration_ms: float,
    result: str,
    logical_request_id: str,
    attempt_index: int,
    total_attempts: int,
    response: Any = None,
    method: str = "GET",
    ret_code: int | None = None,
    ret_msg: str = "",
    retry_reason: str = "none",
) -> None:
    """Record exactly one HTTP attempt as a BYBIT_API_TELEMETRY_V1 record.

    Fail-open by contract: telemetry is strictly observational, so any
    failure here is swallowed and can never fail or alter a fetch. Only the
    allowlisted scalars reach telemetry — the response is reduced to its
    HTTP status and X-Bapi-Limit* headers, and request payloads, credentials
    and auth headers are never passed in.
    """
    try:
        telemetry.record_http_attempt(
            endpoint=endpoint, caller=caller, symbol=symbol,
            interval=str(interval), limit=int(limit),
            duration_ms=duration_ms, result=result, ret_msg=str(ret_msg),
            logical_request_id=logical_request_id,
            attempt_index=attempt_index,
            total_attempts=total_attempts,
            method=method,
            ret_code=ret_code,
            retry_reason=retry_reason,
            response=response,
        )
    except Exception:
        pass


def _safe_exc_category(exc: BaseException) -> str:
    """Secret-free telemetry label for a failure: the exception TYPE only.

    Exception messages may embed URLs, auth headers, API keys, signatures or
    the signed request payload, so no ``str(exc)``/``repr(exc)`` may ever
    reach a telemetry record (P1 security follow-up, review PR #232 /
    Issue #228). The exception object itself is never modified or wrapped —
    callers re-raise the identical object with its original traceback.
    """
    return type(exc).__name__


def _response_status(response: Any) -> int | None:
    """Measured HTTP status of a response (None when not exposed)."""
    try:
        from app.exchange.api_telemetry import extract_response_meta
        return extract_response_meta(response).status_code
    except Exception:
        return None


def _http_failure_label(response: Any) -> str:
    """Secret-free label for an HTTP-level failure: ``HTTP <status>``.

    Derived from the measured response status only — never from exception
    text — so the legacy ``retMsg=HTTP <status>`` output stays byte-compatible
    while exception messages can never leak into it. Falls back to the bare
    ``HTTP_ERROR`` category when the response exposes no measurable status.
    """
    status = _response_status(response)
    return f"HTTP {status}" if status is not None else "HTTP_ERROR"


def _send_audited(send, record_failure):
    """Run one HTTP send with a narrow unhandled-failure attempt recorder.

    Guarantees exactly one telemetry record per HTTP attempt (Issue #228,
    review PR #232): the instrumented branches already record
    ``TimeoutError``/``socket.timeout`` (timeout retry), ``URLError``
    (transport retry / fail-fast), ``BybitError`` (HTTP and API errors) and
    ``ValueError`` (malformed JSON), so those propagate untouched and are
    never double-recorded. Any other exception raised while sending the
    request (e.g. ``ConnectionResetError``, ``ConnectionAbortedError``, an
    unexpected transport ``OSError``) is recorded here exactly once and
    re-raised as the identical exception object without retrying. Recording
    is fail-open and can never mask the original failure.

    ``record_failure`` receives the exception only to derive a safe label
    with :func:`_safe_exc_category` (type/category name) — exception text
    must never reach telemetry.
    """
    try:
        return send()
    except (TimeoutError, socket.timeout, URLError):
        # recorded by the established timeout/transport branches
        raise
    except Exception as exc:
        record_failure(exc)
        raise


def _resolve_caller() -> str:
    """Walk the stack to find the first caller outside bybit_client.py."""
    for frame_info in inspect.stack():
        filename = frame_info.filename
        if "bybit_client" not in filename:
            parts = filename.replace("\\", "/").split("/")
            for i, part in enumerate(parts):
                if part == "app":
                    return "/".join(parts[i:])
            return filename
    return "<unknown>"


def _is_rate_limit(ret_msg: str) -> bool:
    """Check if the Bybit response indicates a rate limit."""
    return retry_budget.is_rate_limit_result(0, ret_msg)


class _Response:
    def __init__(self, response):
        self._response = response

    @property
    def status_code(self):
        """HTTP status of the wrapped response (None when not exposed)."""
        status = getattr(self._response, "status", None)
        if status is None:
            getcode = getattr(self._response, "getcode", None)
            if callable(getcode):
                try:
                    status = getcode()
                except Exception:
                    status = None
        return status

    @property
    def headers(self):
        """Headers of the wrapped response (None when not exposed)."""
        return getattr(self._response, "headers", None)

    def raise_for_status(self) -> None:
        if not 200 <= self._response.status < 300:
            raise BybitError(f"HTTP {self._response.status}")

    def json(self) -> dict[str, Any]:
        return json.loads(self._response.read().decode("utf-8"))


class _UrlSession:
    def get(self, url: str, params: dict[str, Any], timeout: int) -> _Response:
        return _Response(urlopen(url + "?" + urlencode(params), timeout=timeout))

    def post(self, url: str, data: str, headers: dict[str, str], timeout: int) -> _Response:
        request = Request(url, data=data.encode(), headers=headers, method="POST")
        return _Response(urlopen(request, timeout=timeout))


class BybitClient:
    BASE_URL = "https://api.bybit.com"
    _global_limiter: _RateLimiter | None = None
    _global_limiter_lock: type[threading.Lock] = threading.Lock

    def __init__(self, settings: Settings, session: Any | None = None):
        self.settings = settings
        self.session = session or _UrlSession()
        self._ensure_global_limiter()

    @classmethod
    def _ensure_global_limiter(cls) -> None:
        """Create the global rate limiter if it does not already exist."""
        if cls._global_limiter is None:
            with cls._global_limiter_lock():
                if cls._global_limiter is None:
                    cls._global_limiter = _RateLimiter(
                        rps=getattr(
                            Settings(),
                            "bybit_rate_limit_rps",
                            10.0,
                        )
                    )

    @classmethod
    def get_global_limiter(cls) -> _RateLimiter:
        """Return the global rate limiter, creating it lazily if needed."""
        cls._ensure_global_limiter()
        assert cls._global_limiter is not None
        return cls._global_limiter

    def _public_get(
        self, endpoint: str, *, retry_priority: str | None = None, **params: Any
    ) -> dict[str, Any]:
        """One BUDGETED logical GET request (BYBIT_RETRY_BUDGET_HARDENING_V1).

        A single attempt state machine replaces the former L1 timeout loop ×
        L2 rate-limit loop nesting (Issue #229): for **any** error sequence
        this logical request issues at most :data:`HARD_MAX_ATTEMPTS` (≤3)
        HTTP attempts, and there is no independent retry layer above it —
        consumers must never wrap this call in their own retry loop.

        * classification (``app.exchange.retry_budget``): only ``TIMEOUT``
          and ``RATE_LIMIT`` are retryable; non-rate-limit retCode,
          ``ACCESS_DENIED`` (403 / "access too frequent"), other HTTP errors,
          malformed payloads and unhandled transport errors are fail-closed —
          the identical exception object propagates;
        * HTTP 429 is a rate limit (retryable within caps), HTTP 403 is
          fail-closed;
        * per-priority rate-limit retry caps (P0 3 / P1 2 / P2 2 / P3 1)
          truncated by the unified budget and ``bybit_rate_limit_max_retries``;
        * exponential backoff, bounded and non-decreasing per sequence;
          jitter on the rate-limit class (see
          :class:`~app.exchange.retry_budget.BackoffSchedule`);
        * breaker-aware gating: with an open rate-limit cooldown P3 defers
          the request (fail-closed) and P0/P1/P2 wait, bounded;
        * global retry share per window is counted and capped (storm guard)
          — a denied retry fails closed instead of amplifying the burst;
        * deadline fail-closed: ``bybit_retry_deadline_s`` bounds the whole
          logical request.

        Telemetry parity with BYBIT_API_TELEMETRY_V1 (Issue #228): exactly
        ONE ``BYBIT_API_CALL`` record per HTTP attempt, all attempts sharing
        one ``logical_request_id`` with strictly increasing ``attempt_index``
        and ``retry_reason`` in {none, timeout, rate_limit}.
        """
        budget = max(1, min(
            retry_budget.int_setting(self.settings, "bybit_max_attempts", 3),
            retry_budget.HARD_MAX_ATTEMPTS,
        ))
        rl_max_retries = max(0, retry_budget.int_setting(
            self.settings, "bybit_rate_limit_max_retries", 0))
        timeout_backoff = retry_budget.float_setting(
            self.settings, "bybit_retry_backoff", 1.0)
        rl_backoff = retry_budget.float_setting(
            self.settings, "bybit_rate_limit_retry_backoff", 0.5)
        cooldown_s = retry_budget.float_setting(
            self.settings, "bybit_rate_limit_cooldown_s", 2.0)
        max_wait_s = retry_budget.float_setting(
            self.settings, "bybit_breaker_max_wait_s", 30.0)
        deadline_s = retry_budget.float_setting(
            self.settings, "bybit_retry_deadline_s", 60.0)

        priority = retry_budget.resolve_priority(retry_priority, endpoint)
        rl_retry_cap = min(retry_budget.rl_retry_cap(priority), rl_max_retries)

        limiter = self.get_global_limiter()
        breaker = retry_budget.get_breaker()
        storm = retry_budget.get_storm_guard()
        storm.configure(
            window_s=retry_budget.float_setting(
                self.settings, "bybit_retry_storm_window_s", 60.0),
            max_retries=retry_budget.int_setting(
                self.settings, "bybit_retry_storm_max_retries", 30),
            max_share=retry_budget.float_setting(
                self.settings, "bybit_retry_storm_max_share", 0.5),
        )
        schedule = retry_budget.BackoffSchedule(timeout_backoff, rl_backoff)

        # BYBIT_API_TELEMETRY_V1: one logical request spans every HTTP
        # attempt below; each send is recorded exactly once with a strictly
        # increasing attempt_index sharing this logical_request_id.
        logical_request_id = _new_logical_request_id()
        telemetry = _get_telemetry()
        caller = _resolve_caller()
        symbol = params.get("symbol", endpoint)
        interval = params.get("interval", "")
        limit = params.get("limit", 0)
        deadline_at = (time.monotonic() + deadline_s) if deadline_s > 0 else None

        attempt_index = 0
        rl_retries_used = 0
        timeout_retries_used = 0
        last_class: str | None = None
        last_error: BaseException | None = None

        def send_once(retry_reason: str):
            """One HTTP attempt + exactly one telemetry record (Issue #228)."""
            t_start = time.monotonic()

            def rec(result: str, *, response: Any = None,
                    ret_code: int | None = None, ret_msg: str = "") -> None:
                _record_attempt(
                    telemetry, response=response,
                    endpoint=endpoint, caller=caller, symbol=symbol,
                    interval=interval, limit=limit,
                    duration_ms=(time.monotonic() - t_start) * 1000,
                    result=result, ret_msg=ret_msg,
                    logical_request_id=logical_request_id,
                    attempt_index=attempt_index,
                    total_attempts=budget,
                    ret_code=ret_code,
                    retry_reason=retry_reason,
                )

            try:
                response = _send_audited(
                    lambda: self.session.get(
                        self.BASE_URL + endpoint, params=params,
                        timeout=self.settings.bybit_timeout,
                    ),
                    lambda exc: rec(
                        "ERROR", ret_msg=_safe_exc_category(exc)),
                )
            except (TimeoutError, socket.timeout) as exc:
                rec("TIMEOUT")
                return None, retry_budget.CLASS_TIMEOUT, exc
            except URLError as exc:
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    rec("TIMEOUT")
                    return None, retry_budget.CLASS_TIMEOUT, exc
                # Non-timeout transport failure: record the attempt and keep
                # the fail-fast behavior (no retry).
                rec("ERROR")
                raise
            status = _response_status(response)
            if status == 429:
                # HTTP 429 is a rate limit (Issue #229 classification table):
                # retryable within the budget/caps, unlike other HTTP errors.
                rec("RATE_LIMIT", response=response,
                    ret_msg=_http_failure_label(response))
                return None, retry_budget.CLASS_RATE_LIMIT, None
            try:
                response.raise_for_status()
            except Exception:
                # HTTP-level failure: record the attempt with a secret-free
                # label derived from the measured response status — never
                # str(exc) — and keep the fail-fast propagation of the
                # identical exception (403 stays fail-closed, no retry).
                rec("ERROR", response=response,
                    ret_msg=_http_failure_label(response))
                raise
            try:
                payload = response.json()
                ret_code = payload.get("retCode", 0)
                ret_msg = payload.get("retMsg", "")
            except Exception:
                # Any payload failure (malformed JSON body or an unexpected
                # decode/shape exception): record the attempt once and
                # re-raise unchanged (fail-closed, no retry).
                rec("ERROR", response=response)
                raise

            classification = retry_budget.classify_api_result(ret_code, ret_msg)
            if classification == retry_budget.CLASS_OK:
                rec("OK", response=response, ret_code=ret_code)
                return payload, retry_budget.CLASS_OK, None
            if classification == retry_budget.CLASS_RATE_LIMIT:
                rec("RATE_LIMIT", response=response,
                    ret_msg=ret_msg, ret_code=ret_code)
                return None, retry_budget.CLASS_RATE_LIMIT, None
            # API_ERROR / ACCESS_DENIED: fail-closed, NO retry (Issue #229).
            rec("ERROR", response=response, ret_msg=ret_msg, ret_code=ret_code)
            raise BybitError(ret_msg or "Bybit request failed")

        def deny(reason: str) -> None:
            """Fail closed: no further attempt (budget/cap/guard/deadline)."""
            if last_class == retry_budget.CLASS_RATE_LIMIT:
                raise BybitRateLimitError(
                    f"{symbol}: Rate limited on {endpoint} after "
                    f"{attempt_index} attempt(s) ({reason})")
            if reason == "attempt budget":
                # legacy concise message, preserved byte-for-byte
                raise BybitTimeoutError(
                    f"{symbol}: Bybit request timed out after {budget} attempts"
                ) from last_error
            raise BybitTimeoutError(
                f"{symbol}: Bybit request timed out on {endpoint} after "
                f"{attempt_index} attempt(s) ({reason})"
            ) from last_error

        while True:
            attempt_index += 1
            is_retry = attempt_index > 1
            retry_reason = (
                "none" if not is_retry else (
                    "rate_limit" if last_class == retry_budget.CLASS_RATE_LIMIT
                    else "timeout"
                )
            )

            # Fail-closed gating BEFORE the attempt (Issue #229 storm guard):
            # deadline and the bounded global retry share govern retries.
            if is_retry:
                if deadline_at is not None and time.monotonic() >= deadline_at:
                    deny("retry deadline exceeded")
                if not storm.allow_retry():
                    deny("storm guard: retry share capped")
            # Breaker-aware gating: P3 defers while a cooldown is open,
            # P0/P1/P2 wait (bounded) and only leave after the cooldown.
            breaker.gate(priority, max_wait_s=max_wait_s, sleep=time.sleep)

            limiter.acquire()
            storm.note_attempt(is_retry=is_retry)
            payload, classification, error = send_once(retry_reason)
            if classification == retry_budget.CLASS_OK:
                return payload
            last_class, last_error = classification, error
            if last_class == retry_budget.CLASS_RATE_LIMIT:
                # Open the process-local breaker cooldown: retries may only
                # leave after it (P3 defers entirely) — breaker-aware gating.
                breaker.trip(cooldown_s)

            # Unified retry decision (single place, no nested loops).
            if attempt_index >= budget:
                deny("attempt budget")
            if (last_class == retry_budget.CLASS_RATE_LIMIT
                    and rl_retries_used >= rl_retry_cap):
                deny("rate-limit retry cap")

            if last_class == retry_budget.CLASS_RATE_LIMIT:
                rl_retries_used += 1
                delay = schedule.next_delay(
                    retry_budget.CLASS_RATE_LIMIT, rl_retries_used)
                logger.warning(
                    "%s: Rate limited on %s (attempt %d/%d), retrying in %.2fs",
                    symbol, endpoint, rl_retries_used, rl_retry_cap, delay,
                )
            else:
                timeout_retries_used += 1
                delay = schedule.next_delay(
                    retry_budget.CLASS_TIMEOUT, timeout_retries_used)
                logger.warning(
                    "%s: Bybit timeout, retry %d/%d", symbol, attempt_index, budget)
            time.sleep(delay)

    def get_klines(self, symbol: str, interval: str = "5", limit: int = 200) -> list[Candle]:
        payload = self._public_get("/v5/market/kline", category="linear", symbol=symbol,
                                   interval=interval, limit=limit)
        rows = payload["result"]["list"]
        rows.reverse()
        return [Candle(int(r[0]), *(float(value) for value in r[1:6])) for r in rows]

    def get_klines_range(
        self, symbol: str, interval: str = "5", *, start_ms: int, end_ms: int,
        page_size: int = 1000, max_pages: int = 200,
    ) -> list[Candle]:
        """Read historical candles with explicit inclusive millisecond bounds.

        Separate from get_klines(): trading callers retain their existing
        latest-N behavior. Bybit returns pages newest-first; this method
        paginates the requested ``[start_ms, end_ms]`` range in bounded pages
        and returns unique ascending candle opens, never fabricating gaps.

        Error contract (fail closed):

        * a transport, API or payload-shape error RAISES and no partial
          history is returned, so a transient failure can never finalize a
          historical outcome from half a window;
        * a malformed or off-grid row is explicitly rejected (counted and
          logged) and never synthesized — the caller's completeness check
          then reports the gap instead of receiving invented OHLC;
        * pagination is bounded: at most ``max_pages`` requests per call.
        """
        try:
            minutes = int(interval)
        except (TypeError, ValueError):
            raise ValueError(
                "get_klines_range supports minute intervals only, "
                f"got {interval!r}")
        if minutes <= 0 or minutes > 1440:
            raise ValueError("get_klines_range requires a positive minute interval")
        start_ms, end_ms = int(start_ms), int(end_ms)
        if end_ms < start_ms:
            return []
        # Bybit API hard cap is 1000 rows per request.
        page_size = max(1, min(int(page_size), 1000))
        step_ms = minutes * 60_000
        out: dict[int, Candle] = {}
        rejected = 0
        pages = 0
        page_start = start_ms
        while page_start <= end_ms:
            pages += 1
            if pages > max_pages:
                # Explicit request-count bound: pagination can never spin.
                raise BybitError(
                    f"{symbol}: kline range fetch exceeded {max_pages} pages")
            # Inclusive API boundaries; each page covers at most page_size bars.
            page_end = min(end_ms, page_start + page_size * step_ms - 1)
            limit = min(page_size, (page_end - page_start) // step_ms + 1)
            payload = self._public_get(
                "/v5/market/kline", category="linear", symbol=symbol,
                interval=interval, start=page_start, end=page_end, limit=limit,
            )
            result = payload.get("result")
            rows = result.get("list") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise BybitError(
                    f"{symbol}: malformed kline payload: "
                    "result.list must be a JSON array")
            for row in rows:
                parsed = self._parse_range_row(row, step_ms=step_ms)
                if parsed is None:
                    rejected += 1
                    continue
                ts, candle = parsed
                if page_start <= ts <= page_end:
                    out[ts] = candle     # dedup on (symbol, interval, open)
            # Explicit forward progress regardless of empty/duplicate pages.
            page_start = page_end + 1
        if rejected:
            logger.warning(
                "%s: rejected %d malformed/off-grid kline rows in range fetch",
                symbol, rejected)
        return [out[ts] for ts in sorted(out)]

    @staticmethod
    def _parse_range_row(
        row: Any, *, step_ms: int,
    ) -> tuple[int, Candle] | None:
        """Validate one raw kline row; ``None`` means reject, never synthesize.

        A row must be a 6+ element sequence with an integer open timestamp on
        the interval grid and finite numeric OHLC values.
        """
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            return None
        try:
            ts = int(row[0])
            values = [float(value) for value in row[1:6]]
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if ts < 0 or ts % step_ms != 0:
            return None
        if not all(math.isfinite(value) for value in values):
            return None
        return ts, Candle(ts, *values)

    def get_open_interest(self, symbol: str, interval: str = "5min", limit: int = 200) -> list[tuple[int, float]]:
        payload = self._public_get("/v5/market/open-interest", category="linear", symbol=symbol,
                                   intervalTime=interval, limit=limit)
        rows = [(int(r["timestamp"]), float(r["openInterest"])) for r in payload["result"]["list"]]
        return sorted(rows)

    def get_funding_rate(self, symbol: str) -> float:
        payload = self._public_get("/v5/market/funding/history", category="linear", symbol=symbol, limit=1)
        rows = payload["result"]["list"]
        return float(rows[0]["fundingRate"]) * 100.0 if rows else 0.0

    def get_linear_instruments(self, quote_coin: str = "USDT") -> list[dict[str, Any]]:
        """Return all actively trading linear perpetuals for a quote coin."""
        instruments: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = self._public_get("/v5/market/instruments-info", **params)["result"]
            instruments.extend(
                row for row in result.get("list", [])
                if row.get("status") == "Trading"
                and row.get("quoteCoin") == quote_coin
                and row.get("contractType") == "LinearPerpetual"
            )
            next_cursor = result.get("nextPageCursor", "")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return instruments

    def get_tickers(self, category: str = "linear") -> list[dict[str, Any]]:
        """Return the latest 24-hour market statistics for a category."""
        payload = self._public_get("/v5/market/tickers", category=category)
        return payload["result"].get("list", [])

    def get_liquid_symbols(
        self,
        top_n: int = 50,
        min_turnover_24h: float = 10_000_000.0,
        min_volume_24h: float = 0.0,
        quote_coin: str = "USDT",
    ) -> list[str]:
        """Select active USDT perpetuals, ranked by their 24-hour turnover."""
        return [row["symbol"] for row in self.get_liquid_instruments(
            top_n=top_n,
            min_turnover_24h=min_turnover_24h,
            min_volume_24h=min_volume_24h,
            quote_coin=quote_coin,
        )]

    def get_liquid_instruments(
        self,
        top_n: int = 50,
        min_turnover_24h: float = 10_000_000.0,
        min_volume_24h: float = 0.0,
        quote_coin: str = "USDT",
    ) -> list[dict[str, str | float | int]]:
        """Return ranked liquid perpetuals together with selection metadata."""
        eligible = {row["symbol"] for row in self.get_linear_instruments(quote_coin)}
        liquid: list[tuple[str, float, float]] = []
        for ticker in self.get_tickers("linear"):
            symbol = ticker.get("symbol")
            try:
                turnover = float(ticker.get("turnover24h", 0))
                volume = float(ticker.get("volume24h", 0))
            except (TypeError, ValueError):
                continue
            if (symbol in eligible and turnover >= min_turnover_24h
                    and volume >= min_volume_24h):
                liquid.append((symbol, turnover, volume))
        liquid.sort(key=lambda item: (-item[1], item[0]))
        return [
            {
                "symbol": symbol,
                "turnover_24h": turnover,
                "volume_24h": volume,
                "rank": rank,
            }
            for rank, (symbol, turnover, volume) in enumerate(liquid[:top_n], start=1)
        ]

    def create_order(self, symbol: str, side: str, qty: float, order_type: str = "Market") -> dict[str, Any]:
        """Submit one order as a SINGLE non-retryable POST attempt.

        BYBIT_RETRY_BUDGET_HARDENING_V1 (Issue #229): ``POST
        /v5/order/create`` is non-idempotent — this method NEVER retries and
        no retry layer may wrap it. An identical submission whose outcome is
        indeterminate (timeout / transport failure / 5xx / unparseable body —
        the order may already exist server-side) is blocked fail-closed by
        the order-submit guard, so a caller-level retry can never double-fill.
        """
        if self.settings.trading_mode != "live" or not self.settings.live_trading_enabled:
            raise RuntimeError("live order rejected: both live mode and explicit switch are required")
        if not self.settings.bybit_api_key or not self.settings.bybit_api_secret:
            raise RuntimeError("Bybit credentials are required for live trading")

        guard = retry_budget.get_order_guard()
        block_s = retry_budget.float_setting(
            self.settings, "bybit_order_resubmit_block_s", 60.0)
        fingerprint = (str(symbol), str(side), str(qty), str(order_type))
        if not guard.begin(fingerprint, block_s):
            raise BybitOrderUncertainError(
                f"{symbol}: identical {side} {qty} {order_type} order submission "
                "blocked — the previous identical submission has an unknown "
                "outcome (fail-closed, no resend)")

        timestamp = str(int(time.time() * 1000))
        body = json.dumps({"category": "linear", "symbol": symbol, "side": side,
                           "orderType": order_type, "qty": str(qty)}, separators=(",", ":"))
        recv_window = "5000"
        signature = hmac.new(self.settings.bybit_api_secret.encode(),
                             (timestamp + self.settings.bybit_api_key + recv_window + body).encode(),
                             hashlib.sha256).hexdigest()
        headers = {"X-BAPI-API-KEY": self.settings.bybit_api_key, "X-BAPI-TIMESTAMP": timestamp,
                   "X-BAPI-RECV-WINDOW": recv_window, "X-BAPI-SIGN": signature,
                   "Content-Type": "application/json"}
        # BYBIT_API_TELEMETRY_V1: instrument the POST /v5/order/create path
        # with exactly one record per HTTP attempt (always attempt 1 of 1).
        # Secret hygiene: the signed body, credentials and auth headers are
        # never passed to telemetry — only endpoint/symbol/scalars are logged.
        logical_request_id = _new_logical_request_id()
        telemetry = _get_telemetry()
        caller = _resolve_caller()
        t_start = time.monotonic()
        try:
            response = _send_audited(
                lambda: self.session.post(
                    self.BASE_URL + "/v5/order/create", data=body,
                    headers=headers, timeout=10,
                ),
                lambda exc: _record_attempt(
                    telemetry, response=None,
                    endpoint="/v5/order/create", caller=caller, symbol=symbol,
                    interval="", limit=0,
                    duration_ms=(time.monotonic() - t_start) * 1000,
                    result="ERROR",
                    ret_msg=_safe_exc_category(exc),
                    logical_request_id=logical_request_id,
                    attempt_index=1, total_attempts=1, method="POST",
                ),
            )
        except (TimeoutError, socket.timeout):
            _record_attempt(
                telemetry, response=None,
                endpoint="/v5/order/create", caller=caller, symbol=symbol,
                interval="", limit=0,
                duration_ms=(time.monotonic() - t_start) * 1000,
                result="TIMEOUT",
                logical_request_id=logical_request_id,
                attempt_index=1, total_attempts=1, method="POST",
            )
            guard.mark_indeterminate(fingerprint, block_s)
            raise
        except URLError as exc:
            _record_attempt(
                telemetry, response=None,
                endpoint="/v5/order/create", caller=caller, symbol=symbol,
                interval="", limit=0,
                duration_ms=(time.monotonic() - t_start) * 1000,
                result="TIMEOUT" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "ERROR",
                logical_request_id=logical_request_id,
                attempt_index=1, total_attempts=1, method="POST",
            )
            guard.mark_indeterminate(fingerprint, block_s)
            raise
        except Exception:
            # Unhandled transport failure: recorded exactly once by
            # _send_audited. The submission outcome is UNKNOWN — the order
            # may exist server-side — so block identical resends (fail-closed).
            guard.mark_indeterminate(fingerprint, block_s)
            raise
        try:
            response.raise_for_status()
        except Exception:
            _record_attempt(
                telemetry, response=response,
                endpoint="/v5/order/create", caller=caller, symbol=symbol,
                interval="", limit=0,
                duration_ms=(time.monotonic() - t_start) * 1000,
                result="ERROR", ret_msg=_http_failure_label(response),
                logical_request_id=logical_request_id,
                attempt_index=1, total_attempts=1, method="POST",
            )
            status = _response_status(response)
            if status is None or status >= 500:
                guard.mark_indeterminate(fingerprint, block_s)
            else:
                # Definitive HTTP rejection (4xx): no order was created.
                guard.mark_resolved(fingerprint)
            raise
        try:
            payload = response.json()
            ret_code = payload.get("retCode")
        except Exception:
            _record_attempt(
                telemetry, response=response,
                endpoint="/v5/order/create", caller=caller, symbol=symbol,
                interval="", limit=0,
                duration_ms=(time.monotonic() - t_start) * 1000,
                result="ERROR",
                logical_request_id=logical_request_id,
                attempt_index=1, total_attempts=1, method="POST",
            )
            guard.mark_indeterminate(fingerprint, block_s)
            raise
        duration_ms = (time.monotonic() - t_start) * 1000
        if ret_code != 0:
            _record_attempt(
                telemetry, response=response,
                endpoint="/v5/order/create", caller=caller, symbol=symbol,
                interval="", limit=0,
                duration_ms=duration_ms, result="ERROR",
                ret_msg=payload.get("retMsg", ""), ret_code=ret_code,
                logical_request_id=logical_request_id,
                attempt_index=1, total_attempts=1, method="POST",
            )
            # Definitive API rejection: the order was NOT created, so a later
            # identical submission is a fresh decision, not a blind retry.
            guard.mark_resolved(fingerprint)
            raise BybitError(payload.get("retMsg", "order rejected"))
        _record_attempt(
            telemetry, response=response,
            endpoint="/v5/order/create", caller=caller, symbol=symbol,
            interval="", limit=0,
            duration_ms=duration_ms, result="OK", ret_code=ret_code,
            logical_request_id=logical_request_id,
            attempt_index=1, total_attempts=1, method="POST",
        )
        guard.mark_resolved(fingerprint)
        return payload
