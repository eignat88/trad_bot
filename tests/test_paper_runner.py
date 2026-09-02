from app.config import Settings
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
