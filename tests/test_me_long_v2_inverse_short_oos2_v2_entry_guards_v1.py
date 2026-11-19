"""ME_LONG_V2_INVERSE_SHORT_OOS2_V2 — Issue #287 entry guards (unit + parity).

``ME_INVERSE_SHORT_OOS2_ENTRY_GUARDS_AND_CAPACITY_V1`` — the three frozen
entry guards of the OOS-2 v2 contour, proven against the in-memory ledger
(the peer double of the PostgreSQL admission transaction) and against the
frozen OOS-2 geometry parity with the read-only OOS-2 v1 contour:

  1. ``MAX_OPEN_POSITIONS = 12``   — OPEN logical OOS-2-family positions
     (legacy v1 OPEN rows READ-ONLY + this contour; a TP1/TP2 partial close
     does NOT free the slot); V2/V3 are never counted.
  2. ``SINGLE_ACTIVE_SYMBOL = True`` — at most ONE OPEN logical OOS-2
     position per symbol (family-wide).
  3. ``SYMBOL_ENTRY_COOLDOWN_HOURS = 12`` — from the last COMMITTED
     entered_at of an ACCEPTED OOS-2 entry (family, any status: a CLOSED
     position keeps cooling its symbol); refusals never extend the timer
     and a terminally refused event is NEVER replayed.

Terminal refusals (``SYMBOL_ALREADY_OPEN`` / ``SYMBOL_COOLDOWN_12H``)
dispose the event in the same admission critical section
(``SYMBOL_REJECTED`` + reason + ``status_at``) and never book a position or
entry fee.  A cap-bound signal keeps the frozen pending -> TTL contract
(``ENTRY_CAP`` deferred; disposed at TTL).

Concurrency/race/DB-time behaviour is proven against REAL PostgreSQL in
``tests/test_me_long_v2_inverse_short_oos2_v2_pg_admission_it_v1.py``.
"""
from __future__ import annotations

