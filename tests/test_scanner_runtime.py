import threading
import time

import pytest

import scanner_runner
from app.config import ScannerUniverseSettings, Settings
from app.exchange.bybit_client import BybitClient, BybitTimeoutError


class SuccessfulResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"retCode": 0, "result": {"list": []}}


def test_bybit_timeout_retries_with_linear_backoff(monkeypatch, caplog):
    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise TimeoutError("slow")
            return SuccessfulResponse()

    sleeps = []
    monkeypatch.setattr("app.exchange.bybit_client.time.sleep", sleeps.append)
    session = Session()
    settings = Settings(bybit_timeout=15, bybit_max_attempts=3, bybit_retry_backoff=1)

    assert BybitClient(settings, session).get_klines("VIRTUALUSDT") == []
    assert session.calls == 3
    assert sleeps == [1, 2]
    assert "VIRTUALUSDT: Bybit timeout, retry 1/3" in caplog.text
    assert "VIRTUALUSDT: Bybit timeout, retry 2/3" in caplog.text


def test_bybit_timeout_raises_concise_error_after_last_attempt(monkeypatch):
    class Session:
        def get(self, *args, **kwargs):
            raise TimeoutError("slow")

    monkeypatch.setattr("app.exchange.bybit_client.time.sleep", lambda delay: None)
    client = BybitClient(Settings(bybit_max_attempts=3), Session())

    with pytest.raises(BybitTimeoutError, match="VIRTUALUSDT.*3 attempts"):
        client.get_klines("VIRTUALUSDT")


def test_scan_cycle_fetches_market_data_in_parallel(monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    def build_context(client, symbol, settings):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return symbol

    class Orchestrator:
        def scan_all(self, ctx):
            return []

    monkeypatch.setattr(scanner_runner, "build_market_context", build_context)
    total, scanned, failed = scanner_runner.run_scan_cycle(
        object(), Orchestrator(), object(), ["A", "B", "C", "D"],
        Settings(scanner_workers=3),
    )

    assert total == 0
    assert scanned == 4
    assert failed == 0
    assert peak == 3


def test_get_scanner_symbols_refreshes_dynamic_universe():
    class Client:
        def get_liquid_symbols(self, **kwargs):
            assert kwargs == {
                "top_n": 2,
                "min_turnover_24h": 123,
                "min_volume_24h": 45,
                "quote_coin": "USDT",
            }
            return ["SOLUSDT", "BTCUSDT"]

    settings = Settings(scanner_universe=ScannerUniverseSettings(
        mode="dynamic", top_n=2, min_turnover_24h=123, min_volume_24h=45,
    ))

    assert scanner_runner.get_scanner_symbols(Client(), settings) == [
        "SOLUSDT", "BTCUSDT",
    ]


def test_get_scanner_symbols_rejects_empty_dynamic_universe():
    class Client:
        def get_liquid_symbols(self, **kwargs):
            return []

    settings = Settings(scanner_universe=ScannerUniverseSettings(mode="dynamic"))

    with pytest.raises(RuntimeError, match="Dynamic scanner universe is empty"):
        scanner_runner.get_scanner_symbols(Client(), settings)


def test_cycle_delay_is_measured_from_cycle_start():
    assert scanner_runner.seconds_until_next_cycle(100, 300, 283) == 117
    assert scanner_runner.seconds_until_next_cycle(100, 300, 450) == 0
