"""Tests for paper-engine daily risk state UTC rollover.

Covers the 10 scenarios from the task specification:
  1.  daily loss below limit → midnight transition → entries still allowed
  2.  daily loss at limit → blocked → midnight → entries allowed
  3.  After midnight no closed trades → daily_loss_usdt = 0
  4.  After midnight already has closed losing trades → loss restored from DB
  5.  Restart before midnight → current day loss maintained
  6.  Restart after midnight → previous day trades excluded
  7.  MAX_CONSECUTIVE_LOSSES cooldown continues to work
  8.  max_drawdown unchanged by day rollover
  9.  max_open_positions unchanged by day rollover
 10.  READY_TO_TRADE after midnight may execute
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.paper.engine import PaperTradingEngine
from app.scanners.models import SetupCandidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candidate(**changes) -> SetupCandidate:
    values = {
        "scanner_name": "TEST",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "score": 90.0,
        "entry_zone_low": 99.0,
        "entry_zone_high": 101.0,
        "invalidation_price": 90.0,
        "target_1": 120.0,
    }
    values.update(changes)
    return SetupCandidate(**values)


class FakeRiskRepository:
    """In-memory repository whose get_paper_risk_state returns controlled data."""

    def __init__(
        self,
        risk_state: dict[str, Any] | None = None,
        account_snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.risk_state = risk_state or {
            "daily_loss_usdt": 0.0,
            "consecutive_losses": 0,
            "cooldown_until": None,
        }
        self.account_snapshot = account_snapshot
        self.saved: list = []
        self.closed: list = []
        self._get_paper_risk_state_calls: list = []

    # -- required hooks ---------------------------------------------------
    def get_open_paper_trades(self) -> list[dict]:
        return []

    def get_paper_risk_state(self) -> dict[str, Any]:
        self._get_paper_risk_state_calls.append(datetime.now(timezone.utc))
        return self.risk_state

    def get_latest_paper_account_snapshot(self):
        return self.account_snapshot

    def get_paper_trade_by_setup(self, setup_id):
        for i, t in enumerate(self.saved, 1):
            if t.setup_id == setup_id:
                return i
        return None

    def save_paper_trade(self, trade):
        self.saved.append(trade)
        return len(self.saved)

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        pass


def _settings(**overrides) -> Settings:
    defaults = dict(
        initial_balance=10_000.0,
        risk_per_trade=0.005,
        max_daily_loss=0.03,
        max_consecutive_losses=4,
        max_open_positions=5,
        paper_consecutive_loss_cooldown_minutes=5,
        trading_mode="paper",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# Scenario 1 — daily loss below limit → midnight → entries still allowed
# ---------------------------------------------------------------------------
def test_daily_loss_below_limit_midnight_rollover():
    """When daily loss is below the limit, midnight rollover keeps entries open."""
    day1 = date(2026, 8, 30)
    day2 = date(2026, 8, 31)
    t1 = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 31, 0, 0, 1, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 50.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings(initial_balance=10_000.0, max_daily_loss=0.03)  # limit = $300

    clock_values = [t1]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])
    assert engine._risk_day == day1

    # Before midnight: entries allowed (50 < 300)
    opened = engine.check_entries([_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 1

    # Advance clock past midnight
    clock_values[0] = t2

    # After midnight: repo returns 0.0 for new day
    repo.risk_state = {"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}

    opened = engine.check_entries(
        [_candidate(symbol="ETHUSDT", entry_zone_low=199.0, entry_zone_high=201.0, invalidation_price=190.0)],
        {"ETHUSDT": 200.0},
    )
    assert len(opened) == 1
    assert engine._risk_day == day2
    assert engine._daily_loss_usdt == 0.0


# ---------------------------------------------------------------------------
# Scenario 2 — daily loss at limit → blocked → midnight → entries allowed
# ---------------------------------------------------------------------------
def test_daily_loss_at_limit_blocked_then_midnight_allows():
    """Daily loss limit reached → entries blocked → midnight → entries allowed."""
    t1 = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 31, 0, 0, 1, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 300.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings(initial_balance=10_000.0, max_daily_loss=0.03)  # limit = $300

    clock_values = [t1]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])

    # Before midnight: daily loss = limit → entries blocked
    opened = engine.check_entries([_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 0

    # Advance past midnight
    clock_values[0] = t2
    repo.risk_state = {"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}

    opened = engine.check_entries(
        [_candidate(symbol="ETHUSDT", entry_zone_low=199.0, entry_zone_high=201.0, invalidation_price=190.0)],
        {"ETHUSDT": 200.0},
    )
    assert len(opened) == 1
    assert engine._daily_loss_usdt == 0.0


# ---------------------------------------------------------------------------
# Scenario 3 — After midnight no closed trades → daily_loss_usdt = 0
# ---------------------------------------------------------------------------
def test_midnight_no_closed_trades_loss_is_zero():
    """After midnight with no closed trades in the new day, loss is zero."""
    t2 = datetime(2026, 8, 31, 0, 5, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings()
    clock_values = [t2]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])
    engine._risk_day = date(2026, 8, 30)  # simulate previous day

    engine.check_entries([_candidate()], {"BTCUSDT": 100.0})
    assert engine._daily_loss_usdt == 0.0
    assert engine._risk_day == date(2026, 8, 31)


# ---------------------------------------------------------------------------
# Scenario 4 — After midnight with existing losing trades → loss restored
# ---------------------------------------------------------------------------
def test_midnight_restores_loss_from_db():
    """After midnight, if the new day already has closed losing trades, loss is restored."""
    t2 = datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 120.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings(initial_balance=10_000.0, max_daily_loss=0.03)
    clock_values = [t2]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])
    engine._risk_day = date(2026, 8, 30)  # simulate previous day
    engine._daily_loss_usdt = 300.0  # simulate previous day's loss

    engine.check_entries([_candidate()], {"BTCUSDT": 100.0})

    # Rollover reloaded from DB: loss for new day = 120
    assert engine._daily_loss_usdt == 120.0
    assert engine._risk_day == date(2026, 8, 31)


# ---------------------------------------------------------------------------
# Scenario 5 — Restart before midnight: current day loss maintained
# ---------------------------------------------------------------------------
def test_restart_before_midnight_restores_current_day_loss():
    """On restart before midnight, current day loss is correctly restored."""
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 250.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings(initial_balance=10_000.0, max_daily_loss=0.03)
    engine = PaperTradingEngine(settings, repo, clock=lambda: now)

    assert engine._daily_loss_usdt == 250.0
    assert engine._risk_day == date(2026, 8, 30)

    # Entries should be blocked (250 < 300 limit... actually 250 < 300, so allowed)
    # Let's set it above limit
    repo.risk_state = {"daily_loss_usdt": 350.0, "consecutive_losses": 0, "cooldown_until": None}
    engine2 = PaperTradingEngine(settings, repo, clock=lambda: now)
    assert engine2._daily_loss_usdt == 350.0
    opened = engine2.check_entries([_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 0


# ---------------------------------------------------------------------------
# Scenario 6 — Restart after midnight: previous day trades excluded
# ---------------------------------------------------------------------------
def test_restart_after_midnight_excludes_previous_day():
    """On restart after midnight, previous day trades do not affect new day loss."""
    now = datetime(2026, 8, 31, 0, 10, tzinfo=timezone.utc)

    # Repository returns current day (Aug 31) loss = 0
    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings()
    engine = PaperTradingEngine(settings, repo, clock=lambda: now)

    assert engine._risk_day == date(2026, 8, 31)
    assert engine._daily_loss_usdt == 0.0


# ---------------------------------------------------------------------------
# Scenario 7 — MAX_CONSECUTIVE_LOSSES cooldown still works
# ---------------------------------------------------------------------------
def test_consecutive_loss_cooldown_unaffected_by_rollover():
    """Day rollover does NOT reset consecutive_losses or cooldown state."""
    t1 = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 31, 0, 0, 1, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={
            "daily_loss_usdt": 300.0,
            "consecutive_losses": 4,
            "cooldown_until": t1 + timedelta(hours=6),
        }
    )
    settings = _settings(max_consecutive_losses=4)
    clock_values = [t1]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])

    # Before midnight: consecutive losses = 4, cooldown active → blocked
    opened = engine.check_entries([_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 0
    assert engine._consecutive_losses == 4

    # Advance past midnight — day rollover happens
    clock_values[0] = t2
    repo.risk_state = {
        "daily_loss_usdt": 0.0,
        "consecutive_losses": 4,  # still 4 — rollover does not reset this
        "cooldown_until": t1 + timedelta(hours=6),
    }

    engine.check_entries(
        [_candidate(symbol="ETHUSDT", entry_zone_low=199.0, entry_zone_high=201.0, invalidation_price=190.0)],
        {"ETHUSDT": 200.0},
    )
    # Consecutive losses still 4 — cooldown lifecycle untouched
    assert engine._consecutive_losses == 4
    # Cooldown still active → entries blocked
    assert len(engine.open_trades) == 0


# ---------------------------------------------------------------------------
# Scenario 8 — max_drawdown not changed by day rollover
# ---------------------------------------------------------------------------
def test_max_drawdown_unaffected_by_rollover():
    """Day rollover does not reset max_drawdown."""
    t1 = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 31, 0, 1, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None},
        account_snapshot={"balance": 9_500.0, "equity": 9_500.0, "max_drawdown": 0.05},
    )
    settings = _settings()
    clock_values = [t1]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])
    engine._max_drawdown = 0.07  # simulate existing drawdown

    clock_values[0] = t2
    repo.risk_state = {"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}

    engine.check_entries([_candidate()], {"BTCUSDT": 100.0})

    # Drawdown preserved — not reset by rollover
    assert engine._max_drawdown >= 0.05


# ---------------------------------------------------------------------------
# Scenario 9 — max_open_positions not affected by day rollover
# ---------------------------------------------------------------------------
def test_max_open_positions_unaffected_by_rollover():
    """Day rollover does not change max_open_positions."""
    t1 = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 31, 0, 0, 1, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings(max_open_positions=2)
    clock_values = [t1]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])

    # Open 2 positions
    engine.check_entries([_candidate()], {"BTCUSDT": 100.0})
    engine.check_entries(
        [_candidate(symbol="ETHUSDT", entry_zone_low=199.0, entry_zone_high=201.0, invalidation_price=190.0)],
        {"ETHUSDT": 200.0},
    )
    assert len(engine.open_trades) == 2

    # 3rd entry blocked before midnight
    opened = engine.check_entries(
        [_candidate(symbol="SOLUSDT", entry_zone_low=49.0, entry_zone_high=51.0, invalidation_price=40.0)],
        {"SOLUSDT": 50.0},
    )
    assert len(opened) == 0

    # Midnight rollover
    clock_values[0] = t2
    repo.risk_state = {"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}

    # Still blocked by max_open_positions (2/2) — not by daily loss
    opened = engine.check_entries(
        [_candidate(symbol="SOLUSDT", entry_zone_low=49.0, entry_zone_high=51.0, invalidation_price=40.0)],
        {"SOLUSDT": 50.0},
    )
    assert len(opened) == 0
    assert settings.max_open_positions == 2


# ---------------------------------------------------------------------------
# Scenario 10 — READY_TO_TRADE after midnight can be executed
# ---------------------------------------------------------------------------
def test_ready_to_trade_after_midnight_executes():
    """After midnight, new READY_TO_TRADE setup can be entered if all gates pass."""
    t1 = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 31, 0, 1, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings(
        initial_balance=10_000.0,
        max_daily_loss=0.03,
        max_consecutive_losses=4,
        paper_consecutive_loss_cooldown_minutes=0,  # hard stop for simplicity
    )
    clock_values = [t1]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])

    # Before midnight: blocked by daily loss
    engine._daily_loss_usdt = 300.0  # simulate reached limit in memory
    opened = engine.check_entries([_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 0

    # Midnight rollover
    clock_values[0] = t2
    repo.risk_state = {"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}

    opened = engine.check_entries([_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 1
    assert engine.open_trades["BTCUSDT"].symbol == "BTCUSDT"


# ---------------------------------------------------------------------------
# Rollover logging verification
# ---------------------------------------------------------------------------
def test_rollover_logs_previous_and_new_day(caplog):
    """Verify the rollover log message contains both days and loss values."""
    import logging

    t1 = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 31, 0, 0, 1, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 150.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings(initial_balance=10_000.0, max_daily_loss=0.03)
    clock_values = [t1]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])
    engine._daily_loss_usdt = 250.0  # simulate previous day accumulated loss

    clock_values[0] = t2
    repo.risk_state = {"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}

    with caplog.at_level(logging.INFO, logger="app.paper.engine"):
        engine.check_entries([_candidate()], {"BTCUSDT": 100.0})

    rollover_msgs = [r for r in caplog.records if "daily risk state rollover" in r.message]
    assert len(rollover_msgs) == 1
    msg = rollover_msgs[0].message
    assert "previous_day=2026-08-30" in msg
    assert "new_day=2026-08-31" in msg
    assert "previous_daily_loss_usdt=250.00" in msg
    assert "current_daily_loss_usdt=0.00" in msg
    assert "entries_enabled=True" in msg


def test_rollover_logs_restored_loss_when_nonzero(caplog):
    """When the new day already has losses, log shows restored (non-zero) value."""
    import logging

    t2 = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 120.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings(initial_balance=10_000.0, max_daily_loss=0.03)
    engine = PaperTradingEngine(settings, repo, clock=lambda: t2)
    engine._risk_day = date(2026, 8, 30)
    engine._daily_loss_usdt = 250.0

    with caplog.at_level(logging.INFO, logger="app.paper.engine"):
        engine.check_entries([_candidate()], {"BTCUSDT": 100.0})

    rollover_msgs = [r for r in caplog.records if "daily risk state rollover" in r.message]
    assert len(rollover_msgs) == 1
    msg = rollover_msgs[0].message
    assert "current_daily_loss_usdt=120.00" in msg
    assert "entries_enabled=True" in msg  # 120 < 300


# ---------------------------------------------------------------------------
# Edge: no rollover on same day
# ---------------------------------------------------------------------------
def test_no_rollover_on_same_day():
    """check_entries does not call get_paper_risk_state again if still same day."""
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 50.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings()
    engine = PaperTradingEngine(settings, repo, clock=lambda: now)

    initial_calls = len(repo._get_paper_risk_state_calls)

    # On the same day, no extra call should happen
    engine.check_entries([_candidate()], {"BTCUSDT": 100.0})
    assert len(repo._get_paper_risk_state_calls) == initial_calls


# ---------------------------------------------------------------------------
# Edge: rollover only once per new day (not on every check_entries call)
# ---------------------------------------------------------------------------
def test_rollover_only_once_per_new_day():
    """After the first check_entries on a new day, subsequent calls don't re-roll."""
    t1 = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 31, 0, 1, tzinfo=timezone.utc)

    repo = FakeRiskRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 0, "cooldown_until": None}
    )
    settings = _settings()
    clock_values = [t1]
    engine = PaperTradingEngine(settings, repo, clock=lambda: clock_values[0])

    clock_values[0] = t2

    engine.check_entries([_candidate()], {"BTCUSDT": 100.0})
    first_count = len(repo._get_paper_risk_state_calls)

    engine.check_entries([_candidate(symbol="ETHUSDT")], {"ETHUSDT": 200.0})
    # No additional call — risk_day already set to t2's date
    assert len(repo._get_paper_risk_state_calls) == first_count
