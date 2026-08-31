from app.paper.cli import _load_ready_setups


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

    setups = _load_ready_setups(Repo())
    assert setups[0]["entry_timeframe"] == "15m"
