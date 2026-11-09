"""Codex review fixes for PR #267: pending_events resilience + dirty funding retry.

P1 — protective exits must survive pending_events() DB failure
    * symbol gathering is SPLIT: open-position symbols are in-memory and
      always available; prospective entry symbols come from a guarded store
      read;
    * a ``pending_events()`` failure never aborts price loading nor
      ``run_cycle()`` — STOP/TP/TIMEOUT still execute and entries fail
      closed (honestly reported as ``pending_events_unavailable``);
    * EXIT_ONLY (DRAINING) never requests prospective entry events at all;
    * a full store outage still executes the protective exit, retains it
      through the pending-closes/outbox mechanism with an EXPLICIT
      durability risk, refuses entries, and recovers completely once the
      store is back (stage → terminal update → clear).

P2 — retry dirty funding persistence
    * a failed ``update_position()`` after a funding accrual keeps the EXACT
      snapshot (``funding_paid`` / ``funding_periods_charged``) as DIRTY;
    * every later cycle retries it — even when no new funding interval is
      due — and a retry NEVER re-accrues anything;
    * terminal close facts and the outbox mechanism are never modified by
      the funding retry;
    * a restart restores only CONFIRMED store data and never claims
      durability for a write the database never accepted.

No PostgreSQL required: in-memory store behind an injected-fault subclass.
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.me_long_v2_inverse_short_v1 import (
    ActivationRecord,
    InMemoryInverseShortStore,
    InverseShortContour,
    capture_with_status,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_v1.capture import CAPTURED, CAPTURE_NOT_ACTIVE
from app.paper.me_long_v2_inverse_short_v1.models import (
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
)
from app.scanners.models import SetupCandidate, SetupState

ENGINE_LOGGER = "app.paper.me_long_v2_inverse_short_v1.engine"
RUNNER_LOGGER = "me_inverse_short_runner"
FUNDING_INTERVAL_SECONDS = 8 * 3600

FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)


# ======================================================================
# Shared helpers (mirrors the other contour suites)
# ======================================================================

class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def _settings(**overrides) -> Settings:
    defaults = dict(
        initial_balance=10_000.0,
        risk_per_trade=0.005,
        max_symbol_exposure=0.20,
        max_open_positions=3,
        taker_fee=0.00055,
        slippage_percent=0.0005,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )
    defaults.update(overrides)
    return dataclasses.replace(Settings(), **defaults)


def _activation(status: str = "RUNNING") -> ActivationRecord:
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status=status,
        protocol_fingerprint=protocol_fingerprint(),
        freeze_ts=FREEZE_TS,
    )


def _candidate(symbol: str = "BTCUSDT", price: float = 100.0,
               detected_at: datetime = SIGNAL_AT) -> SetupCandidate:
    candle_ms = int(detected_at.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name=SOURCE_SCANNER,
        scanner_version="1.0.0",
        symbol=symbol,
        direction=SOURCE_DIRECTION,
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
        features={"exhaustion_magnitude": 0.01, "rsi_14": 70.0},
    )


def _capture(store, candidate, *, clock=None) -> str:
    return capture_with_status(
        store, candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"}, clock=clock,
    )


class _Store(InMemoryInverseShortStore):
    """In-memory store with INJECTED, independently switchable faults.

    * ``fail_list_events``    — prospective entry event reads raise (the
                                pending_events DB failure of review P1).
    * ``fail_stage``          — outbox staging raises (PG fully down).
    * ``fail_close_updates``  — terminal position UPDATEs raise.
    * ``fail_open_updates``   — OPEN position UPDATEs raise (funding writes).
    * ``fail_balance_reads``  — balance_components raises.
    """

    def __init__(self, activation: ActivationRecord | None = None,
                 *, initial_balance: float = 10_000.0) -> None:
        super().__init__(activation, initial_balance=initial_balance)
        self.fail_list_events = 0
        self.fail_stage = 0
        self.fail_close_updates = 0
        self.fail_open_updates = 0
        self.fail_balance_reads = 0
        self.list_events_calls = 0
        self.open_update_attempts = 0
        self.stage_attempts = 0

    def list_events(self, status: str | None = None):
        self.list_events_calls += 1
        if self.fail_list_events > 0:
            self.fail_list_events -= 1
            raise RuntimeError("INJECTED_LIST_EVENTS_FAILURE")
        return super().list_events(status=status)

    def stage_close(self, position):
        self.stage_attempts += 1
        if self.fail_stage > 0:
            self.fail_stage -= 1
            raise RuntimeError("INJECTED_STAGE_FAILURE")
        return super().stage_close(position)

    def update_position(self, position) -> None:
        if position.status == POSITION_STATUS_OPEN:
            self.open_update_attempts += 1
            if self.fail_open_updates > 0:
                self.fail_open_updates -= 1
                raise RuntimeError("INJECTED_OPEN_UPDATE_FAILURE")
        elif self.fail_close_updates > 0:
            self.fail_close_updates -= 1
            raise RuntimeError("INJECTED_CLOSE_UPDATE_FAILURE")
        return super().update_position(position)

    def balance_components(self):
        if self.fail_balance_reads > 0:
            self.fail_balance_reads -= 1
            raise RuntimeError("INJECTED_BALANCE_READ_FAILURE")
        return super().balance_components()


def _open(store: _Store, *, symbol: str = "BTCUSDT"):
    """Open one SHORT position with a clean store (no faults active yet)."""
    clock = _Clock(SIGNAL_AT)
    contour = InverseShortContour(
        _settings(), store, clock=clock, activation_enabled=True,
    )
    assert _capture(store, _candidate(symbol, 100.0, detected_at=clock()),
                    clock=clock) == CAPTURED
    cycle = contour.run_cycle({symbol: 100.0})
    assert cycle["entries"] == 1, cycle
    position = next(iter(contour.engine.open_positions.values()))
    return contour, clock, position


def _full_outage(store: _Store) -> None:
    store.fail_list_events = 10**6
    store.fail_stage = 10**6
    store.fail_close_updates = 10**6
    store.fail_open_updates = 10**6
    store.fail_balance_reads = 10**6


def _restore(store: _Store) -> None:
    store.fail_list_events = 0
    store.fail_stage = 0
    store.fail_close_updates = 0
    store.fail_open_updates = 0
    store.fail_balance_reads = 0


# ======================================================================
# P1: protective exits survive pending_events() failures
# ======================================================================

class TestPendingEventsResilience:
    def test_pending_events_failure_never_aborts_the_cycle_or_exits(self):
        store = _Store(_activation())
        contour, clock, position = _open(store)
        symbol = position.symbol

        # The prospective-entry read path goes down (PG read failure).
        store.fail_list_events = 10**6
        with pytest.raises(RuntimeError):          # the fault is real
            contour.engine.pending_events()

        clock.advance(hours=128)
        cycle = contour.run_cycle({symbol: 100.0})  # must NOT raise
        # (1) protective exit executed …
        assert cycle["exits"] == 1
        assert cycle["exit_reasons"] == ["TIMEOUT"]
        closed = contour.engine.closed_positions()[0]
        assert closed.status == POSITION_STATUS_CLOSED
        assert closed.exit_reason == "TIMEOUT"
        # (2) … while entries failed closed and the failure is reported.
        assert cycle["entries"] == 0
        assert cycle["pending_events"] == -1
        assert cycle["pending_events_unavailable"] is True
        # The exit itself was persisted (writes are healthy in this test).
        stored = store.list_positions(status=POSITION_STATUS_CLOSED)[0]
        assert stored.position_key == closed.position_key

    def test_runner_symbol_split_keeps_exit_prices_on_store_failure(self, caplog):
        from me_inverse_short_runner import (
            _cycle_symbols,
            _open_position_symbols,
            _pending_entry_symbols,
        )

        store = _Store(_activation())
        contour, clock, position = _open(store)
        # A prospective entry event exists (its symbol is normally priced).
        assert _capture(store, _candidate("ETHUSDT", 50.0, detected_at=clock()),
                        clock=clock) == CAPTURED
        store.fail_list_events = 10**6
        clock.advance(hours=128)

        # Open-position symbols: in-memory — ZERO store contact, always there.
        calls_before = store.list_events_calls
        open_symbols = _open_position_symbols(contour)
        assert open_symbols == {"BTCUSDT"}
        assert store.list_events_calls == calls_before   # no store read at all

        # Prospective-entry symbols: guarded — empty on failure, no raise.
        with caplog.at_level(logging.WARNING, logger=RUNNER_LOGGER):
            assert _pending_entry_symbols(contour) == set()
        assert store.list_events_calls == calls_before + 1
        assert "protective exits are unaffected" in caplog.text

        # The exact path the runner loop uses:
        symbols, open_symbols = _cycle_symbols(contour)
        assert open_symbols == {"BTCUSDT"}
        assert "BTCUSDT" in symbols               # exit price still fetched
        assert "ETHUSDT" not in symbols           # entry symbol dropped (fail closed)

        symbol = position.symbol
        cycle = contour.run_cycle({symbol: 100.0, "ETHUSDT": 50.0})
        assert cycle["exits"] == 1                # exits computed anyway
        assert cycle["exit_reasons"] == ["TIMEOUT"]
        assert cycle["entries"] == 0              # entries fail closed
        assert cycle["pending_events_unavailable"] is True

    def test_exit_only_never_queries_pending_entry_events(self, caplog):
        from me_inverse_short_runner import _cycle_symbols

        store = _Store(_activation())
        contour, clock, position = _open(store)
        assert _capture(store, _candidate("ETHUSDT", 50.0, detected_at=clock()),
                        clock=clock) == CAPTURED
        store.set_activation(_activation("DRAINING"))
        # Even a BROKEN pending-event read cannot matter: EXIT_ONLY never
        # asks for prospective entry events (review P1 + P2-2).
        store.fail_list_events = 10**6
        clock.advance(hours=128)

        calls_before = store.list_events_calls
        symbols, open_symbols = _cycle_symbols(contour)
        assert contour.exit_only is True
        assert store.list_events_calls == calls_before   # NOT CALLED at all
        assert open_symbols == {"BTCUSDT"}
        assert "BTCUSDT" in symbols

        symbol = position.symbol
        cycle = contour.run_cycle({symbol: 100.0, "ETHUSDT": 50.0})
        assert cycle["exit_only"] is True
        assert cycle["exits"] == 1                # MANAGE_EXISTING continues
        assert cycle["entries"] == 0
        assert cycle["pending_events"] == 0       # not requested in EXIT_ONLY
        assert store.list_events_calls == calls_before   # still never called
        # No new capture either (P2-2 holds while the read path is down).
        assert _capture(store, _candidate("SOLUSDT", 20.0, detected_at=clock()),
                        clock=clock) == CAPTURE_NOT_ACTIVE

    def test_full_store_outage_exit_durability_and_recovery(self, caplog):
        store = _Store(_activation())
        contour, clock, position = _open(store)
        symbol = position.symbol

        # PostgreSQL fully down: no pending reads, no staging, no writes.
        _full_outage(store)
        clock.advance(hours=128)
        cycle = contour.run_cycle({symbol: 100.0})
        engine = contour.engine

        # (5) the protective exit still executed; its snapshot is retained
        # through the EXISTING pending-closes mechanism with an explicit
        # durability risk (staging never committed).
        assert cycle["exits"] == 1
        assert len(engine.pending_closes) == 1
        assert len(engine.unstaged_close_keys) == 1
        assert engine.durability_risk is True
        # Entries fail closed (durability gate), exits are unaffected.
        assert cycle["entries"] == 0
        assert cycle["durability_risk"] is True
        snap = engine.snapshot({symbol: 100.0})
        assert snap["durability_risk"] is True
        assert snap["unstaged_closes"] == 1
        with caplog.at_level(logging.ERROR, logger=ENGINE_LOGGER):
            engine.flush_pending_closes()
            assert "UNRESOLVED close durability risk" in caplog.text
        # The store has NO closed row yet — the exit lives only in the
        # retained pending snapshot (durability risk).
        closed_facts = next(iter(engine.pending_closes.values()))
        assert closed_facts.exit_reason == "TIMEOUT"

        # (6) PostgreSQL recovers → complete, byte-for-byte recovery.
        _restore(store)
        cycle2 = contour.run_cycle({symbol: 100.0})
        assert len(engine.pending_closes) == 0
        assert len(engine.unstaged_close_keys) == 0
        assert engine.durability_risk is False
        assert len(store.list_pending_closes()) == 0
        recovered = engine.closed_positions()[0]
        assert recovered.position_key == closed_facts.position_key
        assert recovered.closed_at == closed_facts.closed_at
        assert recovered.net_pnl == closed_facts.net_pnl
        assert recovered.exit_reason == "TIMEOUT"
        assert cycle2["durability_risk"] is False
        assert cycle2["accounting_consistent"] is True
        assert dict(engine.uncommitted_deltas) == {}
        # No re-exit and no entry (the event already entered):
        assert cycle2["exits"] == 0
        assert cycle2["entries"] == 0

        # A restart after recovery reads only confirmed, durable data.
        contour2 = InverseShortContour(
            contour.settings, store, clock=clock, activation_enabled=True,
        )
        assert len(contour2.engine.closed_positions()) == 1
        assert contour2.engine.durability_risk is False
        assert contour2.engine.balance == pytest.approx(
            contour2.engine.confirmed_balance,
        )
        assert contour2.engine.snapshot({symbol: 100.0})["dirty_funding"] == 0


# ======================================================================
# P2: dirty funding persistence retry
# ======================================================================

class TestDirtyFundingRetry:
    @staticmethod
    def _expected_paid(position, periods: int, rate_percent: float) -> float:
        notional = position.entry_price * position.position_size
        return -notional * (rate_percent / 100.0) * periods  # SHORT receives

    def test_funding_write_failure_marks_dirty_and_retries_next_cycle(self):
        store = _Store(_activation())
        contour, clock, position = _open(store)
        symbol = position.symbol
        engine = contour.engine
        key = position.position_key
        clock.advance(hours=8)

        # The funding accrual's UPDATE fails.
        store.fail_open_updates = 1
        cycle = contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert cycle["exits"] == 0
        expected_paid = self._expected_paid(position, 1, 0.01)
        # Local accounting advanced exactly once …
        assert position.funding_periods_charged == 1
        assert position.funding_paid == pytest.approx(expected_paid)
        # … the exact snapshot is retained as DIRTY …
        assert key in engine._dirty_funding
        dirty = engine._dirty_funding[key]
        assert dirty.funding_paid == pytest.approx(expected_paid)
        assert dirty.funding_periods_charged == 1
        snap = engine.snapshot({symbol: 100.0})
        assert snap["dirty_funding"] == 1
        assert snap["accounting_consistent"] is True   # delta reconciles balance
        assert snap["uncommitted_delta"] != 0.0
        # … and the store row still holds ONLY confirmed data.
        stored = store.list_positions(status=POSITION_STATUS_OPEN)[0]
        assert stored.funding_periods_charged == 0
        assert stored.funding_paid == 0.0

        # NEXT cycle — no new interval became due (no clock movement) and NO
        # rate feed at all: the dirty snapshot must still be persisted.
        cycle2 = contour.run_cycle({symbol: 100.0})
        assert key not in engine._dirty_funding
        assert engine.snapshot({symbol: 100.0})["dirty_funding"] == 0
        stored = store.list_positions(status=POSITION_STATUS_OPEN)[0]
        assert stored.funding_periods_charged == 1            # exact values
        assert stored.funding_paid == pytest.approx(expected_paid)
        # A retry never re-accrues: local values are untouched.
        assert position.funding_periods_charged == 1
        assert position.funding_paid == pytest.approx(expected_paid)
        assert dict(engine.uncommitted_deltas) == {}          # delta settled
        assert engine.snapshot({symbol: 100.0})["accounting_consistent"] is True

        # One more plain cycle changes nothing.
        contour.run_cycle({symbol: 100.0})
        assert position.funding_periods_charged == 1
        assert position.funding_paid == pytest.approx(expected_paid)

    def test_multiple_missed_intervals_dirty_retry_is_exact_no_double(self):
        store = _Store(_activation())
        contour, clock, position = _open(store)
        symbol = position.symbol
        engine = contour.engine
        key = position.position_key

        # Two intervals elapse while the rate feed is down (nothing booked).
        clock.advance(hours=16)
        contour.run_cycle({symbol: 100.0})
        assert position.funding_periods_charged == 0
        assert engine.unsettled_funding_periods() == 2

        # Recovery accrues BOTH intervals in one step — and that write fails.
        store.fail_open_updates = 1
        contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        expected_paid = self._expected_paid(position, 2, 0.01)
        assert position.funding_periods_charged == 2
        assert position.funding_paid == pytest.approx(expected_paid)
        assert engine._dirty_funding[key].funding_periods_charged == 2
        assert engine._dirty_funding[key].funding_paid == pytest.approx(expected_paid)
        assert store.list_positions(status=POSITION_STATUS_OPEN)[0].funding_periods_charged == 0

        # Retry happens WITHOUT any new interval and WITHOUT any rate feed.
        contour.run_cycle({symbol: 100.0})
        stored = store.list_positions(status=POSITION_STATUS_OPEN)[0]
        assert stored.funding_periods_charged == 2                 # no interval lost
        assert stored.funding_paid == pytest.approx(expected_paid)  # exact, no double
        assert position.funding_paid == pytest.approx(expected_paid)
        assert dict(engine.uncommitted_deltas) == {}
        assert key not in engine._dirty_funding

        # Replay cycles change nothing; the next real interval adds once.
        contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert stored.funding_periods_charged == 2
        assert position.funding_paid == pytest.approx(expected_paid)
        clock.advance(hours=8)
        contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert position.funding_periods_charged == 3
        assert position.funding_paid == pytest.approx(
            self._expected_paid(position, 3, 0.01),
        )

    def test_restart_after_dirty_failure_uses_only_confirmed_data(self):
        store = _Store(_activation())
        contour, clock, position = _open(store)
        symbol = position.symbol
        key = position.position_key
        clock.advance(hours=8)

        # Accrual persisted nowhere: dirty only, NO retry before the restart.
        store.fail_open_updates = 1
        contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert key in contour.engine._dirty_funding

        # RESTART: only confirmed store data may be used, and nothing may be
        # claimed durable that the database never accepted.
        contour2 = InverseShortContour(
            contour.settings, store, clock=clock, activation_enabled=True,
        )
        engine2 = contour2.engine
        assert dict(engine2._dirty_funding) == {}        # dirty state is memory-only
        snap = engine2.snapshot({symbol: 100.0})
        assert snap["dirty_funding"] == 0
        assert snap["accounting_consistent"] is True
        assert dict(engine2.uncommitted_deltas) == {}
        assert engine2.balance == pytest.approx(engine2.confirmed_balance)
        restored = next(iter(engine2.open_positions.values()))
        assert restored.funding_periods_charged == 0       # confirmed row only
        assert restored.funding_paid == 0.0
        # The obligation is not lost: it re-derives from the confirmed row.
        assert engine2.unsettled_funding_periods() == 1

        # Data recovery → settled exactly once from confirmed state.
        expected_paid = self._expected_paid(restored, 1, 0.01)
        contour2.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert restored.funding_periods_charged == 1
        assert restored.funding_paid == pytest.approx(expected_paid)
        assert store.list_positions(status=POSITION_STATUS_OPEN)[0].funding_paid == (
            pytest.approx(expected_paid)
        )
        assert engine2.unsettled_funding_periods() == 0

    def test_terminal_close_commits_dirty_funding_and_leaves_outbox_clean(self):
        store = _Store(_activation())
        contour, clock, position = _open(store)
        symbol = position.symbol
        engine = contour.engine
        key = position.position_key

        # The funding accrual's write fails at +8h → DIRTY (memory only).
        clock.advance(hours=8)
        store.fail_open_updates = 10**6
        contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert key in engine._dirty_funding
        assert position.funding_periods_charged == 1

        # Exit cycle: dirty retry and the new-interval accrual keep failing
        # (write path still down) while staging stays healthy → the
        # protective exit executes and its terminal UPDATE commits the
        # cumulative funding, dirty marker included.
        clock.advance(hours=120)                       # total 128h → due 16
        cycle = contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        expected_paid = position.funding_paid
        assert position.funding_periods_charged == 16
        assert cycle["exits"] == 1
        assert key not in engine._dirty_funding        # committed by the close
        assert dict(engine.uncommitted_deltas) == {}
        assert len(engine.pending_closes) == 0
        assert len(store.list_pending_closes()) == 0     # outbox clean
        closed = engine.closed_positions()[0]
        assert closed.status == POSITION_STATUS_CLOSED
        assert closed.funding_paid == pytest.approx(expected_paid)  # exact, once
        assert closed.funding_periods_charged == 16
        stored = store.list_positions(status=POSITION_STATUS_CLOSED)[0]
        assert stored.funding_paid == pytest.approx(expected_paid)
        assert stored.funding_periods_charged == 16

    def test_dirty_funding_retry_never_overwrites_a_pending_close(self):
        store = _Store(_activation())
        contour, clock, position = _open(store)
        symbol = position.symbol
        engine = contour.engine
        key = position.position_key

        # Funding accrual write fails at +8h → DIRTY.
        clock.advance(hours=8)
        store.fail_open_updates = 10**6
        contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert key in engine._dirty_funding
        assert position.funding_periods_charged == 1

        # Exit cycle with BOTH write paths down: staging fails (unstaged →
        # explicit durability risk) and the terminal UPDATE fails → the exit
        # is retained by the existing pending-closes mechanism.
        clock.advance(hours=120)                       # total 128h → due 16
        store.fail_stage = 10**6
        store.fail_close_updates = 10**6
        cycle = contour.run_cycle({symbol: 100.0}, {symbol: 0.01})
        assert cycle["exits"] == 1
        assert len(engine.pending_closes) == 1
        assert engine.durability_risk is True
        assert key in engine._dirty_funding            # retained (retry also failed)

        # Balance reads fail too; the dirty retry must SKIP the pending-close
        # key — an OPEN snapshot may never overwrite terminal facts.
        store.fail_balance_reads = 10**6
        contour.run_cycle({symbol: 100.0})
        assert key in engine._dirty_funding            # skipped, retained
        stored_row = store.list_positions(status=POSITION_STATUS_OPEN)
        assert len(stored_row) == 1                    # row NOT overwritten
        assert stored_row[0].funding_periods_charged == 0  # confirmed only

        # PG fully recovers: the outbox replay owns the key and clears the
        # dirty marker together with the terminal facts.
        _restore(store)
        contour.run_cycle({symbol: 100.0})
        assert len(engine.pending_closes) == 0
        assert key not in engine._dirty_funding
        assert dict(engine.uncommitted_deltas) == {}
        assert engine.durability_risk is False
        closed = engine.closed_positions()[0]
        assert closed.status == POSITION_STATUS_CLOSED
        assert closed.funding_periods_charged == 16
        assert closed.funding_paid == pytest.approx(position.funding_paid)
        assert len(store.list_pending_closes()) == 0


# ======================================================================
# Frozen protocol guard
# ======================================================================

def test_frozen_protocol_fingerprint_unchanged():
    assert (
        protocol_fingerprint()
        == "4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c"
    )
