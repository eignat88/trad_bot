"""ME_LONG_V2_INVERSE_SHORT_OOS2_V1 — Codex review hardening regressions.

Fault-injection suite for the three review findings on PR #278:

* **P1-1** — ``persist_marks()`` may only write MARK telemetry
  (``update_position_marks``): after a FAILED ``commit_partial_exit()``
  the durable row must stay exactly what PostgreSQL confirmed — a mark
  write never carries ``remaining_size`` / TP flags / realized PnL /
  funding / status forward.
* **P1-2** — the ladder never advances over an unconfirmed partial leg:
  TP1's dirty row is retried and confirmed BEFORE TP2 (or TP3) may fire,
  so the mandatory fault-injection scenario
  ``TP1 calculated -> DB write fails -> retry -> TP1 confirmed -> TP2 ->
  restart`` ends with each stage exactly once, no missing stages, the
  12.5% remainder, PnL/fees identical to a failure-free reference run,
  and no permanently blocked recovery.  Protective STOP/TIMEOUT stay
  active while the ledger is dirty, and a terminal close stages the owed
  partial leg rows with its own snapshot (no lost TP records on restart).
* **P2** — funding settles PER PERIOD on the quantity outstanding at that
  period's funding instant, reconstructed from the DURABLE close-leg
  history; an unprovable history never finalizes a settlement, and an
  unsettled obligation survives restart exactly once.

Everything here runs against the in-memory store — no PostgreSQL in this
suite (the real-PostgreSQL fault-injection lives in
``test_me_long_v2_inverse_short_oos2_partial_restart_pg_it_v1.py``,
gated on ``TEST_DB_HOST``).
"""
from __future__ import annotations