import dataclasses
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.paper.me_long_v2_inverse_short_oos2_v2 import (
    InverseShortContour,
    InverseShortEvent,
    InverseShortPosition,
    InMemoryInverseShortStore,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.activation import (
    ACTIVATION_FLAG,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.capture import (
    capture_with_status,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.config import (
    Oos2V2ConfigError,
    load_oos2_v2_guard_config,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.identity import (
    position_key as build_position_key,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.market_data import (
    MarketDataSnapshot,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_CAPACITY_REJECTED,
    EVENT_STATUS_SYMBOL_REJECTED,
    POSITION_STATUS_OPEN,
    ActivationRecord,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.protocol import (
    ENTRY_MAX_AGE_MINUTES,
    EXPERIMENT_ID,
    GUARD_SCOPE,
    MAX_OPEN_POSITIONS,
    PROTOCOL_VERSION,
    REFUSAL_ENTRY_CAP,
    REFUSAL_SYMBOL_ALREADY_OPEN,
    REFUSAL_SYMBOL_COOLDOWN,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    SYMBOL_ENTRY_COOLDOWN_HOURS,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.store import (
    ADMIT_ADOPT,
    ADMIT_DEFERRED,
    ADMIT_INSERTED,
    ADMIT_NOT_ADMISSIBLE,
    ADMIT_REFUSED_TERMINAL,
    REFUSAL_DISPOSITION_UNCONFIRMED,
    InMemoryLegacyOos2Ledger,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.production_runtime import (
    ProductionRuntime,
)
from app.scanners.models import SetupCandidate, SetupState

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
FREEZE_TS = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# The frozen OOS-2 v2 fingerprint (guard-covered; see protocol.py).
PINNED_FINGERPRINT = (
    "6b091805b0a77fb0c241a9dc7275f7627f2ceebf421f64adc214635d534edbc7"
)


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
        max_open_positions=3,       # must be IGNORED by this contour
        taker_fee=0.00055,
        slippage_percent=0.0005,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )
    defaults.update(overrides)
    return dataclasses.replace(Settings(), **defaults)


def _activation() -> ActivationRecord:
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        protocol_fingerprint=protocol_fingerprint(),
        freeze_ts=FREEZE_TS,
    )


def _candidate(
    symbol: str,
    price: float = 100.0,
    *,
    detected_at: datetime = NOW,
) -> SetupCandidate:
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
        features={
            "exhaustion_magnitude": 0.01,
            "rsi_14": 70.0,
            "rsi_delta_3": 2.0,
            "entry_confirmation_rsi_rising": True,
        },
    )


def _contour(
    settings: Settings | None = None,
    clock: _Clock | None = None,
    legacy: InMemoryLegacyOos2Ledger | None = None,
) -> tuple[InverseShortContour, InMemoryInverseShortStore, _Clock]:
    settings = settings or _settings()
    clock = clock or _Clock(NOW)
    store = InMemoryInverseShortStore(
        _activation(), initial_balance=settings.initial_balance,
        legacy=legacy, now=clock,
    )
    contour = InverseShortContour(
        settings, store, clock=clock, activation_enabled=True,
    )
    assert contour.activated, contour.activation_reason
    return contour, store, clock


def _capture(store: InMemoryInverseShortStore, candidate: SetupCandidate) -> str:
    return capture_with_status(
        store, candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"},
    )


def _open_one(
    contour: InverseShortContour,
    store: InMemoryInverseShortStore,
    clock: _Clock,
    symbol: str = "BTCUSDT",
    price: float = 100.0,
) -> InverseShortPosition:
    """Capture one fresh event and enter it at ``price`` (age 0)."""
    assert _capture(store, _candidate(symbol, price, detected_at=clock())) \
        == "CAPTURED"
    cycle = contour.run_cycle({symbol: price})
    assert cycle["entries"] == 1, cycle
    opened = [
        p for p in contour.engine.open_positions.values() if p.symbol == symbol
    ]
    assert len(opened) == 1
    return opened[0]


def _events(store: InMemoryInverseShortStore, status: str | None = None):
    return store.list_events(status=status)


# ---------------------------------------------------------------------------
# Protocol identity + frozen-geometry parity
# ---------------------------------------------------------------------------
class TestProtocolIdentity:
    def test_fingerprint_pinned_and_guard_covered(self):
        assert protocol_fingerprint() == PINNED_FINGERPRINT
        spec = __import__(
            "app.paper.me_long_v2_inverse_short_oos2_v2.protocol",
            fromlist=["execution_spec"],
        ).execution_spec()
        assert spec["max_open_positions"] == MAX_OPEN_POSITIONS == 12
        assert spec["single_active_symbol"] is True
        assert spec["symbol_entry_cooldown_hours"] == 12
        assert spec["guard_scope"] == GUARD_SCOPE == "OOS2_V1+OOS2_V2"
        assert spec["protocol_version"] == PROTOCOL_VERSION == "2.0.0"

    def test_geometry_and_execution_are_identical_to_frozen_v1(self):
        """Issue #287 must not move any OOS-2 geometry/execution value."""
        from app.paper.me_long_v2_inverse_short_oos2_v1 import protocol as v1p

        v2p = __import__(
            "app.paper.me_long_v2_inverse_short_oos2_v2.protocol",
            fromlist=["execution_spec"],
        )
        v1_spec = dict(v1p.execution_spec())
        v2_spec = dict(v2p.execution_spec())
        identity = {"strategy_id", "namespace", "protocol_version"}
        guards = {
            "max_open_positions", "single_active_symbol",
            "symbol_entry_cooldown_hours", "guard_scope",
        }
        for key in identity:
            v1_spec.pop(key, None)
            v2_spec.pop(key, None)
        for key in guards:
            v1_spec.pop(key, None)
            v2_spec.pop(key, None)
        assert v2_spec == v1_spec

    def test_event_and_position_namespacing_is_v2(self):
        from app.paper.me_long_v2_inverse_short_oos2_v2.identity import (
            event_key as build_event_key,
            position_key as build_position_key_local,
        )

        key = build_position_key_local("deadbeefcafe")
        assert key.startswith("ME_LONG_V2_INVERSE_SHORT_OOS2_V2:")

    def test_five_state_event_disposition(self):
        from app.paper.me_long_v2_inverse_short_oos2_v2.models import (
            EVENT_STATUS_REASONS,
        )

        assert EVENT_STATUS_SYMBOL_REJECTED == "SYMBOL_REJECTED"
        assert REFUSAL_SYMBOL_ALREADY_OPEN in EVENT_STATUS_REASONS
        assert REFUSAL_SYMBOL_COOLDOWN in EVENT_STATUS_REASONS


# ---------------------------------------------------------------------------
# Guard 2: SINGLE_ACTIVE_SYMBOL
# ---------------------------------------------------------------------------
class TestSingleActiveSymbolGuard:
    def test_second_same_symbol_event_is_refused_terminal(self):
        contour, store, clock = _contour()
        _open_one(contour, store, clock, "BTCUSDT")
        clock.advance(minutes=5)  # fresh candle -> fresh deterministic event
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        refused = [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
        ]
        assert len(refused) == 1
        assert refused[0].status_reason == REFUSAL_SYMBOL_ALREADY_OPEN
        assert refused[0].status_at is not None
        assert refused[0].position_key is None

    def test_refused_event_is_never_replayed(self):
        contour, store, clock = _contour()
        _open_one(contour, store, clock, "BTCUSDT")
        clock.advance(minutes=5)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        contour.run_cycle({"BTCUSDT": 100.0})
        # replay attempts: the event is TERMINAL and never re-enters
        for _ in range(3):
            cycle = contour.run_cycle({"BTCUSDT": 100.0})
            assert cycle["entries"] == 0
        assert not _events(store, EVENT_STATUS_CAPTURED)
        assert len(store.list_positions()) == 1

    def test_other_symbols_still_admit_while_one_symbol_is_blocked(self):
        contour, store, clock = _contour()
        _open_one(contour, store, clock, "BTCUSDT")
        assert _capture(store, _candidate("ETHUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle["entries"] == 1, cycle

    def test_partially_closed_position_still_blocks_its_symbol(self):
        """A TP1 partial keeps the OPEN row and the symbol block (Issue #287)."""
        contour, store, clock = _contour()
        _open_one(contour, store, clock, "BTCUSDT")
        position = [
            p for p in store.list_positions() if p.symbol == "BTCUSDT"
        ][0]
        # TP1 fires (price falls to entry*0.95) — partial leg, row stays OPEN.
        contour.run_cycle({"BTCUSDT": position.entry_price * 0.95})
        assert [
            p for p in store.list_positions()
            if p.symbol == "BTCUSDT" and p.status == "OPEN"
        ], "TP1 must leave the position OPEN"
        clock.advance(minutes=5)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        assert [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
            and e.status_reason == REFUSAL_SYMBOL_ALREADY_OPEN
        ]

    def test_legacy_open_duplicate_rows_block_new_family_duplicate(self):
        """Historical duplicate OPEN v1 rows coexist READ-ONLY and still
        refuse new OOS-2 entries on the symbol (family scope)."""
        legacy = InMemoryLegacyOos2Ledger()
        legacy.add("BTCUSDT", NOW - timedelta(hours=3), status="OPEN")
        legacy.add("BTCUSDT", NOW - timedelta(hours=2), status="OPEN")
        contour, store, clock = _contour(legacy=legacy)
        clock.advance(minutes=5)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        # legacy rows were NEVER touched
        assert legacy.open_count() == 2
        assert not store.list_positions()

    def test_concurrent_same_symbol_admits_exactly_one(self):
        legacy = InMemoryLegacyOos2Ledger()
        store = InMemoryInverseShortStore(
            _activation(), legacy=legacy, now=lambda: NOW,
        )
        from app.paper.me_long_v2_inverse_short_oos2_v2.models import (
            InverseShortEvent as _E,
        )
        from app.paper.me_long_v2_inverse_short_oos2_v2.protocol import (
            TRADE_DIRECTION,
        )

        def _mk(key: str) -> InverseShortPosition:
            store.save_event(_E(
                event_key=key,
                symbol="BTCUSDT",
                signal_time=NOW - timedelta(minutes=1),
                captured_at=NOW - timedelta(minutes=1),
                activation_freeze_ts=FREEZE_TS,
                signal_candle_open_time=int(NOW.timestamp() // 300) * 300,
                source_scanner=SOURCE_SCANNER,
                source_scanner_version="1.0.0",
                source_direction=SOURCE_DIRECTION,
                source_setup_id=f"setup-{key}",
            ))
            return InverseShortPosition(
                position_key=build_position_key(key),
                event_key=key,
                symbol="BTCUSDT",
                direction=TRADE_DIRECTION,
                entered_at=NOW,
                deadline_at=NOW + timedelta(minutes=ENTRY_MAX_AGE_MINUTES),
                entry_market_price=100.0,
                entry_price=100.0,
                entry_fee=0.055,
                entry_slippage_cost=0.05,
                stop_price=110.0,
                target_price=95.0,
                tp2_price=93.0,
                tp3_price=90.0,
                position_size=1.0,
                remaining_size=1.0,
                risk_usdt=50.0,
                balance_before=10_000.0,
            )

        positions = [_mk(f"evt-{i:04d}") for i in range(8)]
        results = []
        barrier = threading.Barrier(len(positions))

        def _admit(p: InverseShortPosition) -> None:
            barrier.wait()
            results.append(store.admit_position(p))

        threads = [
            threading.Thread(target=_admit, args=(p,)) for p in positions
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(1 for r in results if r.outcome == ADMIT_INSERTED) == 1
        assert sum(
            1 for r in results if r.outcome == ADMIT_REFUSED_TERMINAL
        ) == len(positions) - 1
        assert len(store.list_positions()) == 1


# ---------------------------------------------------------------------------
# Guard 3: SYMBOL_ENTRY_COOLDOWN_HOURS = 12
# ---------------------------------------------------------------------------
class TestSymbolEntryCooldown12h:
    def test_new_signal_under_12h_is_refused_terminal(self):
        contour, store, clock = _contour()
        first = _open_one(contour, store, clock, "BTCUSDT")
        # close the position (TP3) so the SYMBOL guard cannot bind first
        contour.run_cycle({"BTCUSDT": first.entry_price * 0.89})
        assert not [
            p for p in store.list_positions() if p.status == "OPEN"
        ]
        clock.advance(hours=11, minutes=59)  # 11h59 from the ACCEPTED entry
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        refused = [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
        ]
        assert len(refused) == 1
        assert refused[0].status_reason == REFUSAL_SYMBOL_COOLDOWN

    def test_boundary_exactly_12h_is_admissible(self):
        contour, store, clock = _contour()
        _open_one(contour, store, clock, "BTCUSDT")
        # close the position first (symbol guard would block otherwise)
        _position = [
            p for p in store.list_positions() if p.symbol == "BTCUSDT"
        ][0]
        contour.run_cycle({"BTCUSDT": _position.entry_price * 0.89})  # TP3
        assert not [
            p for p in store.list_positions() if p.status == "OPEN"
        ]
        clock.advance(hours=12)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle

    def test_cooldown_runs_from_entry_not_from_close(self):
        contour, store, clock = _contour()
        _open_one(contour, store, clock, "BTCUSDT")
        _position = [
            p for p in store.list_positions() if p.symbol == "BTCUSDT"
        ][0]
        clock.advance(hours=2)
        # terminal close at T+2h — cooldown keeps running from ENTERED_AT
        contour.run_cycle({"BTCUSDT": _position.entry_price * 0.89})
        clock.advance(hours=11)  # T+13h from entry, T+11h from close
        # boundary: entry was 13h ago -> admissible
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle

    def test_refusals_never_extend_the_timer(self):
        contour, store, clock = _contour()
        first = _open_one(contour, store, clock, "BTCUSDT")
        entered_at = first.entered_at
        # refused signals while the position is OPEN (terminal refusals)
        for _ in range(3):
            clock.advance(minutes=5)
            assert _capture(
                store, _candidate("BTCUSDT", detected_at=clock()),
            ) == "CAPTURED"
            cycle = contour.run_cycle({"BTCUSDT": 100.0})
            assert cycle["entries"] == 0
        assert len([
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
        ]) == 3
        # close at T0+15m — cooldown still runs from ENTERED_AT, not the close
        contour.run_cycle({"BTCUSDT": first.entry_price * 0.89})
        assert not [
            p for p in store.list_positions() if p.status == "OPEN"
        ]
        # timer measured from the single ACCEPTED entry at ``entered_at``:
        # exactly 12h later the boundary is admissible (refusals did not
        # extend the timer, the close did not restart it).
        clock.now = entered_at + timedelta(hours=12)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle

    def test_closed_position_keeps_cooling_its_symbol(self):
        contour, store, clock = _contour()
        first = _open_one(contour, store, clock, "BTCUSDT")
        contour.run_cycle({"BTCUSDT": first.entry_price * 0.89})  # TP3 close
        clock.advance(hours=6)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        assert [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
            and e.status_reason == REFUSAL_SYMBOL_COOLDOWN
        ]

    def test_legacy_v1_entered_at_also_cools_the_symbol(self):
        """Family scope: an ACCEPTED legacy v1 entry cools the symbol for v2."""
        legacy = InMemoryLegacyOos2Ledger()
        legacy.add("BTCUSDT", NOW - timedelta(hours=2), status="CLOSED")
        contour, store, clock = _contour(legacy=legacy)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        assert [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
            and e.status_reason == REFUSAL_SYMBOL_COOLDOWN
        ]
        assert legacy.open_count() == 0
        # ...and the legacy row was never written to
        assert legacy.last_entered_at()["BTCUSDT"] == NOW - timedelta(hours=2)

    def test_cooldown_and_symbol_checked_before_capacity(self):
        """Terminal refusals fire even while the family cap is full (the
        event is disposed, never parked as ENTRY_CAP)."""
        legacy = InMemoryLegacyOos2Ledger()
        contour, store, clock = _contour(legacy=legacy)
        _open_one(contour, store, clock, "BTCUSDT")
        for i in range(MAX_OPEN_POSITIONS - 1):
            _open_one(contour, store, clock, f"SYM{i:02d}")
        assert len(store.list_positions()) == MAX_OPEN_POSITIONS
        clock.advance(minutes=5)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle(
            {f"SYM{i:02d}": 100.0 for i in range(MAX_OPEN_POSITIONS - 1)}
            | {"BTCUSDT": 100.0},
        )
        assert cycle["entries"] == 0
        assert [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
            and e.status_reason == REFUSAL_SYMBOL_ALREADY_OPEN
        ]


# ---------------------------------------------------------------------------
# Guard 1: MAX_OPEN_POSITIONS = 12 (family)
# ---------------------------------------------------------------------------
class TestFamilyCapacityTwelve:
    def test_twelve_symbols_admit_and_the_thirteenth_waits_for_ttl(self):
        contour, store, clock = _contour()
        prices = {}
        for i in range(MAX_OPEN_POSITIONS):
            symbol = f"SYM{i:02d}"
            prices[symbol] = 100.0
            assert _capture(store, _candidate(symbol, detected_at=clock())) \
                == "CAPTURED"
            cycle = contour.run_cycle(prices)
            assert cycle["entries"] == 1, (i, cycle)
        assert len(store.list_positions()) == MAX_OPEN_POSITIONS
        # 13th: DEFERRED ENTRY_CAP — stays CAPTURED (pending -> TTL contract)
        assert _capture(store, _candidate("SYM99", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle(prices | {"SYM99": 100.0})
        assert cycle["entries"] == 0
        pending = _events(store, EVENT_STATUS_CAPTURED)
        assert [e.event_key for e in pending] == [
            e.event_key for e in _events(store) if e.symbol == "SYM99"
        ]
        # store-level decision says ENTRY_CAP (not a symbol/cooldown refusal)
        decision = store.admit_position(
            dataclasses.replace(
                store.list_positions()[0],
                position_key=build_position_key(
                    _events(store, EVENT_STATUS_CAPTURED)[0].event_key,
                ),
                event_key=_events(store, EVENT_STATUS_CAPTURED)[0].event_key,
                symbol="SYM99",
            )
        )
        assert decision.outcome == ADMIT_DEFERRED
        assert decision.reason == REFUSAL_ENTRY_CAP

    def test_thirteenth_is_disposed_at_ttl_as_capacity_rejected(self):
        contour, store, clock = _contour()
        prices = {}
        for i in range(MAX_OPEN_POSITIONS):
            symbol = f"SYM{i:02d}"
            prices[symbol] = 100.0
            assert _capture(store, _candidate(symbol, detected_at=clock())) \
                == "CAPTURED"
            assert contour.run_cycle(prices)["entries"] == 1
        assert _capture(store, _candidate("SYM99", detected_at=clock())) \
            == "CAPTURED"
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)
        contour.run_cycle(prices | {"SYM99": 100.0})
        disposed = [
            e for e in _events(store, EVENT_STATUS_CAPACITY_REJECTED)
        ]
        assert len(disposed) == 1
        assert disposed[0].status_reason == REFUSAL_ENTRY_CAP
        assert disposed[0].status_at is not None
        assert not _events(store, EVENT_STATUS_CAPTURED)

    def test_a_confirmed_close_frees_the_slot(self):
        contour, store, clock = _contour()
        prices = {}
        for i in range(MAX_OPEN_POSITIONS):
            symbol = f"SYM{i:02d}"
            prices[symbol] = 100.0
            assert _capture(store, _candidate(symbol, detected_at=clock())) \
                == "CAPTURED"
            assert contour.run_cycle(prices)["entries"] == 1
        closed = store.list_positions()[0]
        contour.run_cycle(prices | {closed.symbol: closed.entry_price * 0.89})
        assert [
            p for p in store.list_positions() if p.status == "OPEN"
        ].__len__() == MAX_OPEN_POSITIONS - 1
        assert _capture(store, _candidate("SYM99", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle(prices | {"SYM99": 100.0})
        assert cycle["entries"] == 1, cycle

    def test_tp1_partial_never_frees_the_slot(self):
        contour, store, clock = _contour()
        prices = {}
        for i in range(MAX_OPEN_POSITIONS):
            symbol = f"SYM{i:02d}"
            prices[symbol] = 100.0
            assert _capture(store, _candidate(symbol, detected_at=clock())) \
                == "CAPTURED"
            assert contour.run_cycle(prices)["entries"] == 1
        partial = store.list_positions()[0]
        prices[partial.symbol] = partial.entry_price * 0.95  # TP1 only
        contour.run_cycle(prices)
        assert len([
            p for p in store.list_positions() if p.status == "OPEN"
        ]) == MAX_OPEN_POSITIONS, "TP1 partial must keep its slot"
        assert _capture(store, _candidate("SYM99", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle(prices | {"SYM99": 100.0})
        assert cycle["entries"] == 0, cycle
        assert [
            e for e in _events(store, EVENT_STATUS_CAPTURED)
            if e.symbol == "SYM99"
        ], "13th stays pending until TTL while a partial keeps its slot"

    def test_cap_counts_the_family_only(self):
        legacy = InMemoryLegacyOos2Ledger()
        for i in range(5):
            legacy.add(f"LEG{i:02d}", NOW - timedelta(days=1), status="OPEN")
        contour, store, clock = _contour(legacy=legacy)
        state = store.cross_contour_state()
        assert state.open_total == 5 + 0
        prices = {}
        for i in range(MAX_OPEN_POSITIONS - 5):
            symbol = f"SYM{i:02d}"
            prices[symbol] = 100.0
            assert _capture(store, _candidate(symbol, detected_at=clock())) \
                == "CAPTURED"
            assert contour.run_cycle(prices)["entries"] == 1
        state = store.cross_contour_state()
        assert state.open_total == MAX_OPEN_POSITIONS
        assert state.legacy_open == 5
        assert state.v2_open == MAX_OPEN_POSITIONS - 5
        # family full -> 13th family signal deferred
        assert _capture(store, _candidate("SYM99", detected_at=clock())) \
            == "CAPTURED"
        assert contour.run_cycle(prices | {"SYM99": 100.0})["entries"] == 0


# ---------------------------------------------------------------------------
# Fail-closed entries / adoption
# ---------------------------------------------------------------------------
class TestFailClosedEntries:
    def test_legacy_unavailable_blocks_entries_but_not_exits(self):
        legacy = InMemoryLegacyOos2Ledger()
        contour, store, clock = _contour(legacy=legacy)
        position = _open_one(contour, store, clock, "BTCUSDT")
        legacy.unavailable = True
        assert _capture(store, _candidate("ETHUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle(
            {"BTCUSDT": position.entry_price * 0.95, "ETHUSDT": 100.0},
        )
        assert cycle["entries"] == 0
        # exits still work: TP1 partial booked on BTCUSDT
        assert [
            l for l in store.list_close_legs(position.position_key)
        ], "exits must proceed while entries fail closed"
        assert _events(store, EVENT_STATUS_CAPTURED), "entry stays pending"

    def test_legacy_still_admitting_blocks_v2_entries(self):
        legacy = InMemoryLegacyOos2Ledger(activation_status="RUNNING")
        contour, store, clock = _contour(legacy=legacy)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0
        assert not store.list_positions()
        assert _events(store, EVENT_STATUS_CAPTURED), "entry stays pending"

    def test_fingerprint_mismatch_fails_closed(self):
        contour, store, clock = _contour()
        bad = _candidate("BTCUSDT", detected_at=clock())
        assert _capture(store, bad) == "CAPTURED"
        event = store.list_events()[0]
        event.protocol_hash = "0" * 64
        decision = store.admit_position(
            InverseShortPosition(
                position_key=build_position_key(event.event_key),
                event_key=event.event_key,
                symbol="BTCUSDT",
                direction="SHORT",
                entered_at=clock(),
                deadline_at=clock() + timedelta(hours=2),
                entry_market_price=100.0,
                entry_price=100.0,
                entry_fee=0.0,
                entry_slippage_cost=0.0,
                stop_price=110.0,
                target_price=95.0,
                tp2_price=93.0,
                tp3_price=90.0,
                position_size=1.0,
                remaining_size=1.0,
                risk_usdt=1.0,
                balance_before=10_000.0,
            ),
        )
        assert decision.outcome == ADMIT_NOT_ADMISSIBLE
        assert decision.reason == "EVENT_FINGERPRINT_MISMATCH"

    def test_adoption_of_a_committed_row_is_never_a_refusal(self):
        legacy = InMemoryLegacyOos2Ledger()
        store = InMemoryInverseShortStore(
            _activation(), legacy=legacy, now=lambda: NOW,
        )
        from app.paper.me_long_v2_inverse_short_oos2_v2.models import (
            InverseShortEvent as _E,
        )

        event = _E(
            event_key="evt-adopt",
            symbol="BTCUSDT",
            signal_time=NOW - timedelta(minutes=1),
            captured_at=NOW - timedelta(minutes=1),
            activation_freeze_ts=FREEZE_TS,
            signal_candle_open_time=int(NOW.timestamp() // 300) * 300,
            source_scanner=SOURCE_SCANNER,
            source_scanner_version="1.0.0",
            source_direction=SOURCE_DIRECTION,
            source_setup_id="setup-adopt",
        )
        store.save_event(event)
        position = InverseShortPosition(
            position_key=build_position_key(event.event_key),
            event_key=event.event_key,
            symbol="BTCUSDT",
            direction="SHORT",
            entered_at=NOW,
            deadline_at=NOW + timedelta(hours=2),
            entry_market_price=100.0,
            entry_price=100.0,
            entry_fee=0.055,
            entry_slippage_cost=0.05,
            stop_price=110.0,
            target_price=95.0,
            tp2_price=93.0,
            tp3_price=90.0,
            position_size=1.0,
            remaining_size=1.0,
            risk_usdt=50.0,
            balance_before=10_000.0,
        )
        first = store.admit_position(position)
        assert first.outcome == ADMIT_INSERTED
        # ambiguous COMMIT replay: the same key resolves to ADOPT, never a
        # symbol/cooldown refusal and never a second row/fee.
        replay = store.admit_position(position)
        assert replay.outcome == ADMIT_ADOPT
        assert len(store.list_positions()) == 1


# ---------------------------------------------------------------------------
# Strict guard configuration (fingerprint-covered)
# ---------------------------------------------------------------------------
class TestGuardConfig:
    def test_defaults_equal_the_frozen_values(self):
        config = load_oos2_v2_guard_config()
        assert config.max_open_positions == 12
        assert config.single_active_symbol is True
        assert config.symbol_entry_cooldown_hours == 12

    def test_unknown_keys_are_rejected(self, tmp_path):
        path = tmp_path / "config.me_inverse_short_oos2_v2.json"
        path.write_text(
            '{"max_open_positions": 12, "typo_key": 1}', encoding="utf-8",
        )
        with pytest.raises(Oos2V2ConfigError, match="UNKNOWN_CONFIG_KEYS"):
            load_oos2_v2_guard_config(path)

    def test_deviating_value_is_guard_mismatch(self, tmp_path):
        path = tmp_path / "config.me_inverse_short_oos2_v2.json"
        path.write_text('{"max_open_positions": 7}', encoding="utf-8")
        with pytest.raises(Oos2V2ConfigError, match="GUARD_MISMATCH"):
            load_oos2_v2_guard_config(path)

    def test_env_deviation_aborts(self):
        with pytest.raises(Oos2V2ConfigError, match="GUARD_MISMATCH"):
            load_oos2_v2_guard_config(
                environ={"ME_INVERSE_SHORT_OOS2_V2_MAX_OPEN_POSITIONS": "7"},
            )

    def test_env_equal_to_frozen_value_is_accepted(self):
        config = load_oos2_v2_guard_config(
            environ={
                "ME_INVERSE_SHORT_OOS2_V2_MAX_OPEN_POSITIONS": "12",
                "ME_INVERSE_SHORT_OOS2_V2_SINGLE_ACTIVE_SYMBOL": "1",
                "ME_INVERSE_SHORT_OOS2_V2_SYMBOL_ENTRY_COOLDOWN_HOURS": "12",
            },
        )
        assert config.as_dict() == {
            "max_open_positions": 12,
            "single_active_symbol": True,
            "symbol_entry_cooldown_hours": 12,
        }

    def test_type_violations_are_rejected(self, tmp_path):
        path = tmp_path / "config.me_inverse_short_oos2_v2.json"
        path.write_text('{"single_active_symbol": "yes"}', encoding="utf-8")
        with pytest.raises(Oos2V2ConfigError, match="CONFIG_VALUE_TYPE"):
            load_oos2_v2_guard_config(path)


# ---------------------------------------------------------------------------
# Migration 091 static contract
# ---------------------------------------------------------------------------
class TestMigration091Contract:
    @staticmethod
    def _sql() -> str:
        return (
            REPO_ROOT / "sql" / "migrations"
            / "091_me_long_v2_inverse_short_oos2_v2.sql"
        ).read_text(encoding="utf-8")

    def test_five_state_disposition_and_reasons(self):
        sql = self._sql()
        assert "'CAPTURED', 'ENTERED', 'EXPIRED', 'CAPACITY_REJECTED'" in sql
        assert "'SYMBOL_REJECTED'" in sql
        assert "'SYMBOL_ALREADY_OPEN'" in sql
        assert "'SYMBOL_COOLDOWN_12H'" in sql

    def test_guard_indexes(self):
        sql = self._sql()
        assert "uq_me_inverse_short_oos2_v2_position_symbol_open" in sql
        assert "idx_me_inverse_short_oos2_v2_position_symbol_entered" in sql

    def test_migration_never_touches_legacy_v1_tables(self):
        import re

        statements = re.findall(
            r"(?:CREATE|ALTER|DROP)\s+TABLE[^\n;]*", self._sql(),
            flags=re.IGNORECASE,
        )
        offenders = [s for s in statements if "oos2_v1" in s]
        assert not offenders, offenders

    def test_migration_is_idempotent(self):
        sql = self._sql()
        assert "CREATE TABLE IF NOT EXISTS" in sql
        assert "DROP TRIGGER IF EXISTS" in sql
        assert "CREATE OR REPLACE FUNCTION" in sql


# ---------------------------------------------------------------------------
# Review P2-1: ENTRY_CAP telemetry uses the OOS-2 FAMILY capacity
# ---------------------------------------------------------------------------
def _result(**overrides) -> dict:
    result = {
        "activated": True,
        "exit_only": False,
        "durability_risk": False,
        "accounting_consistent": True,
        "pending_events_unavailable": False,
        "open_positions": 0,
    }
    result.update(overrides)
    return result


class TestEntryCapTelemetryFamilyScope:
    """``_entry_refusals`` must reflect the AUTHORITATIVE admission gate —
    OPEN logical positions over the OOS-2 family (legacy v1 READ-ONLY +
    this contour) — never this engine's LOCAL open count (review P2-1:
    with family 12/12 but v2 local < 12 the telemetry was silent).
    An unreadable family view is a DISTINCT ``..._UNAVAILABLE`` cause,
    never an invented zero, and V2/V3 are never mixed in."""

    @staticmethod
    def _runtime(
        legacy: InMemoryLegacyOos2Ledger, clock: _Clock,
    ) -> tuple[ProductionRuntime, InMemoryInverseShortStore]:
        settings = _settings()
        store = InMemoryInverseShortStore(
            _activation(), initial_balance=settings.initial_balance,
            legacy=legacy, now=clock,
        )
        runtime = ProductionRuntime(
            settings, store, provider=object(), clock=clock,
            activation_enabled=True,
        )
        assert runtime.contour.activated, runtime.contour.activation_reason
        return runtime, store

    @staticmethod
    def _refusals(runtime: ProductionRuntime, result=None, guard=None) -> list:
        snapshot = MarketDataSnapshot(observed_at=runtime._clock())
        return runtime._entry_refusals(
            result or _result(), snapshot, set(), 0, guard,
        )

    def test_family_twelve_of_twelve_reports_entry_cap_at_local_two(self):
        # 10 OPEN legacy v1 + 2 OPEN v2 = 12/12 while the LOCAL count is 2.
        legacy = InMemoryLegacyOos2Ledger()
        for i in range(10):
            legacy.add(f"L{i:02d}USDT", NOW - timedelta(hours=30))
        clock = _Clock(NOW)
        runtime, store = self._runtime(legacy, clock)
        _open_one(runtime.contour, store, clock, "BTCUSDT")
        _open_one(runtime.contour, store, clock, "ETHUSDT")
        guard = runtime.contour.engine.entry_guard_snapshot()
        assert guard["oos2_family_available"] is True
        assert guard["legacy_open"] == 10
        assert guard["v2_open"] == 2
        assert guard["open_total"] == MAX_OPEN_POSITIONS == 12
        # the LOCAL count alone (2 < 12) would have stayed silent
        refusals = self._refusals(runtime, _result(open_positions=2), guard)
        assert "ENTRY_CAP" in refusals

    def test_v2_local_twelve_open_reports_entry_cap(self):
        legacy = InMemoryLegacyOos2Ledger()
        clock = _Clock(NOW)
        runtime, store = self._runtime(legacy, clock)
        for i in range(MAX_OPEN_POSITIONS):
            _open_one(runtime.contour, store, clock, f"SYM{i:02d}USDT")
        guard = runtime.contour.engine.entry_guard_snapshot()
        assert guard["legacy_open"] == 0
        assert guard["v2_open"] == MAX_OPEN_POSITIONS
        assert guard["open_total"] == MAX_OPEN_POSITIONS
        refusals = self._refusals(
            runtime, _result(open_positions=MAX_OPEN_POSITIONS), guard,
        )
        assert "ENTRY_CAP" in refusals

    def test_family_eleven_of_twelve_never_reports_entry_cap(self):
        # 5 legacy + 6 v2 = 11 < 12: no ENTRY_CAP, however the local count
        # and the family count are read (no false refusal either way).
        legacy = InMemoryLegacyOos2Ledger()
        for i in range(5):
            legacy.add(f"L{i:02d}USDT", NOW - timedelta(hours=30))
        clock = _Clock(NOW)
        runtime, store = self._runtime(legacy, clock)
        for i in range(6):
            _open_one(runtime.contour, store, clock, f"SYM{i:02d}USDT")
        guard = runtime.contour.engine.entry_guard_snapshot()
        assert guard["open_total"] == 11
        refusals = self._refusals(runtime, _result(open_positions=6), guard)
        assert "ENTRY_CAP" not in refusals
        assert "ENTRY_CAP_UNAVAILABLE" not in refusals

    def test_unavailable_family_is_unavailable_not_invented_zero(self):
        legacy = InMemoryLegacyOos2Ledger()
        for i in range(10):
            legacy.add(f"L{i:02d}USDT", NOW - timedelta(hours=30))
        clock = _Clock(NOW)
        runtime, store = self._runtime(legacy, clock)
        _open_one(runtime.contour, store, clock, "BTCUSDT")
        legacy.unavailable = True
        guard = runtime.contour.engine.entry_guard_snapshot()
        assert guard["oos2_family_available"] is False
        assert guard["open_total"] is None      # NEVER an invented zero
        assert guard["legacy_open"] is None
        assert guard["v2_open"] is None
        refusals = self._refusals(runtime, _result(open_positions=1), guard)
        assert "ENTRY_CAP_UNAVAILABLE" in refusals
        assert "ENTRY_CAP" not in refusals

    def test_tp_partial_still_counts_as_an_open_family_slot(self):
        # A TP1 partial close keeps the OPEN row (and its family slot):
        # 11 legacy + 1 partially-closed v2 = 12/12 -> ENTRY_CAP.
        legacy = InMemoryLegacyOos2Ledger()
        for i in range(11):
            legacy.add(f"L{i:02d}USDT", NOW - timedelta(hours=30))
        clock = _Clock(NOW)
        runtime, store = self._runtime(legacy, clock)
        position = _open_one(runtime.contour, store, clock, "BTCUSDT")
        runtime.contour.run_cycle({"BTCUSDT": position.entry_price * 0.95})
        stored = [p for p in store.list_positions() if p.symbol == "BTCUSDT"][0]
        assert stored.status == POSITION_STATUS_OPEN
        assert stored.remaining_size < stored.position_size   # TP1 partial
        guard = runtime.contour.engine.entry_guard_snapshot()
        assert guard["v2_open"] == 1
        assert guard["open_total"] == MAX_OPEN_POSITIONS == 12
        refusals = self._refusals(runtime, _result(open_positions=1), guard)
        assert "ENTRY_CAP" in refusals


# ---------------------------------------------------------------------------
# Review P2-2: terminal refusal only after VERIFIED durable disposition
# ---------------------------------------------------------------------------
class TestTerminalRefusalDurableDispositionInMemory:
    """In-memory symmetry with the PostgreSQL admission: ``REFUSED_TERMINAL``
    is reported ONLY when the event's disposition actually landed.  A missed
    disposition resolves to ``ADOPT_EXISTING`` when a committed position
    exists (never ``EXPIRED-with-position``) and FAILS CLOSED with the
    explicit consistency reason otherwise — never a false refusal."""

    def test_verified_disposition_reports_refused_terminal(self):
        contour, store, clock = _contour()
        _open_one(contour, store, clock, "BTCUSDT")
        clock.advance(minutes=5)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        contour.run_cycle({"BTCUSDT": 100.0})
        refused = _events(store, EVENT_STATUS_SYMBOL_REJECTED)
        assert len(refused) == 1
        assert refused[0].status_reason == REFUSAL_SYMBOL_ALREADY_OPEN
        assert refused[0].status_at is not None

    def test_missed_disposition_with_committed_position_adopts(self):
        contour, store, clock = _contour()
        first = _open_one(contour, store, clock, "BTCUSDT")
        # The disposition can NEVER land for an event whose position is
        # committed (P1-2 guard: no EXPIRED-with-position) — the refusal
        # must resolve to ADOPT_EXISTING, not to a claimed terminal refusal.
        redelivery = dataclasses.replace(
            first, position_key=build_position_key("redelivery"),
        )
        decision = store._refuse_symbol_locked(
            redelivery, REFUSAL_SYMBOL_ALREADY_OPEN, clock(),
        )
        assert decision.outcome == ADMIT_ADOPT
        assert not _events(store, EVENT_STATUS_SYMBOL_REJECTED)
        assert len(store.list_positions()) == 1

    def test_missed_disposition_without_position_fails_closed(
        self, monkeypatch,
    ):
        contour, store, clock = _contour()
        first = _open_one(contour, store, clock, "BTCUSDT")
        clock.advance(minutes=5)
        assert _capture(store, _candidate("BTCUSDT", detected_at=clock())) \
            == "CAPTURED"
        event = _events(store, EVENT_STATUS_CAPTURED)[0]
        attempt = dataclasses.replace(
            first,
            position_key=build_position_key(event.event_key),
            event_key=event.event_key,
            entered_at=clock(),
        )
        # Fault injection: the disposition reports success=False although
        # the event is CAPTURED and has no position.
        monkeypatch.setattr(store, "mark_event_expired", lambda *a, **k: False)
        decision = store.admit_position(attempt)
        assert decision.outcome == ADMIT_NOT_ADMISSIBLE
        assert decision.reason == REFUSAL_DISPOSITION_UNCONFIRMED
        assert decision.outcome != ADMIT_REFUSED_TERMINAL
        # no false terminal state, no orphan, no position
        assert not _events(store, EVENT_STATUS_SYMBOL_REJECTED)
        assert [
            e for e in _events(store) if e.event_key == event.event_key
        ][0].status == EVENT_STATUS_CAPTURED
        assert len(store.list_positions()) == 1
