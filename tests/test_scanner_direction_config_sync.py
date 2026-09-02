import pytest

import scanner_runner
from app.config import Settings
from app.db.repository import ScannerRepository


class _MemoryCursor:
    def __init__(self, state):
        self.state = state

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split()).upper()
        if normalized.startswith("DELETE FROM DDS.SCANNER_DIRECTION_CONFIG"):
            self.state.clear()
        elif normalized.startswith("INSERT INTO DDS.SCANNER_DIRECTION_CONFIG"):
            scanner_name, direction, enabled, block_reason, regime_whitelist = params
            self.state[(scanner_name, direction)] = {
                "enabled": enabled,
                "block_reason": block_reason,
                "regime_whitelist": regime_whitelist,
            }
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")


class _MemoryConnection:
    def __init__(self):
        self.state = {}
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _MemoryCursor(self.state)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _repository_with_memory_db():
    repository = ScannerRepository(backend="jsonl")
    repository._use_pg = True
    repository._conn = _MemoryConnection()
    return repository


def test_sync_persists_blocked_direction():
    repository = _repository_with_memory_db()

    repository.sync_scanner_direction_config(
        active_scanners=["BREAKOUT_RETEST"],
        blocked_scanner_directions={("BREAKOUT_RETEST", "LONG")},
        scanner_regime_whitelist={},
    )

    assert repository._conn.state[("BREAKOUT_RETEST", "LONG")] == {
        "enabled": False,
        "block_reason": "config_block",
        "regime_whitelist": None,
    }


def test_sync_persists_regime_direction_and_enabled_direction():
    repository = _repository_with_memory_db()

    repository.sync_scanner_direction_config(
        active_scanners=["TREND_PULLBACK_V2", "LIQUIDITY_SWEEP_CHOCH_OB"],
        blocked_scanner_directions=set(),
        scanner_regime_whitelist={
            "TREND_PULLBACK_V2": {"LONG": ("TREND_UP",)},
        },
    )

    assert repository._conn.state[("TREND_PULLBACK_V2", "LONG")] == {
        "enabled": False,
        "block_reason": "regime_filter",
        "regime_whitelist": ["TREND_UP"],
    }
    assert repository._conn.state[("LIQUIDITY_SWEEP_CHOCH_OB", "LONG")] == {
        "enabled": True,
        "block_reason": None,
        "regime_whitelist": None,
    }


def test_sync_blocked_direction_takes_priority_over_regime_filter():
    repository = _repository_with_memory_db()

    repository.sync_scanner_direction_config(
        active_scanners=["TREND_PULLBACK_V2"],
        blocked_scanner_directions={("TREND_PULLBACK_V2", "LONG")},
        scanner_regime_whitelist={
            "TREND_PULLBACK_V2": {"LONG": ("TREND_UP",)},
        },
    )

    assert repository._conn.state[("TREND_PULLBACK_V2", "LONG")] == {
        "enabled": False,
        "block_reason": "config_block",
        "regime_whitelist": None,
    }


def test_sync_updates_existing_config_removes_legacy_scanner_and_is_idempotent():
    repository = _repository_with_memory_db()
    repository._conn.state[("TREND_PULLBACK", "LONG")] = {
        "enabled": True,
        "block_reason": None,
        "regime_whitelist": None,
    }
    initial = dict(repository._conn.state)

    kwargs = {
        "active_scanners": ["VOLATILITY_COMPRESSION"],
        "blocked_scanner_directions": set(),
        "scanner_regime_whitelist": {},
    }
    repository.sync_scanner_direction_config(**kwargs)
    assert repository._conn.state[("VOLATILITY_COMPRESSION", "LONG")]["enabled"] is True
    assert all(scanner_name != "TREND_PULLBACK" for scanner_name, _ in repository._conn.state)

    repository.sync_scanner_direction_config(
        **{**kwargs, "blocked_scanner_directions": {("VOLATILITY_COMPRESSION", "LONG")}},
    )
    assert repository._conn.state[("VOLATILITY_COMPRESSION", "LONG")]["block_reason"] == "config_block"

    expected = dict(repository._conn.state)
    repository.sync_scanner_direction_config(
        **{**kwargs, "blocked_scanner_directions": {("VOLATILITY_COMPRESSION", "LONG")}},
    )
    assert repository._conn.state == expected
    assert repository._conn.state != initial


def test_runner_synchronizes_registered_scanners_before_first_cycle(monkeypatch):
    settings = Settings(
        blocked_scanner_directions=(("ACTIVE", "LONG"),),
        scanner_regime_whitelist={"ACTIVE": {"SHORT": ("TREND_DOWN",)}},
    )

    class Repository:
        def __init__(self, **kwargs):
            self.sync_args = None
            self.closed = False

        def ping(self):
            return True

        def acquire_runner_lock(self):
            return True

        def abort_stale_runs(self):
            return 0

        def sync_scanner_direction_config(self, **kwargs):
            self.sync_args = kwargs

        def close(self):
            self.closed = True

    class Orchestrator:
        scanners = {"ACTIVE": object()}

        def __init__(self, repository):
            self.repository = repository

    repository = Repository()
    monkeypatch.setattr(scanner_runner, "_load_runner_settings", lambda: settings)
    monkeypatch.setattr(scanner_runner, "BybitClient", lambda value: object())
    monkeypatch.setattr(scanner_runner, "get_scanner_universe", lambda *args: [{"symbol": "BTCUSDT"}])
    monkeypatch.setattr(scanner_runner, "ScannerRepository", lambda **kwargs: repository)
    monkeypatch.setattr(scanner_runner, "ScannerOrchestrator", Orchestrator)
    monkeypatch.setattr(scanner_runner, "setup_logging", lambda: None)
    monkeypatch.setattr(scanner_runner, "SHUTDOWN", True)

    scanner_runner.main()

    assert repository.sync_args == {
        "active_scanners": ["ACTIVE"],
        "blocked_scanner_directions": settings.blocked_scanner_directions,
        "scanner_regime_whitelist": settings.scanner_regime_whitelist,
    }
    assert repository.closed is True


def test_runner_closes_repository_when_direction_sync_fails(monkeypatch):
    class Repository:
        def __init__(self, **kwargs):
            self.closed = False

        def ping(self):
            return True

        def acquire_runner_lock(self):
            return True

        def abort_stale_runs(self):
            return 0

        def sync_scanner_direction_config(self, **kwargs):
            raise RuntimeError("database unavailable")

        def close(self):
            self.closed = True

    class Orchestrator:
        scanners = {"ACTIVE": object()}

        def __init__(self, repository):
            self.repository = repository

    repository = Repository()
    monkeypatch.setattr(scanner_runner, "_load_runner_settings", lambda: Settings())
    monkeypatch.setattr(scanner_runner, "BybitClient", lambda value: object())
    monkeypatch.setattr(scanner_runner, "get_scanner_universe", lambda *args: [{"symbol": "BTCUSDT"}])
    monkeypatch.setattr(scanner_runner, "ScannerRepository", lambda **kwargs: repository)
    monkeypatch.setattr(scanner_runner, "ScannerOrchestrator", Orchestrator)
    monkeypatch.setattr(scanner_runner, "setup_logging", lambda: None)

    with pytest.raises(SystemExit, match="scanner direction config synchronization failed"):
        scanner_runner.main()

    assert repository.closed is True
