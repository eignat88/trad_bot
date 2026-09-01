from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.engine import PaperTradingEngine
from app.scanners.models import SetupCandidate


class FakeRepository:
    def __init__(self, risk_state=None, account_snapshot=None, safety_gate_state=None) -> None:
        self.saved = []
        self.closed = []
        self.risk_state = risk_state or {"daily_loss_usdt": 0.0, "consecutive_losses": 0}
        self.account_snapshot = account_snapshot
        self.safety_gate_state = safety_gate_state or {
            "is_blocked": False, "reason": None, "blocked_since": None,
        }

    def get_open_paper_trades(self):
        return []

    def get_paper_risk_state(self):
        return self.risk_state

    def get_paper_safety_gate_state(self):
        return self.safety_gate_state

    def block_paper_safety_gate(self, reason, blocked_since):
        self.safety_gate_state = {
            "is_blocked": True,
            "reason": reason,
            "blocked_since": blocked_since,
        }

    def get_latest_paper_account_snapshot(self):
        return self.account_snapshot

    def get_paper_trade_by_setup(self, setup_id):
        for index, trade in enumerate(self.saved, start=1):
            if trade.setup_id == setup_id:
                return index
        return None

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
    assert opened[0].position_size == pytest.approx(10 / 10.1, rel=1e-6)
    assert opened[0].risk_usdt == pytest.approx(10.0, abs=1e-5)
    assert engine.balance < 1_000.0  # entry fee and entry slippage booked once
    assert engine.check_exits({"BTCUSDT": 120.0})
    # The exit is filled below TP and all fees/slippage are included.
    assert repo.closed[0]["exit_price"] == 119.88
    assert repo.closed[0]["pnl_usdt"] == 19.37
    assert engine.balance == pytest.approx(1019.36636, rel=1e-6)


def test_setup_is_not_reexecuted_after_its_trade_is_closed():
    repo = FakeRepository()
    engine = PaperTradingEngine(Settings(taker_fee=0, slippage_percent=0), repo)
    candidate = _candidate()
    assert len(engine.check_entries([candidate], {"BTCUSDT": 100.0})) == 1
    engine.check_exits({"BTCUSDT": 120.0})
    assert engine.check_entries([candidate], {"BTCUSDT": 100.0}) == []
    assert len(repo.saved) == 1


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
        paper_consecutive_loss_cooldown_minutes=0,  # disable cooldown for hard-stop test
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
    # Use a 4h timeframe so the 2h timeout is well within the base TTL (8 bars × 4h = 32h).
    trade.entry_timeframe = "4h"
    trade.entered_at = datetime.now(timezone.utc) - timedelta(hours=33)

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
        slippage_percent=0.0,
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


def test_exposure_cap_recalculates_actual_risk():
    engine = PaperTradingEngine(
        Settings(initial_balance=1_000, risk_per_trade=0.10,
                 max_symbol_exposure=0.10, taker_fee=0, slippage_percent=0),
        FakeRepository(),
    )
    trade = engine.check_entries([_candidate(invalidation_price=90)], {"BTCUSDT": 100})[0]
    assert trade.position_size == 1
    assert trade.risk_usdt == 10


def test_mark_to_market_equity_and_drawdown_use_open_pnl():
    engine = PaperTradingEngine(
        Settings(initial_balance=1_000, taker_fee=0, slippage_percent=0),
        FakeRepository(),
    )
    engine.check_entries([_candidate()], {"BTCUSDT": 100})
    assert engine.snapshot({"BTCUSDT": 105})["equity"] > engine.balance
    loss = engine.snapshot({"BTCUSDT": 95})
    assert loss["equity"] < engine.balance
    assert loss["max_drawdown_pct"] > 0


def test_long_stop_gap_uses_observed_price_not_stop():
    repo = FakeRepository()
    engine = PaperTradingEngine(Settings(taker_fee=0, slippage_percent=0), repo)
    engine.check_entries([_candidate(invalidation_price=90)], {"BTCUSDT": 100})
    engine.check_exits({"BTCUSDT": 85})
    assert repo.closed[0]["exit_reason"] == "STOP_LOSS_GAP"
    assert repo.closed[0]["exit_price"] == 85


def test_short_stop_gap_uses_observed_price_not_stop():
    repo = FakeRepository()
    engine = PaperTradingEngine(Settings(taker_fee=0, slippage_percent=0), repo)
    engine.check_entries([
        _candidate(direction="SHORT", invalidation_price=110, target_1=80)
    ], {"BTCUSDT": 100})
    engine.check_exits({"BTCUSDT": 115})
    assert repo.closed[0]["exit_reason"] == "STOP_LOSS_GAP"
    assert repo.closed[0]["exit_price"] == 115


