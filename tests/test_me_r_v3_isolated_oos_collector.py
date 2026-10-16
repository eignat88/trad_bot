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


def test_market_preflight_is_read_only_and_uses_closed_context(monkeypatch):
    from types import SimpleNamespace
    checked = []
    fake_closed = SimpleNamespace(
        signal_candle=SimpleNamespace(
            close_at=datetime(2026, 10, 15, 16, 5, tzinfo=timezone.utc)
        ),
        candles_5m=tuple(range(30)),
        candles_15m=tuple(range(40)),
        candles_1h=tuple(range(24)),
    )
    monkeypatch.setattr(module, "validate_identity", lambda: None)
    monkeypatch.setattr(module, "configured_symbols", lambda: ("BTCUSDT",))
    monkeypatch.setattr(module, "collect_context", lambda client, symbol: (
        SimpleNamespace(symbol=symbol),
        datetime(2026, 10, 15, 16, 5, 30, tzinfo=timezone.utc),
    ))
    monkeypatch.setattr(module, "build_closed_context",
                        lambda context, asof, completed: checked.append(context.symbol) or fake_closed)
    monkeypatch.setattr(module, "connect",
                        lambda: (_ for _ in ()).throw(AssertionError("DB must not be accessed")))
    result = module.market_preflight(client=object())
    assert checked == ["BTCUSDT"]
    assert result["BTCUSDT"]["delay_seconds"] == 30.0
    assert result["BTCUSDT"]["closed_5m"] == 30


