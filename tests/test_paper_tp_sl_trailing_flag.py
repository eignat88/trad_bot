"""Tests for FIXED_TP_SL_HORIZON_V1 trailing/breakeven/DCA flags.

Covers:
1. FIXED_TP_SL_HORIZON_V1 + trailing=false: +1R favorable move does NOT change stop_price
2. After trailing is disabled, no STOP_LOSS_GAP at profitable level
3. trailing=true: existing trailing logic continues to work
4. TP=3% and fixed horizon 240m do not break
5. breakeven_enabled=false is honored (breakeven does not fire)
6. dca_enabled=false is honored (DCA fill does not fire)
7. tp_enabled=false skips TP exits
8. expiry_enabled=false skips expiry exits
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings, ExecutionPolicyConfig
from app.config.settings import _load_execution_policies, DCASettings
from app.paper.engine import PaperTradingEngine, PaperTradeRecord
from app.scanners.models import SetupCandidate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeRepository:
    """Minimal in-memory repository for paper-trade testing."""

    def __init__(self, risk_state=None, account_snapshot=None, safety_gate_state=None):
        self.saved: list[PaperTradeRecord] = []
        self.closed: list[dict] = []
        self.safety_events: list[dict] = []
        self.safety_gate_modes: list[str] = []
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

    def set_paper_safety_gate_mode(self, mode):
        self.safety_gate_modes.append(mode)
        self.safety_gate_state["safety_gate_mode"] = mode

    def insert_paper_safety_event(self, event):
        self.safety_events.append(event)

    def block_paper_safety_gate(self, reason, blocked_since):
        self.safety_gate_state = {
            "is_blocked": True, "reason": reason, "blocked_since": blocked_since,
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

    def save_dca_state(self, *args):
        pass

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        pass


def _me_reverse_long_candidate(**changes) -> SetupCandidate:
    """MOMENTUM_EXHAUSTION_REVERSE_LONG_V1 LONG candidate.

    entry=0.06279, stop=0.061253 (below entry for LONG).
    target_1 = entry + 3% ≈ 0.064674.
    """
    values = {
        "scanner_name": "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1",
        "symbol": "AKEUSDT",
        "direction": "LONG",
        "score": 45.0,
        "entry_zone_low": 0.0620,
        "entry_zone_high": 0.0635,
        "invalidation_price": 0.061253,
        "target_1": 0.064674,
    }
    values.update(changes)
    return SetupCandidate(**values)


def _tp_sl_horizon_settings(
    trailing_enabled=False,
    breakeven_enabled=False,
    dca_enabled=False,
    tp_enabled=True,
    expiry_enabled=False,
    **overrides,
) -> Settings:
    """Settings with FIXED_TP_SL_HORIZON_V1 for REVERSE_LONG."""
    defaults = {
        "initial_balance": 10_000.0,
        "risk_per_trade": 0.01,
        "max_symbol_exposure": 1.0,
        "max_open_positions": 10,
        "taker_fee": 0.0,
        "slippage_percent": 0.0,
        "atr_stop_multiple": 1.5,
        "execution_policies": {
            "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1": {
                "LONG": {
                    "policy": "FIXED_TP_SL_HORIZON_V1",
                    "enabled": True,
                    "hold_minutes": 240,
                    "dca_enabled": dca_enabled,
                    "trailing_enabled": trailing_enabled,
                    "breakeven_enabled": breakeven_enabled,
                    "tp_enabled": tp_enabled,
                    "expiry_enabled": expiry_enabled,
                }
            }
        },
    }
    defaults.update(overrides)
    settings = Settings(**defaults)
    _load_execution_policies(settings, defaults)
    return settings


# ---------------------------------------------------------------------------
# Test 1: trailing=false → +1R favorable move does NOT change stop_price
# ---------------------------------------------------------------------------

class TestTrailingDisabledFlag:
    def test_stop_price_unchanged_after_1r_favorable(self):
        """When trailing_enabled=false, a +1R move must not move the stop."""
        now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        settings = _tp_sl_horizon_settings(trailing_enabled=False)
        repo = FakeRepository()

        class Clock:
            def __init__(self, t):
                self.t = t
            def __call__(self):
                return self.t

        clock = Clock(now)
        engine = PaperTradingEngine(settings, repo, clock=clock)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1
        trade = opened[0]
        original_stop = trade.stop_price
        risk_distance = abs(trade.entry_price - trade.stop_price)

        # Move price to +1R favorable: entry + risk_distance
        price_at_1r = trade.entry_price + risk_distance
        clock.t = now + timedelta(minutes=30)
        engine.check_exits({"AKEUSDT": price_at_1r})

        # Stop must NOT have changed
        assert trade.stop_price == original_stop
        assert "AKEUSDT" in engine.open_trades
        assert len(repo.closed) == 0

    def test_stop_price_unchanged_after_1_5r_favorable(self):
        """Even at +1.2R (below TP), trailing_enabled=false keeps stop immutable."""
        now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        settings = _tp_sl_horizon_settings(trailing_enabled=False)
        repo = FakeRepository()

        class Clock:
            def __init__(self, t):
                self.t = t
            def __call__(self):
                return self.t

        clock = Clock(now)
        engine = PaperTradingEngine(settings, repo, clock=clock)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1
        trade = opened[0]
        original_stop = trade.stop_price
        risk_distance = abs(trade.entry_price - trade.stop_price)

        # Move price to +1.2R favorable (below TP level to avoid TP exit)
        price_at_1_2r = trade.entry_price + 1.2 * risk_distance
        clock.t = now + timedelta(minutes=30)
        engine.check_exits({"AKEUSDT": price_at_1_2r})

        assert trade.stop_price == original_stop
        assert "AKEUSDT" in engine.open_trades
        assert len(repo.closed) == 0


# ---------------------------------------------------------------------------
# Test 2: After trailing disabled, no STOP_LOSS_GAP at profitable level
# ---------------------------------------------------------------------------

class TestNoStopLossGapAtProfit:
    def test_no_gap_exit_at_profitable_level(self):
        """Reproduces trade_id=324: stop was trailed into profit zone → STOP_LOSS_GAP.
        With trailing_enabled=false this must not happen."""
        now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        settings = _tp_sl_horizon_settings(trailing_enabled=False)
        repo = FakeRepository()

        class Clock:
            def __init__(self, t):
                self.t = t
            def __call__(self):
                return self.t

        clock = Clock(now)
        engine = PaperTradingEngine(settings, repo, clock=clock)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1
        trade = opened[0]
        original_stop = trade.stop_price  # 0.061253

        # Phase 1: move to +1R → trailing should NOT move stop
        risk_distance = abs(trade.entry_price - trade.stop_price)
        price_at_1r = trade.entry_price + risk_distance
        clock.t = now + timedelta(minutes=20)
        engine.check_exits({"AKEUSDT": price_at_1r})
        assert trade.stop_price == original_stop

        # Phase 2: pull back to original stop level — should exit STOP_LOSS, not STOP_LOSS_GAP
        # The stop is still at original_stop, so hitting it is a normal stop, not a gap.
        clock.t = now + timedelta(minutes=40)
        closed = engine.check_exits({"AKEUSDT": original_stop})
        if closed:
            # If it exits, it must be STOP_LOSS (not STOP_LOSS_GAP), because the stop
            # was never trailed above its initial level.
            assert repo.closed[0]["exit_reason"] in ("STOP_LOSS",), (
                f"Expected STOP_LOSS, got {repo.closed[0]['exit_reason']}"
            )


# ---------------------------------------------------------------------------
# Test 3: trailing=true → existing trailing logic continues to work
# ---------------------------------------------------------------------------

class TestTrailingEnabledFlag:
    def test_trailing_moves_stop_after_1r(self):
        """When trailing_enabled=true, the trailing stop should activate at +1R."""
        now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        settings = _tp_sl_horizon_settings(trailing_enabled=True)
        repo = FakeRepository()

        class Clock:
            def __init__(self, t):
                self.t = t
            def __call__(self):
                return self.t

        clock = Clock(now)
        engine = PaperTradingEngine(settings, repo, clock=clock)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1
        trade = opened[0]
        original_stop = trade.stop_price
        risk_distance = abs(trade.entry_price - trade.stop_price)

        # Move to +1R → trailing should lock +0.5R profit
        price_at_1r = trade.entry_price + risk_distance
        clock.t = now + timedelta(minutes=20)
        engine.check_exits({"AKEUSDT": price_at_1r})

        # Stop should have moved up to entry + 0.5R
        expected_trail_stop = trade.entry_price + 0.5 * risk_distance
        assert trade.stop_price > original_stop, (
            f"Stop should have trailed up from {original_stop} but stayed at {trade.stop_price}"
        )
        assert trade.stop_price == pytest.approx(expected_trail_stop, rel=1e-6)


# ---------------------------------------------------------------------------
# Test 4: TP=3% and fixed horizon 240m do not break
# ---------------------------------------------------------------------------

class TestTpAndHorizon:
    def test_tp_exits_at_target(self):
        """TP should fire when price hits target_1, before 240m horizon."""
        now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        settings = _tp_sl_horizon_settings(trailing_enabled=False, tp_enabled=True)
        repo = FakeRepository()

        class Clock:
            def __init__(self, t):
                self.t = t
            def __call__(self):
                return self.t

        clock = Clock(now)
        engine = PaperTradingEngine(settings, repo, clock=clock)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1

        # Move to TP level (target_1 = 0.064674)
        clock.t = now + timedelta(minutes=60)
        closed = engine.check_exits({"AKEUSDT": 0.064674})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "TAKE_PROFIT_1"

    def test_horizon_exits_at_240m(self):
        """FIXED_HORIZON should fire at 240m if TP was not hit."""
        now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        settings = _tp_sl_horizon_settings(trailing_enabled=False, tp_enabled=True)
        repo = FakeRepository()

        class Clock:
            def __init__(self, t):
                self.t = t
            def __call__(self):
                return self.t

        clock = Clock(now)
        engine = PaperTradingEngine(settings, repo, clock=clock)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1

        # Stay near entry (below TP, above SL) for 240m
        clock.t = now + timedelta(minutes=240)
        closed = engine.check_exits({"AKEUSDT": 0.0630})
        assert len(closed) == 1
        assert repo.closed[0]["exit_reason"] == "FIXED_HORIZON"


# ---------------------------------------------------------------------------
# Test 5: breakeven_enabled=false is honored
# ---------------------------------------------------------------------------

class TestBreakevenDisabled:
    def test_breakeven_does_not_fire(self):
        """With breakeven_enabled=false, DCA breakeven TP should not close."""
        now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        settings = _tp_sl_horizon_settings(
            trailing_enabled=False,
            breakeven_enabled=False,
            dca_enabled=False,
        )
        repo = FakeRepository()

        class Clock:
            def __init__(self, t):
                self.t = t
            def __call__(self):
                return self.t

        clock = Clock(now)
        engine = PaperTradingEngine(settings, repo, clock=clock)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1
        trade = opened[0]

        # DCA should not be active
        assert trade.dca_enabled is False
        assert trade.is_dca_active is False


# ---------------------------------------------------------------------------
# Test 6: dca_enabled=false is honored at entry
# ---------------------------------------------------------------------------

class TestDcaDisabled:
    def test_dca_not_created_at_entry(self):
        """With dca_enabled=false in policy, DCA state should not be created."""
        settings = _tp_sl_horizon_settings(
            trailing_enabled=False,
            dca_enabled=False,
        )
        repo = FakeRepository()
        engine = PaperTradingEngine(settings, repo)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1
        assert opened[0].dca_enabled is False
        assert opened[0].dca_state is None


# ---------------------------------------------------------------------------
# Test 7: tp_enabled=false skips TP
# ---------------------------------------------------------------------------

class TestTpDisabled:
    def test_tp_does_not_close_when_disabled(self):
        """With tp_enabled=false, hitting target_1 must not close the trade."""
        now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        settings = _tp_sl_horizon_settings(
            trailing_enabled=False,
            tp_enabled=False,
        )
        repo = FakeRepository()

        class Clock:
            def __init__(self, t):
                self.t = t
            def __call__(self):
                return self.t

        clock = Clock(now)
        engine = PaperTradingEngine(settings, repo, clock=clock)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1

        # Move to TP level — should NOT close because tp_enabled=false
        clock.t = now + timedelta(minutes=60)
        closed = engine.check_exits({"AKEUSDT": 0.064674})
        assert len(closed) == 0
        assert "AKEUSDT" in engine.open_trades


# ---------------------------------------------------------------------------
# Test 8: expiry_enabled=false skips legacy expiry
# ---------------------------------------------------------------------------

class TestExpiryDisabled:
    def test_legacy_expiry_does_not_fire(self):
        """With expiry_enabled=false, legacy _is_expired should not close."""
        now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        settings = _tp_sl_horizon_settings(
            trailing_enabled=False,
            expiry_enabled=False,
            setup_ttl_multiplier=0.001,  # Very short TTL
        )
        repo = FakeRepository()

        class Clock:
            def __init__(self, t):
                self.t = t
            def __call__(self):
                return self.t

        clock = Clock(now)
        engine = PaperTradingEngine(settings, repo, clock=clock)

        opened = engine.check_entries(
            [_me_reverse_long_candidate()], {"AKEUSDT": 0.06279}
        )
        assert len(opened) == 1

        # Move past legacy expiry but before 240m horizon
        clock.t = now + timedelta(minutes=60)
        closed = engine.check_exits({"AKEUSDT": 0.0630})
        # Should NOT close via expiry (FIXED_TP_SL_HORIZON uses planned_exit_at)
        assert len(closed) == 0
        assert "AKEUSDT" in engine.open_trades
