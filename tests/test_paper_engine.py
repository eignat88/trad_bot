from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.engine import PaperTradingEngine
from app.scanners.models import SetupCandidate


class FakeRepository:
    def __init__(self, risk_state=None, account_snapshot=None) -> None:
        self.saved = []
        self.closed = []
        self.risk_state = risk_state or {"daily_loss_usdt": 0.0, "consecutive_losses": 0}
        self.account_snapshot = account_snapshot

    def get_open_paper_trades(self):
        return []

    def get_paper_risk_state(self):
        return self.risk_state

    def get_latest_paper_account_snapshot(self):
        return self.account_snapshot

    def save_paper_trade(self, trade):
        self.saved.append(trade)
        return len(self.saved)

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        self.funding_update = args


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


def test_entry_and_target_exit_include_fees_and_slippage():
    repo = FakeRepository()
    settings = Settings(
        initial_balance=1_000.0,
        risk_per_trade=0.01,
        max_symbol_exposure=1.0,
        taker_fee=0.001,
        slippage_percent=0.001,
    )
    engine = PaperTradingEngine(settings, repo)

    opened = engine.check_entries([_candidate()], {"BTCUSDT": 100.0})

    assert len(opened) == 1
    assert opened[0].position_size == 1.0
    assert engine.balance == 999.9  # entry fee
    assert engine.check_exits({"BTCUSDT": 120.0})
    # The exit is filled below TP and includes an exit fee.
    assert repo.closed[0]["exit_price"] == 119.88
    assert repo.closed[0]["pnl_usdt"] == 19.76
    assert engine.balance == pytest.approx(1019.66012)


def test_invalid_geometry_and_out_of_zone_do_not_open_trade():
    repo = FakeRepository()
    engine = PaperTradingEngine(Settings(), repo)

    assert engine.check_entries(
        [_candidate(invalidation_price=101.0)],
        {"BTCUSDT": 100.0},
    ) == []
    assert engine.check_entries([_candidate()], {"BTCUSDT": 105.0}) == []
    assert repo.saved == []


def test_daily_and_consecutive_loss_limits_survive_restart():
    settings = Settings(
        initial_balance=1_000.0,
        max_daily_loss=0.03,
        max_consecutive_losses=2,
    )
    daily_limit_repo = FakeRepository({"daily_loss_usdt": 30.0, "consecutive_losses": 0})
    assert PaperTradingEngine(settings, daily_limit_repo).check_entries(
        [_candidate()], {"BTCUSDT": 100.0},
    ) == []

    streak_limit_repo = FakeRepository({"daily_loss_usdt": 0.0, "consecutive_losses": 2})
    assert PaperTradingEngine(settings, streak_limit_repo).check_entries(
        [_candidate()], {"BTCUSDT": 100.0},
    ) == []


def test_account_balance_and_drawdown_restore_from_snapshot():
    repo = FakeRepository(
        account_snapshot={"balance": 1_125.0, "equity": 1_125.0, "max_drawdown": 0.1},
    )
    engine = PaperTradingEngine(Settings(initial_balance=1_000.0), repo)

    assert engine.balance == 1_125.0
    assert engine.snapshot()["max_drawdown_pct"] == 10.0


def test_trade_expiry_uses_the_setup_entry_timeframe():
    repo = FakeRepository()
    engine = PaperTradingEngine(Settings(), repo)
    trade = engine.check_entries(
        [_candidate(entry_timeframe="4h", target_1=None)],
        {"BTCUSDT": 100.0},
    )[0]
    trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=2)

    assert engine.check_exits({"BTCUSDT": 100.0}) == []
    assert "BTCUSDT" in engine.open_trades


def test_overdue_trade_closes_at_first_recovery_price_not_stop_price():
    repo = FakeRepository()
    engine = PaperTradingEngine(Settings(slippage_percent=0.0), repo)
    trade = engine.check_entries(
        [_candidate(direction="SHORT", invalidation_price=110.0, target_1=None)],
        {"BTCUSDT": 100.0},
    )[0]
    trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=2)

    closed = engine.check_exits({"BTCUSDT": 111.0})

    assert len(closed) == 1
    assert repo.closed[0]["exit_reason"] == "EXPIRED"
    assert repo.closed[0]["exit_price"] == 111.0
    assert "BTCUSDT" not in engine.open_trades


def test_funding_is_settled_once_per_completed_interval():
    repo = FakeRepository()
    settings = Settings(
        initial_balance=1_000.0,
        risk_per_trade=0.01,
        max_symbol_exposure=1.0,
        taker_fee=0.0,
        paper_funding_interval_hours=8,
    )
    engine = PaperTradingEngine(settings, repo)
    trade = engine.check_entries([_candidate()], {"BTCUSDT": 100.0})[0]
    balance_after_entry = engine.balance
    trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=8, minutes=1)

    engine._apply_funding(trade, 0.1)
    assert trade.funding_paid == pytest.approx(0.1)
    assert engine.balance == pytest.approx(balance_after_entry - 0.1)
    assert repo.funding_update == (1, 0.1, 1)

    engine._apply_funding(trade, 0.1)
    assert trade.funding_paid == pytest.approx(0.1)