import dataclasses
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.paper.me_long_v2_inverse_short_oos2_v1 import (
    ENTRY_MAX_AGE_MINUTES,
    MAX_HOLD_MINUTES,
    InMemoryInverseShortStore,
    InverseShortContour,
    capture_with_status,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.activation import (
    ACTIVATION_FLAG,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.engine import (
    InverseShortPaperEngine,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.models import (
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
    ActivationRecord,
    InverseShortPosition,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
    EXPERIMENT_ID,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.store import (
    PgInverseShortStore,
    UPDATE_POSITION_SQL,
)
from app.scanners.models import SetupCandidate, SetupState

FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors the ladder suite so scenarios are comparable)
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
    store: InMemoryInverseShortStore | None = None,
) -> tuple[InverseShortContour, InMemoryInverseShortStore, _Clock]:
    settings = settings or _settings()
    clock = clock or _Clock(NOW)
    store = store or InMemoryInverseShortStore(
        _activation(), initial_balance=settings.initial_balance,
    )
    contour = InverseShortContour(
        settings, store, clock=clock, activation_enabled=True,
    )
    assert contour.activated, contour.activation_reason
    return contour, store, clock


def _restart(
    contour: InverseShortContour, settings: Settings, clock: _Clock,
) -> InverseShortContour:
    """Simulate a process restart: a NEW engine over the SAME durable store."""
    restarted = InverseShortContour(
        settings, contour.store, clock=clock, activation_enabled=True,
    )
    assert restarted.activated, restarted.activation_reason
    return restarted


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


def _position(
    contour: InverseShortContour, symbol: str,
) -> InverseShortPosition:
    matches = [
        p for p in contour.engine.open_positions.values() if p.symbol == symbol
    ]
    assert len(matches) == 1, f"expected exactly one OPEN {symbol}"
    return matches[0]


def _assert_ledger_consistent(contour: InverseShortContour) -> None:
    """balance == initial + Σclosed net + Σopen(realized legs − costs)."""
    engine = contour.engine
    expected = engine.initial_balance
    for p in engine.store.list_positions():
        if p.status == POSITION_STATUS_CLOSED:
            expected += p.net_pnl
        else:
            expected += (
                p.realized_legs_net
                - p.entry_fee
                - p.entry_slippage_cost
                - p.funding_paid
            )
            assert p.remaining_size > 0, "OPEN position with no remainder"
            assert p.remaining_size <= p.position_size + 1e-12
    expected += sum(engine._uncommitted_deltas.values())
    assert expected == pytest.approx(engine.balance, abs=1e-6), (
        engine.balance, expected,
    )


def _assert_durable_consistent(
    store: InMemoryInverseShortStore, position_key: str,
) -> None:
    """Section-6 accounting audit against the DURABLE rows only.

    quantity conservation, one durable execution per stage, OPEN/CLOSED
    remainder rules — i.e. exactly what a restart would see.
    """
    pos = store.get_position(position_key)
    assert pos is not None
    legs = store.list_close_legs(position_key)
    sequences = [leg.leg_seq for leg in legs]
    assert len(sequences) == len(set(sequences)), (
        "each stage has at most ONE durable execution",
    )
    for leg in legs:
        assert leg.quantity > 0
        assert leg.remaining_after >= 0
    closed_qty = round(sum(leg.quantity for leg in legs), 9)
    if pos.status == POSITION_STATUS_OPEN:
        assert pos.remaining_size > 0, "OPEN row with zero remainder"
        assert round(pos.position_size - pos.remaining_size, 9) == closed_qty, (
            "conservation: initial = remaining + Σ durable leg quantities",
        )
        assert bool(pos.tp1_executed) == any(
            leg.exit_reason == "TAKE_PROFIT_1" for leg in legs
        ), "TP1 flag without (or missing from) the durable ledger"
        assert bool(pos.tp2_executed) == any(
            leg.exit_reason == "TAKE_PROFIT_2" for leg in legs
        ), "TP2 flag without (or missing from) the durable ledger"
        assert all(not leg.is_terminal for leg in legs), "OPEN has no terminal"
    else:
        assert pos.remaining_size == 0, "CLOSED row with a remainder"
        assert round(pos.position_size, 9) == closed_qty, (
            "conservation: initial = Σ durable leg quantities (terminal "
            "leg absorbs the remainder)",
        )
        terminal = [leg for leg in legs if leg.is_terminal]
        assert len(terminal) == 1, "exactly one durable terminal leg"


def _failing_commit(store: InMemoryInverseShortStore) -> list[int]:
    """Make the first ``commit_partial_exit`` fail, then heal the store."""
    counter: list[int] = [0]
    original = store.commit_partial_exit

    def flaky(position, leg):
        counter[0] += 1
        if counter[0] == 1:
            raise RuntimeError("simulated connection loss before COMMIT")
        return original(position, leg)

    store.commit_partial_exit = flaky
    return counter


def _legs_by_reason(
    store: InMemoryInverseShortStore, position_key: str,
) -> dict[str, object]:
    return {
        leg.exit_reason: leg
        for leg in store.list_close_legs(position_key)
    }


# ---------------------------------------------------------------------------
# P1-1 — mark-only persistence
# ---------------------------------------------------------------------------

class TestP11MarkOnlyPersistence:
    def test_failed_tp1_commit_is_never_persisted_by_mark_writes(self):
        """Acceptance for P1-1: commit fails + marks succeed => durable
        trading state UNCHANGED (only telemetry moves)."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        full_size = pos.position_size

        update_calls = {"n": 0}
        original_update = store.update_position

        def spy_update(position):
            update_calls["n"] += 1
            return original_update(position)

        store.update_position = spy_update          # must stay untouched
        _failing_commit(store)                      # TP1 write fails

        # cycle: TP1 applies IN MEMORY, fails durably, mark changes
        contour.run_cycle({pos.symbol: entry * 0.945})

        # the in-memory object IS ahead of the durable row (the defect
        # scenario of P1-1) ...
        assert pos.tp1_executed is True
        assert pos.remaining_size == pytest.approx(full_size * 0.5, rel=1e-9)

        # ... but the DURABLE row still carries only confirmed state:
        durable = store.get_position(pos.position_key)
        assert durable.tp1_executed is False
        assert durable.tp2_executed is False
        assert durable.remaining_size == pytest.approx(full_size, rel=1e-9)
        assert durable.realized_legs_net == 0.0
        assert durable.net_pnl == 0.0
        assert durable.status == POSITION_STATUS_OPEN

        # the full-object path was NEVER used during that cycle
        assert update_calls["n"] == 0, (
            "persist_marks must not call update_position — a full-object "
            "write would make the unconfirmed TP1 durable",
        )

        # telemetry WAS written (dashboard keeps working)
        assert durable.last_mark_price == pytest.approx(entry * 0.945)
        assert durable.mfe > 0.0, "MFE telemetry written by the mark path"

        # recovery: once the store heals, the SAME leg confirms exactly once
        store.commit_partial_exit = (
            lambda p, l: InMemoryInverseShortStore.commit_partial_exit(store, p, l)
        )
        contour.run_cycle({pos.symbol: entry * 0.945})
        legs = store.list_close_legs(pos.position_key)
        assert [leg.exit_reason for leg in legs] == ["TAKE_PROFIT_1"]
        durable = store.get_position(pos.position_key)
        assert durable.tp1_executed is True
        assert durable.remaining_size == pytest.approx(full_size * 0.5, rel=1e-9)
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)

    def test_restart_after_failed_tp1_loads_only_confirmed_state(self):
        """Restart mid-dirty: the engine restores the PostgreSQL-confirmed
        pre-TP1 state and the next touch re-executes TP1 exactly once."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        full_size = pos.position_size

        _failing_commit(store)
        contour.run_cycle({pos.symbol: entry * 0.945})   # TP1 dirty
        assert pos.position_key in contour.engine.dirty_leg_keys

        # RESTART — dirty state is process-local by contract
        restarted = _restart(contour, settings, clock)
        assert restarted.engine.dirty_leg_keys == ()
        loaded = _position(restarted, pos.symbol)
        assert loaded.remaining_size == pytest.approx(full_size, rel=1e-9)
        assert loaded.tp1_executed is False
        assert store.list_close_legs(pos.position_key) == []

        # the fresh engine executes and DURABLY confirms TP1 exactly once
        restarted.run_cycle({pos.symbol: entry * 0.945})
        legs = store.list_close_legs(pos.position_key)
        assert [leg.exit_reason for leg in legs] == ["TAKE_PROFIT_1"]
        assert len(legs) == 1
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(restarted)

    def test_update_position_marks_moves_only_the_five_telemetry_columns(
        self,
    ):
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)

        before = store.get_position(pos.position_key).to_dict()
        mutated = dataclasses.replace(
            pos,
            remaining_size=pos.remaining_size * 0.5,
            tp1_executed=True,
            realized_legs_net=123.45,
            funding_paid=999.0,
            net_pnl=-1.0,
            status=POSITION_STATUS_CLOSED,
            last_mark_price=pos.entry_price * 0.9,
            highest_since_entry=pos.entry_price * 1.2,
            lowest_since_entry=pos.entry_price * 0.8,
            mfe=7.0,
            mae=8.0,
        )
        store.update_position_marks(mutated)

        after = store.get_position(pos.position_key).to_dict()
        changed = {
            key for key in before
            if before[key] != after[key]
        }
        assert changed <= {
            "last_mark_price", "highest_since_entry", "lowest_since_entry",
            "mfe", "mae",
        }, f"non-telemetry columns changed: {sorted(changed)}"
        # the trading/financial fields stayed byte-identical
        for column in (
            "remaining_size", "tp1_executed", "tp2_executed", "tp3_executed",
            "realized_legs_net", "entry_cost_allocated", "funding_paid",
            "funding_periods", "net_pnl", "gross_pnl", "status",
        ):
            assert before[column] == after[column], column

    def test_marks_sql_can_only_write_telemetry_columns(self):
        """Static contract: the PG mark statement physically cannot touch
        trading state (no database required)."""
        from app.paper.me_long_v2_inverse_short_oos2_v1.store import (
            UPDATE_POSITION_MARKS_SQL,   # absent before the P1-1 fix
        )
        set_clause = UPDATE_POSITION_MARKS_SQL.split("SET", 1)[1].split(
            "WHERE", 1,
        )[0]
        assert set_clause.count("%s") == 5  # five telemetry columns only
        for column in (
            "remaining_size", "tp1_executed", "tp2_executed", "tp3_executed",
            "realized_legs_net", "entry_cost_allocated", "funding_paid",
            "funding_periods", "net_pnl", "gross_pnl", "status",
            "exit_price", "exit_reason", "exit_fee", "slippage", "closed_at",
            "duration_sec",
        ):
            assert f"{column} =" not in set_clause, column
        for column in (
            "highest_since_entry", "lowest_since_entry", "mfe", "mae",
            "last_mark_price",
        ):
            assert f"{column} =" in set_clause, column
        # and it is genuinely a DIFFERENT statement than the full update
        assert UPDATE_POSITION_MARKS_SQL != UPDATE_POSITION_SQL

    def test_persist_marks_source_uses_only_the_mark_api(self):
        source = inspect.getsource(InverseShortPaperEngine.persist_marks)
        assert "update_position_marks(" in source
        assert ".update_position(" not in source, (
            "persist_marks must not reach the full-object write path",
        )


# ---------------------------------------------------------------------------
# P1-2 — ladder gated on durable confirmation
# ---------------------------------------------------------------------------

class TestP12LadderDurabilityGate:
    def test_mandatory_fault_injection_tp1_fail_retry_tp2_restart(self):
        """Mandatory scenario: TP1 calculated -> DB write fails -> retry ->
        TP1 confirmed -> TP2 -> restart, byte-identical to the reference."""
        settings = _settings()

        # ---- reference contour (no failure) ----------------------------
        ref, ref_store, ref_clock = _contour(settings)
        ref_pos = _open_one(ref, ref_store, ref_clock)
        entry = ref_pos.entry_price
        full_size = ref_pos.position_size
        bar = {"high": ref_pos.tp2_price, "low": ref_pos.tp2_price}
        ref.run_cycle({ref_pos.symbol: ref_pos.tp2_price}, bars={ref_pos.symbol: bar})
        assert ref_pos.tp1_executed and ref_pos.tp2_executed
        assert ref_pos.remaining_size == pytest.approx(
            full_size * 0.125, rel=1e-9,
        )
        ref_legs = _legs_by_reason(ref_store, ref_pos.position_key)
        _assert_durable_consistent(ref_store, ref_pos.position_key)

        # ---- fault contour (TP1 write fails exactly once) --------------
        fault, store, clock = _contour(settings)
        pos = _open_one(fault, store, clock)
        assert pos.entry_price == pytest.approx(entry, rel=1e-12)
        assert pos.position_size == pytest.approx(full_size, rel=1e-12)
        _failing_commit(store)

        # cycle 1: bar touches BOTH TP1 and TP2 levels
        fault.run_cycle({pos.symbol: pos.tp2_price}, bars={pos.symbol: bar})
        # TP1 applied in memory but NOT durable; TP2 was HELD by the gate
        assert pos.tp1_executed is True
        assert pos.tp2_executed is False, "ladder advanced over a dirty TP1!"
        assert pos.remaining_size == pytest.approx(full_size * 0.5, rel=1e-9)
        assert pos.position_key in fault.engine.dirty_leg_keys
        assert store.list_close_legs(pos.position_key) == []
        durable = store.get_position(pos.position_key)
        assert durable.tp1_executed is False
        assert durable.remaining_size == pytest.approx(full_size, rel=1e-9)
        _assert_durable_consistent(store, pos.position_key)

        # cycle 2: dirty-leg retry confirms TP1 first, then TP2 may fire
        fault.run_cycle({pos.symbol: pos.tp2_price}, bars={pos.symbol: bar})
        assert pos.tp2_executed is True
        assert pos.remaining_size == pytest.approx(full_size * 0.125, rel=1e-9)
        assert fault.engine.dirty_leg_keys == ()
        assert not fault.engine.durability_risk

        legs = store.list_close_legs(pos.position_key)
        assert sorted(leg.exit_reason for leg in legs) == [
            "TAKE_PROFIT_1", "TAKE_PROFIT_2",
        ], "each stage exactly once, none missing"
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(fault)

        # exact equality with the failure-free reference run
        fault_legs = _legs_by_reason(store, pos.position_key)
        for reason in ("TAKE_PROFIT_1", "TAKE_PROFIT_2"):
            a, b = ref_legs[reason], fault_legs[reason]
            for field in (
                "quantity", "remaining_after", "reference_price",
                "fill_price", "gross_pnl", "exit_fee", "exit_slippage",
                "entry_cost_share", "net_pnl", "cumulative_realized_net",
            ):
                assert getattr(a, field) == pytest.approx(
                    getattr(b, field), abs=1e-9,
                ), f"{reason}.{field}"

        # ---- restart: state preserved, recovery NOT blocked -------------
        restarted = _restart(fault, settings, clock)
        loaded = _position(restarted, pos.symbol)
        assert loaded.tp1_executed and loaded.tp2_executed
        assert loaded.remaining_size == pytest.approx(
            full_size * 0.125, rel=1e-9,
        )
        cycle = restarted.run_cycle(
            {pos.symbol: pos.entry_price},
            bars={pos.symbol: {"high": pos.entry_price,
                               "low": pos.entry_price}},
        )
        assert restarted.engine.dirty_leg_keys == ()
        assert not restarted.engine.durability_risk
        assert cycle["exit_reasons"] == []
        # entries are usable again (no permanent fail-closed deadlock)
        assert restarted.engine.accounting_consistent
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(restarted)

    def test_same_bar_tp2_is_held_until_tp1_confirms(self):
        """The dirty set is re-read inside the ladder loop: a TP1 commit
        that fails IN THIS BAR blocks TP2 of the SAME bar."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        full_size = pos.position_size
        _failing_commit(store)
        bar = {"high": pos.tp2_price, "low": pos.tp2_price}
        contour.run_cycle({pos.symbol: pos.tp2_price}, bars={pos.symbol: bar})
        assert pos.tp1_executed is True
        assert pos.tp2_executed is False
        assert pos.position_key in contour.engine.dirty_leg_keys
        assert store.list_close_legs(pos.position_key) == []
        # recovery converges on the next cycle
        contour.run_cycle({pos.symbol: pos.tp2_price}, bars={pos.symbol: bar})
        assert pos.tp2_executed is True
        assert contour.engine.dirty_leg_keys == ()
        assert pos.remaining_size == pytest.approx(full_size * 0.125, rel=1e-9)
        _assert_durable_consistent(store, pos.position_key)

    def test_tp3_is_also_gated_while_a_partial_leg_is_unconfirmed(self):
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        _failing_commit(store)
        # single bar through TP1 AND TP3
        bar = {"high": pos.tp3_price, "low": pos.tp3_price}
        contour.run_cycle({pos.symbol: pos.tp3_price}, bars={pos.symbol: bar})
        assert pos.tp1_executed is True
        assert pos.status == POSITION_STATUS_OPEN, (
            "TP3 closed the position while TP1 was unconfirmed",
        )
        assert pos.position_key in contour.engine.dirty_leg_keys
        # next cycle: retry confirms TP1, then the rest of the ladder
        # (TP2 of the same bar, then TP3) fires normally — exactly once
        # each, all durable.
        contour.run_cycle({pos.symbol: pos.tp3_price}, bars={pos.symbol: bar})
        assert pos.status == POSITION_STATUS_CLOSED
        assert contour.engine.dirty_leg_keys == ()
        legs = store.list_close_legs(pos.position_key)
        assert sorted(leg.exit_reason for leg in legs) == [
            "TAKE_PROFIT_1", "TAKE_PROFIT_2", "TAKE_PROFIT_3",
        ]
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)

    def test_ambiguous_commit_is_adopted_without_dirty_state(self):
        """COMMIT applied, acknowledgement lost: adopted on read-back —
        nothing dirty, nothing double-booked, ladder continues."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        original = store.commit_partial_exit

        def apply_then_lose_ack(position, leg):
            original(position, leg)               # durable NOW
            raise RuntimeError("connection reset before acknowledgement")

        store.commit_partial_exit = apply_then_lose_ack
        contour.run_cycle({pos.symbol: entry * 0.945})
        store.commit_partial_exit = original

        assert contour.engine.dirty_leg_keys == ()
        assert contour.engine._uncommitted_deltas == {}
        assert contour.engine._dirty_funding == {}
        assert not contour.engine.durability_risk
        legs = store.list_close_legs(pos.position_key)
        assert [leg.exit_reason for leg in legs] == ["TAKE_PROFIT_1"]
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)

        # a re-issued retry (duplicate delivery) never books the leg twice
        store.commit_partial_exit(pos, legs[0])
        assert len(store.list_close_legs(pos.position_key)) == 1
        _assert_durable_consistent(store, pos.position_key)

        # and the ladder continues normally afterwards
        contour.run_cycle({pos.symbol: entry * 0.929})
        assert pos.tp2_executed is True
        assert sorted(
            leg.exit_reason for leg in store.list_close_legs(pos.position_key)
        ) == ["TAKE_PROFIT_1", "TAKE_PROFIT_2"]
        _assert_ledger_consistent(contour)

    def test_duplicate_partial_leg_delivery_books_once(self):
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        legs = store.list_close_legs(pos.position_key)
        assert len(legs) == 1
        # duplicate delivery of the SAME leg (restart replay / redelivery)
        store.commit_partial_exit(pos, legs[0])
        store.commit_partial_exit(pos, legs[0])
        assert len(store.list_close_legs(pos.position_key)) == 1
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)

    def test_protective_stop_stages_the_owed_leg_rows(self):
        """A STOP while TP1 is dirty stages TP1's row with its own
        snapshot — the restart can never lose the executed TP record."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price

        def always_fail(position, leg):
            raise RuntimeError("ledger down")

        store.commit_partial_exit = always_fail
        contour.run_cycle({pos.symbol: entry * 0.945})       # TP1 dirty

        bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                             "low": entry * 0.99}}
        cycle = contour.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]

        # TP1's leg row is durable ALREADY — staged with the terminal
        # snapshot even though commit_partial_exit never succeeded.
        legs = store.list_close_legs(pos.position_key)
        assert sorted(leg.exit_reason for leg in legs) == [
            "STOP_LOSS", "TAKE_PROFIT_1",
        ], legs
        durable = store.get_position(pos.position_key)
        assert durable.status == POSITION_STATUS_CLOSED
        assert durable.tp1_executed is True
        _assert_durable_consistent(store, pos.position_key)

        # restart with the ledger STILL failing: rows and rows stay correct
        restarted = _restart(contour, settings, clock)
        assert restarted.engine.dirty_leg_keys == ()
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(restarted)

        # with the store healed, the in-memory dirty marker converges too
        store.commit_partial_exit = (
            lambda p, l: InMemoryInverseShortStore.commit_partial_exit(store, p, l)
        )
        restarted.run_cycle({pos.symbol: entry})
        assert restarted.engine.dirty_leg_keys == ()
        _assert_ledger_consistent(restarted)

    def test_single_bar_fires_the_whole_ladder_exactly_once(self):
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        full_size = pos.position_size
        bar = {"high": pos.tp3_price, "low": pos.tp3_price}
        contour.run_cycle({pos.symbol: pos.tp3_price}, bars={pos.symbol: bar})

        assert pos.status == POSITION_STATUS_CLOSED
        assert pos.remaining_size == 0.0
        legs = store.list_close_legs(pos.position_key)
        assert sorted(leg.exit_reason for leg in legs) == [
            "TAKE_PROFIT_1", "TAKE_PROFIT_2", "TAKE_PROFIT_3",
        ]
        assert round(sum(leg.quantity for leg in legs), 9) == round(
            full_size, 9,
        ), "quantity conservation across the whole ladder"
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)

    def test_tp1_tp2_then_stop_closes_only_the_remainder(self):
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        full_size = pos.position_size

        contour.run_cycle({pos.symbol: entry * 0.945})
        contour.run_cycle({pos.symbol: entry * 0.929})
        assert pos.tp1_executed and pos.tp2_executed
        remainder = pos.remaining_size
        assert remainder == pytest.approx(full_size * 0.125, rel=1e-9)

        bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                             "low": entry * 0.99}}
        cycle = contour.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        legs = store.list_close_legs(pos.position_key)
        assert sorted(leg.exit_reason for leg in legs) == [
            "STOP_LOSS", "TAKE_PROFIT_1", "TAKE_PROFIT_2",
        ]
        terminal = [leg for leg in legs if leg.is_terminal][0]
        assert terminal.quantity == pytest.approx(remainder, rel=1e-9)
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)

    def test_funding_settlement_is_deferred_while_a_leg_is_unconfirmed(self):
        """P1-2 x P2: the full-object funding write never runs while this
        key owes leg rows; the obligation stays open and settles once the
        leg confirms, on the historically correct quantity."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price

        def always_fail(position, leg):
            raise RuntimeError("ledger down")

        store.commit_partial_exit = always_fail
        contour.run_cycle({pos.symbol: entry * 0.945})   # TP1 dirty at t0
        assert pos.position_key in contour.engine.dirty_leg_keys

        # one period is due while the leg rows are still owed -> the
        # settlement (a FULL-object write) must be deferred, not snapped
        # into a dirty-funding marker and never partially booked.
        clock.advance(hours=9)
        contour.run_cycle(
            {pos.symbol: entry * 0.945},
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert pos.position_key in contour.engine.dirty_leg_keys
        assert pos.funding_paid == 0.0
        assert pos.funding_periods_charged == 0
        assert contour.engine._dirty_funding == {}
        # only the LEG delta is uncommitted — funding added no delta of its
        # own (the settlement was deferred before any accrual)
        assert set(contour.engine._uncommitted_deltas) == {pos.position_key}
        durable = store.get_position(pos.position_key)
        assert durable.funding_paid == 0.0
        assert durable.remaining_size == pos.position_size

        # store heals: the dirty-leg retry confirms TP1 FIRST (start of
        # cycle), the gate lifts and funding settles exactly once — on the
        # historical quantity (TP1 executed at t0, BEFORE the t0+8h
        # boundary -> the period is charged on the post-TP1 quantity).
        store.commit_partial_exit = (
            lambda p, l: InMemoryInverseShortStore.commit_partial_exit(store, p, l)
        )
        contour.run_cycle(
            {pos.symbol: entry * 0.945},
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert contour.engine.dirty_leg_keys == ()
        assert pos.funding_periods_charged == 1
        legs = store.list_close_legs(pos.position_key)
        assert [leg.exit_reason for leg in legs] == ["TAKE_PROFIT_1"]
        qty_at_boundary = pos.position_size - legs[0].quantity
        expected = -(pos.entry_price * qty_at_boundary) * (0.01 / 100.0)
        assert pos.funding_paid == pytest.approx(expected, rel=1e-9)
        durable = store.get_position(pos.position_key)
        assert durable.funding_paid == pytest.approx(expected, rel=1e-9)
        assert durable.funding_periods_charged == 1
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)

    def test_interleaved_mark_writes_never_change_pnl(self):
        """'Concurrent' persistence (marks + legs + funding interleaved in
        one process) never double-books anything."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price

        for price in (entry * 0.945, entry * 0.929, entry * 0.899):
            contour.run_cycle({pos.symbol: price})
            # extra mark persistence between cycles — telemetry only
            contour.engine.mark_to_market({pos.symbol: price})
            contour.engine.persist_marks()
            _assert_durable_consistent(store, pos.position_key)
            _assert_ledger_consistent(contour)

        assert pos.status == POSITION_STATUS_CLOSED
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# P2 — funding on historical (per-period) quantities
# ---------------------------------------------------------------------------

class TestP2HistoricalFundingQuantities:
    def test_backlog_period_before_tp1_charged_on_full_quantity(self):
        """Funding period settled BEFORE TP1's boundary uses the pre-exit
        quantity (100), not the live remainder (50)."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        full_size = pos.position_size

        clock.advance(hours=8, minutes=1)
        # period 1 due, rate UNKNOWN -> obligation stays open
        contour.run_cycle(
            {pos.symbol: pos.entry_price},
            funding_rates_percent={},
        )
        assert pos.funding_periods_charged == 0
        assert pos.funding_paid == 0.0

        # TP1 AFTER the period-1 boundary
        contour.run_cycle({pos.symbol: pos.entry_price * 0.945})
        assert pos.tp1_executed
        assert pos.remaining_size == pytest.approx(full_size * 0.5, rel=1e-9)

        # rate observed one hour later: period 1 must charge quantity 100
        clock.advance(hours=1)
        contour.run_cycle(
            {pos.symbol: pos.entry_price * 0.945},
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert pos.funding_periods_charged == 1
        expected_full = -(pos.entry_price * full_size) * (0.01 / 100.0)
        expected_remaining = -(
            pos.entry_price * pos.remaining_size
        ) * (0.01 / 100.0)
        assert pos.funding_paid == pytest.approx(expected_full, rel=1e-9), (
            "the pre-TP1 period must be charged on the HISTORICAL full "
            "quantity, not on the live remainder",
        )
        assert pos.funding_paid != pytest.approx(expected_remaining, rel=1e-6)
        _assert_ledger_consistent(contour)

    def test_mixed_backlog_spans_tp1_and_tp2_with_per_period_quantities(self):
        """Funding UNKNOWN across TP1 and TP2: period 1 -> full quantity,
        period 2 -> quantity after TP2, each reconstructed from the
        durable leg history."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        full_size = pos.position_size

        # period 1 boundary passes with UNKNOWN rate
        clock.advance(hours=8, minutes=1)
        contour.run_cycle({pos.symbol: entry}, funding_rates_percent={})
        # TP1 after boundary 1, TP2 before boundary 2
        contour.run_cycle({pos.symbol: entry * 0.945})
        clock.advance(hours=1)
        contour.run_cycle({pos.symbol: entry * 0.929})
        assert pos.tp1_executed and pos.tp2_executed
        # period 2 boundary passes (t0 + 16h), still UNKNOWN
        clock.advance(hours=7)                     # t0 + 16h01m
        contour.run_cycle({pos.symbol: entry * 0.929}, funding_rates_percent={})
        assert pos.funding_periods_charged == 0

        # first observed rate settles BOTH periods, each on its own
        # historically outstanding quantity
        legs = store.list_close_legs(pos.position_key)
        qty_after_tp2 = pos.remaining_size
        qty_at_period1 = full_size                # both legs AFTER +8h
        qty_at_period2 = qty_after_tp2            # both legs BEFORE +16h
        clock.advance(hours=1)                    # t0 + 17h01m -> due = 2
        contour.run_cycle(
            {pos.symbol: entry * 0.929},
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert pos.funding_periods_charged == 2
        rate = 0.01 / 100.0
        expected = -(
            pos.entry_price * qty_at_period1
            + pos.entry_price * qty_at_period2
        ) * rate
        assert len(legs) == 2
        assert pos.funding_paid == pytest.approx(expected, rel=1e-9), (
            "each backlog period must be charged on its own outstanding "
            "quantity (100 at period 1, 12.5% at period 2)",
        )
        # never the naive live-remainder calc for both periods
        naive = -(pos.entry_price * qty_after_tp2) * rate * 2
        assert pos.funding_paid != pytest.approx(naive, rel=1e-6)
        durable = store.get_position(pos.position_key)
        assert durable.funding_paid == pytest.approx(expected, rel=1e-9)
        assert durable.funding_periods_charged == 2
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)

    def test_unsettled_funding_obligation_survives_restart_and_settles_once(
        self,
    ):
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        full_size = pos.position_size

        clock.advance(hours=8, minutes=1)
        contour.run_cycle({pos.symbol: entry}, funding_rates_percent={})
        assert pos.funding_periods_charged == 0    # memory

        # RESTART: the obligation is reconstructible from the row alone
        restarted = _restart(contour, settings, clock)
        loaded = _position(restarted, pos.symbol)
        assert loaded.funding_periods_charged == 0
        assert loaded.funding_paid == 0.0
        restarted.run_cycle(
            {pos.symbol: entry},
            funding_rates_percent={pos.symbol: 0.01},
        )
        loaded = _position(restarted, pos.symbol)
        assert loaded.funding_periods_charged == 1
        expected = -(loaded.entry_price * full_size) * (0.01 / 100.0)
        assert loaded.funding_paid == pytest.approx(expected, rel=1e-9)

        # replaying the same cycle window books NOTHING twice
        restarted.run_cycle(
            {pos.symbol: entry},
            funding_rates_percent={pos.symbol: 0.01},
        )
        loaded = _position(restarted, pos.symbol)
        assert loaded.funding_periods_charged == 1
        assert loaded.funding_paid == pytest.approx(expected, rel=1e-9)
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(restarted)

    def test_ambiguous_funding_commit_is_applied_exactly_once(self):
        """COMMIT applied, acknowledgement lost: adopted on read-back —
        no dirty delta, no double booking on the next cycle."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        full_size = pos.position_size

        clock.advance(hours=8, minutes=1)
        original_update = store.update_position

        def apply_then_lose_ack(position):
            original_update(position)
            raise RuntimeError("connection reset before acknowledgement")

        store.update_position = apply_then_lose_ack
        contour.run_cycle(
            {pos.symbol: entry},
            funding_rates_percent={pos.symbol: 0.01},
        )
        store.update_position = original_update

        assert contour.engine._uncommitted_deltas == {}
        assert contour.engine._dirty_funding == {}
        expected = -(pos.entry_price * full_size) * (0.01 / 100.0)
        assert pos.funding_paid == pytest.approx(expected, rel=1e-9)
        durable = store.get_position(pos.position_key)
        assert durable.funding_paid == pytest.approx(expected, rel=1e-9)

        # next cycle: charged already == due -> nothing booked again
        contour.run_cycle(
            {pos.symbol: entry},
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert pos.funding_paid == pytest.approx(expected, rel=1e-9)
        assert pos.funding_periods_charged == 1
        _assert_ledger_consistent(contour)

    def test_unprovable_history_never_finalizes_a_settlement(self):
        """If the durable leg history cannot prove the executed stages,
        the settlement is NOT finalized (counter and balance unchanged)."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        assert pos.tp1_executed

        # corrupt the ledger: the durable leg row disappears
        original_list = store.list_close_legs

        def missing_history(position_key=None):
            return []

        store.list_close_legs = missing_history
        clock.advance(hours=9)
        contour.run_cycle(
            {pos.symbol: entry * 0.945},
            funding_rates_percent={pos.symbol: 0.01},
        )
        store.list_close_legs = original_list

        # nothing was booked, obligation stays open
        assert pos.funding_periods_charged == 0
        assert pos.funding_paid == 0.0
        assert contour.engine._uncommitted_deltas == {}
        _assert_ledger_consistent(contour)

        # with the history readable again the settlement completes once
        contour.run_cycle(
            {pos.symbol: entry * 0.945},
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert pos.funding_periods_charged == 1
        assert pos.funding_paid != 0.0
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# Static source contracts (no database required)
# ---------------------------------------------------------------------------

class TestHardeningSourceContracts:
    def test_check_exits_gates_the_ladder_on_dirty_legs(self):
        source = inspect.getsource(InverseShortPaperEngine.check_exits)
        assert "_dirty_legs" in source, "ladder gate missing from check_exits"
        assert "ladder HELD" in source

    def test_apply_funding_uses_durable_leg_history(self):
        source = inspect.getsource(InverseShortPaperEngine._apply_funding)
        assert "list_close_legs" in source, (
            "funding must derive per-period quantities from the durable "
            "close-leg history",
        )
        assert "executed_at <= boundary" in source, (
            "per-period outstanding quantity must be reconstructed at each "
            "period's funding boundary",
        )
        assert "historical quantity cannot be proven" in source
        assert "_dirty_legs" in source, (
            "settlement must be deferred while leg rows are owed",
        )

    def test_retry_dirty_funding_never_writes_ahead_of_the_leg_ledger(self):
        source = inspect.getsource(InverseShortPaperEngine._retry_dirty_funding)
        assert "_dirty_legs" in source

    def test_both_stores_implement_the_same_mark_contract(self):
        for cls in (InMemoryInverseShortStore, PgInverseShortStore):
            assert hasattr(cls, "update_position_marks"), cls.__name__
            parameters = inspect.signature(cls.stage_close).parameters
            assert "partial_legs" in parameters, cls.__name__

    def test_engine_stage_constants_match_the_models(self):
        # TP1/TP2/terminal leg identity used by the gate and staging
        from app.paper.me_long_v2_inverse_short_oos2_v1.models import (
            LEG_SEQ_TAKE_PROFIT_1,
            LEG_SEQ_TAKE_PROFIT_2,
            LEG_SEQ_TERMINAL,
        )
        assert (LEG_SEQ_TAKE_PROFIT_1, LEG_SEQ_TAKE_PROFIT_2,
                LEG_SEQ_TERMINAL) == (1, 2, 3)
        # frozen protocol parameters were NOT touched by the hardening
        assert ENTRY_MAX_AGE_MINUTES == 120
        assert MAX_HOLD_MINUTES == 7200
        from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
            MAX_OPEN_POSITIONS,
        )
        assert MAX_OPEN_POSITIONS == 12
