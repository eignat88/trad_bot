from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import random
import socket
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
# Telemetry helpers
# ---------------------------------------------------------------------------

_telemetry = None


def _get_telemetry():
    """Lazy-load the ApiTelemetry singleton (avoids circular import)."""
    global _telemetry
    if _telemetry is None:
        from app.exchange.api_telemetry import ApiTelemetry
        _telemetry = ApiTelemetry.get_instance()
    return _telemetry


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

        for attempt in range(1, attempts + 1):
            t_start = time.monotonic()
            telemetry = _get_telemetry()
            caller = _resolve_caller()
            symbol = params.get("symbol", endpoint)
            interval = params.get("interval", "")
            limit = params.get("limit", 0)

            try:
                response = self.session.get(
                    self.BASE_URL + endpoint, params=params,
                    timeout=self.settings.bybit_timeout,
                )
                response.raise_for_status()
                payload = response.json()

                duration_ms = (time.monotonic() - t_start) * 1000
                ret_code = payload.get("retCode", 0)
                ret_msg = payload.get("retMsg", "")

                if ret_code != 0:
                    # Check for rate limit before raising
                    if _is_rate_limit(ret_msg):
                        telemetry.record_call(
                            endpoint=endpoint, caller=caller, symbol=symbol,
                            interval=str(interval), limit=int(limit),
                            duration_ms=duration_ms, result="RATE_LIMIT",
                            ret_msg=ret_msg,
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
                            try:
                                retry_resp = self.session.get(
                                    self.BASE_URL + endpoint, params=params,
                                    timeout=self.settings.bybit_timeout,
                                )
                                retry_resp.raise_for_status()
                                retry_payload = retry_resp.json()
                                retry_duration = (time.monotonic() - t_retry_start) * 1000
                                retry_ret_code = retry_payload.get("retCode", 0)
                                retry_ret_msg = retry_payload.get("retMsg", "")

                                if retry_ret_code == 0:
                                    telemetry.record_call(
                                        endpoint=endpoint, caller=caller, symbol=symbol,
                                        interval=str(interval), limit=int(limit),
                                        duration_ms=retry_duration, result="OK",
                                    )
                                    return retry_payload
                                elif _is_rate_limit(retry_ret_msg):
                                    telemetry.record_call(
                                        endpoint=endpoint, caller=caller, symbol=symbol,
                                        interval=str(interval), limit=int(limit),
                                        duration_ms=retry_duration, result="RATE_LIMIT",
                                        ret_msg=retry_ret_msg,
                                    )
                                    continue  # Keep retrying within the rate-limit loop
                                else:
                                    # Non-rate-limit error after rate limit
                                    telemetry.record_call(
                                        endpoint=endpoint, caller=caller, symbol=symbol,
                                        interval=str(interval), limit=int(limit),
                                        duration_ms=retry_duration, result="ERROR",
                                        ret_msg=retry_ret_msg,
                                    )
                                    raise BybitError(retry_ret_msg or "Bybit request failed")
                            except (TimeoutError, socket.timeout, URLError):
                                retry_duration = (time.monotonic() - t_retry_start) * 1000
                                telemetry.record_call(
                                    endpoint=endpoint, caller=caller, symbol=symbol,
                                    interval=str(interval), limit=int(limit),
                                    duration_ms=retry_duration, result="TIMEOUT",
                                )
                                continue

                        # Exhausted all rate-limit retries
                        raise BybitError(
                            f"{symbol}: Rate limited on {endpoint} after "
                            f"{max_retries} retries"
                        )

                    # Non-rate-limit API error
                    telemetry.record_call(
                        endpoint=endpoint, caller=caller, symbol=symbol,
                        interval=str(interval), limit=int(limit),
                        duration_ms=duration_ms, result="ERROR",
                        ret_msg=ret_msg,
                    )
                    raise BybitError(ret_msg or "Bybit request failed")

                # Success
                telemetry.record_call(
                    endpoint=endpoint, caller=caller, symbol=symbol,
                    interval=str(interval), limit=int(limit),
                    duration_ms=duration_ms, result="OK",
                )
                return payload

            except (TimeoutError, socket.timeout) as exc:
                duration_ms = (time.monotonic() - t_start) * 1000
                telemetry.record_call(
                    endpoint=endpoint, caller=caller, symbol=symbol,
                    interval=str(interval), limit=int(limit),
                    duration_ms=duration_ms, result="TIMEOUT",
                )
                timeout_error = exc
            except URLError as exc:
                if not isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    raise
                duration_ms = (time.monotonic() - t_start) * 1000
                telemetry.record_call(
                    endpoint=endpoint, caller=caller, symbol=symbol,
                    interval=str(interval), limit=int(limit),
                    duration_ms=duration_ms, result="TIMEOUT",
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
        response = self.session.post(self.BASE_URL + "/v5/order/create", data=body, headers=headers, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise BybitError(payload.get("retMsg", "order rejected"))
        return payload
