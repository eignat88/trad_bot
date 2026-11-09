"""ME_LONG_V2_INVERSE_SHORT_V1 production runtime contract (Issue #268).

No PostgreSQL and no network: the runtime runs against the in-memory store
and scripted market data, exercising the REAL ``ProductionRuntime`` +
``InverseShortContour`` code paths.

Coverage map for the Issue §5 mandatory test list:

 1. runtime starts disabled, writes nothing ........ this file (disabled)
 2. RUNNING + fingerprint + freeze allows entry .... this file (running)
 3. READY_TO_START blocks new entries ............. this file (ready_to_start)
 4. DRAINING blocks entries, keeps exits .......... this file (draining)
 5. restart in DRAINING restores exit-only ........ this file (restart_draining)
 6. stale price blocks entries .................... this file (stale/missing)
 7. missing price creates no fake execution ....... this file (stale/missing)
 8. intrabar SL/TP uses only admissible OHLC ...... this file (intrabar) +
                                                    market-data suite (closed bars)
 9. timeout per frozen protocol ................... this file (timeout)
10. funding settled idempotently .................. this file (funding) +
                                                    tests/test_me_long_v2_inverse_short_funding_draining_v1.py
11. PostgreSQL disconnect/reconnect ............... tests/test_me_long_v2_inverse_short_pg_reconnect_v1.py
                                                    + this file (store outage)
12. ambiguous commit (open + close) ............... tests/test_me_long_v2_inverse_short_ambiguous_commit_v1.py
13. close outbox replay after restart ............. tests/test_me_long_v2_inverse_short_close_durability_v1.py
14. duplicate event opens no second position ...... this file (duplicate)
15. concurrent runtime excluded ................... this file (single_instance)
16. SIGTERM keeps confirmed operations ............ this file (sigterm)
17. API rate limit / timeout / outage ............. this file (api_outage)
18. Scanner/Paper/Evaluator regression ............ executed separately (see runbook §7)
"""
from __future__ import annotations

import ast
import dataclasses
import json
import signal
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models import Candle
from app.paper.me_long_v2_inverse_short_v1 import (
    ActivationRecord,
    InMemoryInverseShortStore,
    capture_with_status,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_v1.market_data import (
    STATUS_API_ERROR,
    STATUS_OK,
    STATUS_STALE,
    MarketDataConfig,
    MarketDataSnapshot,
    MarketDataProvider,
)
from app.paper.me_long_v2_inverse_short_v1.models import (
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID,
    MAX_HOLD_MINUTES,
    ROUTE_PRE_FILTER_SOURCE,
)
from app.paper.me_long_v2_inverse_short_v1 import production_runtime as pr_runtime
from app.paper.me_long_v2_inverse_short_v1.production_runtime import (
    EXIT_ALREADY_RUNNING,
    EXIT_OK,
    InstanceLockHeld,
    ProductionRuntime,
    RuntimeInstanceLock,
    ensure_writable,
    install_signal_handlers,
    main,
)
from app.scanners.models import SetupCandidate, SetupState

FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)

ENTRY_PRICE = 100.0
# Entry at 100 with the frozen slippage: SHORT fill = 100 * (1 - 0.0005).
ENTRY_SLIPPAGE = 0.0005


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# The SHORT opens at SIGNAL_AT = 12:00:30.  Three candles matter for the
# whole-OHLC-interval admissibility rule (Codex R2 P1) — all on the 5m grid:
#   [11:55, 12:00) — CLOSED entirely BEFORE the entry;
#   [12:00, 12:05) — STRADDLES the entry (the position opened inside it,
#                    so its high/low may predate the entry);
#   [12:05, 12:10) — lies ENTIRELY AFTER the entry (first admissible one).
ENTRY_BOUNDARY_MS = _epoch_ms(
    SIGNAL_AT.replace(second=0, microsecond=0),
)                                        # 12:00:00 (candle-grid boundary)
BAR_OPEN_PRE_ENTRY_MS = ENTRY_BOUNDARY_MS - 300_000      # 11:55:00
BAR_CLOSE_PRE_ENTRY_MS = ENTRY_BOUNDARY_MS              # 12:00:00
BAR_OPEN_STRADDLE_MS = ENTRY_BOUNDARY_MS                # 12:00:00
BAR_CLOSE_STRADDLE_MS = ENTRY_BOUNDARY_MS + 300_000     # 12:05:00
BAR_OPEN_POST_ENTRY_MS = ENTRY_BOUNDARY_MS + 300_000    # 12:05:00
BAR_CLOSE_POST_ENTRY_MS = ENTRY_BOUNDARY_MS + 600_000   # 12:10:00


# ---------------------------------------------------------------------------
# Helpers (mirroring tests/test_me_long_v2_inverse_short_v1.py)
# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def _settings() -> Settings:
    return dataclasses.replace(
        Settings(),
        initial_balance=10_000.0,
        risk_per_trade=0.005,
        max_symbol_exposure=0.20,
        max_open_positions=3,
        taker_fee=0.00055,
        slippage_percent=ENTRY_SLIPPAGE,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )


def _activation(
    status: str = "RUNNING",
    fingerprint: str | None = None,
    freeze_ts: datetime | None = FREEZE_TS,
) -> ActivationRecord:
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status=status,
        protocol_fingerprint=(
            fingerprint if fingerprint is not None else protocol_fingerprint()
        ),
        freeze_ts=freeze_ts,
    )


def _store(activation: ActivationRecord | None = None) -> InMemoryInverseShortStore:
    return InMemoryInverseShortStore(
        activation if activation is not None else _activation(),
        initial_balance=10_000.0,
    )


def _candidate(symbol: str = "BTCUSDT", price: float = ENTRY_PRICE,
               detected_at: datetime = SIGNAL_AT) -> SetupCandidate:
    candle_ms = int(detected_at.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        scanner_version="1.0.0",
        symbol=symbol,
        direction="LONG",
        entry_timeframe="5m",
        detected_at=detected_at,
        setup_started_at=detected_at,
        signal_candle_open_time=candle_ms,
        reference_price=price,
        entry_zone_low=price * 0.998,
        entry_zone_high=price * 1.001,
        invalidation_price=price * 0.975,
        target_1=price * 1.03,
        score=45.0,
        market_regime="RANGE",
        state=SetupState.SETUP_READY,
        features={
            "exhaustion_magnitude": 0.01,
            "rsi_14": 70.0,
            "rsi_delta_3": 2.0,
            "entry_confirmation_rsi_rising": True,
        },
    )


def _capture(store, clock, symbol: str = "BTCUSDT", *, env=None) -> str:
    return capture_with_status(
        store, _candidate(symbol, detected_at=clock()),
        capture_route=ROUTE_PRE_FILTER_SOURCE, env=env, clock=clock,
    )


