"""Issue #154: isolated OOS sampling safety contracts (no DB / exchange needed)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.research.me_r_long_early_mae_v3 import isolated_oos as module


def candle(opened, value=100.0):
    return SimpleNamespace(
        timestamp=int(opened.timestamp() * 1000),
        open=value, high=value + 1, low=value - 1, close=value, volume=5,
    )


def test_reject_missing_universe(monkeypatch):
    monkeypatch.delenv("ME_R_V3_OOS_SYMBOLS", raising=False)
    with pytest.raises(RuntimeError, match="EXPLICIT_SYMBOL_UNIVERSE_REQUIRED"):
        module.configured_symbols()


def test_reject_injected_symbol(monkeypatch):
    monkeypatch.setenv("ME_R_V3_OOS_SYMBOLS", "BTCUSDT,ETHUSDT;DROP")
    with pytest.raises(RuntimeError, match="INVALID_SYMBOL"):
        module.configured_symbols()


def test_context_excludes_forming_candle():
    opened = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)
    candles = [candle(opened), candle(opened + timedelta(minutes=5))]
    asof = opened + timedelta(minutes=6)
    assert module.completed_only(candles, 300, asof) == (candles[0],)


class NoCallConnection:
    def cursor(self):
        raise AssertionError("No DB writes should occur")


def test_no_pre_activation_backfill(monkeypatch):
    activation = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "ACTIVATION_AT", activation)
    opened = activation - timedelta(minutes=5)
    assert module.store_latest_5m(
        NoCallConnection(), "BTCUSDT", candle(opened),
        activation + timedelta(seconds=20),
    ) == (False, None)


def test_no_late_retroactive_candle(monkeypatch):
    activation = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "ACTIVATION_AT", activation)
    opened = activation
    assert module.store_latest_5m(
        NoCallConnection(), "BTCUSDT", candle(opened),
        opened + timedelta(minutes=11),
    ) == (False, None)


def test_sql_migration_requires_exact_database():
    from pathlib import Path
    sql = Path("sql/migrations/071_me_r_v3_isolated_oos_market_data.sql").read_text()
    assert "'trad_bot_me_r_v3_oos_pg_test'" in sql
    assert "'trad_bot_me_r_v3_oos'" in sql
    assert "PRIMARY KEY (exchange, market_type, instrument_id, timeframe, open_time)" in sql
    assert "ON DELETE CASCADE" not in sql


def test_no_paper_live_route():
    from pathlib import Path
    path = Path(module.__file__)
    source = path.read_text()
    assert "create_order(" not in source
    assert "paper_engine" not in source
    assert "trad_bot_runner_152_it" not in source

def test_duplicate_candle_cannot_replay_capture():
    from pathlib import Path
    source = Path(module.__file__).read_text()
    assert "if not inserted:" in source
    assert "connection.rollback()" in source
    assert "pipeline.capture_source({" in source
    assert "connection.commit()" in source


def test_systemd_disabled_by_default():
    from pathlib import Path
    service = Path("deploy/systemd/trad-bot-me-r-v3-oos.service").read_text()
    timer = Path("deploy/systemd/trad-bot-me-r-v3-oos.timer").read_text()
    assert "ME_EARLY_MAE_V3_CAPTURE_ENABLED=false" in service
    assert "User=trad_bot_me_r_v3_oos" in service
    assert "Persistent=false" in timer
    assert "WantedBy=timers.target" in timer


def test_capture_facade_preserves_default_commit_contract():
    from app.research.me_r_long_early_mae_v3.pipeline import MeEarlyMaeV3Pipeline
    class Adapter:
        def capture(self, source, *, commit=True):
            return source["id"], commit
    pipeline = object.__new__(MeEarlyMaeV3Pipeline)
    pipeline.source_adapter = Adapter()
    assert pipeline.capture_source({"id": 7}) == (7, True)
    assert pipeline.capture_source({"id": 7}, commit=False) == (7, False)


def test_collector_owns_commit_only_after_source_capture():
    from pathlib import Path
    source = Path(module.__file__).read_text()
    assert "}, commit=False)" in source
    capture = source.index("}, commit=False)")
    assert "connection.commit()" in source[capture:capture + 100]
