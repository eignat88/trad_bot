"""ME_LONG_V2_INVERSE_SHORT_OOS2_V1 — entry-TTL / disposition contract tests.

Owner decisions under test:
  Q1: entry signal TTL = 120 minutes from ``signal_time`` (frozen spec
      ``entry_max_age_minutes``);
  Q2: variant B — a NEW experiment with its own fingerprint/activation and
      an isolated data namespace (isolation assertions live in
      ``test_me_long_v2_inverse_short_oos2_v1_isolation_v1.py``).

Covered acceptance criteria:
  * age < 120 min may open a position (subject to the other gates);
  * age >= 120 min can NEVER open a new position — not even right after a
    capacity slot frees;
  * ENTRY_CAP blocks new positions and the blocked-then-stale event is
    terminally disposed as CAPACITY_REJECTED (reason + timestamp preserved);
  * EXPIRED vs CAPACITY_REJECTED are distinct, each with cause + status_at;
  * FIFO backlog order, duplicate delivery, restart with a CAPTURED queue,
    ambiguous COMMIT adoption (never age-gated), no backfill;
  * STOP / TP / TIMEOUT exits unchanged; sweep failures are fail-soft;
  * DRAINING (EXIT_ONLY) never sweeps and never enters.

Everything runs against the in-memory store — no PostgreSQL in this suite.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.paper.me_long_v2_inverse_short_oos2_v1 import (
    ENTRY_MAX_AGE_MINUTES,
    MAX_OPEN_POSITIONS,
    InMemoryInverseShortStore,
    InverseShortContour,
    capture_with_status,
    event_from_candidate,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.activation import (
    ACTIVATION_FLAG,
    evaluate_activation,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.capture import (
    CAPTURED,
    CAPTURE_DISABLED,
    CAPTURE_DUPLICATE,
    CAPTURE_PRE_FREEZE,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.identity import (
    position_key as build_position_key,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_CAPACITY_REJECTED,
    EVENT_STATUS_ENTERED,
    EVENT_STATUS_EXPIRED,
    ActivationRecord,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
    EXPERIMENT_ID,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    protocol_fingerprint,
)
from app.scanners.models import SetupCandidate, SetupState

FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
TTL = timedelta(minutes=ENTRY_MAX_AGE_MINUTES)   # 120 minutes (frozen)

# OOS-2 LOCAL capacity (Issue #276): the frozen protocol constant
# MAX_OPEN_POSITIONS == 12 — NOT Settings.max_open_positions.  _settings()
# below deliberately keeps the settings value at 3 so every capacity test
# in this file proves that value is IGNORED by the contour.
FULL_BOOK_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "LINKUSDT",
    "AVAXUSDT", "DOTUSDT", "MATICUSDT", "LTCUSDT", "ATOMUSDT", "UNIUSDT",
)
assert MAX_OPEN_POSITIONS == 12
assert len(FULL_BOOK_SYMBOLS) == MAX_OPEN_POSITIONS


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors the V1 suite; V2 identity everywhere)
# ---------------------------------------------------------------------------

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
        # Deliberately 3: the OOS-2 contour must NEVER read this value —
        # its capacity is the frozen protocol constant MAX_OPEN_POSITIONS
        # (12).  Every capacity test below fills all 12 slots anyway.
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


def _store(
    settings: Settings | None = None,
    activation: ActivationRecord | None | object = ...,
) -> InMemoryInverseShortStore:
    if activation is ...:
        activation = _activation()
    return InMemoryInverseShortStore(
        activation, initial_balance=(settings or _settings()).initial_balance,
    )


def _candidate(
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    *,
    detected_at: datetime = SIGNAL_AT,
    candle_ms: int | None = None,
    scanner: str = SOURCE_SCANNER,
    direction: str = SOURCE_DIRECTION,
) -> SetupCandidate:
    if candle_ms is None:
        candle_ms = int(detected_at.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name=scanner,
        scanner_version="1.0.0",
        symbol=symbol,
        direction=direction,
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


def _capture(store, candidate, *, clock=None, env=None) -> str:
    return capture_with_status(
        store, candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"} if env is None else env,
        clock=clock,
    )


def _contour(store, settings=None, clock=None) -> InverseShortContour:
    return InverseShortContour(
        settings or _settings(), store, clock=clock, activation_enabled=True,
    )


def _event(store, symbol: str, *, detected_at: datetime = SIGNAL_AT):
    """The single captured event row for ``symbol`` (exactly one)."""
    rows = [e for e in store.list_events() if e.symbol == symbol]
    assert len(rows) == 1, [e.symbol for e in store.list_events()]
    return rows[0]


def _open_n(store, clock, symbols, price=100.0):
    """Open ``len(symbols) <= MAX_OPEN_POSITIONS`` (frozen 12) at ``price``."""
    assert len(symbols) <= MAX_OPEN_POSITIONS, symbols
    settings = _settings()
    contour = _contour(store, settings, clock)
    prices = {}
    for symbol in symbols:
        assert _capture(store, _candidate(symbol, price, detected_at=clock()),
                        clock=clock) == CAPTURED
        prices[symbol] = price
    cycle = contour.run_cycle(prices)
    assert cycle["entries"] == len(symbols), cycle
    return contour, prices


# ---------------------------------------------------------------------------
# 1. Fresh signals (age < TTL) keep the frozen near-signal behaviour
# ---------------------------------------------------------------------------

class TestFreshEntries:
    def test_fresh_event_enters_same_cycle(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_ENTERED

    def test_event_just_under_ttl_still_enters(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        clock.advance(minutes=119, seconds=59)      # age < 120 min
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_ENTERED

    def test_fresh_event_without_price_stays_pending(self):
        # The TTL only bounds age — the other gates still apply unchanged.
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        cycle = contour.run_cycle({"ETHUSDT": 100.0})   # wrong symbol only
        assert cycle["entries"] == 0
        assert cycle["pending_events"] == 1
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_CAPTURED


# ---------------------------------------------------------------------------
# 2. The TTL itself (Q1): age >= 120 min never opens a NEW position
# ---------------------------------------------------------------------------

class TestEntryTtl:
    def test_event_at_exactly_ttl_never_enters(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)   # age == 120 min exactly
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        event = _event(store, "BTCUSDT")
        assert event.status == EVENT_STATUS_EXPIRED
        assert event.status_reason == "ENTRY_TTL"
        assert event.status_at == clock()              # decision timestamp
        assert store.list_positions() == []            # no position ever

    def test_event_past_ttl_never_enters(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES, seconds=1)
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_EXPIRED

    def test_expired_event_is_terminal_across_cycles_and_redelivery(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        cand = _candidate()
        assert _capture(store, cand, clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)
        assert contour.run_cycle({"BTCUSDT": 100.0})["entries"] == 0
        # Another cycle with a free slot and a valid price: still terminal.
        assert contour.run_cycle({"BTCUSDT": 100.0})["entries"] == 0
        # Duplicate delivery of the SAME source event never resurrects it.
        assert _capture(store, cand, clock=clock) == CAPTURE_DUPLICATE
        assert len(store.list_events()) == 1
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_EXPIRED

    def test_fresh_event_enters_after_stale_ones_are_disposed(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        # stale + fresh captured back-to-back
        assert _capture(store, _candidate("BTCUSDT"),
                        clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)
        assert _capture(store, _candidate("ETHUSDT", detected_at=clock()),
                        clock=clock) == CAPTURED
        cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle["entries"] == 1, cycle              # only the fresh one
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_EXPIRED
        assert _event(store, "ETHUSDT").status == EVENT_STATUS_ENTERED


# ---------------------------------------------------------------------------
# 3. Capacity: ENTRY_CAP blocks entries; capacity-caused staleness is its
#    own terminal disposition (EXPIRED != CAPACITY_REJECTED)
# ---------------------------------------------------------------------------

class TestCapacityDispositions:
    def _full_book(self):
        """Fill the OOS-2 book to its FROZEN capacity (12, protocol constant).

        Settings.max_open_positions stays at 3 in these settings — the
        contour must ignore it and open all 12 slots regardless.
        """
        settings = _settings()
        assert settings.max_open_positions == 3   # deliberately IGNORED
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour, prices = _open_n(store, clock, list(FULL_BOOK_SYMBOLS))
        assert len(store.list_positions()) == MAX_OPEN_POSITIONS
        return settings, store, clock, contour, prices

    @staticmethod
    def _prices(**overrides) -> dict:
        prices = {s: 100.0 for s in FULL_BOOK_SYMBOLS}
        prices.update(overrides)
        return prices

    def test_entry_cap_blocks_a_fresh_signal_without_disposing_it(self):
        settings, store, clock, contour, _ = self._full_book()
        assert _capture(store, _candidate("XRPUSDT"), clock=clock) == CAPTURED
        cycle = contour.run_cycle({"XRPUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        assert cycle["pending_events"] == 1
        event = _event(store, "XRPUSDT")
        assert event.status == EVENT_STATUS_CAPTURED     # fresh: still eligible
        assert len(store.list_positions()) == MAX_OPEN_POSITIONS

    def test_stale_event_while_book_is_full_is_capacity_rejected(self):
        settings, store, clock, contour, _ = self._full_book()
        assert _capture(store, _candidate("XRPUSDT"), clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)
        cycle = contour.run_cycle(self._prices(XRPUSDT=100.0))
        assert cycle["entries"] == 0, cycle
        event = _event(store, "XRPUSDT")
        assert event.status == EVENT_STATUS_CAPACITY_REJECTED
        assert event.status_reason == "ENTRY_CAP"        # capacity was binding
        assert event.status_at == clock()
        assert not store.has_position(build_position_key(event.event_key))
        # the full 12-position OOS-2 book is untouched (settings' 3 ignored)
        assert len(store.list_positions()) == MAX_OPEN_POSITIONS

    def test_slot_release_never_opens_a_stale_backlog_event(self):
        """THE headline scenario: slot frees after the TTL — no stale entry."""
        settings, store, clock, contour, _ = self._full_book()
        stale = _candidate("XRPUSDT")
        assert _capture(store, stale, clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)     # TTL elapses
        assert _capture(store, _candidate(
            "DOGEUSDT", detected_at=clock(),
        ), clock=clock) == CAPTURED
        # BTC trades through the whole ladder below TP3 in THIS bar —
        # TP1 + TP2 partial legs, then the TERMINAL TP3 close — so one
        # capacity slot frees in this cycle (a partial leg never frees).
        cycle = contour.run_cycle(self._prices(BTCUSDT=89.0, DOGEUSDT=100.0))
        assert cycle["exits"] == 1, cycle
        assert cycle["exit_reasons"] == ["TAKE_PROFIT_3"], cycle
        # The freed slot went to the FRESH signal only.
        assert cycle["entries"] == 1, cycle
        assert _event(store, "XRPUSDT").status == EVENT_STATUS_EXPIRED
        assert _event(store, "XRPUSDT").status_reason == "ENTRY_TTL"
        assert _event(store, "DOGEUSDT").status == EVENT_STATUS_ENTERED
        all_rows = store.list_positions()
        entered = {p.symbol for p in all_rows}
        assert "XRPUSDT" not in entered
        open_rows = [p for p in all_rows if p.status == "OPEN"]
        # 11 held + the DOGE fresh entry = the frozen cap of 12
        assert len(open_rows) == MAX_OPEN_POSITIONS
        assert len(all_rows) == MAX_OPEN_POSITIONS + 1   # + closed BTC trade

    def test_sweep_runs_even_while_fail_closed_gates_block_entries(self):
        # Accounting gate down: entries are refused, but the backlog must
        # still age out (the sweep precedes the fail-closed gates).
        settings, store, clock, contour, _ = self._full_book()
        assert _capture(store, _candidate("XRPUSDT"), clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)
        contour.engine._balance_trusted = False          # accounting gate
        cycle = contour.run_cycle(self._prices(XRPUSDT=100.0))
        assert cycle["entries"] == 0, cycle
        assert _event(store, "XRPUSDT").status == EVENT_STATUS_CAPACITY_REJECTED
        assert _event(store, "XRPUSDT").status_reason == "ENTRY_CAP"


# ---------------------------------------------------------------------------
# 4. FIFO backlog
# ---------------------------------------------------------------------------

class TestFifoBacklog:
    def test_fresh_backlog_enters_in_signal_time_order(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        # three distinct 5m candles, oldest first
        for symbol, delta in (("SOLUSDT", 10), ("ETHUSDT", 5), ("BTCUSDT", 0)):
            detected = SIGNAL_AT - timedelta(minutes=delta)
            assert _capture(store, _candidate(symbol, detected_at=detected),
                            clock=clock) == CAPTURED
        cycle = contour.run_cycle(
            {"SOLUSDT": 100.0, "ETHUSDT": 100.0, "BTCUSDT": 100.0},
        )
        assert cycle["entries"] == 3, cycle
        # opened list follows ORDER BY signal_time, event_key
        assert [p.symbol for p in cycle["opened"]] == [
            "SOLUSDT", "ETHUSDT", "BTCUSDT",
        ]

    def test_mixed_ages_stale_head_is_skipped_fresh_tail_enters_in_order(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        stale_detected = SIGNAL_AT - timedelta(minutes=ENTRY_MAX_AGE_MINUTES + 1)
        assert _capture(store, _candidate(
            "SOLUSDT", detected_at=stale_detected,
        ), clock=clock) == CAPTURED
        assert _capture(store, _candidate("ETHUSDT"), clock=clock) == CAPTURED
        clock.advance(seconds=30)   # everyone still (or newly) evaluated now
        assert _capture(store, _candidate(
            "BTCUSDT", detected_at=clock(),
        ), clock=clock) == CAPTURED
        cycle = contour.run_cycle(
            {"SOLUSDT": 100.0, "ETHUSDT": 100.0, "BTCUSDT": 100.0},
        )
        assert [p.symbol for p in cycle["opened"]] == ["ETHUSDT", "BTCUSDT"]
        assert _event(store, "SOLUSDT").status == EVENT_STATUS_EXPIRED


# ---------------------------------------------------------------------------
# 5. Duplicate delivery / restart / ambiguous COMMIT
# ---------------------------------------------------------------------------

class TestIdempotencyAndRecovery:
    def test_restart_with_captured_backlog_resumes_the_sweep(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        # one OPEN position + one pending event
        contour, _ = _open_n(store, clock, ["BTCUSDT"])
        original_entered = next(iter(store.list_positions())).entered_at
        assert _capture(store, _candidate("ETHUSDT"), clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)

        # simulated process restart: fresh objects over the SAME store
        contour2 = _contour(store, settings, clock)
        cycle = contour2.run_cycle(
            {"BTCUSDT": 100.0, "ETHUSDT": 100.0},
        )
        # open position restored verbatim, backlog event disposed once
        restored = next(iter(contour2.engine.open_positions.values()))
        assert restored.entered_at == original_entered
        assert cycle["entries"] == 0, cycle
        assert _event(store, "ETHUSDT").status == EVENT_STATUS_EXPIRED

    def test_ambiguous_commit_adoption_is_never_age_gated(self):
        """Committed-but-unmarked position: adoption wins over the TTL.

        The position row exists (INSERT COMMITTED, mark_event_entered lost)
        while the event is still CAPTURED.  The sweep must SKIP it
        (has_position guard) and the entry loop must ADOPT the committed
        row even though the event is already past the TTL — protective
        STOP/TP/TIMEOUT must attach, and no second INSERT may happen.
        """
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        event = _event(store, "BTCUSDT")
        position_key = build_position_key(event.event_key)
        # The FIRST attempt COMMITTED but the event mark was lost.
        committed = contour.engine._build_position(event, position_key, 100.0)
        assert committed is not None
        assert store.save_position(committed) is True
        assert event.status == EVENT_STATUS_CAPTURED      # mark never ran

        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES + 1)  # now stale
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle               # adopted, not opened
        adopted = contour.engine.open_positions[position_key]
        assert adopted.entered_at == committed.entered_at  # verbatim, no rewrite
        assert len(store.list_positions()) == 1            # exactly one row
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_ENTERED

    def test_duplicate_position_suppression_still_holds_with_ttl(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1
        # re-running the same cycle never creates a second row
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle2["entries"] == 0
        assert len(store.list_positions()) == 1


# ---------------------------------------------------------------------------
# 6. No backfill / activation boundary / DRAINING
# ---------------------------------------------------------------------------

class TestNoBackfillAndActivationBoundary:
    def test_pre_freeze_event_is_refused_at_capture(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        stale = _candidate(detected_at=FREEZE_TS - timedelta(hours=1))
        assert _capture(store, stale, clock=clock) == CAPTURE_PRE_FREEZE
        assert store.list_events() == []

    def test_capture_is_disabled_without_the_v2_flag(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        assert _capture(store, _candidate(), clock=clock, env={}) == (
            CAPTURE_DISABLED
        )
        assert store.list_events() == []

    def test_engine_double_checks_pre_freeze_events(self):
        # A pre-freeze row that somehow reached the store is still refused
        # by the engine (defence in depth) — and at age < TTL it is not even
        # sweep-disposed, it simply never enters.
        settings = _settings()
        store = _store(settings)
        clock = _Clock(FREEZE_TS + timedelta(seconds=60))
        identity = evaluate_activation(_activation(), enabled=True)[0]
        forged = event_from_candidate(
            _candidate(detected_at=FREEZE_TS),
            capture_route=ROUTE_PRE_FILTER_SOURCE,
            activation=identity,
            captured_at=clock(),
        )
        assert store.save_event(forged) == CAPTURED
        contour = _contour(store, settings, clock)
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0, cycle
        stored = store.get_event(forged.event_key)
        assert stored.status == EVENT_STATUS_CAPTURED      # refused, not expired
        assert store.list_positions() == []

    def test_draining_never_sweeps_and_never_enters(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)
        store.set_activation(_activation("DRAINING"))
        contour2 = _contour(store, settings, clock)
        assert contour2.exit_only is True
        cycle = contour2.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0
        # EXIT_ONLY never queries/mutates entry events at all
        assert contour2.engine.open_entries({"BTCUSDT": 100.0}) == []
        event = _event(store, "BTCUSDT")
        assert event.status == EVENT_STATUS_CAPTURED       # untouched
        assert event.status_reason is None


# ---------------------------------------------------------------------------
# 7. Fail-soft sweep + preserved STOP / TP / TIMEOUT exits
# ---------------------------------------------------------------------------

class TestFailSoftAndExits:
    def test_sweep_read_failure_fails_closed_without_raising(self, monkeypatch):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)

        def _boom(*args, **kwargs):
            raise RuntimeError("store read failed")

        monkeypatch.setattr(store, "list_events", _boom)
        # open_entries fails CLOSED without raising (sweep read failure AND
        # entry-loop read failure are both contained inside the method).
        assert contour.engine.open_entries({"BTCUSDT": 100.0}) == []

    def test_sweep_write_failure_keeps_event_capTUREd_but_refuses_entry(
        self, monkeypatch,
    ):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        clock.advance(minutes=ENTRY_MAX_AGE_MINUTES)

        def _boom(*args, **kwargs):
            raise RuntimeError("store write failed")

        monkeypatch.setattr(store, "mark_event_expired", _boom)
        opened = contour.engine.open_entries({"BTCUSDT": 100.0})
        assert opened == []                                  # age check held
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_CAPTURED

    def test_take_profit_exit_is_preserved(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        assert contour.run_cycle({"BTCUSDT": 100.0})["entries"] == 1
        # ONE bar below TP3 (89 <= entry * 0.90) fires the whole frozen
        # ladder in order: TP1 (50% of ORIGINAL, partial) -> TP2 (75% of
        # the remainder, partial) -> TP3 (all that is left, TERMINAL).
        cycle = contour.run_cycle({"BTCUSDT": 89.0})
        assert cycle["exits"] == 1, cycle
        assert cycle["exit_reasons"] == ["TAKE_PROFIT_3"], cycle
        closed = contour.engine.closed_positions()
        assert len(closed) == 1                      # ONE closed trade
        position = closed[0]
        assert position.net_pnl > 0
        assert position.status == "CLOSED"
        assert position.exit_reason == "TAKE_PROFIT_3"
        # Three durable ladder legs, in order; only TP3 is terminal.
        legs = store.list_close_legs()
        assert [leg.exit_reason for leg in legs] == [
            "TAKE_PROFIT_1", "TAKE_PROFIT_2", "TAKE_PROFIT_3",
        ]
        assert [leg.leg_seq for leg in legs] == [1, 2, 3]
        assert [leg.is_terminal for leg in legs] == [False, False, True]
        # The partial legs never closed the position early: it stayed OPEN
        # until the terminal TP3 — the whole ladder is ONE trade, never 3.
        assert len(contour.engine.open_positions) == 0
        assert len(store.list_positions()) == 1
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_ENTERED

    def test_stop_loss_exit_is_preserved(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        assert contour.run_cycle({"BTCUSDT": 100.0})["entries"] == 1
        position = store.list_positions()[0]
        # intrabar stop touch, mid still below the stop → clean STOP_LOSS
        bars = {
            "BTCUSDT": {
                "high": position.stop_price * 1.001,
                "low": 99.0,
            },
        }
        cycle = contour.run_cycle({"BTCUSDT": 100.0}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        assert contour.engine.closed_positions()[0].net_pnl < 0

    def test_timeout_exit_is_preserved_past_the_ttl(self):
        settings = _settings()
        store = _store(settings)
        clock = _Clock(SIGNAL_AT)
        contour = _contour(store, settings, clock)
        assert _capture(store, _candidate(), clock=clock) == CAPTURED
        assert contour.run_cycle({"BTCUSDT": 100.0})["entries"] == 1
        clock.advance(hours=128)          # far past the 7200-minute deadline
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["exit_reasons"] == ["TIMEOUT"], cycle
        # the ENTERED event is never re-considered, TTL or not
        assert _event(store, "BTCUSDT").status == EVENT_STATUS_ENTERED
