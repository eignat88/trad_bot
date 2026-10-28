"""Regression coverage for historical ME SHORT A Bybit candle fetch."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.exchange.bybit_client import BybitClient
from app.models import Candle
from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (
    BybitClosedCandleSource,
)


class StubBybit:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def _public_get(self, endpoint, **params):
        self.calls.append((endpoint, params))
        data = [
            row for row in self.rows
            if params["start"] <= int(row[0]) <= params["end"]
        ]
        return {"result": {"list": list(reversed(data))[:params["limit"]]}}


def _client(stub):
    client = object.__new__(BybitClient)
    client._public_get = stub._public_get
    return client


def _row(ts):
    return [str(ts), "100", "101", "99", "100", "1"]


def test_matured_historical_window_48_of_48():
    start = datetime(2026, 10, 27, 21, 35, tzinfo=timezone.utc)
    base = int(start.timestamp() * 1000)
    stub = StubBybit([_row(base + i * 300_000) for i in range(48)])
    observer = BybitClosedCandleSource(
        _client(stub), clock=lambda: start + timedelta(hours=8))
    candles = observer.closed_candles(
        "DOGEUSDT", start=start, end=start + timedelta(hours=4))
    assert len(candles) == 48
    assert candles[0].open_time == start
    assert candles[-1].open_time == start + timedelta(minutes=235)
    assert all(call[1]["start"] >= base for call in stub.calls)
    assert all(call[1]["end"] < base + 4 * 60 * 60 * 1000 for call in stub.calls)


def test_paginated_historical_range_and_order():
    base = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp() * 1000)
    stub = StubBybit([_row(base + i * 300_000) for i in range(1002)])
    rows = _client(stub).get_klines_range(
        "BTCUSDT", start_ms=base, end_ms=base + 1002 * 300_000 - 1)
    assert len(rows) == 1002
    assert rows[0].timestamp == base
    assert rows[-1].timestamp == base + 1001 * 300_000
    assert len(stub.calls) == 2


def test_missing_history_is_not_synthesized():
    start = datetime(2026, 10, 27, 21, 35, tzinfo=timezone.utc)
    base = int(start.timestamp() * 1000)
    stub = StubBybit([_row(base + i * 300_000) for i in range(48) if i != 8])
    observer = BybitClosedCandleSource(
        _client(stub), clock=lambda: start + timedelta(hours=8))
    assert len(observer.closed_candles(
        "XRPUSDT", start=start, end=start + timedelta(hours=4))) == 47


def test_excludes_unclosed_and_future_candles():
    start = datetime(2026, 10, 27, 21, 35, tzinfo=timezone.utc)
    base = int(start.timestamp() * 1000)
    stub = StubBybit([_row(base + i * 300_000) for i in range(5)])
    observer = BybitClosedCandleSource(
        _client(stub), clock=lambda: start + timedelta(minutes=12))
    candles = observer.closed_candles(
        "BTCUSDT", start=start, end=start + timedelta(minutes=25))
    assert len(candles) == 2
    assert candles[-1].close_time == start + timedelta(minutes=10)


def test_legacy_get_klines_remains_latest_n():
    stub = StubBybit([])
    # Legacy method must remain unchanged: it does not send start/end.
    calls = []
    client = object.__new__(BybitClient)
    client._public_get = lambda endpoint, **kw: (
        calls.append(kw) or {"result": {"list": []}})
    assert client.get_klines("BTCUSDT", "5", 20) == []
    assert "start" not in calls[0] and "end" not in calls[0]
    assert calls[0]["limit"] == 20
