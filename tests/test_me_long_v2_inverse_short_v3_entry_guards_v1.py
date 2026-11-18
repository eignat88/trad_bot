"""ME_LONG_V2_INVERSE_SHORT_V3 — Issue #285 entry guards (in-memory contract).

``ME_INVERSE_SHORT_V3_ENTRY_GUARDS_AND_CAPACITY_V1``: 12 positions counted
OVER BOTH CONTOURS, single-symbol admission over BOTH contours, 12h
per-symbol entry cooldown from the last COMMITTED entered_at, safe V2 drain
semantics — proven here against the symmetric in-memory store; the real
cross-process/cross-contour races are proven in the PostgreSQL suite
(``test_me_long_v2_inverse_short_v3_pg_admission_it_v1.py``).

Contract under test (Issue #285 acceptance):

* exactly one INSERT / one fee entry for two events of the same symbol even
  ~9 ms apart; the loser event is terminally disposed (SYMBOL_REJECTED /
  SYMBOL_ALREADY_OPEN), never left CAPTURED for a later replay;
* an OPEN V2 position blocks the same symbol in V3 (legacy V2 duplicates
  are only ever read — never modified, never re-created);
* cooldown: closes do NOT restart the timer (it runs from the accepted
  ENTRY), a new signal under 12h is blocked, exactly 12h is admissible,
  refused/blocked signals never extend the timer, the blocked event is
  terminal and a NEW eligible signal is required;
* 12 different symbols admit; the 13th stays PENDING under the frozen
  ENTRY_CAP / ENTRY_TTL contract; a confirmed durable close frees a slot;
* every guard failure classifies only after a trustworthy outcome, fail
  closed for NEW ENTRIES only — protective exits keep running;
* V2 detector / geometry / sizing / fees / slippage / funding / TTL are
  untouched (parity with the V2 protocol is asserted explicitly).
"""
from __future__ import annotations