def test_severe_stop_gap_halts_subsequent_entries():
    repo = FakeRepository()
    engine = PaperTradingEngine(
        Settings(taker_fee=0, slippage_percent=0, paper_max_loss_r_per_trade=1.2),
        repo,
    )
    engine.check_entries([_candidate(invalidation_price=90)], {"BTCUSDT": 100})
    engine.check_exits({"BTCUSDT": 85})

    assert engine._gap_loss_halt
    assert engine.check_entries([_candidate()], {"BTCUSDT": 100}) == []


def test_severe_stop_gap_gate_survives_runner_restart():
    repo = FakeRepository()
    settings = Settings(taker_fee=0, slippage_percent=0, paper_severe_stop_gap_r=0.20)
    engine = PaperTradingEngine(settings, repo)
    engine.check_entries([_candidate(invalidation_price=90)], {"BTCUSDT": 100})
    engine.check_exits({"BTCUSDT": 87.5})

    persisted = repo.safety_gate_state
    assert persisted["is_blocked"] is True
    assert persisted["reason"] == "STOP_LOSS_GAP"
    assert persisted["blocked_since"] is not None

    restarted = PaperTradingEngine(settings, repo)
    assert restarted.gate_status["status"] == "BLOCKED"
    assert restarted.gate_status["reason"] == "STOP_LOSS_GAP"
    assert restarted.gate_status["since"] == persisted["blocked_since"].isoformat()
    assert restarted.check_entries(
        [_candidate(symbol="ETHUSDT")], {"ETHUSDT": 100}
    ) == []


def test_stop_gap_metrics_keep_small_market_gap_gate_open():
    repo = FakeRepository()
    engine = PaperTradingEngine(
        Settings(taker_fee=0, slippage_percent=0, paper_severe_stop_gap_r=0.20),
        repo,
    )
    engine.check_entries([_candidate(invalidation_price=90)], {"BTCUSDT": 100})
    engine.check_exits({"BTCUSDT": 89.3})  # raw loss = -1.07R; gap = 0.07R

    event = engine.last_stop_gap
    assert repo.closed[0]["exit_reason"] == "STOP_LOSS_GAP"
    assert event is not None
    assert event["raw_stop_r"] == pytest.approx(-1.07)
    assert event["expected_stop_net_r"] == pytest.approx(-1.0)
    assert event["actual_net_r"] == pytest.approx(-1.07)
    assert event["gap_r"] == pytest.approx(0.07)
    assert event["excess_execution_r"] == pytest.approx(-0.07)
    assert event["severe"] is False
    assert engine.gate_status["status"] == "OPEN"


def test_gap_just_below_market_threshold_keeps_gate_open():
    repo = FakeRepository()
    engine = PaperTradingEngine(
        Settings(
            taker_fee=0,
            slippage_percent=0,
            paper_severe_stop_gap_r=0.20,
            paper_severe_execution_extra_r=0.20,
        ),
        repo,
    )
    engine.check_entries([_candidate(invalidation_price=90)], {"BTCUSDT": 100})
    engine.check_exits({"BTCUSDT": 88.1})  # raw loss = -1.19R; gap = 0.19R

    assert engine.last_stop_gap is not None
    assert engine.last_stop_gap["raw_stop_r"] == pytest.approx(-1.19)
    assert engine.last_stop_gap["gap_r"] == pytest.approx(0.19)
    assert engine.last_stop_gap["severe"] is False
    assert engine.gate_status["status"] == "OPEN"


def test_fees_and_slippage_do_not_turn_small_stop_gap_into_severe_halt():
    repo = FakeRepository()
    engine = PaperTradingEngine(
        Settings(
            taker_fee=0.01,
            slippage_percent=0.01,
            paper_severe_stop_gap_r=0.20,
            paper_severe_execution_extra_r=0.15,
        ),
        repo,
    )
    trade = engine.check_entries([_candidate(invalidation_price=90)], {"BTCUSDT": 100})[0]
    observed = trade.stop_price - (0.07 * trade.risk_usdt / trade.position_size)
    engine.check_exits({"BTCUSDT": observed})

    event = engine.last_stop_gap
    assert repo.closed[0]["pnl_r"] < -1.20
    assert event is not None
    assert event["gap_r"] == pytest.approx(0.07)
    assert event["severe"] is False
    assert engine.gate_status["status"] == "OPEN"


def test_large_market_gap_blocks_new_entries():
    repo = FakeRepository()
    engine = PaperTradingEngine(
        Settings(taker_fee=0, slippage_percent=0, paper_severe_stop_gap_r=0.20),
        repo,
    )
    engine.check_entries([_candidate(invalidation_price=90)], {"BTCUSDT": 100})
    engine.check_exits({"BTCUSDT": 87.5})  # 0.25R through the stop

    assert engine.last_stop_gap is not None
    assert engine.last_stop_gap["severe"] is True
    assert engine.gate_status["status"] == "BLOCKED"


