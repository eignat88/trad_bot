import threading
import time

import pytest

import scanner_runner
from app.config import ScannerUniverseSettings, Settings
from app.exchange.bybit_client import BybitClient, BybitTimeoutError
from app.scanners.models import SetupCandidate
from app.scanners.orchestrator import ScannerOrchestrator
from app.scanners.scoring import score_candidate


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
        scanners = {"TEST": object()}

        def scan_all_with_stats(self, ctx):
            return [], {"TEST": {"candidates_found": 0, "setups_saved": 0,
                                  "errors_count": 0, "duration_ms": 1}}

    class Repository:
        def save_run_stat(self, *args, **kwargs):
            pass

    monkeypatch.setattr(scanner_runner, "build_market_context", build_context)
    total, scanned, failed = scanner_runner.run_scan_cycle(
        object(), Orchestrator(), Repository(), ["A", "B", "C", "D"], None,
        Settings(scanner_workers=3),
    )

    assert total == 0
    assert scanned == 4
    assert failed == 0
    assert peak == 3


def test_get_scanner_symbols_refreshes_dynamic_universe():
    class Client:
        def get_liquid_instruments(self, **kwargs):
            assert kwargs == {
                "top_n": 2,
                "min_turnover_24h": 123,
                "min_volume_24h": 45,
                "quote_coin": "USDT",
            }
            return [
                {"symbol": "SOLUSDT", "turnover_24h": 200, "volume_24h": 50, "rank": 1},
                {"symbol": "BTCUSDT", "turnover_24h": 150, "volume_24h": 46, "rank": 2},
            ]

    settings = Settings(scanner_universe=ScannerUniverseSettings(
        mode="dynamic", top_n=2, min_turnover_24h=123, min_volume_24h=45,
    ))

    assert scanner_runner.get_scanner_symbols(Client(), settings) == [
        "SOLUSDT", "BTCUSDT",
    ]


def test_get_scanner_symbols_rejects_empty_dynamic_universe():
    class Client:
        def get_liquid_instruments(self, **kwargs):
            return []

    settings = Settings(scanner_universe=ScannerUniverseSettings(mode="dynamic"))

    with pytest.raises(RuntimeError, match="Dynamic scanner universe is empty"):
        scanner_runner.get_scanner_symbols(Client(), settings)


def test_cycle_delay_is_measured_from_cycle_start():
    assert scanner_runner.seconds_until_next_cycle(100, 300, 283) == 117
    assert scanner_runner.seconds_until_next_cycle(100, 300, 450) == 0


def test_scan_cycle_propagates_run_id_to_all_records(monkeypatch):
    candidate = SetupCandidate(
        scanner_name="TEST", symbol="BTCUSDT",
        signal_candle_open_time=1_777_294_700_000,
    )

    class Orchestrator:
        scanners = {"TEST": object()}

        def scan_all_with_stats(self, ctx):
            return [candidate], {"TEST": {"candidates_found": 1,
                "setups_saved": 1, "errors_count": 0, "duration_ms": 2}}

    class Repository:
        def __init__(self):
            self.calls = []

        def save_setup(self, value, run_id=None):
            self.calls.append(("setup", run_id))

        def save_event(self, *args, run_id=None, **kwargs):
            self.calls.append(("event", run_id))

        def save_run_stat(self, run_id, *args, **kwargs):
            self.calls.append(("stat", run_id))

    repository = Repository()
    monkeypatch.setattr(scanner_runner, "build_market_context", lambda *args: object())

    assert scanner_runner.run_scan_cycle(
        object(), Orchestrator(), repository, ["BTCUSDT"], 42,
        Settings(scanner_workers=1),
    ) == (1, 1, 0)
    assert repository.calls == [("setup", 42), ("event", 42), ("stat", 42)]


def test_scanner_duration_preserves_sub_millisecond_precision(monkeypatch):
    class Scanner:
        def scan(self, ctx):
            return []

    orchestrator = ScannerOrchestrator(enabled_scanners=[])
    orchestrator.scanners = {"TEST": Scanner()}
    ticks = iter([10.0, 10.000184])
    monkeypatch.setattr("app.scanners.orchestrator.time.perf_counter", lambda: next(ticks))

    _, stats = orchestrator.scan_all_with_stats(object())

    assert stats["TEST"]["duration_ms"] == pytest.approx(0.184)


def test_shared_scoring_ranks_confirmation_quality():
    weak = SetupCandidate(
        scanner_name="TREND_PULLBACK", symbol="ALTUSDT", reference_price=100,
        invalidation_price=98, target_1=103,
        features={"htf_context": True, "pullback_quality": 0.1,
                  "rsi_confirmation": 0.2, "stop_distance_ok": True},
    )
    strong = SetupCandidate(
        scanner_name="TREND_PULLBACK", symbol="BTCUSDT", reference_price=100,
        invalidation_price=98, target_1=104,
        features={"htf_context": True, "pullback_quality": 0.9,
                  "rsi_confirmation": 0.8, "volume_confirmation": 1.0,
                  "stop_distance_ok": True},
    )

    assert score_candidate(strong).score > score_candidate(weak).score


def test_schema_keeps_fractional_runtime_and_run_universe_history():
    # Read through the repository package location rather than the process CWD.
    from pathlib import Path
    import app.db
    sql = (Path(app.db.__file__).parent / "schema.sql").read_text(encoding="utf-8")

    assert "duration_ms NUMERIC(12,3)" in sql
    assert "CREATE TABLE IF NOT EXISTS dds.scanner_run_instrument" in sql


def test_dynamic_universe_preserves_liquidity_metadata():
    class Client:
        def get_liquid_instruments(self, **kwargs):
            return [{"symbol": "BTCUSDT", "turnover_24h": 123.5,
                     "volume_24h": 4.25, "rank": 1}]

    universe = scanner_runner.get_scanner_universe(
        Client(), Settings(scanner_universe=ScannerUniverseSettings(mode="dynamic")),
    )

    assert universe == [{"symbol": "BTCUSDT", "turnover_24h": 123.5,
                         "volume_24h": 4.25, "rank": 1}]
