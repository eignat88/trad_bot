from __future__ import annotations

import threading
from types import SimpleNamespace

from app.config import Settings
from app.paper.position_monitor import PositionMonitor
from app.research.adapters.momentum_exhaustion import SCANNER_NAME as ME_RESEARCH_SCANNER
from app.scanners.models import SetupCandidate
from app.scanners.orchestrator import ScannerOrchestrator
import paper_runner


def test_emergency_stop_file_blocks_new_paper_entries(monkeypatch):
    monkeypatch.setattr(paper_runner.Path, "exists", lambda _path: True)

    assert paper_runner._emergency_stop_requested(Settings())


def test_missing_emergency_stop_file_allows_entries(monkeypatch):
    monkeypatch.setattr(paper_runner.Path, "exists", lambda _path: False)

    assert not paper_runner._emergency_stop_requested(Settings())


def test_ready_setup_loader_preserves_entry_timeframe():
    row = {
        "setup_id": "setup", "symbol": "BTCUSDT", "scanner_name": "TREND_PULLBACK",
        "direction": "LONG", "score": 80,
        "entry_zone_low": 99, "entry_zone_high": 101, "invalidation_price": 95,
        "target_1": 105, "target_2": None, "market_regime": "TREND_UP",
        "detected_at": None, "reference_price": 100, "entry_timeframe": "15m",
    }

    class Repo:
        def load_ready_setups(self):
            return [row]

    setups = paper_runner._load_ready_setups(Repo())
    assert setups[0]["entry_timeframe"] == "15m"


def _candidate(scanner_name: str) -> SetupCandidate:
    return SetupCandidate(
        setup_id=f"setup-{scanner_name}",
        scanner_name=scanner_name,
        symbol="BTCUSDT",
        direction="LONG",
        score=80,
        entry_zone_low=99,
        entry_zone_high=101,
        invalidation_price=95,
        target_1=110,
    )


def _ready_setup(scanner_name: str, symbol: str) -> dict:
    return {
        "setup_id": f"setup-{scanner_name}",
        "symbol": symbol,
        "scanner_name": scanner_name,
        "direction": "LONG",
        "score": 80,
        "entry_zone_low": 99,
        "entry_zone_high": 101,
        "invalidation_price": 95,
        "target_1": 110,
        "target_2": None,
        "market_regime": "RANGE",
        "reference_price": 100,
        "entry_timeframe": "5m",
    }


def test_paper_disable_uses_exact_canonical_identifier():
    canonical = _candidate("MOMENTUM_EXHAUSTION")
    distinct_variants = [
        _candidate("MOMENTUM_EXHAUSTION_R"),
        _candidate("MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"),
        _candidate("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"),
        _candidate("ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"),
    ]

    allowed, disabled = paper_runner._filter_paper_entry_candidates(
        [canonical, *distinct_variants]
    )

    assert disabled == 1
    assert [candidate.scanner_name for candidate in allowed] == [
        candidate.scanner_name for candidate in distinct_variants
    ]


def test_entry_cycle_blocks_me_but_routes_other_strategy(monkeypatch):
    ready_setups = [
        _ready_setup("MOMENTUM_EXHAUSTION", "BTCUSDT"),
        _ready_setup("MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "ETHUSDT"),
    ]

    class Repo:
        def load_ready_setups(self):
            return ready_setups

        def expire_stale_setups(self, max_age_minutes):
            return 0

        def get_paper_trade_stats(self):
            return []

        def save_paper_account_snapshot(self, **_values):
            return None

    class Engine:
        def __init__(self):
            self.open_trades = {}
            self.trading_lock = threading.Lock()
            self.balance = 10_000
            self._max_drawdown = 0
            self._cooldown_until = None
            self.received = []

        def check_entries(self, candidates, _prices):
            self.received = list(candidates)
            return list(candidates)

    class AllowAllPolicy:
        def evaluate(self, *_args):
            return SimpleNamespace(allowed=True, reason_code=None)

    monkeypatch.setattr(paper_runner, "_emergency_stop_requested", lambda _settings: False)
    monkeypatch.setattr(
        paper_runner,
        "_get_prices",
        lambda _client, _symbols: {"BTCUSDT": 100.0, "ETHUSDT": 100.0},
    )
    monkeypatch.setattr(
        paper_runner.ScannerDirectionGatePolicy,
        "load_for_cycle",
        lambda *_args, **_kwargs: AllowAllPolicy(),
    )

    engine = Engine()
    stats = paper_runner.run_entry_cycle(
        engine, object(), Repo(), expectancy_filter=None, settings=Settings()
    )

    assert stats["strategy_disabled"] == 1
    assert stats["entries"] == 1
    assert [candidate.scanner_name for candidate in engine.received] == [
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2"
    ]


def test_me_scanner_and_research_registration_remain_enabled():
    assert "MOMENTUM_EXHAUSTION" in ScannerOrchestrator().scanners
    assert ME_RESEARCH_SCANNER == "MOMENTUM_EXHAUSTION"


def test_existing_me_position_still_reaches_exit_management():
    existing_trade = SimpleNamespace(scanner_name="MOMENTUM_EXHAUSTION")

    class Engine:
        def __init__(self):
            self.open_trades = {"BTCUSDT": existing_trade}
            self.trading_lock = threading.Lock()
            self.seen_prices = None

        def check_exits(self, prices, funding_rates):
            self.seen_prices = prices
            return [existing_trade]

    engine = Engine()
    monitor = PositionMonitor(
        engine=engine,
        price_fetcher=lambda symbols: {symbol: 100.0 for symbol in symbols},
    )

    assert monitor.run_once() == [existing_trade]
    assert engine.seen_prices == {"BTCUSDT": 100.0}