class FakeProvider:
    """Scripted market data with honest per-symbol statuses."""

    def __init__(self, *, prices=None, price_status=None, bars=None,
                 funding=None, api_errors=0, on_snapshot=None) -> None:
        self.prices = dict(prices or {})
        self.price_status = dict(price_status or {})
        self.bars = {k: dict(v) for k, v in (bars or {}).items()}
        self.funding = dict(funding or {})
        self.api_errors = api_errors
        self.on_snapshot = on_snapshot
        self.calls: list[dict] = []

    def snapshot(self, *, price_symbols, bar_symbols=(), funding_symbols=()):
        price_symbols = set(price_symbols)
        bar_symbols = set(bar_symbols)
        funding_symbols = set(funding_symbols)
        self.calls.append({
            "price_symbols": set(price_symbols),
            "bar_symbols": set(bar_symbols),
            "funding_symbols": set(funding_symbols),
        })
        if self.on_snapshot is not None:
            self.on_snapshot()
        all_symbols = price_symbols | bar_symbols | funding_symbols
        statuses = {
            symbol: {"price": "MISSING", "bar": "MISSING", "funding": "MISSING"}
            for symbol in all_symbols
        }
        prices: dict[str, float] = {}
        ages: dict[str, int] = {}
        for symbol in price_symbols:
            status = self.price_status.get(
                symbol,
                STATUS_OK if symbol in self.prices else "MISSING",
            )
            statuses[symbol]["price"] = status
            if status in (STATUS_OK, "DELAYED") and symbol in self.prices:
                prices[symbol] = float(self.prices[symbol])
                ages[symbol] = 0
        bars = {
            symbol: dict(bar) for symbol, bar in self.bars.items()
            if symbol in bar_symbols
        }
        for symbol in bars:
            statuses[symbol]["bar"] = STATUS_OK
        funding = {
            symbol: float(rate) for symbol, rate in self.funding.items()
            if symbol in funding_symbols
        }
        for symbol in funding:
            statuses[symbol]["funding"] = STATUS_OK
        return MarketDataSnapshot(
            observed_at=datetime.now(timezone.utc),
            prices=prices, bars=bars, funding_rates_percent=funding,
            price_age_ms=ages, statuses=statuses, api_errors=self.api_errors,
        )


def _runtime(store, provider, *, clock, activation_enabled=None,
             heartbeat=None, interval=0.01) -> ProductionRuntime:
    return ProductionRuntime(
        _settings(), store, provider, clock=clock,
        interval_s=interval, activation_enabled=activation_enabled,
        heartbeat_path=heartbeat,
    )


def _open_position(runtime: ProductionRuntime, provider: FakeProvider) -> None:
    """Drive one cycle that opens the SHORT for the captured event."""
    provider.prices.setdefault("BTCUSDT", ENTRY_PRICE)
    result = runtime.run(once=True)
    assert result == EXIT_OK
    assert runtime.last_telemetry["entries"] == 1


# ---------------------------------------------------------------------------
# 1. Disabled runtime
# ---------------------------------------------------------------------------
class TestRuntimeDisabled:
    def test_starts_disabled_and_writes_nothing(self, monkeypatch, tmp_path):
        clock = _Clock(SIGNAL_AT)
        store = _store()
        # The event was captured earlier, while the flag was still ON.
        assert _capture(store, clock, env={ACTIVATION_FLAG: "1"}) == "CAPTURED"
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)

        # The runtime must not capture events itself, ever.
        def _no_capture(*_args, **_kwargs):
            raise AssertionError("runtime must never write source events")

        store.save_event = _no_capture  # type: ignore[method-assign]

        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(
            store, provider, clock=clock,
            heartbeat=tmp_path / "heartbeat.json",
        )
        assert runtime.run(once=True) == EXIT_OK

        # No positions, no event state change, not even a data request.
        assert store.list_positions() == []
        events = store.list_events()
        assert len(events) == 1 and events[0].status == "CAPTURED"
        assert provider.calls == [] or provider.calls[0]["price_symbols"] == set()

        telemetry = runtime.last_telemetry
        assert telemetry["experiment_id"] == EXPERIMENT_ID
        assert telemetry["activation_mode"] == "NONE"
        assert telemetry["entries"] == 0 and telemetry["exits"] == 0
        assert "ACTIVATION:DISABLED_FLAG" in telemetry["entry_refusals"]
        # The activation row itself is untouched (second factor intact).
        row = store.load_activation()
        assert row.status == "RUNNING"
        assert row.protocol_fingerprint == protocol_fingerprint()
        assert row.freeze_ts == FREEZE_TS

        heartbeat = json.loads(
            (tmp_path / "heartbeat.json").read_text(encoding="utf-8"),
        )
        assert heartbeat["cycle"] == 1 and heartbeat["state"] == "running"
        assert heartbeat["pid"] > 0

    def test_disabled_with_open_position_still_manages_exits(
        self, monkeypatch,
    ):
        clock = _Clock(SIGNAL_AT)
        store = _store()
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock)
        _open_position(runtime, provider)

        # The flag is switched OFF afterwards: exits keep running, the
        # remaining captured event is never entered.
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)
        assert _capture(store, clock, symbol="ETHUSDT",
                        env={ACTIVATION_FLAG: "1"}) == "CAPTURED"
        provider.prices["ETHUSDT"] = ENTRY_PRICE

        provider.price_status["BTCUSDT"] = STATUS_OK
        provider.prices["BTCUSDT"] = 89.0   # through the frozen target
        result = runtime.run(once=True)
        assert result == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["exits"] == 1
        assert telemetry["exit_reasons"] == ["TAKE_PROFIT_1"]
        assert telemetry["entries"] == 0
        assert "ACTIVATION:DISABLED_FLAG" in telemetry["entry_refusals"]
        # Only the open symbol was priced (entries are not even considered).
        assert provider.calls[-1]["price_symbols"] == {"BTCUSDT"}
        assert [p.symbol for p in store.list_positions(POSITION_STATUS_OPEN)] == []
        entered = store.list_positions(POSITION_STATUS_CLOSED)
        assert len(entered) == 1
        assert store.list_events(status="CAPTURED")[0].symbol == "ETHUSDT"


