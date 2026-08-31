from app.config import Settings
import paper_runner


def test_emergency_stop_file_blocks_new_paper_entries(monkeypatch):
    monkeypatch.setattr(paper_runner.Path, "exists", lambda _path: True)

    assert paper_runner._emergency_stop_requested(Settings())


def test_missing_emergency_stop_file_allows_entries(monkeypatch):
    monkeypatch.setattr(paper_runner.Path, "exists", lambda _path: False)

    assert not paper_runner._emergency_stop_requested(Settings())


def test_ready_setup_loader_preserves_entry_timeframe():
    row = (
        "setup", "BTCUSDT", "TREND_PULLBACK", "LONG", 80,
        99, 101, 95, 105, None, "TREND_UP", None, 100, "15m",
    )

    class Cursor:
        def execute(self, _query):
            pass

        def fetchall(self):
            return [row]

    class Repo:
        _conn = type("Connection", (), {"cursor": lambda self: Cursor()})()

    setups = paper_runner._load_ready_setups(Repo())
    assert setups[0]["entry_timeframe"] == "15m"
