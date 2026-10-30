from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import math
import random
import socket
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from app.config import Settings
from app.models import Candle


class BybitError(RuntimeError):
    pass


class BybitTimeoutError(BybitError):
    pass


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


def _http_failure_label(response: Any) -> str:
    """Secret-free label for an HTTP-level failure: ``HTTP <status>``.

    Derived from the measured response status only — never from exception
    text — so the legacy ``retMsg=HTTP <status>`` output stays byte-compatible
    while exception messages can never leak into it. Falls back to the bare
    ``HTTP_ERROR`` category when the response exposes no measurable status.
    """
    try:
        from app.exchange.api_telemetry import extract_response_meta
        status = extract_response_meta(response).status_code
    except Exception:
        status = None
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
    lower = ret_msg.lower()
    return "too many visits" in lower or "exceeded the api rate limit" in lower


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

    def _public_get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        max_retries = getattr(self.settings, "bybit_rate_limit_max_retries", 5)
        retry_backoff = getattr(self.settings, "bybit_rate_limit_retry_backoff", 0.5)
        attempts = self.settings.bybit_max_attempts

        # Acquire a token from the global rate limiter before each HTTP request
        limiter = self.get_global_limiter()
        limiter.acquire()

        # BYBIT_API_TELEMETRY_V1: one logical request spans every HTTP attempt
        # below (initial, timeout retries and rate-limit retries). Each
        # session.get() call is recorded exactly once, with a strictly
        # increasing attempt_index sharing this logical_request_id.
        logical_request_id = _new_logical_request_id()
        attempt_budget = attempts * (1 + max_retries)
        http_attempt = 0

        for attempt in range(1, attempts + 1):
            t_start = time.monotonic()
            telemetry = _get_telemetry()
            caller = _resolve_caller()
            symbol = params.get("symbol", endpoint)
            interval = params.get("interval", "")
            limit = params.get("limit", 0)
            retry_reason = "none" if attempt == 1 else "timeout"
            http_attempt += 1
            attempt_index = http_attempt

            try:
                response = _send_audited(
                    lambda: self.session.get(
                        self.BASE_URL + endpoint, params=params,
                        timeout=self.settings.bybit_timeout,
                    ),
                    lambda exc: _record_attempt(
                        telemetry, response=None,
                        endpoint=endpoint, caller=caller, symbol=symbol,
                        interval=interval, limit=limit,
                        duration_ms=(time.monotonic() - t_start) * 1000,
                        result="ERROR",
                        ret_msg=_safe_exc_category(exc),
                        logical_request_id=logical_request_id,
                        attempt_index=attempt_index,
                        total_attempts=attempt_budget,
                        retry_reason=retry_reason,
                    ),
                )
                try:
                    response.raise_for_status()
                except Exception:
                    # HTTP-level failure: record the attempt (previously
                    # invisible) with a secret-free label derived from the
                    # measured response status — never str(exc) — and keep
                    # the fail-fast behavior.
                    _record_attempt(
                        telemetry, response=response,
                        endpoint=endpoint, caller=caller, symbol=symbol,
                        interval=interval, limit=limit,
                        duration_ms=(time.monotonic() - t_start) * 1000,
                        result="ERROR", ret_msg=_http_failure_label(response),
                        logical_request_id=logical_request_id,
                        attempt_index=attempt_index,
                        total_attempts=attempt_budget,
                        retry_reason=retry_reason,
                    )
                    raise
                try:
                    payload = response.json()
                    ret_code = payload.get("retCode", 0)
                    ret_msg = payload.get("retMsg", "")
                except Exception:
                    # Any payload failure (malformed JSON body or an
                    # unexpected decode/shape exception): record the attempt
                    # once and re-raise unchanged (no retry, as before).
                    _record_attempt(
                        telemetry, response=response,
                        endpoint=endpoint, caller=caller, symbol=symbol,
                        interval=interval, limit=limit,
                        duration_ms=(time.monotonic() - t_start) * 1000,
                        result="ERROR",
                        logical_request_id=logical_request_id,
                        attempt_index=attempt_index,
                        total_attempts=attempt_budget,
                        retry_reason=retry_reason,
                    )
                    raise

                duration_ms = (time.monotonic() - t_start) * 1000

                if ret_code != 0:
                    # Check for rate limit before raising
                    if _is_rate_limit(ret_msg):
                        _record_attempt(
                            telemetry, response=response,
                            endpoint=endpoint, caller=caller, symbol=symbol,
                            interval=interval, limit=limit,
                            duration_ms=duration_ms, result="RATE_LIMIT",
                            ret_msg=ret_msg, ret_code=ret_code,
                            logical_request_id=logical_request_id,
                            attempt_index=attempt_index,
                            total_attempts=attempt_budget,
                            retry_reason=retry_reason,
                        )
                        # Rate-limit retry with exponential backoff + jitter
                        for rl_attempt in range(1, max_retries + 1):
                            backoff = retry_backoff * (2 ** (rl_attempt - 1))
                            jitter = random.uniform(0, backoff * 0.5)
                            sleep_time = backoff + jitter
                            logger.warning(
                                "%s: Rate limited on %s (attempt %d/%d), "
                                "retrying in %.2fs",
                                symbol, endpoint, rl_attempt, max_retries,
                                sleep_time,
                            )
                            time.sleep(sleep_time)

                            # Re-acquire limiter token
                            limiter.acquire()

                            t_retry_start = time.monotonic()
                            http_attempt += 1
                            attempt_index = http_attempt
                            try:
                                retry_resp = _send_audited(
                                    lambda: self.session.get(
                                        self.BASE_URL + endpoint, params=params,
                                        timeout=self.settings.bybit_timeout,
                                    ),
                                    lambda exc: _record_attempt(
                                        telemetry, response=None,
                                        endpoint=endpoint, caller=caller,
                                        symbol=symbol, interval=interval,
                                        limit=limit,
                                        duration_ms=(time.monotonic() - t_retry_start) * 1000,
                                        result="ERROR",
                                        ret_msg=_safe_exc_category(exc),
                                        logical_request_id=logical_request_id,
                                        attempt_index=attempt_index,
                                        total_attempts=attempt_budget,
                                        retry_reason="rate_limit",
                                    ),
                                )
                                try:
                                    retry_resp.raise_for_status()
                                except Exception:
                                    _record_attempt(
                                        telemetry, response=retry_resp,
                                        endpoint=endpoint, caller=caller,
                                        symbol=symbol, interval=interval,
                                        limit=limit,
                                        duration_ms=(time.monotonic() - t_retry_start) * 1000,
                                        result="ERROR",
                                        ret_msg=_http_failure_label(retry_resp),
                                        logical_request_id=logical_request_id,
                                        attempt_index=attempt_index,
                                        total_attempts=attempt_budget,
                                        retry_reason="rate_limit",
                                    )
                                    raise
                                try:
                                    retry_payload = retry_resp.json()
                                    retry_ret_code = retry_payload.get("retCode", 0)
                                    retry_ret_msg = retry_payload.get("retMsg", "")
                                except Exception:
                                    _record_attempt(
                                        telemetry, response=retry_resp,
                                        endpoint=endpoint, caller=caller,
                                        symbol=symbol, interval=interval,
                                        limit=limit,
                                        duration_ms=(time.monotonic() - t_retry_start) * 1000,
                                        result="ERROR",
                                        logical_request_id=logical_request_id,
                                        attempt_index=attempt_index,
                                        total_attempts=attempt_budget,
                                        retry_reason="rate_limit",
                                    )
                                    raise
                                retry_duration = (time.monotonic() - t_retry_start) * 1000

                                if retry_ret_code == 0:
                                    _record_attempt(
                                        telemetry, response=retry_resp,
                                        endpoint=endpoint, caller=caller,
                                        symbol=symbol, interval=interval,
                                        limit=limit,
                                        duration_ms=retry_duration,
                                        result="OK", ret_code=retry_ret_code,
                                        logical_request_id=logical_request_id,
                                        attempt_index=attempt_index,
                                        total_attempts=attempt_budget,
                                        retry_reason="rate_limit",
                                    )
                                    return retry_payload
                                elif _is_rate_limit(retry_ret_msg):
                                    _record_attempt(
                                        telemetry, response=retry_resp,
                                        endpoint=endpoint, caller=caller,
                                        symbol=symbol, interval=interval,
                                        limit=limit,
                                        duration_ms=retry_duration,
                                        result="RATE_LIMIT",
                                        ret_msg=retry_ret_msg,
                                        ret_code=retry_ret_code,
                                        logical_request_id=logical_request_id,
                                        attempt_index=attempt_index,
                                        total_attempts=attempt_budget,
                                        retry_reason="rate_limit",
                                    )
                                    continue  # Keep retrying within the rate-limit loop
                                else:
                                    # Non-rate-limit error after rate limit
                                    _record_attempt(
                                        telemetry, response=retry_resp,
                                        endpoint=endpoint, caller=caller,
                                        symbol=symbol, interval=interval,
                                        limit=limit,
                                        duration_ms=retry_duration,
                                        result="ERROR",
                                        ret_msg=retry_ret_msg,
                                        ret_code=retry_ret_code,
                                        logical_request_id=logical_request_id,
                                        attempt_index=attempt_index,
                                        total_attempts=attempt_budget,
                                        retry_reason="rate_limit",
                                    )
                                    raise BybitError(retry_ret_msg or "Bybit request failed")
                            except (TimeoutError, socket.timeout, URLError):
                                retry_duration = (time.monotonic() - t_retry_start) * 1000
                                _record_attempt(
                                    telemetry, response=None,
                                    endpoint=endpoint, caller=caller,
                                    symbol=symbol, interval=interval,
                                    limit=limit,
                                    duration_ms=retry_duration,
                                    result="TIMEOUT",
                                    logical_request_id=logical_request_id,
                                    attempt_index=attempt_index,
                                    total_attempts=attempt_budget,
                                    retry_reason="rate_limit",
                                )
                                continue

                        # Exhausted all rate-limit retries
                        raise BybitError(
                            f"{symbol}: Rate limited on {endpoint} after "
                            f"{max_retries} retries"
                        )

                    # Non-rate-limit API error
                    _record_attempt(
                        telemetry, response=response,
                        endpoint=endpoint, caller=caller, symbol=symbol,
                        interval=interval, limit=limit,
                        duration_ms=duration_ms, result="ERROR",
                        ret_msg=ret_msg, ret_code=ret_code,
                        logical_request_id=logical_request_id,
                        attempt_index=attempt_index,
                        total_attempts=attempt_budget,
                        retry_reason=retry_reason,
                    )
                    raise BybitError(ret_msg or "Bybit request failed")

                # Success
                _record_attempt(
                    telemetry, response=response,
                    endpoint=endpoint, caller=caller, symbol=symbol,
                    interval=interval, limit=limit,
                    duration_ms=duration_ms, result="OK",
                    ret_code=ret_code,
                    logical_request_id=logical_request_id,
                    attempt_index=attempt_index,
                    total_attempts=attempt_budget,
                    retry_reason=retry_reason,
                )
                return payload

            except (TimeoutError, socket.timeout) as exc:
                duration_ms = (time.monotonic() - t_start) * 1000
                _record_attempt(
                    telemetry, response=None,
                    endpoint=endpoint, caller=caller, symbol=symbol,
                    interval=interval, limit=limit,
                    duration_ms=duration_ms, result="TIMEOUT",
                    logical_request_id=logical_request_id,
                    attempt_index=attempt_index,
                    total_attempts=attempt_budget,
                    retry_reason=retry_reason,
                )
                timeout_error = exc
            except URLError as exc:
                if not isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    # Non-timeout transport failure: record the attempt
                    # (previously invisible) and keep the fail-fast behavior.
                    _record_attempt(
                        telemetry, response=None,
                        endpoint=endpoint, caller=caller, symbol=symbol,
                        interval=interval, limit=limit,
                        duration_ms=(time.monotonic() - t_start) * 1000,
                        result="ERROR",
                        logical_request_id=logical_request_id,
                        attempt_index=attempt_index,
                        total_attempts=attempt_budget,
                        retry_reason=retry_reason,
                    )
                    raise
                duration_ms = (time.monotonic() - t_start) * 1000
                _record_attempt(
                    telemetry, response=None,
                    endpoint=endpoint, caller=caller, symbol=symbol,
                    interval=interval, limit=limit,
                    duration_ms=duration_ms, result="TIMEOUT",
                    logical_request_id=logical_request_id,
                    attempt_index=attempt_index,
                    total_attempts=attempt_budget,
                    retry_reason=retry_reason,
                )
                timeout_error = exc
            except BybitError:
                # Re-raise rate-limit errors that escaped the inner retry loop
                raise

            if attempt < attempts:
                logger.warning("%s: Bybit timeout, retry %d/%d", symbol, attempt, attempts)
                time.sleep(self.settings.bybit_retry_backoff * attempt)
                # Re-acquire limiter after backoff
                limiter.acquire()
                continue
            raise BybitTimeoutError(
                f"{symbol}: Bybit request timed out after {attempts} attempts"
            ) from timeout_error

        raise AssertionError("unreachable")

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
        if self.settings.trading_mode != "live" or not self.settings.live_trading_enabled:
            raise RuntimeError("live order rejected: both live mode and explicit switch are required")
        if not self.settings.bybit_api_key or not self.settings.bybit_api_secret:
            raise RuntimeError("Bybit credentials are required for live trading")
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
        # BYBIT_API_TELEMETRY_V1: instrument the previously uninstrumented
        # POST /v5/order/create path with exactly one record per HTTP attempt.
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
            raise BybitError(payload.get("retMsg", "order rejected"))
        _record_attempt(
            telemetry, response=response,
            endpoint="/v5/order/create", caller=caller, symbol=symbol,
            interval="", limit=0,
            duration_ms=duration_ms, result="OK", ret_code=ret_code,
            logical_request_id=logical_request_id,
            attempt_index=1, total_attempts=1, method="POST",
        )
        return payload