def test_excess_execution_loss_can_block_before_market_gap_threshold():
    repo = FakeRepository()
    engine = PaperTradingEngine(
        Settings(
            taker_fee=0,
            slippage_percent=0,
            paper_severe_stop_gap_r=0.50,
            paper_severe_execution_extra_r=0.05,
        ),
        repo,
    )
    engine.check_entries([_candidate(invalidation_price=90)], {"BTCUSDT": 100})
    engine.check_exits({"BTCUSDT": 89.0})  # 0.10R through stop, but 0.10R worse than normal stop

    assert engine.last_stop_gap is not None
    assert engine.last_stop_gap["gap_r"] == pytest.approx(0.10)
    assert engine.last_stop_gap["excess_execution_r"] == pytest.approx(-0.10)
    assert engine.last_stop_gap["severe"] is True
    assert engine.gate_status["status"] == "BLOCKED"


def test_non_stop_exits_never_activate_severe_gap_gate():
    repo = FakeRepository()
    engine = PaperTradingEngine(Settings(taker_fee=0, slippage_percent=0), repo)
    engine.check_entries([_candidate()], {"BTCUSDT": 100})
    engine.check_exits({"BTCUSDT": 120})

    assert repo.closed[0]["exit_reason"] == "TAKE_PROFIT_1"
    assert engine.gate_status["status"] == "OPEN"


def test_portfolio_exposure_limits_and_metrics():
    settings = Settings(
        initial_balance=1_000, risk_per_trade=0.5, max_symbol_exposure=1,
        max_portfolio_gross_exposure=0.2, max_portfolio_net_exposure=0.1,
        taker_fee=0, slippage_percent=0,
    )
    engine = PaperTradingEngine(settings, FakeRepository())
    trade = engine.check_entries([_candidate()], {"BTCUSDT": 100})[0]
    assert trade.position_size == 1  # net limit caps notional to $100
    exposure = engine.portfolio_exposure()
    assert exposure["gross_exposure_pct"] == pytest.approx(0.1)
    assert exposure["long_exposure_pct"] == pytest.approx(0.1)
    assert exposure["short_exposure_pct"] == 0
    assert exposure["net_exposure_pct"] == pytest.approx(0.1)


# ===================================================================
# CONSECUTIVE-LOSS COOLDOWN TESTS (P0 fix)
# ===================================================================

def _cooldown_settings(**overrides) -> Settings:
    defaults = dict(
        initial_balance=10_000.0,
        risk_per_trade=0.005,
        max_consecutive_losses=4,
        paper_consecutive_loss_cooldown_minutes=360,
        paper_max_loss_r_per_trade=2.0,
        trading_mode="paper",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeCooldownRepository:
    """Repository that tracks cooldown state in memory."""

    def __init__(self, risk_state=None):
        self.saved = []
        self.closed = []
        self.risk_state = risk_state or {
            "daily_loss_usdt": 0.0,
            "consecutive_losses": 0,
            "cooldown_until": None,
        }
        self.last_snapshot_cooldown = None

    def get_open_paper_trades(self):
        return []

    def get_paper_risk_state(self):
        return self.risk_state

    def get_latest_paper_account_snapshot(self):
        return None

    def save_paper_trade(self, trade):
        self.saved.append(trade)
        return len(self.saved)

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        pass

    def save_paper_account_snapshot(self, **kwargs):
        self.last_snapshot_cooldown = kwargs.get("cooldown_until")


def _make_loss_candidate() -> SetupCandidate:
    return _candidate()


def _simulate_n_losses(engine, n, repo):
    """Simulate n consecutive losses by entering and immediately stopping."""
    for _ in range(n):
        opened = engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
        assert len(opened) == 1
        closed = engine.check_exits({"BTCUSDT": 90.0})
        assert len(closed) == 1
        assert repo.closed[-1]["exit_reason"] in ("STOP_LOSS", "STOP_LOSS_GAP")


# TEST 1 — below limit: consecutive_losses=3, max=4 → entry allowed
def test_below_limit_entry_allowed():
    repo = FakeCooldownRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 3, "cooldown_until": None}
    )
    settings = _cooldown_settings(max_consecutive_losses=4)
    engine = PaperTradingEngine(settings, repo)

    opened = engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 1
    assert engine._cooldown_until is None