import dataclasses
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.me_long_v2_inverse_short_v3.activation import ActivationIdentity
from app.paper.me_long_v2_inverse_short_v3.capture import (
    CAPTURED,
    capture_with_status,
)
from app.paper.me_long_v2_inverse_short_v3.config import (
    V3ConfigError,
    V3GuardConfig,
    load_v3_guard_config,
)
from app.paper.me_long_v2_inverse_short_v3.engine import InverseShortPaperEngine
from app.paper.me_long_v2_inverse_short_v3.models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_CAPACITY_REJECTED,
    EVENT_STATUS_ENTERED,
    EVENT_STATUS_EXPIRED,
    EVENT_STATUS_SYMBOL_REJECTED,
    EVENT_REASON_ENTRY_CAP,
    EVENT_REASON_SYMBOL_ALREADY_OPEN,
    EVENT_REASON_SYMBOL_COOLDOWN,
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
    ActivationRecord,
)
from app.paper.me_long_v2_inverse_short_v3.protocol import (
    ENTRY_MAX_AGE_MINUTES,
    EXPERIMENT_ID,
    MAX_OPEN_POSITIONS_TOTAL,
    SINGLE_ACTIVE_SYMBOL,
    STRATEGY_ID,
    SYMBOL_ENTRY_COOLDOWN_HOURS,
    execution_spec,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v3.store import (
    ADMIT_ADOPT,
    ADMIT_DEFERRED,
    ADMIT_INSERTED,
    ADMIT_NOT_ADMISSIBLE,
    ADMIT_REFUSED_TERMINAL,
    InMemoryInverseShortStore,
    InMemoryV2Ledger,
)
from app.scanners.models import SetupCandidate, SetupState

# Pinned V3 fingerprint (guard parameters are part of the frozen spec).
PINNED_V3_FINGERPRINT = (
    "c10eb00f2777c2ca9b42d465ccbad4dc7908d86676b678fddcff5722f6f80aa4"
)

FREEZE_TS = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


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


def _identity() -> ActivationIdentity:
    identity, _reason = (
        __import__(
            "app.paper.me_long_v2_inverse_short_v3.activation",
            fromlist=["evaluate_activation"],
        ).evaluate_activation(_activation(), enabled=True)
    )
    assert identity is not None
    return identity


def _store(
    clock: _Clock,
    *,
    peer: InMemoryV2Ledger | None = None,
    activation: ActivationRecord | None = None,
) -> InMemoryInverseShortStore:
    return InMemoryInverseShortStore(
        activation if activation is not None else _activation(),
        initial_balance=_settings().initial_balance,
        peer=peer if peer is not None else InMemoryV2Ledger(),
        now=clock,
    )


def _candidate(
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    *,
    detected_at: datetime,
) -> SetupCandidate:
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


_CAPTURE_ENV = {"ME_INVERSE_SHORT_V3_ENABLED": "1"}


def _capture(store, candidate) -> str:
    status = capture_with_status(
        store, candidate, env=_CAPTURE_ENV, clock=store._now,
    )
    assert status == CAPTURED, status
    return status


def _engine(store, clock: _Clock, **overrides) -> InverseShortPaperEngine:
    return InverseShortPaperEngine(
        _settings(**overrides), store, clock=clock, activation=_identity(),
    )


def _events(store, status: str | None = None):
    return store.list_events(status=status)


# ---------------------------------------------------------------------------
# 1. Identity + frozen protocol parity with V2
# ---------------------------------------------------------------------------
class TestV3ProtocolIdentity:
    def test_fingerprint_pinned_and_guard_covered(self):
        assert protocol_fingerprint() == PINNED_V3_FINGERPRINT
        spec = execution_spec()
        assert spec["strategy_id"] == STRATEGY_ID
        assert spec["max_open_positions_total"] == MAX_OPEN_POSITIONS_TOTAL == 12
        assert spec["single_active_symbol"] == SINGLE_ACTIVE_SYMBOL is True
        assert (
            spec["symbol_entry_cooldown_hours"]
            == SYMBOL_ENTRY_COOLDOWN_HOURS
            == 12
        )
        assert spec["cross_contour_scope"] == "V2+V3"

    def test_geometry_and_execution_are_identical_to_v2(self):
        """Issue #285 forbids ANY change to V2 detector/geometry/sizing."""
        from app.paper.me_long_v2_inverse_short_v2 import protocol as v2p

        assert STRATEGY_ID != v2p.STRATEGY_ID
        assert protocol_fingerprint() != v2p.protocol_fingerprint()
        for name in (
            "TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "MAX_HOLD_MINUTES",
            "ENTRY_MAX_AGE_MINUTES", "DCA_ENABLED", "TRAILING_ENABLED",
            "BREAKEVEN_ENABLED", "ENTRY_MODEL", "EXIT_PRIORITY",
            "INTRABAR_POLICY", "EXIT_REASONS", "FEE_PER_SIDE",
            "SLIPPAGE_PER_SIDE", "FUNDING_SETTLEMENT", "SOURCE_SCANNER",
            "SOURCE_DIRECTION", "TRADE_DIRECTION",
        ):
            ours = getattr(
                __import__(
                    "app.paper.me_long_v2_inverse_short_v3.protocol",
                    fromlist=[name],
                ),
                name,
            )
            assert ours == getattr(v2p, name), f"V3 changed frozen {name}"
        for price in (1.0, 123.45, 0.00012):
            assert (
                __import__(
                    "app.paper.me_long_v2_inverse_short_v3.protocol",
                    fromlist=["short_geometry"],
                ).short_geometry(price)
                == v2p.short_geometry(price)
            )

    def test_event_and_position_namespacing_is_v3(self):
        from app.paper.me_long_v2_inverse_short_v3.identity import (
            event_key,
            position_key,
        )

        candidate = _candidate(detected_at=T0)
        key = event_key(candidate)
        assert position_key(key).startswith(f"{STRATEGY_ID}:")


# ---------------------------------------------------------------------------
# 2. SINGLE_ACTIVE_SYMBOL across V2+V3
# ---------------------------------------------------------------------------
class TestSingleActiveSymbolGuard:
    def test_two_events_same_symbol_admitted_in_one_cycle_open_exactly_one(self):
        """The production AKEUSDT failure: two SL entries of ONE symbol ~9 ms
        apart.  Two DISTINCT source events (different signal candles) of the
        same symbol admitted in the same cycle must produce exactly one
        INSERT / one fee entry — the loser is terminally disposed."""
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)

        first = _candidate("AKEUSDT", detected_at=T0)
        second = _candidate("AKEUSDT", detected_at=T0 - timedelta(minutes=5))
        _capture(store, first)
        _capture(store, second)

        opened = engine.open_entries({"AKEUSDT": 100.0})
        assert len(opened) == 1
        assert len(store.list_positions()) == 1  # exactly ONE row
        assert engine.admission_stats["admitted"] == 1
        assert engine.admission_stats["refused_symbol_open"] == 1

        # Exactly one fee entry: the loser never books an entry cost.
        assert opened[0].entry_fee > 0
        loser = [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
        ]
        assert len(loser) == 1
        assert loser[0].status_reason == EVENT_REASON_SYMBOL_ALREADY_OPEN
        assert loser[0].status_at is not None

    def test_blocked_event_is_terminal_and_never_replayed(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        first = _candidate("AKEUSDT", detected_at=T0)
        second = _candidate("AKEUSDT", detected_at=T0 - timedelta(minutes=5))
        _capture(store, first)
        _capture(store, second)
        engine.open_entries({"AKEUSDT": 100.0})

        # Later cycles never reopen the disposed event, even when the
        # symbol frees up: the event is TERMINAL.
        before = len(store.list_positions())
        clock.advance(minutes=30)
        engine.open_entries({"AKEUSDT": 100.0})
        assert len(store.list_positions()) == before
        rejected = [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].status_reason == EVENT_REASON_SYMBOL_ALREADY_OPEN

    def test_open_v2_position_blocks_same_symbol_in_v3(self):
        clock = _Clock(T0)
        peer = InMemoryV2Ledger()
        peer.add("CLUSDT", T0 - timedelta(hours=3))
        peer.add("CLUSDT", T0 - timedelta(hours=3, milliseconds=9))  # legacy dup
        store = _store(clock, peer=peer)
        engine = _engine(store, clock)

        _capture(store, _candidate("CLUSDT", detected_at=T0))
        opened = engine.open_entries({"CLUSDT": 100.0})
        assert opened == []
        rejected = [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].status_reason == EVENT_REASON_SYMBOL_ALREADY_OPEN

    def test_v2_rows_are_read_only_and_unchanged(self):
        clock = _Clock(T0)
        peer = InMemoryV2Ledger()
        a = peer.add("CLUSDT", T0 - timedelta(hours=3))
        b = peer.add("CLUSDT", T0 - timedelta(hours=3, milliseconds=9))
        store = _store(clock, peer=peer)
        engine = _engine(store, clock)
        _capture(store, _candidate("CLUSDT", detected_at=T0))
        engine.open_entries({"CLUSDT": 100.0})

        # Historical V2 duplicates unchanged: same rows, same statuses,
        # same entered_at — V3 only ever READS the V2 ledger.
        assert set(peer._positions) == {a.position_key, b.position_key}
        for p in (a, b):
            stored = peer._positions[p.position_key]
            assert stored.status == POSITION_STATUS_OPEN
            assert stored.entered_at == p.entered_at

    def test_other_symbols_still_admit_while_one_symbol_is_blocked(self):
        clock = _Clock(T0)
        peer = InMemoryV2Ledger()
        peer.add("CLUSDT", T0 - timedelta(hours=3))
        store = _store(clock, peer=peer)
        engine = _engine(store, clock)

        _capture(store, _candidate("CLUSDT", detected_at=T0))
        _capture(store, _candidate("BTCUSDT", detected_at=T0))
        opened = engine.open_entries({"CLUSDT": 100.0, "BTCUSDT": 100.0})
        assert [p.symbol for p in opened] == ["BTCUSDT"]

    def test_concurrent_same_symbol_events_open_exactly_one(self):
        """The store-level admission critical section serializes admissions."""
        clock = _Clock(T0)
        store = _store(clock)
        a = _candidate("XRPUSDT", detected_at=T0)
        b = _candidate("XRPUSDT", detected_at=T0 - timedelta(minutes=5))
        _capture(store, a)
        _capture(store, b)

        from app.paper.me_long_v2_inverse_short_v3.capture import (
            event_from_candidate,
        )

        decisions = []

        def _admit(candidate):
            event = event_from_candidate(
                candidate,
                capture_route="PRE_FILTER_SOURCE",
                activation=_identity(),
                captured_at=T0,
            )
            position = dataclasses.replace(
                _build_test_position(event),
            )
            decisions.append(store.admit_position(position))

        threads = [
            threading.Thread(target=_admit, args=(c,)) for c in (a, b)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        outcomes = sorted(d.outcome for d in decisions)
        assert outcomes == sorted([ADMIT_INSERTED, ADMIT_REFUSED_TERMINAL])
        assert len(store.list_positions()) == 1


def _build_test_position(event) -> "object":
    from app.paper.me_long_v2_inverse_short_v3.models import InverseShortPosition
    from app.paper.me_long_v2_inverse_short_v3.identity import position_key
    from app.paper.me_long_v2_inverse_short_v3.protocol import short_geometry

    stop, target = short_geometry(100.0)
    return InverseShortPosition(
        position_key=position_key(event.event_key),
        event_key=event.event_key,
        symbol=event.symbol,
        direction="SHORT",
        entered_at=T0,
        deadline_at=T0 + timedelta(minutes=7200),
        entry_market_price=100.0,
        entry_price=100.0,
        entry_fee=0.055,
        entry_slippage_cost=0.05,
        stop_price=stop,
        target_price=target,
        position_size=0.5,
        risk_usdt=5.0,
        balance_before=10_000.0,
    )


# ---------------------------------------------------------------------------
# 3. Per-symbol entry cooldown 12h (from the ACCEPTED ENTRY)
# ---------------------------------------------------------------------------
class TestSymbolEntryCooldown12h:
    def _enter_and_close(self, clock, store, engine, symbol, *, close_after):
        _capture(store, _candidate(symbol, detected_at=T0))
        opened = engine.open_entries({symbol: 100.0})
        assert len(opened) == 1
        entered_at = opened[0].entered_at
        clock.advance(**close_after)
        # EXACTLY at the 110 stop -> STOP_LOSS (any price strictly above
        # the stop is classified STOP_LOSS_GAP by the frozen gap rule).
        closed = engine.check_exits({symbol: opened[0].stop_price})
        assert [p.exit_reason for p in closed] == ["STOP_LOSS"]
        engine.flush_pending_closes()
        return entered_at

    def test_new_signal_under_12h_from_entry_is_blocked(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        entered_at = self._enter_and_close(
            clock, store, engine, "BTCUSDT", close_after={"minutes": 5},
        )
        # 11h59m after the ENTRY (well after the close): still blocked.
        clock.now = entered_at + timedelta(hours=11, minutes=59)
        _capture(store, _candidate("BTCUSDT", detected_at=clock.now))
        opened = engine.open_entries({"BTCUSDT": 100.0})
        assert opened == []
        rejected = [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].status_reason == EVENT_REASON_SYMBOL_COOLDOWN

    def test_boundary_exactly_12h_is_admissible(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        entered_at = self._enter_and_close(
            clock, store, engine, "BTCUSDT", close_after={"minutes": 5},
        )
        clock.now = entered_at + timedelta(hours=12)  # boundary: >= 12h
        _capture(store, _candidate("BTCUSDT", detected_at=clock.now))
        opened = engine.open_entries({"BTCUSDT": 100.0})
        assert len(opened) == 1

    def test_cooldown_runs_from_entry_not_from_close(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        entered_at = self._enter_and_close(
            clock, store, engine, "ETHUSDT",
            close_after={"hours": 5},  # the CLOSE is 5h after the entry
        )
        # ~7h after the close (= 12h-ε after the entry): still blocked.
        clock.now = entered_at + timedelta(hours=11, minutes=59)
        _capture(store, _candidate("ETHUSDT", detected_at=clock.now))
        assert engine.open_entries({"ETHUSDT": 100.0}) == []

    def test_rejected_signals_never_extend_the_timer(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        entered_at = self._enter_and_close(
            clock, store, engine, "BTCUSDT", close_after={"minutes": 5},
        )
        # Refused signals inside the cooldown never move the timer.
        for offset_min in (30, 60, 90):
            clock.now = entered_at + timedelta(minutes=offset_min)
            _capture(store, _candidate("BTCUSDT", detected_at=clock.now))
            engine.open_entries({"BTCUSDT": 100.0})
        state = store.cross_contour_state()
        assert state.last_entered_at["BTCUSDT"] == entered_at
        # The original boundary still holds after the refused signals.
        clock.now = entered_at + timedelta(hours=11, minutes=59)
        _capture(store, _candidate("BTCUSDT", detected_at=clock.now))
        assert engine.open_entries({"BTCUSDT": 100.0}) == []
        clock.now = entered_at + timedelta(hours=12, minutes=1)
        _capture(store, _candidate("BTCUSDT", detected_at=clock.now))
        assert len(engine.open_entries({"BTCUSDT": 100.0})) == 1

    def test_v2_entry_also_cools_the_symbol_down(self):
        clock = _Clock(T0)
        peer = InMemoryV2Ledger()
        # A V2 entry 2h ago that has since CLOSED still cools the symbol:
        # the cooldown runs from the accepted ENTRY, never from the close.
        peer.add(
            "SOLUSDT", T0 - timedelta(hours=2),
            status=POSITION_STATUS_CLOSED,
        )
        store = _store(clock, peer=peer)
        engine = _engine(store, clock)

        _capture(store, _candidate("SOLUSDT", detected_at=T0))
        assert engine.open_entries({"SOLUSDT": 100.0}) == []
        rejected = [
            e for e in _events(store)
            if e.status == EVENT_STATUS_SYMBOL_REJECTED
        ]
        assert len(rejected) == 1
        assert rejected[0].status_reason == EVENT_REASON_SYMBOL_COOLDOWN


# ---------------------------------------------------------------------------
# 4. Global capacity: OPEN V2 + OPEN V3 <= 12
# ---------------------------------------------------------------------------
class TestGlobalCapacity12:
    SYMBOLS = [
        "S01USDT", "S02USDT", "S03USDT", "S04USDT", "S05USDT", "S06USDT",
        "S07USDT", "S08USDT", "S09USDT", "S10USDT", "S11USDT", "S12USDT",
        "S13USDT",
    ]

    def test_twelve_symbols_admit_and_the_thirteenth_stays_pending(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        for symbol in self.SYMBOLS:
            _capture(store, _candidate(symbol, detected_at=T0))
        opened = engine.open_entries({s: 100.0 for s in self.SYMBOLS})
        assert len(opened) == 12
        assert len(store.list_positions()) == 12

        # Exactly one event stays CAPTURED (deferred) under the frozen
        # contract — it is NOT terminally rejected before its TTL, and the
        # deferred symbol is the one with no position.
        pending = _events(store, status=EVENT_STATUS_CAPTURED)
        assert len(pending) == 1
        opened_symbols = {p.symbol for p in store.list_positions()}
        assert pending[0].symbol not in opened_symbols

    def test_store_defers_the_thirteenth_with_entry_cap(self):
        """Store-level classification: the cap-bound refusal is DEFERRED,
        never terminal, never a fee — and the event stays CAPTURED."""
        clock = _Clock(T0)
        store = _store(clock)
        from app.paper.me_long_v2_inverse_short_v3.capture import (
            event_from_candidate,
        )

        for i, symbol in enumerate(self.SYMBOLS[:13]):
            candidate = _candidate(symbol, detected_at=T0)
            _capture(store, candidate)
            event = event_from_candidate(
                candidate,
                capture_route="PRE_FILTER_SOURCE",
                activation=_identity(),
                captured_at=T0,
            )
            position = _build_test_position(event)
            decision = store.admit_position(position)
            if i < 12:
                assert decision.outcome == ADMIT_INSERTED
            else:
                assert decision.outcome == ADMIT_DEFERRED
                assert decision.reason == EVENT_REASON_ENTRY_CAP
        assert len(store.list_positions()) == 12
        # The deferred event owns no position and stays CAPTURED.
        deferred = [e for e in _events(store) if e.symbol == "S13USDT"]
        assert [e.status for e in deferred] == [EVENT_STATUS_CAPTURED]

    def test_thirteenth_is_disposed_at_ttl_per_entry_cap_contract(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        for symbol in self.SYMBOLS:
            _capture(store, _candidate(symbol, detected_at=T0))
        engine.open_entries({s: 100.0 for s in self.SYMBOLS})

        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES + 1)
        engine.open_entries({s: 100.0 for s in self.SYMBOLS})
        rejected = _events(store, status=EVENT_STATUS_CAPACITY_REJECTED)
        assert len(rejected) == 1
        assert rejected[0].status_reason == EVENT_REASON_ENTRY_CAP
        assert rejected[0].status_at is not None
        opened_symbols = {p.symbol for p in store.list_positions()}
        assert rejected[0].symbol not in opened_symbols

    def test_cap_counts_v2_and_v3_together(self):
        clock = _Clock(T0)
        peer = InMemoryV2Ledger()
        for i in range(5):
            peer.add(f"V2P{i}USDT", T0 - timedelta(hours=20))
        store = _store(clock, peer=peer)
        engine = _engine(store, clock)
        for symbol in self.SYMBOLS[:8]:
            _capture(store, _candidate(symbol, detected_at=T0))
        opened = engine.open_entries({s: 100.0 for s in self.SYMBOLS[:8]})
        assert len(opened) == 7  # 5 V2 + 7 V3 = 12
        pending = _events(store, status=EVENT_STATUS_CAPTURED)
        assert len(pending) == 1
        opened_symbols = {p.symbol for p in store.list_positions()}
        assert pending[0].symbol not in opened_symbols

    def test_confirmed_close_frees_the_slot(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        for symbol in self.SYMBOLS[:13]:
            _capture(store, _candidate(symbol, detected_at=T0))
        assert len(engine.open_entries({s: 100.0 for s in self.SYMBOLS[:13]})) == 12

        # A CONFIRMED durable close frees the slot …
        closed = engine.check_exits(
            {"S01USDT": next(
                p.stop_price for p in store.list_positions()
                if p.symbol == "S01USDT")})
        assert len(closed) == 1
        engine.flush_pending_closes()
        assert engine.pending_closes == {}
        # … and only then may the deferred event enter.
        opened = engine.open_entries({s: 100.0 for s in self.SYMBOLS[:13]})
        assert len(opened) == 1
        assert len(store.list_positions(status=POSITION_STATUS_OPEN)) == 12


# ---------------------------------------------------------------------------
# 5. Fail closed for NEW entries — exits never affected
# ---------------------------------------------------------------------------
class TestFailClosedEntries:
    def _engine_with_open(self, clock, store, engine, symbol="BTCUSDT"):
        _capture(store, _candidate(symbol, detected_at=T0))
        opened = engine.open_entries({symbol: 100.0})
        assert len(opened) == 1
        return opened[0]

    def test_v2_state_unavailable_blocks_entries_but_not_exits(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        position = self._engine_with_open(clock, store, engine)

        store.peer.unavailable = True
        _capture(store, _candidate("ETHUSDT", detected_at=T0))
        assert engine.open_entries({"ETHUSDT": 100.0}) == []
        # Fail closed = no entry, no fee, event stays CAPTURED for retry.
        pending = _events(store, status=EVENT_STATUS_CAPTURED)
        assert [e.symbol for e in pending] == ["ETHUSDT"]

        # Protective exits keep running in the same state.
        closed = engine.check_exits({"BTCUSDT": position.stop_price})
        assert [p.exit_reason for p in closed] == ["STOP_LOSS"]
        assert closed[0].position_key == position.position_key

    def test_v2_still_admitting_blocks_v3_entries(self):
        clock = _Clock(T0)
        peer = InMemoryV2Ledger(activation_status="RUNNING")
        store = _store(clock, peer=peer)
        engine = _engine(store, clock)
        _capture(store, _candidate("BTCUSDT", detected_at=T0))
        assert engine.open_entries({"BTCUSDT": 100.0}) == []
        assert _events(store, status=EVENT_STATUS_CAPTURED) != []

    def test_missing_price_never_guesses_a_fill(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        _capture(store, _candidate("BTCUSDT", detected_at=T0))
        assert engine.open_entries({}) == []
        assert len(store.list_positions()) == 0

    def test_fingerprint_mismatch_fails_closed(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = InverseShortPaperEngine(
            _settings(), store, clock=clock, activation=_identity(),
        )
        _capture(store, _candidate("BTCUSDT", detected_at=T0))
        # The activation fingerprint goes stale AFTER capture: admission
        # fails closed and the event is never entered.
        store.set_activation(_activation(fingerprint="0" * 64))
        assert engine.open_entries({"BTCUSDT": 100.0}) == []
        assert len(store.list_positions()) == 0


# ---------------------------------------------------------------------------
# 6. Exit semantics unchanged + refusal accounting
# ---------------------------------------------------------------------------
class TestExitsAndAccountingUnchanged:
    def test_stop_tp_and_timeout_semantics_unchanged(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
            _capture(store, _candidate(symbol, detected_at=T0))
        opened = engine.open_entries(
            {"AAAUSDT": 100.0, "BBBUSDT": 100.0, "CCCUSDT": 100.0})
        assert len(opened) == 3

        # STOP_FIRST on a shared bar: the STOP of AAA and the TP of BBB,
        # evaluated at each position's own frozen stop/target.
        by_symbol = {p.symbol: p for p in opened}
        closed = engine.check_exits({
            "AAAUSDT": by_symbol["AAAUSDT"].stop_price,
            "BBBUSDT": by_symbol["BBBUSDT"].target_price,
        })
        reasons = sorted(p.exit_reason for p in closed)
        assert reasons == ["STOP_LOSS", "TAKE_PROFIT_1"]

        # TIMEOUT at exactly deadline (7200 min): CCC only.
        clock.advance(minutes=7201)
        closed = engine.check_exits({"CCCUSDT": 100.0})
        assert [p.exit_reason for p in closed] == ["TIMEOUT"]

    def test_adoption_of_a_committed_row_is_never_a_refusal(self):
        clock = _Clock(T0)
        store = _store(clock)
        from app.paper.me_long_v2_inverse_short_v3.capture import (
            event_from_candidate,
        )

        candidate = _candidate("BTCUSDT", detected_at=T0)
        _capture(store, candidate)
        event = event_from_candidate(
            candidate,
            capture_route="PRE_FILTER_SOURCE",
            activation=_identity(),
            captured_at=T0,
        )
        position = _build_test_position(event)
        first = store.admit_position(position)
        second = store.admit_position(position)
        assert first.outcome == ADMIT_INSERTED
        assert second.outcome == ADMIT_ADOPT
        # The ambiguous case is NEVER classified as a cooldown/symbol
        # refusal and never disposes the event.
        assert [e.status for e in _events(store)] == [EVENT_STATUS_ENTERED] or (
            [e.status for e in _events(store)] == [EVENT_STATUS_CAPTURED]
        )


# ---------------------------------------------------------------------------
# 7. Strict V3 guard configuration (Issue #285)
# ---------------------------------------------------------------------------
class TestV3GuardConfig:
    def test_defaults_equal_the_frozen_spec(self):
        config = load_v3_guard_config(environ={})
        assert config == V3GuardConfig(
            max_open_positions=12,
            single_active_symbol=True,
            symbol_entry_cooldown_hours=12,
        )

    def test_unknown_config_keys_are_never_silently_ignored(self, tmp_path):
        path = tmp_path / "config.me_inverse_short_v3.json"
        path.write_text(
            '{"max_open_positions": 12, "mystery_field": 1}', "utf-8")
        with pytest.raises(V3ConfigError, match="UNKNOWN_CONFIG_KEYS"):
            load_v3_guard_config(path, environ={})

    def test_env_mismatch_with_the_fingerprint_aborts(self):
        with pytest.raises(V3ConfigError, match="GUARD_MISMATCH"):
            load_v3_guard_config(
                environ={"ME_INVERSE_SHORT_V3_MAX_OPEN_POSITIONS": "20"},
            )
        with pytest.raises(V3ConfigError, match="GUARD_MISMATCH"):
            load_v3_guard_config(
                environ={"ME_INVERSE_SHORT_V3_SYMBOL_ENTRY_COOLDOWN_HOURS": "24"},
            )
        with pytest.raises(V3ConfigError, match="GUARD_MISMATCH"):
            load_v3_guard_config(
                environ={"ME_INVERSE_SHORT_V3_SINGLE_ACTIVE_SYMBOL": "0"},
            )

    def test_valid_env_and_file_pass(self, tmp_path):
        path = tmp_path / "config.me_inverse_short_v3.json"
        path.write_text(
            '{"max_open_positions": 12, "single_active_symbol": true, '
            '"symbol_entry_cooldown_hours": 12}',
            "utf-8",
        )
        config = load_v3_guard_config(path, environ={})
        assert config.max_open_positions == 12
