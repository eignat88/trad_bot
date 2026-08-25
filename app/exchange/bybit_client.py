from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import Settings
from app.models import Candle


class BybitError(RuntimeError):
    pass


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

    def __init__(self, settings: Settings, session: Any | None = None):
        self.settings = settings
        self.session = session or _UrlSession()

    def _public_get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        response = self.session.get(self.BASE_URL + endpoint, params=params, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") != 0:
            raise BybitError(payload.get("retMsg", "Bybit request failed"))
        return payload

    def get_klines(self, symbol: str, interval: str = "5", limit: int = 200) -> list[Candle]:
        payload = self._public_get("/v5/market/kline", category="linear", symbol=symbol,
                                   interval=interval, limit=limit)
        rows = payload["result"]["list"]
        rows.reverse()  # Bybit returns newest -> oldest; indicators require chronology.
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
        eligible = {row["symbol"] for row in self.get_linear_instruments(quote_coin)}
        liquid: list[tuple[str, float]] = []
        for ticker in self.get_tickers("linear"):
            symbol = ticker.get("symbol")
            try:
                turnover = float(ticker.get("turnover24h", 0))
                volume = float(ticker.get("volume24h", 0))
            except (TypeError, ValueError):
                continue
            if (symbol in eligible and turnover >= min_turnover_24h
                    and volume >= min_volume_24h):
                liquid.append((symbol, turnover))
        liquid.sort(key=lambda item: (-item[1], item[0]))
        return [symbol for symbol, _ in liquid[:top_n]]

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
        return payload  # Acceptance only; caller must reconcile actual order status.