def test_market_preflight_rejects_stale_source_without_db(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(module, "validate_identity", lambda: None)
    monkeypatch.setattr(module, "configured_symbols", lambda: ("BTCUSDT",))
    monkeypatch.setattr(module, "collect_context", lambda client, symbol: (
        SimpleNamespace(symbol=symbol),
        datetime(2026, 10, 15, 16, 11, tzinfo=timezone.utc),
    ))
    monkeypatch.setattr(module, "build_closed_context",
                        lambda context, asof, completed: SimpleNamespace(
                            signal_candle=SimpleNamespace(
                                close_at=datetime(2026, 10, 15, 16, 5, tzinfo=timezone.utc)
                            )
                        ))
    monkeypatch.setattr(module, "connect",
                        lambda: (_ for _ in ()).throw(AssertionError("DB must not be accessed")))
    with pytest.raises(RuntimeError, match="SOURCE_DELAY_INVALID"):
        module.market_preflight(client=object())


def test_service_and_timer_contract():
    from pathlib import Path
    service = Path("deploy/systemd/trad-bot-me-r-v3-oos.service").read_text()
    timer = Path("deploy/systemd/trad-bot-me-r-v3-oos.timer").read_text()
    assert "Type=oneshot" in service
    assert "User=trad_bot_me_r_v3_oos" in service
    assert "WorkingDirectory=/opt/trad_bot_me_r_v3_release" in service
    assert "ExecStart=/opt/trad_bot_me_r_v3_release/.venv/bin/python -m app.research.me_r_long_early_mae_v3.isolated_oos --once" in service
    assert "/opt/trad_bot/.venv/bin/python" not in service
    assert "ME_EARLY_MAE_V3_CAPTURE_ENABLED=false" in service
    assert "EnvironmentFile=/etc/trad-bot/me-r-v3-oos.env" in service
    assert "EnvironmentFile=-" not in service
    assert "OnCalendar=*-*-* *:0/5:30 UTC" in timer
    assert "Persistent=false" in timer


def test_frozen_crypto_universe_exact_match(monkeypatch):
    import hashlib
    csv = module.FROZEN_CRYPTO_UNIVERSE
    assert len(csv.split(",")) == 34
    assert len(set(csv.split(","))) == 34
    assert hashlib.sha256(csv.encode("utf-8")).hexdigest() == module.FROZEN_CRYPTO_UNIVERSE_SHA256
    monkeypatch.setenv("ME_R_V3_OOS_SYMBOLS", csv)
    assert module.configured_symbols() == tuple(csv.split(","))


def test_frozen_crypto_universe_fails_closed_on_membership_or_order(monkeypatch):
    csv = module.FROZEN_CRYPTO_UNIVERSE
    monkeypatch.setenv("ME_R_V3_OOS_SYMBOLS", csv.replace("BTCUSDT,", "", 1))
    with pytest.raises(RuntimeError, match="FROZEN_UNIVERSE_MISMATCH"):
        module.configured_symbols()
    parts = csv.split(",")
    parts[0], parts[1] = parts[1], parts[0]
    monkeypatch.setenv("ME_R_V3_OOS_SYMBOLS", ",".join(parts))
    with pytest.raises(RuntimeError, match="FROZEN_UNIVERSE_MISMATCH"):
        module.configured_symbols()


def test_once_continues_after_symbol_api_failure_and_evaluates_pending(monkeypatch):
    from unittest.mock import MagicMock
    connection = MagicMock()
    pipeline = MagicMock()
    pipeline.evaluate_cycle.return_value = {"errors": 0, "finalized": 2}
    monkeypatch.setattr(module, "validate_identity", lambda: None)
    monkeypatch.setattr(module, "require_runtime_configuration", lambda now: None)
    monkeypatch.setattr(module, "require_protocol_fingerprint", lambda: None)
    monkeypatch.setattr(module, "configured_symbols", lambda: ("BTCUSDT", "ETHUSDT"))
    monkeypatch.setattr(module, "connect", lambda: connection)
    monkeypatch.setattr(module, "MeEarlyMaeV3Pipeline", lambda conn: pipeline)
    def fetch(client, symbol):
        if symbol == "BTCUSDT":
            raise TimeoutError("injected_bybit_timeout")
        return (SimpleNamespace(candles_5m=(candle(
            datetime(2026, 10, 19, 12, 5, tzinfo=timezone.utc)),)),
            datetime(2026, 10, 19, 12, 10, 25, tzinfo=timezone.utc))
    monkeypatch.setattr(module, "collect_context", fetch)
    monkeypatch.setattr(module, "detect_v1_closed",
                        lambda *args: SimpleNamespace(status="NO_SIGNAL"))
    monkeypatch.setattr(module, "store_latest_5m",
                        lambda *args: (True, 10))
    stats = module.once(client=object())
    assert stats["failed_symbols"] == 1
    assert stats["new_candles"] == 1
    assert stats["eval_finalized"] == 2
    assert connection.rollback.call_count >= 1
    pipeline.evaluate_cycle.assert_called_once()
    connection.close.assert_called_once()


def test_connect_rejects_incomplete_v3_schema_before_capture(monkeypatch):
    import sys
    from unittest.mock import MagicMock
    from types import ModuleType
    connection = MagicMock()
    connection.cursor.return_value.fetchone.side_effect = [
        (module.DATABASE, module.ROLE, module.ROLE, True),
        ("market.candle", "dds.instrument"),
    ]
    pg = ModuleType("pg8000")
    dbapi = ModuleType("pg8000.dbapi")
    dbapi.connect = lambda **kwargs: connection
    pg.dbapi = dbapi
    monkeypatch.setitem(sys.modules, "pg8000", pg)
    monkeypatch.setitem(sys.modules, "pg8000.dbapi", dbapi)
    def incomplete(conn):
        assert conn is connection
        raise RuntimeError("ME_R_EARLY_MAE_V3_TRIGGERS_INCOMPLETE:missing")
    monkeypatch.setattr(module, "require_isolated_schema", incomplete)
    with pytest.raises(RuntimeError, match="TRIGGERS_INCOMPLETE"):
        module.connect()
    connection.close.assert_called_once()


def test_connect_calls_full_v3_schema_validation(monkeypatch):
    import sys
    from unittest.mock import MagicMock
    from types import ModuleType
    connection = MagicMock()
    connection.cursor.return_value.fetchone.side_effect = [
        (module.DATABASE, module.ROLE, module.ROLE, True),
        ("market.candle", "dds.instrument"),
    ]
    pg = ModuleType("pg8000")
    dbapi = ModuleType("pg8000.dbapi")
    dbapi.connect = lambda **kwargs: connection
    pg.dbapi = dbapi
    monkeypatch.setitem(sys.modules, "pg8000", pg)
    monkeypatch.setitem(sys.modules, "pg8000.dbapi", dbapi)
    called = []
    monkeypatch.setattr(module, "require_isolated_schema",
                        lambda conn: called.append(conn))
    assert module.connect() is connection
    assert called == [connection]