# TEST 2 — reaching limit: losses become 4 → cooldown activated, entries blocked
def test_reaching_limit_activates_cooldown():
    repo = FakeCooldownRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 3, "cooldown_until": None}
    )
    settings = _cooldown_settings(max_consecutive_losses=4)
    engine = PaperTradingEngine(settings, repo)

    # 4th loss triggers cooldown
    _simulate_n_losses(engine, 1, repo)
    assert engine._consecutive_losses == 4
    assert engine._cooldown_until is not None

    # Next cycle: entries blocked
    opened = engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 0


# TEST 3 — cooldown expires: entries unblocked, losses reset
def test_cooldown_expires_entries_unblocked():
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    repo = FakeCooldownRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 4, "cooldown_until": None}
    )
    settings = _cooldown_settings(max_consecutive_losses=4)
    engine = PaperTradingEngine(settings, repo, clock=lambda: fixed_now)

    # Manually set cooldown that already expired
    engine._consecutive_losses = 4
    engine._cooldown_until = fixed_now - timedelta(hours=1)

    opened = engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 1
    assert engine._consecutive_losses == 0
    assert engine._cooldown_until is None


# TEST 4 — restart during active cooldown: state restored
def test_restart_during_cooldown_restores_state():
    cooldown_until = datetime(2026, 1, 1, 18, 0, 0, tzinfo=timezone.utc)
    fixed_now = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    repo = FakeCooldownRepository(
        risk_state={
            "daily_loss_usdt": 0.0,
            "consecutive_losses": 4,
            "cooldown_until": cooldown_until,
        }
    )
    settings = _cooldown_settings(max_consecutive_losses=4)
    engine = PaperTradingEngine(settings, repo, clock=lambda: fixed_now)

    assert engine._consecutive_losses == 4
    assert engine._cooldown_until == cooldown_until

    # Entries blocked
    opened = engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 0


# TEST 5 — restart after expired cooldown: cleared, entries allowed
def test_restart_after_expired_cooldown_clears():
    cooldown_until = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    fixed_now = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone.utc)
    repo = FakeCooldownRepository(
        risk_state={
            "daily_loss_usdt": 0.0,
            "consecutive_losses": 4,
            "cooldown_until": cooldown_until,
        }
    )
    settings = _cooldown_settings(max_consecutive_losses=4)
    engine = PaperTradingEngine(settings, repo, clock=lambda: fixed_now)

    assert engine._consecutive_losses == 0
    assert engine._cooldown_until is None

    opened = engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 1


# TEST 6 — LIVE mode: hard stop, no cooldown
def test_live_mode_hard_stop_no_cooldown():
    repo = FakeCooldownRepository(
        risk_state={"daily_loss_usdt": 0.0, "consecutive_losses": 4, "cooldown_until": None}
    )
    settings = _cooldown_settings(trading_mode="live")
    engine = PaperTradingEngine(settings, repo)

    opened = engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 0
    assert engine._cooldown_until is None


# TEST 7 — profit before limit resets streak
def test_profit_before_limit_resets_streak():
    repo = FakeCooldownRepository()
    settings = _cooldown_settings(max_consecutive_losses=4)
    engine = PaperTradingEngine(settings, repo)

    # 2 losses
    _simulate_n_losses(engine, 2, repo)
    assert engine._consecutive_losses == 2

    # 1 win → streak resets
    opened = engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 1
    closed = engine.check_exits({"BTCUSDT": 120.0})
    assert len(closed) == 1
    assert engine._consecutive_losses == 0
    assert engine._cooldown_until is None


# TEST 8 — existing position management continues during cooldown
def test_position_management_during_cooldown():
    repo = FakeCooldownRepository()
    settings = _cooldown_settings(max_consecutive_losses=2)
    engine = PaperTradingEngine(settings, repo)

    # Open a trade (before limit)
    opened = engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
    assert len(opened) == 1

    # Simulate 2 losses to activate cooldown
    engine._consecutive_losses = 2
    engine._cooldown_until = datetime.now(timezone.utc) + timedelta(hours=6)

    # Entries blocked
    assert engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0}) == []

    # But exit check still works — position can be closed
    closed = engine.check_exits({"BTCUSDT": 85.0})
    assert len(closed) == 1
    assert "BTCUSDT" not in engine.open_trades


# TEST 9 — repeated cycles don't push cooldown_until forward
def test_cooldown_not_extended_on_each_cycle():
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    repo = FakeCooldownRepository()
    settings = _cooldown_settings(max_consecutive_losses=4)
    engine = PaperTradingEngine(settings, repo, clock=lambda: fixed_now)

    # Activate cooldown
    engine._consecutive_losses = 4
    engine._cooldown_until = fixed_now + timedelta(hours=6)

    original_until = engine._cooldown_until

    # Multiple cycles — cooldown_until must not change
    for _ in range(5):
        engine.check_entries([_make_loss_candidate()], {"BTCUSDT": 100.0})
        assert engine._cooldown_until == original_until
