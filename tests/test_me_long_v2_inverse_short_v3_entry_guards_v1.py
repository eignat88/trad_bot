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


def _identity(status: str = "RUNNING") -> ActivationIdentity:
    identity, _reason = (
        __import__(
            "app.paper.me_long_v2_inverse_short_v3.activation",
            fromlist=["evaluate_activation"],
        ).evaluate_activation(_activation(status=status), enabled=True)
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
        source_scanner=event.source_scanner,
        source_setup_id=event.source_setup_id,
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


# ---------------------------------------------------------------------------
# 8. Codex review P1 — committed-position recovery is INDEPENDENT of capacity
# ---------------------------------------------------------------------------
class TestP1CommittedRecoveryBeyondCapacity:
    """A LOST COMMIT ACKNOWLEDGEMENT must never strand a durable position.

    When PostgreSQL applies the INSERT but the acknowledgement is lost, the
    row is durable while ``open_positions`` never saw it.  Such a row must be
    adopted under STOP/TP/TIMEOUT management even when all 12 slots are
    occupied — and adoption must never re-charge entry fees (accounting
    reconciliation stays intact).
    """

    SYMBOLS = [f"R{i:02d}USDT" for i in range(1, 15)]

    @staticmethod
    def _orphan_commit(store, symbol, *, detected_at):
        """Simulate the lost-COMMIT state: the store INSERT is durable but
        the engine was never told (event stays CAPTURED, no memory entry)."""
        from app.paper.me_long_v2_inverse_short_v3.capture import (
            event_from_candidate,
        )
        candidate = _candidate(symbol, detected_at=detected_at)
        _capture(store, candidate)
        event = event_from_candidate(
            candidate,
            capture_route="PRE_FILTER_SOURCE",
            activation=_identity(),
            captured_at=detected_at,
        )
        decision = store.admit_position(_build_test_position(event))
        assert decision.outcome == ADMIT_INSERTED
        return event

    def test_lost_commit_is_adopted_even_at_full_capacity(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        for symbol in self.SYMBOLS[:11]:
            _capture(store, _candidate(symbol, detected_at=T0))
        assert len(engine.open_entries({s: 100.0 for s in self.SYMBOLS})) == 11

        # The 12th admission commits durably but its ACK is lost.
        self._orphan_commit(store, self.SYMBOLS[11], detected_at=T0)
        assert len(engine.open_positions) == 11  # unmanaged orphan
        assert store.cross_contour_state().open_total == 12  # book FULL

        balance_before = engine.balance
        adopted = engine.open_entries({s: 100.0 for s in self.SYMBOLS})
        # P1: recovery runs regardless of the full 12-slot portfolio.
        assert [p.symbol for p in adopted] == [self.SYMBOLS[11]]
        assert len(engine.open_positions) == 12
        # No double fee: the orphan's entry costs are booked EXACTLY ONCE —
        # via the confirmed ledger — so the balance moves by precisely the
        # orphan's entry cost, and accounting reconciliation confirms.
        balance_after_adoption = engine.balance
        assert balance_after_adoption == pytest.approx(
            balance_before - (0.055 + 0.05))
        assert engine.accounting_consistent

        # A second pass is idempotent: adoption never re-books anything.
        assert engine.open_entries({s: 100.0 for s in self.SYMBOLS}) == []
        assert engine.balance == balance_after_adoption

        # Protective management attaches immediately: the orphan is closed
        # by the STOP rule like any other position.
        orphan = next(
            p for p in engine.open_positions.values()
            if p.symbol == self.SYMBOLS[11])
        closed = engine.check_exits({self.SYMBOLS[11]: orphan.stop_price})
        assert [p.exit_reason for p in closed] == ["STOP_LOSS"]
        engine.flush_pending_closes()

    def test_restart_restores_committed_row_when_every_slot_is_full(self):
        from app.paper.me_long_v2_inverse_short_v3.models import (
            POSITION_STATUS_CLOSED,
        )

        clock = _Clock(T0)
        peer = InMemoryV2Ledger()
        store = _store(clock, peer=peer)
        # The V3 row commits while slots are free; then V2 fills EVERY slot.
        self._orphan_commit(store, "RP1USDT", detected_at=T0)
        for i in range(12):
            peer.add(f"V2P{i}USDT", T0 - timedelta(hours=20))
        assert store.cross_contour_state().open_total == 13  # all slots full

        # RESTART: the constructor restores every committed V3 row —
        # portfolio capacity is never consulted for recovery.
        engine = _engine(store, clock)
        assert [p.symbol for p in engine.open_positions.values()] == ["RP1USDT"]

        orphan = next(iter(engine.open_positions.values()))
        closed = engine.check_exits({"RP1USDT": orphan.stop_price})
        assert [p.exit_reason for p in closed] == ["STOP_LOSS"]
        engine.flush_pending_closes()
        assert store.list_positions(
            status=POSITION_STATUS_CLOSED)[0].symbol == "RP1USDT"

    def test_slot_release_after_recovery_admits_the_deferred_signal(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        for symbol in self.SYMBOLS[:11]:
            _capture(store, _candidate(symbol, detected_at=T0))
        assert len(engine.open_entries({s: 100.0 for s in self.SYMBOLS})) == 11
        self._orphan_commit(store, self.SYMBOLS[11], detected_at=T0)
        _capture(store, _candidate(self.SYMBOLS[12], detected_at=T0))

        # At capacity: the orphan is ADOPTED and the eligible 13th signal
        # merely WAITS (deferred, still CAPTURED) — never terminally rejected.
        engine.open_entries({s: 100.0 for s in self.SYMBOLS})
        assert len(engine.open_positions) == 12
        pending = _events(store, status=EVENT_STATUS_CAPTURED)
        assert [e.symbol for e in pending] == [self.SYMBOLS[12]]

        # A confirmed close frees the slot — only then the waiter enters.
        victim = next(
            p for p in engine.open_positions.values()
            if p.symbol == self.SYMBOLS[0])
        engine.check_exits({self.SYMBOLS[0]: victim.stop_price})
        engine.flush_pending_closes()
        opened = engine.open_entries({s: 100.0 for s in self.SYMBOLS})
        assert [p.symbol for p in opened] == [self.SYMBOLS[12]]

    def test_drainging_lost_commit_is_managed_without_restart(self):
        """Codex follow-up: COMMIT ok -> ack lost -> V3 goes DRAINING ->
        the process keeps running WITHOUT a restart.

        ``open_entries()`` is forbidden in EXIT_ONLY mode — the committed row
        must nevertheless be recovered in memory and served under STOP/TP/
        TIMEOUT by the MANAGEMENT contour (``check_exits``), independently
        of the permission to open new entries.
        """
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        # A normal position keeps being managed across the transition.
        _capture(store, _candidate("KEEPUSDT", detected_at=T0))
        assert len(engine.open_entries({"KEEPUSDT": 100.0})) == 1

        # The second admission commits durably — its ACKNOWLEDGEMENT is lost.
        self._orphan_commit(store, "DRNUSDT", detected_at=T0)

        # V3 flips to DRAINING mid-run (the runner's refresh_activation()
        # swaps the identity in place) — NO restart happens.
        engine.activation = _identity(status="DRAINING")
        assert engine.activation.exit_only

        # open_entries() is blocked by EXIT_ONLY and stays blocked ...
        assert engine.open_entries({"DRNUSDT": 100.0, "KEEPUSDT": 100.0}) == []
        assert "DRNUSDT" not in [
            p.symbol for p in engine.open_positions.values()]

        # ... yet the management contour recovers the committed row in
        # memory and serves it under STOP/TP/TIMEOUT from then on.
        assert engine.check_exits({"DRNUSDT": 100.0, "KEEPUSDT": 100.0}) == []
        assert "DRNUSDT" in [
            p.symbol for p in engine.open_positions.values()]
        orphan = next(
            p for p in engine.open_positions.values()
            if p.symbol == "DRNUSDT")
        closed = engine.check_exits(
            {"DRNUSDT": orphan.stop_price, "KEEPUSDT": 100.0})
        assert [p.symbol for p in closed] == ["DRNUSDT"]
        assert [p.exit_reason for p in closed] == ["STOP_LOSS"]
        engine.flush_pending_closes()
        assert engine.accounting_consistent


class _FlakyRecoveryStore:
    """Store proxy simulating a PostgreSQL connection error on the SECOND
    ``has_position()`` call for ONE key — the re-probe inside ``_open_event``
    — while the recovery probe (first call) succeeds.  Mirrors the review
    scenario: first has_position() OK, second raises."""

    def __init__(self, store, fail_key: str) -> None:
        self._store = store
        self._fail_key = fail_key
        self._calls: dict[str, int] = {}

    def __getattr__(self, name):
        return getattr(self._store, name)

    def has_position(self, position_key: str) -> bool:
        count = self._calls.get(position_key, 0) + 1
        self._calls[position_key] = count
        if position_key == self._fail_key and count >= 2:
            raise ConnectionError("simulated PostgreSQL connection error")
        return self._store.has_position(position_key)


def _pos_key(event) -> str:
    from app.paper.me_long_v2_inverse_short_v3.identity import position_key
    return position_key(event.event_key)


# ---------------------------------------------------------------------------
# 10. Codex review P1 (follow-up) — recovery errors are isolated per row
# ---------------------------------------------------------------------------
class TestP1RecoveryErrorIsolation:
    """A PostgreSQL error during ONE committed-row recovery must never
    interrupt the management of the remaining book: no position loses its
    STOP/TP/TIMEOUT, the failed row is not considered recovered, and no fee
    is ever booked by a failed attempt."""

    _orphan_commit = staticmethod(
        TestP1CommittedRecoveryBeyondCapacity._orphan_commit)

    @pytest.mark.parametrize(
        "exit_kind", ["STOP_LOSS", "TAKE_PROFIT_1", "TIMEOUT"])
    def test_recovery_error_is_isolated_and_exits_still_close(self, exit_kind):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        _capture(store, _candidate("KEEPUSDT", detected_at=T0))
        assert len(engine.open_entries({"KEEPUSDT": 100.0})) == 1
        keep = next(iter(engine.open_positions.values()))
        event = self._orphan_commit(store, "DRNUSDT", detected_at=T0)

        # First has_position() (recovery probe) succeeds, the second one
        # (inside _open_event) raises a connection error.
        engine.store = _FlakyRecoveryStore(store, fail_key=_pos_key(event))
        if exit_kind == "STOP_LOSS":
            prices = {"KEEPUSDT": keep.stop_price, "DRNUSDT": 100.0}
        elif exit_kind == "TAKE_PROFIT_1":
            prices = {"KEEPUSDT": keep.target_price, "DRNUSDT": 100.0}
        else:  # TIMEOUT — past the frozen 7200-minute deadline
            clock.advance(hours=121)
            prices = {"KEEPUSDT": 100.0, "DRNUSDT": 100.0}

        closed = engine.check_exits(prices)
        # The recovery error must NOT interrupt the management contour:
        # the OTHER position is closed by its STOP/TP/TIMEOUT in the SAME
        # cycle.
        assert [p.symbol for p in closed] == ["KEEPUSDT"]
        assert [p.exit_reason for p in closed] == [exit_kind]
        engine.flush_pending_closes()
        # The failed row is NOT considered recovered.
        assert all(
            p.symbol != "DRNUSDT" for p in engine.open_positions.values())
        assert engine.accounting_consistent

    def test_recovery_error_books_nothing_and_retry_is_exactly_once(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        event = self._orphan_commit(store, "DRNUSDT", detected_at=T0)
        engine.store = _FlakyRecoveryStore(store, fail_key=_pos_key(event))
        balance_before = engine.balance

        assert engine.check_exits({"DRNUSDT": 100.0}) == []
        # A failed recovery books NOTHING: no entry cost, no fee, no PnL.
        assert engine.balance == balance_before
        assert engine.open_positions == {}
        assert engine.pending_closes == {}
        assert engine.accounting_consistent

        # A healthy retry adopts the row and books its entry costs
        # EXACTLY ONCE (the failed attempt booked nothing).
        engine.store = store
        engine.check_exits({"DRNUSDT": 100.0})
        assert "DRNUSDT" in [
            p.symbol for p in engine.open_positions.values()]
        assert engine.balance == pytest.approx(
            balance_before - (0.055 + 0.05))

    def test_recovery_error_isolates_multiple_orphans(self):
        clock = _Clock(T0)
        store = _store(clock)
        engine = _engine(store, clock)
        _capture(store, _candidate("KEEPUSDT", detected_at=T0))
        assert len(engine.open_entries({"KEEPUSDT": 100.0})) == 1
        keep = next(iter(engine.open_positions.values()))
        ev_a = self._orphan_commit(store, "DRNAUSDT", detected_at=T0)
        self._orphan_commit(store, "DRNBUSDT", detected_at=T0)
        self._orphan_commit(store, "DRNCUSDT", detected_at=T0)
        engine.store = _FlakyRecoveryStore(store, fail_key=_pos_key(ev_a))

        closed = engine.check_exits(
            {
                "KEEPUSDT": keep.stop_price,
                "DRNAUSDT": 100.0, "DRNBUSDT": 100.0, "DRNCUSDT": 100.0,
            })
        # The STOP close still happens this cycle …
        assert [p.symbol for p in closed] == ["KEEPUSDT"]
        managed = {p.symbol for p in engine.open_positions.values()}
        # … the healthy orphans are recovered despite the failing one …
        assert {"DRNBUSDT", "DRNCUSDT"} <= managed
        # … and only the failing row stays out (retried next cycle).
        assert "DRNAUSDT" not in managed

        engine.flush_pending_closes()
        engine.store = store
        engine.check_exits({"DRNAUSDT": 100.0})
        assert "DRNAUSDT" in [
            p.symbol for p in engine.open_positions.values()]


# ---------------------------------------------------------------------------
# 9. Codex review P2 — terminal symbol guards run even at full capacity
# ---------------------------------------------------------------------------
class TestP2TerminalGuardsAtFullCapacity:
    """SYMBOL_ALREADY_OPEN / SYMBOL_COOLDOWN_12H must dispose pending events
    TERMINALLY and atomically even when all 12 slots are taken; only
    symbol+cooldown-ELIGIBLE signals may wait for a free slot until TTL."""

    def _full_book(self, clock):
        peer = InMemoryV2Ledger()
        for i in range(12):
            peer.add(f"V2P{i}USDT", clock.now - timedelta(hours=13))
        return peer

    def test_symbol_open_refusal_terminal_even_at_full_capacity(self):
        clock = _Clock(T0)
        peer = self._full_book(clock)
        store = _store(clock, peer=peer)
        engine = _engine(store, clock)
        _capture(store, _candidate("V2P0USDT", detected_at=T0))
        _capture(store, _candidate("CLEANUSDT", detected_at=T0))
        engine.open_entries({"V2P0USDT": 100.0, "CLEANUSDT": 100.0})

        rejected = _events(store, status=EVENT_STATUS_SYMBOL_REJECTED)
        assert [e.symbol for e in rejected] == ["V2P0USDT"]
        assert rejected[0].status_reason == EVENT_REASON_SYMBOL_ALREADY_OPEN
        assert rejected[0].status_at is not None
        # The eligible signal only waits; nothing opens at full capacity.
        waiting = _events(store, status=EVENT_STATUS_CAPTURED)
        assert [e.symbol for e in waiting] == ["CLEANUSDT"]
        assert engine.open_positions == {}

        # The V2 position closes a minute later: the OLD signal must NEVER
        # open — it was disposed terminally at capacity.  TWO slots are
        # freed so both the deferred waiter and the NEW signal fit.
        from app.paper.me_long_v2_inverse_short_v3.models import (
            POSITION_STATUS_CLOSED,
        )
        for sym in ("V2P0USDT", "V2P1USDT"):
            victim = next(
                p for p in peer._positions.values() if p.symbol == sym)
            peer._positions[victim.position_key] = dataclasses.replace(
                victim, status=POSITION_STATUS_CLOSED)

        _capture(store, _candidate(
            "V2P0USDT", detected_at=T0 + timedelta(minutes=30)))
        opened = engine.open_entries({"V2P0USDT": 100.0, "CLEANUSDT": 100.0})
        opened_symbols = {p.symbol for p in opened}
        # Only the NEW signal (and the deferred waiter) enter on the freed
        # slot(s) — the terminally rejected event is never replayed.
        assert opened_symbols == {"V2P0USDT", "CLEANUSDT"}
        statuses = {e.symbol: e.status for e in _events(store)}
        assert statuses["CLEANUSDT"] == EVENT_STATUS_ENTERED
        old_rejected = [
            e for e in _events(store, status=EVENT_STATUS_SYMBOL_REJECTED)
            if e.symbol == "V2P0USDT"]
        assert len(old_rejected) == 1  # still rejected, never reopened

    def test_cooldown_refusal_terminal_even_at_full_capacity(self):
        from app.paper.me_long_v2_inverse_short_v3.models import (
            POSITION_STATUS_CLOSED,
        )

        clock = _Clock(T0)
        peer = self._full_book(clock)
        # COOLUSDT history: entered 2h ago and already CLOSED — the slot is
        # free but the 12h cooldown timer still runs.
        peer.add("COOLUSDT", T0 - timedelta(hours=2),
                 status=POSITION_STATUS_CLOSED)
        store = _store(clock, peer=peer)
        engine = _engine(store, clock)
        _capture(store, _candidate("COOLUSDT", detected_at=T0))
        engine.open_entries({"COOLUSDT": 100.0})

        rejected = _events(store, status=EVENT_STATUS_SYMBOL_REJECTED)
        assert [e.symbol for e in rejected] == ["COOLUSDT"]
        assert rejected[0].status_reason == EVENT_REASON_SYMBOL_COOLDOWN
        assert rejected[0].status_at is not None
        assert engine.open_positions == {}

    def test_only_guard_eligible_signals_wait_for_a_slot_until_ttl(self):
        from app.paper.me_long_v2_inverse_short_v3.models import (
            POSITION_STATUS_CLOSED,
        )

        clock = _Clock(T0)
        peer = self._full_book(clock)
        peer.add("COOLUSDT", T0 - timedelta(hours=2),
                 status=POSITION_STATUS_CLOSED)
        store = _store(clock, peer=peer)
        engine = _engine(store, clock)
        prices = {"V2P0USDT": 100.0, "COOLUSDT": 100.0, "CLEANUSDT": 100.0}
        _capture(store, _candidate("V2P0USDT", detected_at=T0))
        _capture(store, _candidate("COOLUSDT", detected_at=T0))
        _capture(store, _candidate("CLEANUSDT", detected_at=T0))
        engine.open_entries(prices)

        statuses = {e.symbol: e.status for e in _events(store)}
        assert statuses["V2P0USDT"] == EVENT_STATUS_SYMBOL_REJECTED
        assert statuses["COOLUSDT"] == EVENT_STATUS_SYMBOL_REJECTED
        # Only the guard-ELIGIBLE signal may wait for a free slot …
        assert statuses["CLEANUSDT"] == EVENT_STATUS_CAPTURED
        assert engine.open_positions == {}

        # … and at TTL it ages out under the frozen CAPACITY_REJECTED
        # (ENTRY_CAP) contract — while the terminal refusals never un-reject.
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES + 1)
        engine.open_entries(prices)
        statuses = {e.symbol: e.status for e in _events(store)}
        assert statuses["CLEANUSDT"] == EVENT_STATUS_CAPACITY_REJECTED
        assert statuses["V2P0USDT"] == EVENT_STATUS_SYMBOL_REJECTED
        assert statuses["COOLUSDT"] == EVENT_STATUS_SYMBOL_REJECTED