# ---------------------------------------------------------------------------
# 2/3. Activation gates
# ---------------------------------------------------------------------------
class TestActivationGates:
    def test_running_with_valid_identity_allows_prospective_entry(
        self, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        assert _capture(store, clock) == "CAPTURED"
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        assert runtime.run(once=True) == EXIT_OK

        positions = store.list_positions(POSITION_STATUS_OPEN)
        assert len(positions) == 1
        position = positions[0]
        assert position.direction == "SHORT"
        assert position.symbol == "BTCUSDT"
        # Frozen geometry from the ACTUAL entry price (PR #267 protocol).
        assert position.stop_price == position.entry_price * 1.10
        assert position.target_price == position.entry_price * 0.90
        assert position.deadline_at == position.entered_at + timedelta(
            minutes=MAX_HOLD_MINUTES,
        )

        telemetry = runtime.last_telemetry
        assert telemetry["activation_status"] == "RUNNING"
        assert telemetry["activation_mode"] == "FULL"
        assert telemetry["protocol_fingerprint"] == protocol_fingerprint()
        assert telemetry["freeze_ts"] == FREEZE_TS.isoformat()
        assert telemetry["entries"] == 1
        assert telemetry["symbols_checked"] == 1
        assert telemetry["entry_refusals"] == []
        assert telemetry["market_data_age_ms"] == 0
        assert provider.calls[0]["price_symbols"] == {"BTCUSDT"}

    def test_ready_to_start_blocks_new_entries(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        _capture(store, clock)
        # Operator (or migration reset) put the row back to READY_TO_START:
        # fingerprint/freeze are NULL — fail closed, event stays CAPTURED.
        store.set_activation(
            _activation(status="READY_TO_START", fingerprint=None,
                        freeze_ts=None),
        )
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        assert runtime.run(once=True) == EXIT_OK

        assert store.list_positions() == []
        telemetry = runtime.last_telemetry
        assert telemetry["entries"] == 0
        assert telemetry["activation_status"] == "READY_TO_START"
        assert "ACTIVATION:ACTIVATION_NOT_RUNNING" in telemetry["entry_refusals"]
        assert store.list_events(status="CAPTURED")[0].symbol == "BTCUSDT"


# ---------------------------------------------------------------------------
# 4/5. DRAINING
# ---------------------------------------------------------------------------
class TestDraining:
    def test_draining_blocks_entries_but_keeps_exits(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)

        _capture(store, clock, symbol="BTCUSDT")
        _open_position(runtime, provider)
        # A second event arrives while RUNNING, then the row is drained
        # (fingerprint + freeze_ts are preserved by --drain).
        _capture(store, clock, symbol="ETHUSDT")
        provider.prices["ETHUSDT"] = ENTRY_PRICE
        store.set_activation(_activation(status="DRAINING"))

        provider.prices["BTCUSDT"] = 89.0   # TP touch on the open position
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["activation_mode"] == "EXIT_ONLY"
        assert telemetry["activation_status"] == "DRAINING"
        assert telemetry["exits"] == 1
        assert telemetry["exit_reasons"] == ["TAKE_PROFIT_1"]
        assert telemetry["entries"] == 0
        assert "EXIT_ONLY" in telemetry["entry_refusals"]
        # The captured ETH event was never reused to open anything, and
        # EXIT_ONLY only requests prices for the symbols it must manage.
        assert store.list_positions(POSITION_STATUS_OPEN) == []
        assert [e.symbol for e in store.list_events(status="CAPTURED")] == [
            "ETHUSDT",
        ]
        assert provider.calls[-1]["price_symbols"] == {"BTCUSDT"}

    def test_restart_in_draining_restores_exit_only(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock, symbol="BTCUSDT")
        _open_position(runtime, provider)
        _capture(store, clock, symbol="ETHUSDT")
        provider.prices["ETHUSDT"] = ENTRY_PRICE

        store.set_activation(_activation(status="DRAINING"))

        # RESTART: a brand-new runtime + contour over the same durable state.
        restarted = _runtime(store, provider, clock=clock)
        assert restarted.contour.activation_mode == "EXIT_ONLY"
        assert restarted.run(once=True) == EXIT_OK
        telemetry = restarted.last_telemetry
        assert telemetry["activation_mode"] == "EXIT_ONLY"
        assert telemetry["entries"] == 0
        assert telemetry["open_positions"] == 1
        # The open position was recovered verbatim (original entry facts).
        positions = store.list_positions(POSITION_STATUS_OPEN)
        assert len(positions) == 1
        assert positions[0].symbol == "BTCUSDT"

        # Exits still run in exit-only mode after the restart.
        provider.prices["BTCUSDT"] = 89.0
        assert restarted.run(once=True) == EXIT_OK
        assert restarted.last_telemetry["exit_reasons"] == ["TAKE_PROFIT_1"]
        assert restarted.last_telemetry["entries"] == 0
        assert store.list_positions(POSITION_STATUS_OPEN) == []


# ---------------------------------------------------------------------------
# 6/7. Stale / missing prices
# ---------------------------------------------------------------------------
class TestPriceFailClosed:
    def test_stale_price_blocks_new_entries(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        assert _capture(store, clock) == "CAPTURED"
        provider = FakeProvider(
            prices={}, price_status={"BTCUSDT": STATUS_STALE},
        )
        runtime = _runtime(store, provider, clock=clock)
        assert runtime.run(once=True) == EXIT_OK

        assert store.list_positions() == []
        telemetry = runtime.last_telemetry
        assert telemetry["entries"] == 0
        assert "PRICE_STALE:BTCUSDT" in telemetry["entry_refusals"]

    def test_missing_price_creates_no_fake_execution(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        assert _capture(store, clock) == "CAPTURED"
        provider = FakeProvider(prices={})   # never observed
        runtime = _runtime(store, provider, clock=clock)
        assert runtime.run(once=True) == EXIT_OK

        assert store.list_positions() == []
        assert store.list_events(status="CAPTURED")[0].symbol == "BTCUSDT"
        assert "PRICE_MISSING:BTCUSDT" in runtime.last_telemetry["entry_refusals"]

    def test_stale_price_holds_open_position_without_fake_close(
        self, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock)
        _open_position(runtime, provider)

        # Feed outage: no usable price at all -> HOLD, never a guessed exit.
        provider.price_status = {"BTCUSDT": STATUS_STALE}
        provider.prices = {}
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["exits"] == 0 and telemetry["entries"] == 0
        assert telemetry["open_positions"] == 1
        positions = store.list_positions(POSITION_STATUS_OPEN)
        assert len(positions) == 1 and positions[0].status == POSITION_STATUS_OPEN

        # Recovery: the next usable price resumes management.
        provider.price_status = {}
        provider.prices = {"BTCUSDT": 89.0}
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == ["TAKE_PROFIT_1"]


# ---------------------------------------------------------------------------
# 8. Intrabar exits from admissible data only
# ---------------------------------------------------------------------------
class TestIntrabar:
    @pytest.fixture
    def opened(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock)
        _open_position(runtime, provider)
        # The entry happened at 12:00:30; move to 12:10:30 so the first
        # ENTIRELY-AFTER candle (12:05-12:10) has genuinely closed — the
        # tests never hand the engine a future candle.
        clock.advance(minutes=10)
        return store, provider, runtime

    def test_closed_bar_range_triggers_stop(self, opened):
        store, provider, runtime = opened
        provider.bars = {"BTCUSDT": {
            "high": 110.5, "low": 95.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}
        provider.prices = {"BTCUSDT": ENTRY_PRICE}   # below the stop itself
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["exit_reasons"] == ["STOP_LOSS"]
        assert store.list_positions(POSITION_STATUS_OPEN) == []

    def test_touch_seen_only_in_live_price_is_never_missed(self, opened):
        store, provider, runtime = opened
        # The last CLOSED bar never touched the stop, but the live price
        # (observable right now) has: the merged range must catch it.
        provider.bars = {"BTCUSDT": {
            "high": 105.0, "low": 95.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}
        provider.prices = {"BTCUSDT": 110.0}
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["exit_reasons"] == ["STOP_LOSS_GAP"]
        assert store.list_positions(POSITION_STATUS_OPEN) == []

    def test_without_closed_bar_exit_uses_only_observed_price(self, opened):
        store, provider, runtime = opened
        provider.bars = {}        # forming candle only -> provider excluded it
        provider.prices = {"BTCUSDT": 110.0}
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == ["STOP_LOSS_GAP"]

    def test_take_profit_from_closed_bar_low(self, opened):
        store, provider, runtime = opened
        provider.bars = {"BTCUSDT": {
            "high": 101.0, "low": 85.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}
        provider.prices = {"BTCUSDT": ENTRY_PRICE}
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["exit_reasons"] == ["TAKE_PROFIT_1"]
        assert store.list_positions(POSITION_STATUS_OPEN) == []

    def test_merge_never_uses_data_the_provider_excluded(self):
        # Bars the provider did not return must never appear in the merge.
        snapshot = MarketDataSnapshot(
            observed_at=datetime.now(timezone.utc),
            prices={"BTCUSDT": 110.0},
            bars={},   # excluded (forming / stale / invalid)
        )
        assert ProductionRuntime._merge_bars(snapshot, snapshot.prices, {}) == {}
        # And the merged range is exactly max/min of admissible inputs.
        snapshot2 = MarketDataSnapshot(
            observed_at=datetime.now(timezone.utc),
            prices={"BTCUSDT": 110.0},
            bars={"BTCUSDT": {
                "high": 105.0, "low": 85.0,
                "open_ms": BAR_OPEN_POST_ENTRY_MS,
                "close_ms": BAR_CLOSE_POST_ENTRY_MS,
            }},
        )
        merged = ProductionRuntime._merge_bars(snapshot2, snapshot2.prices, {})
        assert merged == {"BTCUSDT": {"high": 110.0, "low": 85.0}}


# ---------------------------------------------------------------------------
# Review P1 — a candle formed BEFORE the entry must never close the position
# ---------------------------------------------------------------------------
class TestPreEntryBarClipping:
    """A candle whose window is not ENTIRELY after the entry may carry
    PRE-entry extremes: it either closed before the position existed
    (11:55-12:00) or STRADDLED the entry (12:00-12:05, opened at 12:00:00
    while the SHORT opened at 12:00:30 — its high/low may have printed at
    12:00:10).  Handing either to ``check_exits`` could fabricate an SL/TP
    fill that was never executable after the entry (Codex R2 P1) and would
    feed pre-entry path information into the exit decision.  The runtime
    therefore admits a bar only when its WHOLE interval lies after EVERY
    entry on the symbol (``open_ms >= max(entered_at)``); without such a
    bar the engine falls back to the observed price only (conservative
    delay, never a fabricated fill, never a pre-entry extreme).
    """

    @pytest.fixture
    def opened(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock)
        _open_position(runtime, provider)
        # Entry at 12:00:30; at 12:10:30 the 12:05-12:10 candle (the first
        # one lying ENTIRELY after the entry) has genuinely closed while
        # the 11:55-12:00 candle closed before it and the 12:00-12:05
        # candle straddles it — the three temporal facts under test.
        clock.advance(minutes=10)
        return store, provider, runtime

    def test_pre_entry_candle_high_cannot_stop_out_the_short(self, opened):
        store, provider, runtime = opened
        # 11:55-12:00 candle: CLOSED at 12:00:00, i.e. BEFORE the entry at
        # 12:00:30, and its high 112 breached the SHORT stop (110) pre-entry.
        provider.bars = {"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_PRE_ENTRY_MS,
            "close_ms": BAR_CLOSE_PRE_ENTRY_MS,
        }}
        # The live quote is below the stop: nothing touched the stop AFTER
        # the entry, so the position must stay open (no fake STOP_LOSS).
        provider.prices = {"BTCUSDT": 105.0}
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["exit_reasons"] == []
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1

    def test_pre_entry_candle_low_cannot_take_profit_the_short(self, opened):
        store, provider, runtime = opened
        # The same pre-entry candle already printed a low below the target
        # (90): that touch happened before the position existed, so no
        # fabricated TAKE_PROFIT_1 may be booked.
        provider.bars = {"BTCUSDT": {
            "high": 101.0, "low": 85.0,
            "open_ms": BAR_OPEN_PRE_ENTRY_MS,
            "close_ms": BAR_CLOSE_PRE_ENTRY_MS,
        }}
        provider.prices = {"BTCUSDT": 105.0}
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == []
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1

    def test_straddling_candle_high_cannot_stop_out_the_short(self, opened):
        """Codex R2 P1 (defect): the position opened INSIDE 12:00-12:05.

        The candle closed at 12:05 (after the 12:00:30 entry) so the old
        ``close_ms > entered_at`` rule admitted it, but its high 112 may
        have printed at 12:00:10 — BEFORE the entry.  With every later
        price below the stop, no STOP_LOSS may be fabricated from that
        pre-entry extreme.
        """
        store, provider, runtime = opened
        provider.bars = {"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_STRADDLE_MS,      # 12:00:00 < entered_at
            "close_ms": BAR_CLOSE_STRADDLE_MS,    # 12:05:00 > entered_at
        }}
        provider.prices = {"BTCUSDT": 105.0}      # never touched the stop
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == []
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1

    def test_straddling_candle_low_cannot_take_profit_the_short(self, opened):
        """Same straddle defect for TAKE_PROFIT_1: the low 85 may have
        printed before the entry — never bookable as a post-entry TP."""
        store, provider, runtime = opened
        provider.bars = {"BTCUSDT": {
            "high": 101.0, "low": 85.0,
            "open_ms": BAR_OPEN_STRADDLE_MS,
            "close_ms": BAR_CLOSE_STRADDLE_MS,
        }}
        provider.prices = {"BTCUSDT": 105.0}
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == []
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1

    def test_unknown_bar_provenance_is_never_used_for_exits(self, opened):
        store, provider, runtime = opened
        # close_ms is present but open_ms is NOT: the candle cannot be
        # proven to lie entirely after the entry -> fail-safe price-only
        # check (the live quote still protects the position).
        provider.bars = {"BTCUSDT": {
            "high": 112.0, "low": 99.0, "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}
        provider.prices = {"BTCUSDT": 105.0}
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == []
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1

    def test_post_entry_candle_still_stops_out(self, opened):
        store, provider, runtime = opened
        # The 12:05-12:10 candle lies ENTIRELY after the entry and its
        # high breached the stop while the quote is below it: a real
        # STOP_LOSS (protective exits must keep working after the clipping).
        provider.bars = {"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}
        provider.prices = {"BTCUSDT": 105.0}
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == ["STOP_LOSS"]
        assert store.list_positions(POSITION_STATUS_OPEN) == []

    def test_restart_does_not_change_bar_admissibility(self, monkeypatch):
        """The persisted ``entered_at`` must clip identically after a
        process restart (a fresh runtime instance over the same store)."""
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock)
        _open_position(runtime, provider)
        clock.advance(minutes=10)

        # --- restart: brand-new runtime, same store (entered_at reloaded).
        restarted = _runtime(store, provider, clock=clock)
        provider.bars = {"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_STRADDLE_MS,
            "close_ms": BAR_CLOSE_STRADDLE_MS,
        }}
        provider.prices = {"BTCUSDT": 105.0}
        assert restarted.run(once=True) == EXIT_OK
        assert restarted.last_telemetry["exit_reasons"] == []
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1

        # ...and the entirely-after candle still closes the position.
        provider.bars = {"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}
        assert restarted.run(once=True) == EXIT_OK
        assert restarted.last_telemetry["exit_reasons"] == ["STOP_LOSS"]
        assert store.list_positions(POSITION_STATUS_OPEN) == []

    def test_reverted_forming_bar_touch_is_detected_when_bar_closes(
        self, opened,
    ):
        """No-lookaround honesty for the still-forming candle.

        A touch that happened inside the FORMING bar and already reverted
        is not reconstructable from the last closed bar + the last quote —
        so nothing is invented now (no lookahead, no fabricated fill) —
        but once a candle that lies ENTIRELY after the entry closes with
        the extreme inside its range the exit fires on the next cycle
        (bounded delay of at most one bar).
        """
        store, provider, runtime = opened
        provider.bars = {"BTCUSDT": {
            "high": 101.0, "low": 99.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}
        provider.prices = {"BTCUSDT": 105.0}   # never touched the stop
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == []
        # The NEXT candle closes with the stop inside its range -> the
        # touch is observable and the exit fires exactly once.
        runtime._clock.advance(minutes=5)      # now 12:15:30
        provider.bars = {"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS + 300_000,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS + 300_000,
        }}
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == ["STOP_LOSS"]
        assert store.list_positions(POSITION_STATUS_OPEN) == []

    def test_merge_admits_only_whole_intervals_after_the_entry(self):
        position = SimpleNamespace(symbol="BTCUSDT", entered_at=SIGNAL_AT)
        positions = {"k1": position}
        prices = {"BTCUSDT": 105.0}

        def _merge(bars):
            snapshot = MarketDataSnapshot(
                observed_at=SIGNAL_AT + timedelta(seconds=30),
                prices=prices, bars=bars,
            )
            return ProductionRuntime._merge_bars(snapshot, prices, positions)

        # (a) candle CLOSED entirely before the entry -> never handed over.
        assert _merge({"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_PRE_ENTRY_MS,
            "close_ms": BAR_CLOSE_PRE_ENTRY_MS,
        }}) == {}

        # (b) candle STRADDLING the entry (open before, close after) ->
        # its high/low may predate the entry -> inadmissible (defect case).
        assert _merge({"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_STRADDLE_MS,
            "close_ms": BAR_CLOSE_STRADDLE_MS,
        }}) == {}

        # (c) open_ms missing (close known) -> fail-safe clip.
        assert _merge({"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}) == {}

        # (d) ENTIRELY after the entry -> admissible, widened by the quote.
        assert _merge({"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}) == {"BTCUSDT": {"high": 112.0, "low": 99.0}}

    def test_multiple_positions_symbol_clips_to_the_latest_entry(self):
        """Several open positions on ONE symbol: a candle is admissible
        only when its whole interval lies after the LATEST entry — an
        invalid range is never handed to ANY of them (the engine looks
        bars up per symbol)."""
        early = SimpleNamespace(
            symbol="BTCUSDT",
            entered_at=SIGNAL_AT.replace(second=0),        # 12:00:00
        )
        late = SimpleNamespace(
            symbol="BTCUSDT",
            entered_at=SIGNAL_AT.replace(minute=4, second=0),  # 12:04:00
        )
        prices = {"BTCUSDT": 105.0}

        def _merge(bars, positions):
            snapshot = MarketDataSnapshot(
                observed_at=SIGNAL_AT + timedelta(seconds=30),
                prices=prices, bars=bars,
            )
            return ProductionRuntime._merge_bars(snapshot, prices, positions)

        both = {"k1": early, "k2": late}
        # 12:00-12:05 is after `early` but STRADDLES `late` -> withheld
        # from BOTH positions on the symbol.
        assert _merge({"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_STRADDLE_MS,
            "close_ms": BAR_CLOSE_STRADDLE_MS,
        }}, both) == {}
        # 12:05-12:10 is entirely after BOTH entries -> admissible.
        assert _merge({"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}, both) == {"BTCUSDT": {"high": 112.0, "low": 99.0}}
        # An UNKNOWN entry time is the most restrictive threshold.
        unknown = {"k1": SimpleNamespace(symbol="BTCUSDT", entered_at=None)}
        assert _merge({"BTCUSDT": {
            "high": 112.0, "low": 99.0,
            "open_ms": BAR_OPEN_POST_ENTRY_MS,
            "close_ms": BAR_CLOSE_POST_ENTRY_MS,
        }}, unknown) == {}


# ---------------------------------------------------------------------------
# 9. Timeout (frozen protocol)
# ---------------------------------------------------------------------------
class TestTimeout:
    def test_timeout_fires_at_the_frozen_deadline(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock)
        _open_position(runtime, provider)

        position = store.list_positions(POSITION_STATUS_OPEN)[0]
        assert position.deadline_at - position.entered_at == timedelta(
            minutes=MAX_HOLD_MINUTES,
        )

        clock.advance(minutes=MAX_HOLD_MINUTES - 1)   # one minute early
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exits"] == 0

        clock.advance(minutes=2)                      # past the deadline
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["exit_reasons"] == ["TIMEOUT"]
        closed = store.list_positions(POSITION_STATUS_CLOSED)
        assert len(closed) == 1
        assert closed[0].entered_at == position.entered_at
        assert closed[0].deadline_at == position.deadline_at


# ---------------------------------------------------------------------------
# 10. Funding idempotency through the runtime
# ---------------------------------------------------------------------------
class TestFunding:
    def test_funding_settles_exactly_once_across_cycles(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock)
        _open_position(runtime, provider)

        clock.advance(hours=8)          # exactly one funding interval
        provider.funding = {"BTCUSDT": 0.01}
        assert runtime.run(once=True) == EXIT_OK
        after_first = store.list_positions(POSITION_STATUS_OPEN)[0]
        assert after_first.funding_periods_charged == 1
        assert after_first.funding_paid != 0.0
        balance_after_first = runtime.last_telemetry["balance"]
        assert runtime.last_telemetry["funding_pending"] == 0

        # Same clock, same rate: a second cycle must change NOTHING.
        assert runtime.run(once=True) == EXIT_OK
        after_second = store.list_positions(POSITION_STATUS_OPEN)[0]
        assert after_second.funding_periods_charged == 1
        assert after_second.funding_paid == after_first.funding_paid
        assert runtime.last_telemetry["balance"] == balance_after_first

    def test_unknown_funding_rate_preserves_the_obligation(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock)
        _open_position(runtime, provider)

        clock.advance(hours=8)
        provider.funding = {}           # feed down: rate UNKNOWN
        assert runtime.run(once=True) == EXIT_OK
        position = store.list_positions(POSITION_STATUS_OPEN)[0]
        assert position.funding_periods_charged == 0   # nothing fabricated
        assert runtime.last_telemetry["funding_pending"] == 1

        provider.funding = {"BTCUSDT": 0.01}   # first observed rate settles
        assert runtime.run(once=True) == EXIT_OK
        position = store.list_positions(POSITION_STATUS_OPEN)[0]
        assert position.funding_periods_charged == 1   # backlog, exactly once
        assert runtime.last_telemetry["funding_pending"] == 0


# ---------------------------------------------------------------------------
# Codex R2 P1 — a quote must still be fresh when it REACHES the engine
# ---------------------------------------------------------------------------
class _Resp:
    """Minimal urllib-like response double for the REAL BybitClient."""

    status = 200
    headers = None

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _RuntimeBybitSession:
    """URL-dispatching session double for the REAL ``BybitClient``.

    The slow knobs advance the injected UTC clock INSIDE the request,
    exactly as a slow/retried HTTP call ages everything fetched earlier
    in the same snapshot.
    """

    def __init__(self, clock) -> None:
        self.clock = clock
        self.price = "100.0"
        self.klines: list = []          # raw rows, newest first
        self.funding_rows: list = []
        self.slow_tickers_s = 0.0
        self.slow_klines_s = 0.0
        self.slow_funding_s = 0.0
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        if url.endswith("/v5/market/tickers"):
            if self.slow_tickers_s:
                self.clock.advance(seconds=self.slow_tickers_s)
            return _Resp({"retCode": 0, "retMsg": "OK", "result": {"list": [
                {"symbol": "BTCUSDT", "lastPrice": self.price},
            ]}})
        if url.endswith("/v5/market/kline"):
            if self.slow_klines_s:
                self.clock.advance(seconds=self.slow_klines_s)
            return _Resp({"retCode": 0, "retMsg": "OK",
                          "result": {"list": [list(r) for r in self.klines]}})
        if url.endswith("/v5/market/funding/history"):
            if self.slow_funding_s:
                self.clock.advance(seconds=self.slow_funding_s)
            return _Resp({"retCode": 0, "retMsg": "OK",
                          "result": {"list": [dict(r) for r in self.funding_rows]}})
        raise AssertionError(f"unexpected endpoint: {url}")


def _real_provider_runtime(clock, store, session):
    from app.exchange.bybit_client import BybitClient
    client = BybitClient(
        Settings(bybit_max_attempts=1, bybit_retry_backoff=0.0),
        session,
    )
    provider = MarketDataProvider(client, clock=clock)
    return _runtime(store, provider, clock=clock)


def _safe_kline_row(open_ms: int) -> list:
    """A closed 5m candle inside the safe band (no stop, no TP, no gap)."""
    return [str(open_ms), "100.0", "101.0", "95.0", "100.0", "1.0"]


class TestSlowApiStalenessRuntime:
    """Codex R2 P1 end-to-end: a quote validated when the cycle STARTED
    can age out during later requests of the same cycle.  The engine must
    never receive it — stale quotes neither fill entries nor fabricate
    exits, and recovery never loses the pending event."""

    def test_quote_aged_out_during_requests_blocks_entry_then_recovers(
        self, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        session = _RuntimeBybitSession(clock)
        runtime = _real_provider_runtime(clock, store, session)
        assert _capture(store, clock) == "CAPTURED"

        # The tickers request itself takes 60s (> PRICE_MAX_AGE_S=30):
        # by snapshot return the quote is STALE -> no entry, no fill.
        session.slow_tickers_s = 60.0
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["entries"] == 0
        assert "PRICE_STALE:BTCUSDT" in telemetry["entry_refusals"]
        assert store.list_positions() == []
        # The pending event survives untouched (nothing consumed it).
        assert len(store.list_events(status="CAPTURED")) == 1

        # Recovery: healthy feed again -> the SAME event enters exactly
        # once, and later cycles never re-enter it.
        session.slow_tickers_s = 0.0
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["entries"] == 1
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["entries"] == 0
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1
        assert store.list_events(status="CAPTURED") == []

    def test_quote_aged_out_during_klines_holds_exits_then_recovers(
        self, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        session = _RuntimeBybitSession(clock)
        runtime = _real_provider_runtime(clock, store, session)
        _capture(store, clock)
        assert runtime.run(once=True) == EXIT_OK        # entry at 12:00:30
        assert runtime.last_telemetry["entries"] == 1

        # A closed candle exists, but the kline request takes 60s.  The
        # quote (89.0 — at the TP target) was fetched at cycle start; by
        # snapshot return it is 60s old (> 30s) and must be EXCLUDED: an
        # aged-out quote may not fabricate a TAKE_PROFIT_1, so the
        # position stays open.
        session.price = "89.0"
        session.klines = [_safe_kline_row(
            _epoch_ms(SIGNAL_AT.replace(minute=55, second=0, microsecond=0)),
        )]
        session.slow_klines_s = 60.0
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["exits"] == 0
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1

        # Recovery: fast requests again, the (still) fresh quote at the
        # target -> the real TAKE_PROFIT_1 fires (management resumes,
        # nothing was lost).
        session.slow_klines_s = 0.0
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["exit_reasons"] == ["TAKE_PROFIT_1"]
        assert store.list_positions(POSITION_STATUS_OPEN) == []


# ---------------------------------------------------------------------------
# Codex R2 P2 — an EMPTY funding history must never become a confirmed 0.0
# ---------------------------------------------------------------------------
class TestFundingEmptyHistoryRuntime:
    """End-to-end over the REAL ``BybitClient`` contract: an empty
    funding-history response reports ``MISSING`` (never a fabricated
    ``0.0``), so ``funding_periods_charged`` cannot advance until a rate
    is actually observed — and then exactly once, restart included."""

    @pytest.fixture
    def opened(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        session = _RuntimeBybitSession(clock)   # funding history EMPTY
        runtime = _real_provider_runtime(clock, store, session)
        assert _capture(store, clock) == "CAPTURED"
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["entries"] == 1
        session.klines = [_safe_kline_row(
            _epoch_ms(SIGNAL_AT.replace(minute=55, second=0, microsecond=0)),
        )]
        return clock, store, session, runtime

    def test_empty_history_never_advances_the_counter(self, opened):
        clock, store, session, runtime = opened
        clock.advance(hours=8)               # one funding period due

        assert runtime.run(once=True) == EXIT_OK
        position = store.list_positions(POSITION_STATUS_OPEN)[0]
        assert position.funding_periods_charged == 0   # no fabricated zero
        assert position.funding_paid == 0.0
        assert runtime.last_telemetry["funding_pending"] == 1

        # The first REAL observation settles the backlog exactly once.
        session.funding_rows = [{"fundingRate": "0.0001"}]
        assert runtime.run(once=True) == EXIT_OK
        position = store.list_positions(POSITION_STATUS_OPEN)[0]
        assert position.funding_periods_charged == 1
        assert position.funding_paid != 0.0
        assert runtime.last_telemetry["funding_pending"] == 0

        # Same clock, same rate: a second cycle changes NOTHING.
        assert runtime.run(once=True) == EXIT_OK
        position = store.list_positions(POSITION_STATUS_OPEN)[0]
        assert position.funding_periods_charged == 1

        # Restart (fresh runtime over the same store): no re-charge, no
        # missed period, obligation state carried over.
        restarted = _runtime(store, runtime.provider, clock=clock)
        assert restarted.run(once=True) == EXIT_OK
        position = store.list_positions(POSITION_STATUS_OPEN)[0]
        assert position.funding_periods_charged == 1
        assert restarted.last_telemetry["funding_pending"] == 0


# ---------------------------------------------------------------------------
# 11. Store outage (runtime-level fail-closed + recovery)
# ---------------------------------------------------------------------------
class TestStoreOutage:
    def test_activation_read_failure_fails_closed_then_recovers(
        self, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)

        class FlakyStore(InMemoryInverseShortStore):
            fail_loads = 0

            def load_activation(self):
                if FlakyStore.fail_loads > 0:
                    FlakyStore.fail_loads -= 1
                    raise ConnectionError("postgres unreachable")
                return super().load_activation()

        store = FlakyStore(_activation(), initial_balance=10_000.0)
        assert _capture(store, clock) == "CAPTURED"
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)

        FlakyStore.fail_loads = 3        # runtime read + BOTH refreshes
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["entries"] == 0
        assert telemetry["db_errors"] >= 1
        assert telemetry["activation_status"] == "UNREADABLE"
        assert store.list_positions() == []
        assert "DB_ERRORS" in telemetry["entry_refusals"]
        assert any(
            r.startswith("ACTIVATION:") for r in telemetry["entry_refusals"]
        )

        FlakyStore.fail_loads = 0        # database back
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["entries"] == 1
        assert runtime.last_telemetry["db_errors"] == 0
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1

    def test_cycle_error_is_contained_and_reported(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        assert _capture(store, clock) == "CAPTURED"

        class ExplodingProvider(FakeProvider):
            def snapshot(self, **kwargs):
                raise RuntimeError("provider exploded")

        provider = ExplodingProvider()
        runtime = _runtime(store, provider, clock=clock)
        # run() contains the failure: EXIT_OK, loop continues (once=True).
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["entries"] == 0
        assert "CYCLE_ERROR:RuntimeError" in telemetry["entry_refusals"]
        assert runtime.cycle_errors == 1
        # And the very next cycle works once the provider recovers.
        healthy = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime.provider = healthy
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.cycle_errors == 1
        assert runtime.last_telemetry["entries"] == 1
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1


# ---------------------------------------------------------------------------
# 14. Duplicate delivery
# ---------------------------------------------------------------------------
class TestDuplicateEvent:
    def test_duplicate_delivery_never_opens_a_second_position(
        self, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        assert _capture(store, clock) == "CAPTURED"
        _open_position(runtime, provider)

        # The scanner re-delivers the SAME source event (pre-dedup
        # publication): idempotency must hold through the runtime loop.
        assert _capture(store, clock) == "DUPLICATE"
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["entries"] == 0
        assert len(store.list_positions()) == 1

        # A full restart must not re-enter it either.
        restarted = _runtime(store, provider, clock=clock)
        assert restarted.run(once=True) == EXIT_OK
        assert restarted.last_telemetry["entries"] == 0
        assert len(store.list_positions()) == 1


# ---------------------------------------------------------------------------
# 15. Single instance
# ---------------------------------------------------------------------------
class TestSingleInstance:
    def test_second_lock_holder_is_refused(self, tmp_path):
        lock_path = tmp_path / "runtime.lock"
        first = RuntimeInstanceLock(lock_path)
        first.acquire()
        try:
            second = RuntimeInstanceLock(lock_path)
            with pytest.raises(InstanceLockHeld):
                second.acquire()
            assert second.held is False
        finally:
            first.release()
        # Released -> the next instance may take over (crash recovery: the
        # OS drops the lock with the dead process, no stale lock state).
        third = RuntimeInstanceLock(lock_path)
        third.acquire()
        assert third.held
        third.release()

    def test_main_exits_3_when_another_instance_holds_the_lock(self, tmp_path):
        lock_path = tmp_path / "runtime.lock"
        holder = RuntimeInstanceLock(lock_path)
        holder.acquire()
        try:
            code = main([
                "--lock-file", str(lock_path),
                "--log-file", str(tmp_path / "logs" / "rt.log"),
                "--heartbeat-file", str(tmp_path / "var" / "hb.json"),
            ])
            assert code == EXIT_ALREADY_RUNNING
        finally:
            holder.release()

    def test_lock_file_is_never_unlinked(self, tmp_path):
        lock_path = tmp_path / "runtime.lock"
        lock = RuntimeInstanceLock(lock_path)
        lock.acquire()
        lock.release()
        assert lock_path.exists()


# ---------------------------------------------------------------------------
# 16. SIGTERM
# ---------------------------------------------------------------------------
class TestGracefulShutdown:
    def test_sigterm_finishes_the_cycle_and_keeps_committed_work(
        self, monkeypatch,
    ):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        assert _capture(store, clock) == "CAPTURED"
        previous = install_signal_handlers(shutdown := threading.Event())
        try:
            handler = signal.getsignal(signal.SIGTERM)
            # The signal arrives DURING the first cycle (mid data fetch).
            provider = FakeProvider(
                prices={"BTCUSDT": ENTRY_PRICE},
                on_snapshot=lambda: handler(signal.SIGTERM, None),
            )
            runtime = _runtime(store, provider, clock=clock)
            assert runtime.run(shutdown=shutdown) == EXIT_OK
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)

        # The current cycle COMPLETED: the entry is committed, not lost.
        assert runtime.cycles == 1
        positions = store.list_positions(POSITION_STATUS_OPEN)
        assert len(positions) == 1
        assert runtime.last_telemetry["entries"] == 1

        # A restart recovers the committed position without a re-entry.
        restarted = _runtime(
            store, FakeProvider(prices={"BTCUSDT": ENTRY_PRICE}), clock=clock,
        )
        assert restarted.run(once=True) == EXIT_OK
        assert restarted.last_telemetry["entries"] == 0
        assert restarted.last_telemetry["open_positions"] == 1
        assert len(store.list_positions()) == 1

    def test_signal_handler_sets_the_shutdown_event(self):
        shutdown = threading.Event()
        previous = install_signal_handlers(shutdown)
        try:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            assert shutdown.is_set()
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)


# ---------------------------------------------------------------------------
# 17. API rate limit / timeout / outage (real provider + failing client)
# ---------------------------------------------------------------------------
class _OutageClient:
    def __init__(self) -> None:
        self.tickers = TimeoutError("HTTP 429: rate limit")
        self.klines: dict = {}
        self.funding: dict = {}
        self.calls: list[tuple] = []

    def get_tickers(self, category: str = "linear"):
        self.calls.append(("tickers",))
        if isinstance(self.tickers, BaseException):
            raise self.tickers
        return self.tickers

    def get_klines(self, symbol, interval="5", limit=200):
        self.calls.append(("klines", symbol))
        value = self.klines.get(symbol, [])
        if isinstance(value, BaseException):
            raise value
        return value

    def get_funding_rate(self, symbol):
        self.calls.append(("funding", symbol))
        value = self.funding.get(symbol)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise RuntimeError("no row")
        return value


class TestApiOutage:
    def test_rate_limited_feed_fails_closed_then_recovers(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        assert _capture(store, clock) == "CAPTURED"

        client = _OutageClient()
        provider = MarketDataProvider(
            client, clock=lambda: datetime.now(timezone.utc),
            config=MarketDataConfig(),
        )
        runtime = _runtime(store, provider, clock=clock)

        # Outage: entries fail closed (no fabricated price), loop survives.
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["entries"] == 0
        assert telemetry["api_errors"] == 1
        assert "PRICE_API_ERROR:BTCUSDT" in telemetry["entry_refusals"]
        assert store.list_positions() == []

        # Recovery: the next cycle enters at the first usable price.
        client.tickers = [{"symbol": "BTCUSDT", "lastPrice": str(ENTRY_PRICE)}]
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["entries"] == 1
        assert runtime.last_telemetry["api_errors"] == 0
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1

    def test_source_event_delivered_during_outage_is_kept_and_enters_later(
        self, monkeypatch,
    ):
        """Source capture is independent of the Bybit API (review item 4)."""
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        client = _OutageClient()          # every public GET raises
        provider = MarketDataProvider(
            client, clock=lambda: datetime.now(timezone.utc),
            config=MarketDataConfig(),
        )
        runtime = _runtime(store, provider, clock=clock)

        # A source event arrives WHILE the API is down: the capture path
        # (store only) still accepts it — no entry is possible yet.
        clock.advance(minutes=1)
        assert _capture(store, clock) == "CAPTURED"
        assert runtime.run(once=True) == EXIT_OK
        telemetry = runtime.last_telemetry
        assert telemetry["entries"] == 0
        assert "PRICE_API_ERROR:BTCUSDT" in telemetry["entry_refusals"]
        assert store.list_positions() == []

        # Feed recovers: the event captured during the outage enters.
        client.tickers = [{"symbol": "BTCUSDT", "lastPrice": str(ENTRY_PRICE)}]
        assert runtime.run(once=True) == EXIT_OK
        assert runtime.last_telemetry["entries"] == 1
        assert len(store.list_positions(POSITION_STATUS_OPEN)) == 1


# ---------------------------------------------------------------------------
# Telemetry contract (Issue §3.7)
# ---------------------------------------------------------------------------
class TestTelemetry:
    REQUIRED_FIELDS = {
        "experiment_id", "activation_status", "activation_mode",
        "protocol_fingerprint", "freeze_ts", "cycle_duration_ms",
        "market_data_age_ms", "symbols_checked", "pending_events",
        "entries", "exits", "open_positions", "pending_closes",
        "durability_risk", "accounting_consistent", "funding_pending",
        "api_errors", "db_errors",
    }

    def test_every_required_field_is_present(self, monkeypatch):
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        runtime = _runtime(store, provider, clock=clock)
        _capture(store, clock)
        runtime.run(once=True)

        telemetry = runtime.last_telemetry
        missing = self.REQUIRED_FIELDS - set(telemetry)
        assert not missing, f"missing telemetry fields: {sorted(missing)}"
        assert telemetry["cycle"] == 1
        assert isinstance(telemetry["cycle_duration_ms"], int)
        assert telemetry["accounting_consistent"] is True
        assert telemetry["durability_risk"] is False
        # The record is JSON-serializable (single journal line).
        assert json.loads(json.dumps(telemetry, default=str))

    def test_entry_refusal_reasons_are_reported(self, monkeypatch):
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)
        clock = _Clock(SIGNAL_AT)
        store = _store()
        runtime = _runtime(
            store, FakeProvider(prices={"BTCUSDT": ENTRY_PRICE}), clock=clock,
        )
        runtime.run(once=True)
        refusals = runtime.last_telemetry["entry_refusals"]
        assert refusals and all(
            r.startswith(("ACTIVATION:", "EXIT_ONLY", "PRICE_", "DB_ERRORS",
                          "DURABILITY_", "ACCOUNTING_", "ENTRY_CAP",
                          "PENDING_", "CYCLE_ERROR"))
            for r in refusals
        ), refusals


# ---------------------------------------------------------------------------
# Isolation / no-side-effects static contract
# ---------------------------------------------------------------------------
class TestIsolationContract:
    FORBIDDEN_NAMES = {
        # production Paper engine / tables / orders / scanner tables
        "PaperTradingEngine", "ShadowPaperEngine", "create_order",
        "paper_trade", "scanner_setup", "position_monitor",
        # historical backfill paths
        "get_klines_range",
    }

    def test_runtime_modules_never_touch_production_trading(self):
        sources = ""
        for module in (pr_runtime,):
            sources += Path(module.__file__).read_text(encoding="utf-8")
        from app.paper.me_long_v2_inverse_short_v1 import market_data as md_mod
        sources += Path(md_mod.__file__).read_text(encoding="utf-8")

        names: set[str] = set()
        for node in ast.walk(ast.parse(sources)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        offenders = sorted(names & self.FORBIDDEN_NAMES)
        assert not offenders, f"production trading references: {offenders}"

    def test_runtime_reuses_the_frozen_contour(self):
        source = Path(pr_runtime.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "InverseShortContour" in imported          # PR #267 reuse
        assert "connect_pg_store" in imported             # dedicated store
        # No second paper engine module is imported.
        assert "engine" not in {name.split(".")[-1] for name in imported}

    def test_market_data_module_performs_no_http_itself(self):
        from app.paper.me_long_v2_inverse_short_v1 import market_data as md_mod
        source = Path(md_mod.__file__).read_text(encoding="utf-8")
        names: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        assert not ({"requests", "urlopen", "urllib", "http"}
                    & names), "provider must use the shared BybitClient"


# ---------------------------------------------------------------------------
# systemd unit contract (Issue §4)
# ---------------------------------------------------------------------------
class TestSystemdUnitContract:
    UNIT = (Path(__file__).resolve().parents[1]
            / "deploy" / "systemd" / "trad-bot-me-inverse-short-runtime.service")
    ENV_EXAMPLE = (Path(__file__).resolve().parents[1]
                   / "deploy" / "env" / "me-inverse-short-runtime.env.example")

    def test_unit_exists_with_required_directives(self):
        assert self.UNIT.exists(), "systemd unit template is missing"
        content = self.UNIT.read_text(encoding="utf-8")
        # Dedicated service user + separate release path + explicit venv.
        assert "User=tradbot" in content
        assert "WorkingDirectory=/opt/trad_bot_me_inverse_short_release" in content
        assert "/opt/trad_bot_me_inverse_short_release/.venv/bin/python" in content
        assert (
            "-m app.paper.me_long_v2_inverse_short_v1.production_runtime"
            in content
        )
        # Predictable, MANDATORY environment file (no "-" prefix).
        assert "EnvironmentFile=/etc/trad-bot/me-inverse-short-runtime.env" in content
        assert "EnvironmentFile=-" not in content
        # Lifecycle: restart on failure, never restart-loop on a lock
        # conflict, graceful SIGTERM with a bounded stop timeout.
        assert "Restart=on-failure" in content
        assert "RestartPreventExitStatus=3" in content
        assert "KillSignal=SIGTERM" in content
        assert "TimeoutStopSec=" in content
        # Writable directories are explicit under ProtectSystem=strict.
        assert "ProtectSystem=strict" in content
        assert "ReadWritePaths=/opt/trad_bot_me_inverse_short_release/logs" in content
        assert "/opt/trad_bot_me_inverse_short_release/var" in content
        # Separate release path — never the scanner's release tree (the
        # unit may MENTION the old path only to document the lesson).
        assert "WorkingDirectory=/opt/trad_bot_pr267_release" not in content
        assert "ExecStart=/opt/trad_bot_pr267_release" not in content
        # The unit itself must NEVER enable the activation flag.
        assert "ME_INVERSE_SHORT_V1_ENABLED=1" not in content
        assert "[Install]" in content and "WantedBy=multi-user.target" in content

    def test_env_example_keeps_activation_off(self):
        assert self.ENV_EXAMPLE.exists(), "env example is missing"
        content = self.ENV_EXAMPLE.read_text(encoding="utf-8")
        assert "ME_INVERSE_SHORT_V1_ENABLED=0" in content
        assert "ME_INVERSE_SHORT_V1_ENABLED=1" not in content
        assert "ME_INVERSE_SHORT_MD_PRICE_MAX_AGE_S" in content
        assert "ME_INVERSE_SHORT_MD_BAR_MAX_AGE_S" in content
        assert "ME_INVERSE_SHORT_MD_FUNDING_MAX_AGE_S" in content
        # The runtime consumes PUBLIC market data only: no API keys needed.
        assert "BYBIT_API_KEY" not in content

    def test_runbook_documents_install_verify_stop_rollback(self):
        runbook = (Path(__file__).resolve().parents[1]
                   / "docs" / "me_inverse_short_v1_production_runtime_v1.md")
        assert runbook.exists()
        content = runbook.read_text(encoding="utf-8")
        for section in ("## 3. Installation", "## 4. Verification",
                        "## 5. Stop / rollback", "## 6. Activation state"):
            assert section in content, f"runbook missing {section}"
        # The PermissionError deployment lesson must be addressed.
        assert "PermissionError" in content


# ---------------------------------------------------------------------------
# Review item 4 — BybitClient contract: resolved shared limiter, no secrets
# ---------------------------------------------------------------------------
class TestBybitClientContract:
    def test_shared_limiter_follows_the_resolved_settings_flag(self):
        from app.exchange.bybit_client import BybitClient

        # DEFAULT-OFF: no limiter object is built at all (honest default,
        # zero shared-state I/O) — the runtime never force-enables it.
        off = BybitClient(_settings())
        assert off._shared_limiter() is None

        # Resolved flag ON: the client builds the process-shared limiter
        # exactly once and always hands back the SAME object (no per-
        # request rebuild, no private limiter beside it).
        on = BybitClient(dataclasses.replace(
            _settings(), global_bybit_rate_limiter_enabled=True,
        ))
        limiter = on._shared_limiter()
        assert limiter is not None
        assert on._shared_limiter() is limiter

    def test_runtime_cycle_never_logs_credentials(self, monkeypatch, caplog):
        import logging as std_logging

        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        clock = _Clock(SIGNAL_AT)
        store = _store()
        provider = FakeProvider(prices={"BTCUSDT": ENTRY_PRICE})
        settings = dataclasses.replace(
            _settings(),
            db_password="S3CRET-DB-PASSWORD-XYZ",
            bybit_api_secret="S3CRET-BYBIT-SECRET-XYZ",
        )
        runtime = ProductionRuntime(
            settings, store, provider, clock=clock, interval_s=0.01,
        )
        _capture(store, clock)
        with caplog.at_level(std_logging.DEBUG):
            assert runtime.run(once=True) == EXIT_OK
        # Neither the DB password nor the API secret may ever reach the log
        # (systemd ships logs to the journal).
        assert "S3CRET-DB-PASSWORD-XYZ" not in caplog.text
        assert "S3CRET-BYBIT-SECRET-XYZ" not in caplog.text
        # The per-cycle telemetry is equally clean.
        for key in runtime.last_telemetry:
            lowered = key.lower()
            assert "password" not in lowered
            assert "secret" not in lowered
            assert "api_key" not in lowered


# ---------------------------------------------------------------------------
# Startup guards
# ---------------------------------------------------------------------------
class TestStartupGuards:
    def test_interval_must_be_positive(self):
        store = _store()
        with pytest.raises(ValueError, match="INTERVAL_INVALID"):
            ProductionRuntime(
                _settings(), store, FakeProvider(), clock=_Clock(SIGNAL_AT),
                interval_s=0,
            )

    def test_ensure_writable_creates_directories(self, tmp_path):
        target = ensure_writable(tmp_path / "logs" / "rt.log")
        assert target.parent.is_dir()

    def test_ensure_writable_fails_fast_on_unusable_path(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        with pytest.raises(RuntimeError, match="RUNTIME_DIR_NOT_WRITABLE"):
            ensure_writable(blocker / "logs" / "rt.log")


# ---------------------------------------------------------------------------
# Candle helper guard (documents the closed-bar input of the provider)
# ---------------------------------------------------------------------------
def test_candle_fixture_is_grid_aligned():
    candle = Candle(
        int(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000),
        95.0, 101.0, 99.0, 100.0, 1.0,
    )
    assert candle.timestamp % (5 * 60_000) == 0
