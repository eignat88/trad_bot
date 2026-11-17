"""ME_LONG_V2_INVERSE_SHORT_OOS2_V1 вЂ” Codex review hardening regressions.

Fault-injection suite for the three review findings on PR #278:

* **P1-1** вЂ” ``persist_marks()`` may only write MARK telemetry
  (``update_position_marks``): after a FAILED ``commit_partial_exit()``
  the durable row must stay exactly what PostgreSQL confirmed вЂ” a mark
  write never carries ``remaining_size`` / TP flags / realized PnL /
  funding / status forward.
* **P1-2** вЂ” the ladder never advances over an unconfirmed partial leg:
  TP1's dirty row is retried and confirmed BEFORE TP2 (or TP3) may fire,
  so the mandatory fault-injection scenario
  ``TP1 calculated -> DB write fails -> retry -> TP1 confirmed -> TP2 ->
  restart`` ends with each stage exactly once, no missing stages, the
  12.5% remainder, PnL/fees identical to a failure-free reference run,
  and no permanently blocked recovery.  Protective STOP/TIMEOUT stay
  active while the ledger is dirty, and a terminal close stages the owed
  partial leg rows with its own snapshot (no lost TP records on restart).
* **P2** вЂ” funding settles PER PERIOD on the quantity outstanding at that
  period's funding instant, reconstructed from the DURABLE close-leg
  history; an unprovable history never finalizes a settlement, and an
  unsettled obligation survives restart exactly once.

Everything here runs against the in-memory store вЂ” no PostgreSQL in this
suite (the real-PostgreSQL fault-injection lives in
``test_me_long_v2_inverse_short_oos2_partial_restart_pg_it_v1.py``,
gated on ``TEST_DB_HOST``).
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from app.paper.me_long_v2_inverse_short_oos2_v1 import journal as _journal_mod
from app.paper.me_long_v2_inverse_short_oos2_v1.journal import (
    PendingProtectiveClose,
    ProtectiveCloseJournal,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.models import (
    FUNDING_OBLIGATION_OPEN,
    FUNDING_OBLIGATION_SETTLED,
    LEG_SEQ_TERMINAL,
    LEG_SEQ_TAKE_PROFIT_1,
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


@pytest.fixture(autouse=True)
def _isolated_journal_directory(tmp_path, monkeypatch):
    """Every test in this module gets its OWN journal directory.

    Round 9 (P1, Codex r4590138816): a protective close now writes its
    recovery record even when no funding obligation is pending, so a
    fault-injection test that dies before its close completes legitimately
    leaves a record behind.  Sharing the default ``var/`` directory would
    let that record be replayed by the NEXT test in the same session; the
    module therefore redirects the default to a per-test directory that
    pytest discards with the test itself.
    """
    from app.paper.me_long_v2_inverse_short_oos2_v1 import journal as _journal

    monkeypatch.setattr(
        _journal, "DEFAULT_JOURNAL_DIR", tmp_path / "journal",
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
    store: InMemoryInverseShortStore | None = None,
    journal_dir=None,
) -> tuple[InverseShortContour, InMemoryInverseShortStore, _Clock]:
    settings = settings or _settings()
    clock = clock or _Clock(NOW)
    store = store or InMemoryInverseShortStore(
        _activation(), initial_balance=settings.initial_balance,
    )
    contour = InverseShortContour(
        settings, store, clock=clock, activation_enabled=True,
        journal_dir=journal_dir,
    )
    assert contour.activated, contour.activation_reason
    return contour, store, clock


def _restart(
    contour: InverseShortContour, settings: Settings, clock: _Clock,
    journal_dir=None,
) -> InverseShortContour:
    """Simulate a process restart: a NEW engine over the SAME durable store."""
    restarted = InverseShortContour(
        settings, contour.store, clock=clock, activation_enabled=True,
        # Round 7 (P1): the SAME journal directory is replayed by the new
        # engine вЂ” that is exactly what makes a retained protective close
        # survive "restart before PostgreSQL recovery".
        journal_dir=(
            journal_dir if journal_dir is not None
            else getattr(contour, "journal_dir", None)
        ),
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
    """balance == initial + ОЈclosed net + ОЈopen(realized legs в€’ costs)."""
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
    expected += sum(
        delta for key, delta in engine._uncommitted_deltas.items()
        # Round 6 (P1): an AMBIGUOUS key's delta is QUARANTINED вЂ” the
        # engine balance deliberately does not carry it while the durable
        # outcome is unknown, so the identity must not either.
        if key not in engine._ambiguous_keys
    )
    assert expected == pytest.approx(engine.balance, abs=1e-6), (
        engine.balance, expected,
    )


def _assert_durable_consistent(
    store: InMemoryInverseShortStore, position_key: str,
) -> None:
    """Section-6 accounting audit against the DURABLE rows only.

    quantity conservation, one durable execution per stage, OPEN/CLOSED
    remainder rules вЂ” i.e. exactly what a restart would see.
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
            "conservation: initial = remaining + ОЈ durable leg quantities",
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
            "conservation: initial = ОЈ durable leg quantities (terminal "
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
# P1-1 вЂ” mark-only persistence
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
            "persist_marks must not call update_position вЂ” a full-object "
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

        # RESTART вЂ” dirty state is process-local by contract
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
# P1-2 вЂ” ladder gated on durable confirmation
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
        # (TP2 of the same bar, then TP3) fires normally вЂ” exactly once
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
        """COMMIT applied, acknowledgement lost: adopted on read-back вЂ”
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
        snapshot вЂ” the restart can never lose the executed TP record."""
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

        # TP1's leg row is durable ALREADY вЂ” staged with the terminal
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
        # only the LEG delta is uncommitted вЂ” funding added no delta of its
        # own (the settlement was deferred before any accrual)
        assert set(contour.engine._uncommitted_deltas) == {pos.position_key}
        durable = store.get_position(pos.position_key)
        assert durable.funding_paid == 0.0
        assert durable.remaining_size == pos.position_size

        # store heals: the dirty-leg retry confirms TP1 FIRST (start of
        # cycle), the gate lifts and funding settles exactly once вЂ” on the
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
            # extra mark persistence between cycles вЂ” telemetry only
            contour.engine.mark_to_market({pos.symbol: price})
            contour.engine.persist_marks()
            _assert_durable_consistent(store, pos.position_key)
            _assert_ledger_consistent(contour)

        assert pos.status == POSITION_STATUS_CLOSED
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# P2 вЂ” funding on historical (per-period) quantities
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
        """COMMIT applied, acknowledgement lost: adopted on read-back вЂ”
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
# Codex review round 2 вЂ” P1: a terminal close is never "fully saved"
# before its close-leg and outbox rows are durable
# ---------------------------------------------------------------------------

class TestRound2TerminalCloseNeverSavedWithoutLegs:
    def test_partial_commit_and_stage_fail_terminal_update_would_succeed_then_restart(
        self,
    ):
        """Mandatory combined-failure scenario (round 2, P1):
        partial commit AND stage_close() both fail while update_position()
        itself is healthy вЂ” the terminal UPDATE must not run (the position
        may not be reported fully saved without its close-leg and outbox
        rows), every obligation is retained, and a subsequent restart
        restores the partial and terminal closes exactly once."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        full_size = pos.position_size

        # (a) the partial-leg commit ALWAYS fails ...
        def always_fail_leg(position, leg):
            raise RuntimeError("partial leg write failed")

        store.commit_partial_exit = always_fail_leg

        # (b) staging ALWAYS fails ...
        original_stage = store.stage_close

        def always_fail_stage(*args, **kwargs):
            raise RuntimeError("close-leg/outbox staging failed")

        store.stage_close = always_fail_stage

        # (c) ... while update_position() itself is perfectly healthy: the
        # spy proves it must not even be attempted.
        update_calls = {"n": 0}
        original_update = store.update_position

        def spy_update(position_row):
            update_calls["n"] += 1
            return original_update(position_row)

        store.update_position = spy_update

        # TP1 applies in memory, fails durably (dirty leg).
        contour.run_cycle({pos.symbol: entry * 0.945})
        assert pos.position_key in contour.engine.dirty_leg_keys

        # The protective STOP fires while BOTH writes are down.
        bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                             "low": entry * 0.99}}
        cycle = contour.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]

        # The terminal UPDATE was NEVER attempted: the position may not be
        # reported fully saved before its close-leg and outbox rows are
        # durable вЂ” even though update_position() would have succeeded.
        assert update_calls["n"] == 0, (
            "terminal update_position() ran while staging was unconfirmed",
        )
        durable = store.get_position(pos.position_key)
        assert durable.status == POSITION_STATUS_OPEN, (
            "position claimed fully saved without close-leg/outbox rows",
        )
        assert durable.remaining_size == pytest.approx(full_size, rel=1e-9)
        assert store.list_close_legs(pos.position_key) == []
        assert store.list_pending_closes() == []      # nothing staged yet

        # Every obligation to restore the partial AND terminal closes is
        # retained in-process for the stage-before-update retry.
        engine = contour.engine
        assert pos.position_key in engine._unstaged_closes
        assert pos.position_key in engine.pending_closes
        assert pos.position_key in engine._terminal_legs
        assert pos.position_key in engine.dirty_leg_keys
        assert engine.durability_risk is True
        assert cycle["pending_closes"] == 1

        # ---- the process dies here: only the DURABLE state survives ----
        store.stage_close = original_stage
        store.commit_partial_exit = (
            lambda p, l: InMemoryInverseShortStore.commit_partial_exit(store, p, l)
        )
        restarted = _restart(contour, settings, clock)

        # Round 9 (P1, Codex r4590138816): the frozen protective close was
        # journaled BEFORE staging, so the restart replays it as a RETAINED
        # close instead of leaving a STOP to be re-derived from fresh market
        # data.  The DURABLE row is still the untouched pre-close state:
        # nothing was claimed saved that the ledger does not carry.
        engine2 = restarted.engine
        assert pos.position_key in engine2.pending_closes
        assert engine2.journal.exists(pos.position_key)
        record = engine2.journal.read_all()[pos.position_key]
        assert record.owed_partial_legs, "the owed TP1 leg rides the record"
        assert record.staged is False
        assert store.get_position(pos.position_key).status == (
            POSITION_STATUS_OPEN
        )
        assert store.list_close_legs(pos.position_key) == []
        assert store.list_pending_closes() == []
        assert engine2.dirty_leg_keys == ()
        assert engine2.durability_risk is True   # staged snapshot still owed

        # ONE recovery cycle replays the frozen snapshot: the owed partial
        # leg and the terminal leg land in the SAME idempotent staging
        # transaction and then the terminal UPDATE flips the row — the
        # partial AND the terminal closes are restored exactly once and are
        # never re-derived from a new market evaluation.
        cycle = restarted.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == [], (
            "a retained protective close must never be re-evaluated",
        )
        assert cycle["pending_closes"] == 0
        final = store.get_position(pos.position_key)
        assert final.status == POSITION_STATUS_CLOSED
        assert final.tp1_executed is True
        # TP1 already sold half, the STOP retires the rest: nothing open.
        assert final.remaining_size == pytest.approx(0.0, abs=1e-12)
        legs = store.list_close_legs(pos.position_key)
        assert sorted(leg.exit_reason for leg in legs) == [
            "STOP_LOSS", "TAKE_PROFIT_1",
        ]
        assert [leg.leg_seq for leg in legs] == [1, 3]
        assert store.list_pending_closes() == []      # staged AND cleared
        assert engine2.durability_risk is False
        assert engine2.dirty_leg_keys == ()
        assert not engine2.journal.exists(pos.position_key), (
            "the recovery record is retired only after both halves confirm",
        )
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(restarted)

    def test_degraded_staging_mode_keeps_the_documented_update_exception(
        self,
    ):
        """Migration-088-absent mode (stage_close returns False, no raise)
        is the documented exception: the terminal UPDATE is the only
        durability left and still runs."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price

        store.stage_close = lambda *args, **kwargs: False   # degraded, no raise

        bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                             "low": entry * 0.99}}
        cycle = contour.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        durable = store.get_position(pos.position_key)
        assert durable.status == POSITION_STATUS_CLOSED
        assert cycle["pending_closes"] == 0
        assert contour.engine.durability_risk is False
        assert contour.engine.unstaged_close_keys == ()

    def test_staged_close_still_flips_immediately(self):
        """The common path is unchanged: confirmed staging + successful
        update = a fully saved close in one cycle."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                             "low": entry * 0.99}}
        cycle = contour.run_cycle({pos.symbol: entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        assert cycle["pending_closes"] == 0
        assert store.get_position(pos.position_key).status == (
            POSITION_STATUS_CLOSED
        )
        legs = store.list_close_legs(pos.position_key)
        assert [leg.exit_reason for leg in legs] == ["STOP_LOSS"]
        assert store.list_pending_closes() == []
        assert contour.engine.durability_risk is False
        _assert_durable_consistent(store, pos.position_key)
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# Codex review round 2 вЂ” P2: unified funding-before-exit at an EXACT
# boundary timestamp (normal vs restored calculation must agree)
# ---------------------------------------------------------------------------

class TestRound2ExactBoundaryFundingOrder:
    def test_partial_tp_at_exact_boundary_normal_equals_restored(self):
        """A partial TP executed EXACTLY on the funding boundary is
        accounted AFTER the accrual for that boundary: the normal in-cycle
        calculation (funding settles first, then the TP fires at the very
        same timestamp) and the calculation restored from the durable leg
        ledger after a restart must be exactly equal вЂ” and both charge the
        FULL pre-TP quantity for that period, never the live remainder."""
        settings = _settings()

        # --- A: NORMAL вЂ” the rate is observed in the boundary cycle ------
        a, a_store, a_clock = _contour(settings)
        a_pos = _open_one(a, a_store, a_clock)
        a_clock.advance(hours=8)              # EXACTLY the period-1 boundary
        a.run_cycle(
            {a_pos.symbol: a_pos.entry_price * 0.945},
            funding_rates_percent={a_pos.symbol: 0.01},
        )
        assert a_pos.tp1_executed is True
        assert a_pos.funding_periods_charged == 1
        legs_a = a_store.list_close_legs(a_pos.position_key)
        assert [leg.exit_reason for leg in legs_a] == ["TAKE_PROFIT_1"]
        boundary = a_pos.entered_at + timedelta(hours=8)
        assert legs_a[0].executed_at == boundary, (
            "the TP must sit EXACTLY on the funding boundary",
        )
        # funding-before-exit: the accrual ran BEFORE the TP in that cycle,
        # so period 1 was charged on the FULL pre-TP quantity.
        expected_full = -(
            a_pos.entry_price * a_pos.position_size
        ) * (0.01 / 100.0)
        assert a_pos.funding_paid == pytest.approx(expected_full, rel=1e-12)

        # --- B: RESTORED вЂ” same TP on the same instant, rate arrives one
        # hour later, and the settlement happens after a RESTART from the
        # durable ledger.
        b, b_store, b_clock = _contour(settings)
        b_pos = _open_one(b, b_store, b_clock)
        assert b_pos.entered_at == a_pos.entered_at    # identical t0
        b_clock.advance(hours=8)
        b.run_cycle(
            {b_pos.symbol: b_pos.entry_price * 0.945},
            funding_rates_percent={},                  # UNKNOWN at the boundary
        )
        assert b_pos.tp1_executed is True
        assert b_pos.funding_periods_charged == 0      # obligation preserved
        legs_b = b_store.list_close_legs(b_pos.position_key)
        assert legs_b[0].executed_at == boundary       # exact timestamp match

        # RESTART with the unsettled obligation, then the rate arrives.
        b2 = _restart(b, settings, b_clock)
        b2.run_cycle(
            {b_pos.symbol: b_pos.entry_price * 0.945},
            funding_rates_percent={b_pos.symbol: 0.01},
        )
        restored = _position(b2, b_pos.symbol)
        assert restored.funding_periods_charged == 1

        # --- the comparison: normal == restored, both on the FULL size ---
        assert restored.funding_paid == pytest.approx(
            a_pos.funding_paid, rel=1e-12,
        ), "restored calculation must equal the normal funding-before-exit one"
        assert restored.funding_paid == pytest.approx(expected_full, rel=1e-12)
        live_remainder = -(
            restored.entry_price * restored.remaining_size
        ) * (0.01 / 100.0)
        assert restored.funding_paid != pytest.approx(
            live_remainder, rel=1e-6,
        ), "an exact-boundary TP must NOT reduce that period's quantity"

        # replay of the settled period books nothing twice
        b2.run_cycle(
            {b_pos.symbol: b_pos.entry_price * 0.945},
            funding_rates_percent={b_pos.symbol: 0.01},
        )
        assert restored.funding_paid == pytest.approx(expected_full, rel=1e-12)
        assert restored.funding_periods_charged == 1
        _assert_durable_consistent(b_store, b_pos.position_key)
        _assert_ledger_consistent(b2)

    def test_funding_retry_runs_before_the_leg_retry(self):
        """Unified funding-before-exit order restored in check_exits():
        the dirty-FUNDING retry precedes the dirty-LEG retry (exits)."""
        source = inspect.getsource(InverseShortPaperEngine.check_exits)
        funding_pos = source.index("_retry_dirty_funding()")
        legs_pos = source.index("_retry_dirty_legs()")
        assert funding_pos < legs_pos, (
            "funding retry must run before the leg (exit) retry",
        )
        # and the in-loop settlement still precedes the exit ladder
        funding_loop = source.index("self._apply_funding(position")
        ladder = source.index("ladder_blocked")
        assert funding_loop < ladder


# ---------------------------------------------------------------------------
# Codex review round 3 вЂ” P1: a pending terminal snapshot OWNS its key's
# leg rows; no independent dirty-leg replay until staging + terminal
# UPDATE are both confirmed.
# ---------------------------------------------------------------------------

class TestRound3PendingSnapshotOwnsLegReplay:
    def test_fault_injection_pending_snapshot_leg_replay_restart_reconciliation(
        self,
    ):
        """Mandatory fault-injection sequence (round 3, P1):
        TP1 persistence failure -> STOP -> terminal staging failure ->
        next-cycle retry -> store recovery -> restart -> reconciliation.
        The pending terminal snapshot excludes the key from independent
        dirty-leg replay; owed partial legs, terminal leg and outbox are
        restored only through the unified terminal-close recovery; the
        final balance equals the failure-free reference and every close
        leg is present exactly once."""
        settings = _settings()

        # ---- reference contour: the identical bar sequence, no failures --
        ref, ref_store, ref_clock = _contour(settings)
        ref_pos = _open_one(ref, ref_store, ref_clock)
        entry = ref_pos.entry_price
        stop_bars = {
            ref_pos.symbol: {"high": ref_pos.stop_price * 1.001,
                             "low": entry * 0.99},
        }
        ref.run_cycle({ref_pos.symbol: entry * 0.945})
        ref.run_cycle({ref_pos.symbol: entry}, bars=stop_bars)
        assert ref_pos.status == POSITION_STATUS_CLOSED
        ref_legs = _legs_by_reason(ref_store, ref_pos.position_key)
        assert sorted(ref_legs) == ["STOP_LOSS", "TAKE_PROFIT_1"]
        ref_balance = ref.engine.balance

        # ---- fault contour ----------------------------------------------
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        assert pos.entry_price == pytest.approx(entry, rel=1e-12)
        position_key = pos.position_key

        original_commit = store.commit_partial_exit
        original_stage = store.stage_close
        original_update = store.update_position

        # (a) TP1 persistence failure
        def fail_commit(position, leg):
            raise RuntimeError("TP1 persistence failure")

        store.commit_partial_exit = fail_commit

        # terminal staging failure (update_position itself stays healthy)
        def fail_stage(*args, **kwargs):
            raise RuntimeError("terminal staging failure")

        store.stage_close = fail_stage
        update_calls = {"n": 0}

        def spy_update(position_row):
            update_calls["n"] += 1
            return original_update(position_row)

        store.update_position = spy_update

        # (b) TP1 fails durably, then the protective STOP with staging down
        contour.run_cycle({pos.symbol: entry * 0.945})
        assert position_key in contour.engine.dirty_leg_keys
        cycle = contour.run_cycle({pos.symbol: entry}, bars=stop_bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        assert update_calls["n"] == 0, (
            "terminal UPDATE ran before staging was confirmed",
        )
        engine = contour.engine
        assert position_key in engine.pending_closes
        assert position_key in engine._unstaged_closes
        assert position_key in engine._terminal_legs
        assert position_key in engine.dirty_leg_keys
        assert engine.durability_risk is True
        durable = store.get_position(position_key)
        assert durable.status == POSITION_STATUS_OPEN
        assert store.list_close_legs(position_key) == []
        assert store.list_pending_closes() == []

        # (c) next-cycle retry: TP1 commit recovered, staging STILL down.
        #     The pending snapshot must exclude the independent replay вЂ”
        #     no leg-only insert against the still-OPEN durable row.
        store.commit_partial_exit = original_commit
        cycle = contour.run_cycle({pos.symbol: entry}, bars=stop_bars)
        assert store.list_close_legs(position_key) == [], (
            "independent dirty-leg replay ran against a pending terminal "
            "snapshot (leg-only insert into an OPEN row)",
        )
        durable = store.get_position(position_key)
        assert durable.status == POSITION_STATUS_OPEN
        assert durable.tp1_executed is False
        assert durable.remaining_size == pytest.approx(
            durable.position_size, rel=1e-9,
        )
        assert update_calls["n"] == 0
        assert position_key in contour.engine.dirty_leg_keys, (
            "the owed TP1 leg must stay owed until the unified recovery",
        )
        assert position_key in engine.pending_closes
        assert position_key in engine._unstaged_closes
        assert engine.durability_risk is True

        # (d) store recovery (PostgreSQL healed), then (e) RESTART: only
        #     durable state survives.
        store.stage_close = original_stage
        store.update_position = original_update
        restarted = _restart(contour, settings, clock)
        engine2 = restarted.engine
        # Round 9 (P1, Codex r4590138816): the frozen protective close was
        # journaled BEFORE staging, so it survived the restart as a RETAINED
        # close instead of existing only in process memory — while the
        # DURABLE row is still the untouched pre-close state.
        assert position_key in engine2.pending_closes
        assert engine2.journal.exists(position_key)
        record = engine2.journal.read_all()[position_key]
        assert record.owed_partial_legs, "the owed TP1 leg rides the record"
        assert store.get_position(position_key).status == POSITION_STATUS_OPEN
        assert store.list_close_legs(position_key) == []
        assert engine2.dirty_leg_keys == ()
        assert engine2.durability_risk is True   # staged snapshot still owed

        # (f) reconciliation: ONE recovery cycle replays the frozen snapshot
        #     through the unified terminal-close recovery — owed partial leg,
        #     terminal leg and outbox in the same idempotent transaction.
        cycle = restarted.run_cycle({pos.symbol: entry}, bars=stop_bars)
        assert cycle["exit_reasons"] == [], (
            "a retained protective close must never be re-evaluated",
        )
        assert cycle["pending_closes"] == 0
        legs = store.list_close_legs(position_key)
        assert sorted(leg.exit_reason for leg in legs) == [
            "STOP_LOSS", "TAKE_PROFIT_1",
        ]
        assert [leg.leg_seq for leg in legs] == [1, 3]
        final = store.get_position(position_key)
        assert final.status == POSITION_STATUS_CLOSED
        closed_qty = sum(leg.quantity for leg in legs)
        assert round(final.position_size - final.remaining_size, 9) == round(
            closed_qty, 9,
        )
        assert store.list_pending_closes() == []
        assert restarted.engine.durability_risk is False
        assert restarted.engine.dirty_leg_keys == ()
        assert restarted.engine.accounting_consistent is True

        # req 7: balance BEFORE (reference) and AFTER recovery coincide,
        # and every close leg matches the reference exactly.
        assert restarted.engine.balance == pytest.approx(
            ref_balance, abs=1e-9,
        ), "recovered balance must equal the failure-free reference"
        final_legs = _legs_by_reason(store, position_key)
        for reason in ("TAKE_PROFIT_1", "STOP_LOSS"):
            assert final_legs[reason].quantity == pytest.approx(
                ref_legs[reason].quantity, rel=1e-12,
            ), reason
            assert final_legs[reason].net_pnl == pytest.approx(
                ref_legs[reason].net_pnl, abs=1e-9,
            ), reason
        _assert_durable_consistent(store, position_key)
        _assert_ledger_consistent(restarted)

        # a further cycle books nothing twice
        balance_before = restarted.engine.balance
        restarted.run_cycle({pos.symbol: entry})
        assert len(store.list_close_legs(position_key)) == 2
        assert restarted.engine.balance == pytest.approx(
            balance_before, abs=1e-12,
        )

    def test_leg_only_insert_refuses_a_non_closed_durable_row(self):
        """Store-level guard (req 4): commit_partial_exit() must refuse a
        leg-only insert when the DURABLE row is still OPEN, even if handed
        a CLOSED snapshot."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        # build the CLOSED snapshot exactly like a pending terminal close
        contour.run_cycle({pos.symbol: entry * 0.945})     # TP1 committed
        legs = store.list_close_legs(pos.position_key)
        assert len(legs) == 1
        snapshot = dataclasses.replace(
            pos,
            status=POSITION_STATUS_CLOSED,
            remaining_size=0.0,
            tp1_executed=True,
            exit_reason="STOP_LOSS",
            closed_at=clock(),
        )
        # the durable row is still OPEN -> leg-only must be refused
        with pytest.raises(RuntimeError, match="LEG_ONLY_ROW_NOT_CLOSED"):
            store.commit_partial_exit(snapshot, legs[0])
        # and nothing landed
        assert len(store.list_close_legs(pos.position_key)) == 1


# ---------------------------------------------------------------------------
# Round 5 (Codex r4552612002) вЂ” observed funding must survive a dirty-leg
# terminal close: fault-injection scenarios AвЂ“D.  A/C/D are additionally
# executed against real PostgreSQL in
# ``test_me_long_v2_inverse_short_oos2_partial_restart_pg_it_v1.py``.
# ---------------------------------------------------------------------------

def _always_fail(message: str):
    """Store-write fault: every call raises (simulated outage)."""

    def _fail(*args, **kwargs):
        raise RuntimeError(message)

    return _fail


def _scenario_a_control(settings: Settings) -> dict:
    """Failure-free reference run of the scenario-A timeline.

    entry -> TP1 committed -> +9h -> protective STOP in the cycle where
    the rate (0.01%) is observed.  Every scenario-A-like fault run must
    converge to these exact numbers.
    """
    contour, store, clock = _contour(settings)
    pos = _open_one(contour, store, clock)
    entry = pos.entry_price
    stop_bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                              "low": entry * 0.99}}
    contour.run_cycle({pos.symbol: entry * 0.945})        # TP1 committed
    clock.advance(hours=9)
    cycle = contour.run_cycle(
        {pos.symbol: entry}, bars=stop_bars,
        funding_rates_percent={pos.symbol: 0.01},
    )
    assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
    row = store.get_position(pos.position_key)
    assert row.status == POSITION_STATUS_CLOSED
    assert row.funding_periods_charged == 1, "control must settle due=1"
    legs = _legs_by_reason(store, pos.position_key)
    assert sorted(legs) == ["STOP_LOSS", "TAKE_PROFIT_1"]
    return {
        "balance": contour.engine.balance,
        "funding_paid": row.funding_paid,
        "funding_periods_charged": row.funding_periods_charged,
        "net_pnl": row.net_pnl,
        "entry_price": row.entry_price,
        "position_size": row.position_size,
        "position_key": pos.position_key,
        "legs": legs,
    }


class TestRound5FundingSurvivesDirtyTerminalClose:
    """Codex r4552612002 вЂ” funding lost when STOP closes over a dirty TP.

    Scenario A: observed rate + dirty partial leg + protective STOP in
    the same cycle must produce ONE consistent terminal snapshot (owed
    legs + historically correct funding + terminal leg + outbox + final
    remainder + fees/slippage + net PnL), staged before the update, with
    obligations preserved on failure and restart-safe exactly-once.
    """

    def test_scenario_a_observed_funding_survives_dirty_tp_stop(self):
        """A (mandatory): TP1 commit fails -> 9h pass -> rate observed ->
        protective STOP in the SAME cycle -> staging -> restart ->
        reconciliation.  Funding is NOT lost: the known backlog is folded
        into the terminal snapshot with historically correct per-period
        quantities (TP1's 50% was already closed before the boundary),
        TP1 and STOP exist exactly once, the outbox is drained,
        remaining_size=0, the final balance equals the failure-free
        control, and a second restart changes nothing."""
        settings = _settings()
        control = _scenario_a_control(settings)

        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        position_key = pos.position_key
        stop_bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                                  "low": entry * 0.99}}
        original_commit = store.commit_partial_exit

        # (1) TP1 computed -> PostgreSQL write fails -> stays DIRTY.
        store.commit_partial_exit = _always_fail("TP1 persistence failure")
        contour.run_cycle({pos.symbol: entry * 0.945})
        assert position_key in contour.engine.dirty_leg_keys

        # (2) +9 hours: one funding interval is due; the rate arrives in
        #     the very cycle that fires the protective STOP.
        clock.advance(hours=9)
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=stop_bars,
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        assert cycle["pending_closes"] == 0, cycle
        engine = contour.engine

        # (3) the terminal snapshot froze the OBSERVED funding (P2): the
        #     dirty-leg guard deferred the in-cycle write, the close fold
        #     settled it INTO the snapshot instead of losing it.
        row = store.get_position(position_key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 1, (
            "observed due period lost by the dirty-leg terminal close",
        )
        legs = _legs_by_reason(store, position_key)
        assert sorted(legs) == ["STOP_LOSS", "TAKE_PROFIT_1"]
        # historically correct quantity: TP1 closed 50% BEFORE the
        # boundary (executed at t0 < t0+8h), so period 1 charges the
        # REMAINING half only.
        tp1_qty = legs["TAKE_PROFIT_1"].quantity
        expected_qty = round(row.position_size - tp1_qty, 12)
        expected_cost = -(row.entry_price * expected_qty * (0.01 / 100.0))
        assert row.funding_paid == pytest.approx(expected_cost, abs=1e-9), (
            "per-period funding must use the TP history quantity",
        )
        # exactly the control's numbers: no loss, no double-booking.
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        assert engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        ), "balance must equal the failure-free control"
        # outbox drained, exactly-once legs, conservation, no orphans.
        assert store.list_pending_closes() == []
        assert engine.pending_closes == {}
        assert engine._uncommitted_deltas == {}
        assert [leg.leg_seq for leg in store.list_close_legs(position_key)] \
            == [1, 3]
        assert row.remaining_size == 0.0
        _assert_durable_consistent(store, position_key)
        # the owed leg row survived staging even though commit stays down:
        # round 6 (P1) classifies by durable read-back вЂ” a PROVEN leg row
        # fulfils the obligation, so the dirty marker is retired exactly
        # when durability is proven (never earlier, never lost: the row is
        # asserted above as legs [1, 3] and the accounting it carries is
        # inside the CLOSED row вЂ” no double booking follows).
        assert position_key not in engine.dirty_leg_keys
        store.commit_partial_exit = original_commit

        # (4) RESTART: only durable state survives; reconciliation sees
        #     the CLOSED row with the folded funding вЂ” nothing changes.
        restarted = _restart(contour, settings, clock)
        assert restarted.engine.dirty_leg_keys == ()
        assert restarted.engine.durability_risk is False
        balance_before = restarted.engine.balance
        restarted.run_cycle({pos.symbol: entry})
        restarted.run_cycle(
            {pos.symbol: entry},
            funding_rates_percent={pos.symbol: 0.01},
        )
        after = store.get_position(position_key)
        assert after.funding_periods_charged == 1
        assert after.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert len(store.list_close_legs(position_key)) == 2
        assert store.list_pending_closes() == []
        assert restarted.engine.balance == pytest.approx(
            balance_before, abs=1e-12,
        ), "a further cycle must book nothing twice"
        assert restarted.engine.unsettled_funding_periods() == 0
        _assert_ledger_consistent(restarted)
        assert restarted.engine.accounting_consistent is True

        # (5) SECOND restart: PnL, funding and fees are identical.
        again = _restart(restarted, settings, clock)
        again.run_cycle({pos.symbol: entry})
        final = store.get_position(position_key)
        assert final.net_pnl == pytest.approx(row.net_pnl, abs=1e-12)
        assert final.funding_paid == pytest.approx(row.funding_paid, abs=1e-12)
        assert final.funding_periods_charged == 1
        assert again.engine.balance == pytest.approx(
            restarted.engine.balance, abs=1e-12,
        )
        assert [leg.exit_reason
                for leg in store.list_close_legs(position_key)] == [
            "TAKE_PROFIT_1", "STOP_LOSS",
        ]
        _assert_durable_consistent(store, position_key)
        _assert_ledger_consistent(again)

    def test_scenario_b_unknown_rate_close_keeps_obligation_open(self):
        """B (mandatory): the rate feed is DOWN when the STOP closes the
        position over its dirty TP.  No zero is ever fabricated, the
        due-but-unsettled obligation stays recorded and diagnosable on
        the CLOSED row (unsettled periods reported through
        ``unsettled_funding_periods()``/``snapshot()``), and no later
        recovery ever retroactively books it (P2-1 #7) вЂ” so no
        double-count either.

        DOCUMENTED LIMITATION (not hidden by this test): funding still
        unsettled AT a terminal close is NEVER settled after the close вЂ”
        exit facts (net_pnl incl. funding_paid) are frozen by design.
        The safe durable scheme is to settle the backlog BEFORE freezing
        whenever a rate is observed (exactly what the round-5 close fold
        does for known rates); for a genuinely UNKNOWN rate at close the
        obligation stays an explicitly reported gap on the row вЂ” see the
        implementation report, round 5, scenario B."""
        settings = _settings()
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        position_key = pos.position_key
        stop_bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                                  "low": entry * 0.99}}
        original_commit = store.commit_partial_exit
        store.commit_partial_exit = _always_fail("TP1 persistence failure")

        contour.run_cycle({pos.symbol: entry * 0.945})
        clock.advance(hours=9)
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=stop_bars,
            funding_rates_percent={},             # rate UNKNOWN at close
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine

        row = store.get_position(position_key)
        assert row.status == POSITION_STATUS_CLOSED
        # (1) NEVER a fabricated zero: the counter does not advance and
        #     no funding value is written at the close.
        assert row.funding_periods_charged == 0, (
            "an UNKNOWN rate must never book a period (zero or otherwise)",
        )
        assert row.funding_paid == 0.0
        # (2) the obligation stays diagnosable вЂ” durable on the row and
        #     reported through the contract methods.
        assert engine.unsettled_funding_periods() == 1
        assert position_key in engine._funding_unknown
        # (3) the exit itself froze consistently with the open obligation.
        assert sorted(_legs_by_reason(store, position_key)) == [
            "STOP_LOSS", "TAKE_PROFIT_1",
        ]
        assert store.list_pending_closes() == []
        assert engine.pending_closes == {}
        assert row.remaining_size == 0.0
        _assert_durable_consistent(store, position_key)
        store.commit_partial_exit = original_commit

        # (4) later recovery NEVER retroactively books it (P2-1 #7): a
        #     restart with the rate back does not touch the CLOSED row вЂ”
        #     no zero booking, no double-count, obligation still reported.
        restarted = _restart(contour, settings, clock)
        assert restarted.engine.unsettled_funding_periods() == 1, (
            "the obligation must remain reported after a restart",
        )
        restarted.run_cycle(
            {pos.symbol: entry},
            funding_rates_percent={pos.symbol: 0.01},   # rate is BACK
        )
        after = store.get_position(position_key)
        assert after.funding_periods_charged == 0, (
            "a CLOSED position's exit facts are never rewritten",
        )
        assert after.funding_paid == 0.0
        assert restarted.engine.unsettled_funding_periods() == 1
        assert restarted.engine.snapshot()[
            "unsettled_funding_periods"
        ] == 1
        _assert_durable_consistent(store, position_key)

    def test_scenario_c_lost_ack_after_successful_terminal_commit(self):
        """C (mandatory): scenario A plus a LOST ACKNOWLEDGEMENT after
        the terminal COMMIT was applied.  Read-back verification proves
        the facts identical вЂ” folded funding, net PnL, terminal leg вЂ” so
        the state settles as committed: no pending close, no uncommitted
        delta, no duplicated leg/period/balance operation, and the final
        balance equals the failure-free control."""
        settings = _settings()
        control = _scenario_a_control(settings)

        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        position_key = pos.position_key
        stop_bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                                  "low": entry * 0.99}}
        original_commit = store.commit_partial_exit
        original_update = store.update_position

        store.commit_partial_exit = _always_fail("TP1 persistence failure")

        def lost_ack_update(position_row):
            # the COMMIT succeeds inside the store, then the ACK is lost.
            original_update(position_row)
            if position_row.status == POSITION_STATUS_CLOSED:
                raise RuntimeError(
                    "simulated lost ACK after successful COMMIT",
                )
            return None

        store.update_position = lost_ack_update

        contour.run_cycle({pos.symbol: entry * 0.945})
        clock.advance(hours=9)
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=stop_bars,
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine

        # read-back identity: the store carries exactly the snapshot
        # (incl. the folded funding) despite the lost acknowledgement.
        assert engine._confirmed_close(pos) is True
        row = store.get_position(position_key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)

        # no duplicate operations: nothing left to replay and no local
        # delta that could double-count at the next reconcile.
        assert engine.pending_closes == {}
        assert engine._uncommitted_deltas == {}
        assert store.list_pending_closes() == []
        assert [leg.leg_seq for leg in store.list_close_legs(position_key)] \
            == [1, 3]
        assert engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        _assert_durable_consistent(store, position_key)
        store.commit_partial_exit = original_commit
        store.update_position = original_update

        restarted = _restart(contour, settings, clock)
        assert restarted.engine.durability_risk is False
        balance_before = restarted.engine.balance
        restarted.run_cycle(
            {pos.symbol: entry},
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert restarted.engine.balance == pytest.approx(
            balance_before, abs=1e-12,
        )
        assert len(store.list_close_legs(position_key)) == 2
        after = store.get_position(position_key)
        assert after.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert after.funding_periods_charged == 1
        _assert_ledger_consistent(restarted)
        assert restarted.engine.accounting_consistent is True

    def test_scenario_d_store_outage_at_tp_and_staging_recovers_sequentially(
        self,
    ):
        """D (mandatory): the store is unavailable during the partial TP,
        then again during terminal staging.  The close defers with its
        folded funding retained (no UPDATE attempted without staging);
        after the store recovers the unified terminal-close recovery
        replays owed legs + terminal + outbox SEQUENTIALLY in one
        transaction, then the terminal UPDATE вЂ” conservation holds
        (per-position_size == sum of leg quantities), funding equals the
        failure-free control, and a restart changes nothing."""
        settings = _settings()
        control = _scenario_a_control(settings)

        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        position_key = pos.position_key
        stop_bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                                  "low": entry * 0.99}}
        original_commit = store.commit_partial_exit
        original_stage = store.stage_close
        original_update = store.update_position

        store.commit_partial_exit = _always_fail(
            "PostgreSQL unavailable during partial TP",
        )
        update_calls = {"n": 0}

        def spy_update(position_row):
            update_calls["n"] += 1
            return original_update(position_row)

        store.update_position = spy_update

        # (1) partial TP write fails -> DIRTY
        contour.run_cycle({pos.symbol: entry * 0.945})
        assert position_key in contour.engine.dirty_leg_keys

        # (2) +9h, rate observed, protective STOP вЂ” but the store is now
        #     also unavailable for terminal STAGING.
        store.stage_close = _always_fail(
            "PostgreSQL unavailable during terminal staging",
        )
        clock.advance(hours=9)
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=stop_bars,
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine
        # deferred correctly: snapshot + obligations retained, NO update
        # attempted before staging was confirmed.
        assert update_calls["n"] == 0, (
            "terminal UPDATE ran although staging was not confirmed",
        )
        assert position_key in engine.pending_closes
        assert position_key in engine._unstaged_closes
        assert engine.durability_risk is True
        # NOTE: ``_balance_trusted`` may already be True again вЂ” the entry
        # gate re-runs reconcile_balance() (confirmed + retained deltas),
        # which legitimately re-trusts the BALANCE.  The fail-closed state
        # here is the close durability itself: _unstaged_closes above,
        # _uncommitted_deltas below and the refusal logged by open_entries.
        assert position_key in engine._uncommitted_deltas, (
            "the folded funding must stay uncommitted until the write",
        )
        durable = store.get_position(position_key)
        assert durable.status == POSITION_STATUS_OPEN
        assert durable.funding_periods_charged == 0, (
            "nothing may be durable before staging confirms",
        )
        assert store.list_close_legs(position_key) == []
        assert store.list_pending_closes() == []

        # (3) store recovery -> the unified recovery replays everything.
        store.commit_partial_exit = original_commit
        store.stage_close = original_stage
        cycle = contour.run_cycle({pos.symbol: entry})
        assert cycle["pending_closes"] == 0, cycle
        assert update_calls["n"] >= 1

        # sequential replay + conservation after recovery: TP1 leg and
        # terminal leg exactly once each, funding folded, outbox drained.
        row = store.get_position(position_key)
        assert row.status == POSITION_STATUS_CLOSED
        legs = store.list_close_legs(position_key)
        assert [leg.leg_seq for leg in legs] == [1, 3]
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row.remaining_size == 0.0
        _assert_durable_consistent(store, position_key)   # conservation
        assert store.list_pending_closes() == []
        assert engine.pending_closes == {}
        assert engine._uncommitted_deltas == {}
        assert engine.durability_risk is False
        assert engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        _assert_ledger_consistent(contour)

        # (4) restart: nothing changes.
        restarted = _restart(contour, settings, clock)
        balance_before = restarted.engine.balance
        restarted.run_cycle({pos.symbol: entry})
        after = store.get_position(position_key)
        assert after.net_pnl == pytest.approx(row.net_pnl, abs=1e-12)
        assert after.funding_paid == pytest.approx(row.funding_paid, abs=1e-12)
        assert len(store.list_close_legs(position_key)) == 2
        assert restarted.engine.balance == pytest.approx(
            balance_before, abs=1e-12,
        )
        assert restarted.engine.accounting_consistent is True


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
        assert "_funding_backlog" in source, (
            "the per-period settlement must route through the shared "
            "backlog computation (the terminal-close fold uses the exact "
            "same rules)",
        )
        assert "historical quantity cannot be proven" in source
        assert "_dirty_legs" in source, (
            "settlement must be deferred while leg rows are owed",
        )
        # The per-period reconstruction itself lives in the shared helper
        # (identical frozen rules for the in-cycle settlement and the
        # terminal-close fold вЂ” P2, Codex r4552612002).
        helper = inspect.getsource(InverseShortPaperEngine._funding_backlog)
        assert "executed_at < boundary" in helper, (
            "per-period outstanding quantity must be reconstructed at each "
            "period's funding boundary with STRICTLY-before legs only - a "
            "leg executed exactly at the boundary is accounted after that "
            "boundary's accrual (unified funding-before-exit)",
        )
        assert "UNPROVABLE" in helper  # unprovable history is never booked

    def test_close_fold_settles_observed_funding_before_freezing(self):
        """P2 (Codex r4552612002): the terminal close must fold known
        funding into its snapshot instead of freezing a stale counter."""
        fold_source = inspect.getsource(
            InverseShortPaperEngine._settle_funding_for_close,
        )
        assert "_funding_backlog" in fold_source, (
            "the close fold must use the same per-period computation",
        )
        assert "_dirty_legs" in fold_source, (
            "the fold must count the owed dirty legs staged with the "
            "snapshot",
        )
        assert "funding_periods_charged = due_periods" in fold_source, (
            "the counter must advance atomically with the folded accrual",
        )
        assert "return 0.0" in fold_source, (
            "UNKNOWN/unprovable must book NOTHING (obligation stays open)",
        )
        close_source = inspect.getsource(InverseShortPaperEngine._close)
        assert "_settle_funding_for_close" in close_source, (
            "_close must settle observed funding BEFORE freezing the "
            "terminal snapshot",
        )
        assert "funding_folded" in close_source
        exits_source = inspect.getsource(InverseShortPaperEngine.check_exits)
        assert exits_source.count("funding_rate_percent=funding_rate") == 4, (
            "every terminal exit (STOP, defensive stage, TP3, TIMEOUT) "
            "must pass the cycle's funding rate to _close",
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


# ---------------------------------------------------------------------------
# Round 6 (Codex r4555591304) вЂ” P1: an ambiguous terminal COMMIT must never
# be double-counted, must block new entries and must resolve through a
# durable read-back (mandatory scenarios AвЂ“E; PostgreSQL variants live in
# ``test_me_long_v2_inverse_short_oos2_partial_restart_pg_it_v1.py``).
# ---------------------------------------------------------------------------

def _inject_ambiguous_terminal(store, position_key: str, *, applied: bool):
    """Terminal UPDATE loses its ACK (optionally AFTER PostgreSQL applied
    it) and the durable read-back for THIS key is unavailable.

    Returns ``(restore, original_get)`` вЂ” ``restore()`` puts the store back
    and ``original_get`` reads the DURABLE truth through the simulated
    outage (test-side only, never through the engine).
    """
    original_update = store.update_position
    original_get = store.get_position

    def update_losing_ack(position):
        if applied:
            original_update(position)      # PostgreSQL DID apply it
        raise RuntimeError("terminal COMMIT acknowledgement lost")

    def read_back_down(key):
        if key == position_key:
            raise RuntimeError("durable read-back unavailable")
        return original_get(key)

    store.update_position = update_losing_ack
    store.get_position = read_back_down

    def restore():
        store.update_position = original_update
        store.get_position = original_get

    return restore, original_get


def _p1_control(settings: Settings) -> dict:
    """Failure-free reference: entry -> TP1 committed -> protective STOP."""
    contour, store, clock = _contour(settings)
    pos = _open_one(contour, store, clock)
    entry = pos.entry_price
    contour.run_cycle({pos.symbol: entry * 0.945})        # TP1 committed
    stop_bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                              "low": entry * 0.99}}
    cycle = contour.run_cycle({pos.symbol: entry}, bars=stop_bars)
    assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
    row = store.get_position(pos.position_key)
    assert row.status == POSITION_STATUS_CLOSED
    legs = _legs_by_reason(store, pos.position_key)
    assert sorted(legs) == ["STOP_LOSS", "TAKE_PROFIT_1"]
    return {
        "balance": contour.engine.balance,
        "net_pnl": row.net_pnl,
        "symbol": pos.symbol,
        "entry": entry,
    }


class TestRound6P1AmbiguousCommit:
    """Mandatory scenarios AвЂ“E for Codex thread r4555591304."""

    @staticmethod
    def _ambiguous_stop_run(settings: Settings, *, applied: bool):
        """entry -> TP1 committed -> protective STOP whose terminal COMMIT
        outcome is unknown (ACK lost; read-back for the key unavailable)."""
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})    # TP1 committed
        # a fresh candidate waits in the capture backlog the whole time
        assert _capture(store, _candidate("ETHUSDT", entry)) == "CAPTURED"
        restore, original_get = _inject_ambiguous_terminal(
            store, key, applied=applied,
        )
        stop_bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                                  "low": entry * 0.99}}
        cycle = contour.run_cycle({pos.symbol: entry}, bars=stop_bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        return contour, store, clock, pos, restore, original_get

    def test_a_commit_succeeded_ack_lost_readback_failed(self):
        """A: COMMIT applied, ACK lost, read-back FAILED -> no consistency
        claim, entries blocked, engine balance == durable balance (the
        applied COMMIT is NEVER added on top вЂ” Codex repro gap = 0)."""
        settings = _settings()
        control = _p1_control(settings)
        contour, store, clock, pos, restore, original_get = (
            self._ambiguous_stop_run(settings, applied=True)
        )
        engine = contour.engine
        key = pos.position_key

        # unknown outcome -> quarantined; consistency NEVER claimed (rule 2)
        assert engine.ambiguous_write_keys == (key,)
        assert engine.accounting_consistent is False
        assert key in engine.pending_closes
        # the snapshot itself is durable (staged before the UPDATE), so
        # this is an accounting-classification risk, not a memory-only risk
        assert engine.durability_risk is False

        # durable truth through the outage-free reader: the close APPLIED
        durable = original_get(key)
        assert durable.status == POSITION_STATUS_CLOSED
        assert durable.net_pnl == pytest.approx(
            control["net_pnl"], abs=1e-9,
        )

        # rule 5: the applied COMMIT is never counted twice вЂ” the engine
        # balance equals the DURABLE ledger exactly (repro: 6.22 gap = 0)
        assert engine.reconcile_balance() is True
        components = store.balance_components()
        assert engine.confirmed_balance == pytest.approx(
            components["balance"], abs=1e-9,
        )
        assert engine.balance == pytest.approx(
            components["balance"], abs=1e-9,
        ), "the quarantined delta must stay out of the balance"
        assert engine.accounting_consistent is False   # rule 2 again

        # rule 3: new entries refused while the outcome is unknown
        assert engine.open_entries({"ETHUSDT": pos.entry_price}) == []

        # read-back recovers -> CONFIRMED classification -> retired, resumed
        restore()
        contour.run_cycle({pos.symbol: pos.entry_price})
        assert engine.ambiguous_write_keys == ()
        assert engine.pending_closes == {}
        assert engine.accounting_consistent is True
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        # partial + terminal legs exactly once, exactly one terminal close
        legs = store.list_close_legs(key)
        assert [leg.leg_seq for leg in legs] == [1, 3]
        assert sum(1 for leg in legs if leg.is_terminal) == 1
        # entries resume (no permanent fail-closed deadlock)
        opened = engine.open_entries({"ETHUSDT": pos.entry_price})
        assert len(opened) == 1, "entries must resume after classification"

    def test_b_readback_recovers_exact_balance_without_double_count(self):
        """B: after the durable read-back confirms the operation the engine
        restores the CORRECT balance вЂ” identical to the failure-free
        reference, never durable + delta, and idempotent afterwards."""
        settings = _settings()
        control = _p1_control(settings)
        contour, store, clock, pos, restore, _ = (
            self._ambiguous_stop_run(settings, applied=True)
        )
        engine = contour.engine
        # while unknown: balance == durable (never durable + delta)
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        restore()                       # store fully recovered
        contour.run_cycle({pos.symbol: pos.entry_price})
        assert engine.reconcile_balance() is True
        assert engine.ambiguous_write_keys == ()
        assert engine.accounting_consistent is True
        assert engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        ), "recovered balance must equal the failure-free reference"
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        # a further cycle books nothing twice (rule 10 for the close too)
        before = engine.balance
        contour.run_cycle({pos.symbol: pos.entry_price})
        assert engine.balance == pytest.approx(before, abs=1e-12)
        _assert_ledger_consistent(contour)

    def test_c_commit_genuinely_absent_keeps_obligation_and_replays(self):
        """C: the COMMIT never reached PostgreSQL -> read-back PROVES it
        absent -> the delta is safe to add, the close stays a recovery
        obligation with entries blocked, and the replay recovers the exact
        reference result (partial + terminal legs exactly once)."""
        settings = _settings()
        control = _p1_control(settings)
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})    # TP1 committed
        assert _capture(store, _candidate("ETHUSDT", entry)) == "CAPTURED"

        original_update = store.update_position

        def update_rejected(position):
            raise RuntimeError("terminal COMMIT rejected by PostgreSQL")

        store.update_position = update_rejected
        stop_bars = {pos.symbol: {"high": pos.stop_price * 1.001,
                                  "low": entry * 0.99}}
        cycle = contour.run_cycle({pos.symbol: entry}, bars=stop_bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine

        # read-back succeeded -> the outcome is KNOWN (never ambiguous),
        # but the operation is still owed: pending close + proven delta
        assert engine.ambiguous_write_keys == ()
        assert key in engine.pending_closes
        delta = engine._uncommitted_deltas[key]
        assert delta != 0.0
        # durable row still OPEN (only staging вЂ” legs + outbox вЂ” is durable)
        durable = store.get_position(key)
        assert durable.status == POSITION_STATUS_OPEN
        assert [leg.leg_seq for leg in store.list_close_legs(key)] == [1, 3]

        # balance identity: durable ledger + the PROVEN-ABSENT delta вЂ”
        # the close is not in the durable ledger yet, so adding it once is
        # correct (the double-count defect would be durable ALREADY + delta)
        assert engine.reconcile_balance() is True
        components = store.balance_components()
        assert engine.balance == pytest.approx(
            components["balance"] + delta, abs=1e-9,
        )
        # rule 3: entries blocked while the close write is owed
        assert engine.open_entries({"ETHUSDT": entry}) == []

        # heal -> the stage-before-update replay commits exactly once
        store.update_position = original_update
        contour.run_cycle({pos.symbol: entry})
        assert engine.pending_closes == {}
        assert store.list_pending_closes() == []
        assert engine._uncommitted_deltas == {}
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        assert engine.balance == pytest.approx(control["balance"], abs=1e-9)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        assert engine.accounting_consistent is True
        assert [leg.leg_seq for leg in store.list_close_legs(key)] == [1, 3]
        assert sum(
            1 for leg in store.list_close_legs(key) if leg.is_terminal
        ) == 1
        opened = engine.open_entries({"ETHUSDT": entry})
        assert len(opened) == 1

    def test_d_restart_while_ambiguous_keeps_durable_facts_and_blocks(self):
        """D: a restart in the middle of the unknown outcome rebuilds from
        DURABLE facts only; the entry block survives (outbox replay) until
        the durable replay confirms, and the final result equals the
        failure-free reference."""
        settings = _settings()
        control = _p1_control(settings)
        contour, store, clock, pos, restore, _ = (
            self._ambiguous_stop_run(settings, applied=True)
        )
        engine = contour.engine
        key = pos.position_key
        assert engine.ambiguous_write_keys == (key,)

        # restart: only durable state survives (the quarantine is in-memory)
        restarted = _restart(contour, settings, clock)
        r_engine = restarted.engine
        # durable facts: the applied CLOSED row IS the balance (rule 8)
        assert r_engine.reconcile_balance() is True
        assert r_engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        # the close-outbox row (staged before the lost ack) is replayed:
        # pending resumes + entries stay blocked until it confirms
        assert key in r_engine.pending_closes
        assert r_engine.accounting_consistent is False
        assert r_engine.open_entries({"ETHUSDT": pos.entry_price}) == []

        # recovery: heal the store -> the durable replay is idempotent
        restore()
        restarted.run_cycle({pos.symbol: pos.entry_price})
        assert r_engine.pending_closes == {}
        assert store.list_pending_closes() == []
        assert r_engine.accounting_consistent is True
        assert r_engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        assert r_engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        assert [leg.leg_seq for leg in store.list_close_legs(key)] == [1, 3]
        opened = r_engine.open_entries({"ETHUSDT": pos.entry_price})
        assert len(opened) == 1

    def test_e_ten_repeated_reconciliations_are_idempotent(self):
        """E: ten repeated reconciliations change nothing вЂ” in the
        AMBIGUOUS state (the quarantined delta is never added, consistency
        never claimed) and after classification (pure durable read + empty
        buckets == reference)."""
        settings = _settings()
        control = _p1_control(settings)
        contour, store, clock, pos, restore, _ = (
            self._ambiguous_stop_run(settings, applied=True)
        )
        engine = contour.engine

        # phase 1 вЂ” AMBIGUOUS: ten reads, ten identical results
        durable = store.balance_components()["balance"]
        phase1 = []
        for _ in range(10):
            assert engine.reconcile_balance() is True
            phase1.append((
                engine.balance, engine.confirmed_balance,
                engine.accounting_consistent,
            ))
        assert len({round(b, 9) for b, _c, _s in phase1}) == 1
        assert all(
            b == pytest.approx(durable, abs=1e-9) for b, _c, _s in phase1
        ), "the quarantined delta must never be added, ten times in a row"
        assert all(s is False for _b, _c, s in phase1)

        # phase 2 вЂ” classified: ten more, identical, == reference
        restore()
        contour.run_cycle({pos.symbol: pos.entry_price})
        assert engine.ambiguous_write_keys == ()
        phase2 = []
        for _ in range(10):
            assert engine.reconcile_balance() is True
            phase2.append((engine.balance, engine.accounting_consistent))
        assert len({round(b, 9) for b, _s in phase2}) == 1
        assert all(s is True for _b, s in phase2)
        assert phase2[0][0] == pytest.approx(
            control["balance"], abs=1e-9,
        )


# ---------------------------------------------------------------------------
# Round 6 (Codex r4555591322) вЂ” P2-funding: HISTORY_UNAVAILABLE at a
# protective close must persist the OBSERVED rate as a DURABLE obligation
# (never process-local, never an approximate booking), and the recovery
# pass settles it EXACTLY ONCE (mandatory scenarios AвЂ“F; PostgreSQL
# variants in the partial-restart PG IT).
# ---------------------------------------------------------------------------

def _timeout_control(settings: Settings) -> dict:
    """Failure-free reference: entry -> +121h -> TIMEOUT with the rate.

    121h past entry: the frozen 120h deadline has passed and
    floor(121/8) = 15 funding periods are due on the full quantity.
    """
    contour, store, clock = _contour(settings)
    pos = _open_one(contour, store, clock)
    entry = pos.entry_price
    clock.advance(hours=121)
    cycle = contour.run_cycle(
        {pos.symbol: entry},
        funding_rates_percent={pos.symbol: 0.01},
    )
    assert cycle["exit_reasons"] == ["TIMEOUT"], cycle
    row = store.get_position(pos.position_key)
    assert row.status == POSITION_STATUS_CLOSED
    assert row.funding_periods_charged == 15, "control must settle due=15"
    return {
        "balance": contour.engine.balance,
        "funding_paid": row.funding_paid,
        "net_pnl": row.net_pnl,
        "symbol": pos.symbol,
        "entry": entry,
    }


class TestRound6FundingObligationRecovery:
    """Mandatory scenarios AвЂ“F for Codex thread r4555591322."""

    @staticmethod
    def _stop_bars(pos, entry):
        return {pos.symbol: {"high": pos.stop_price * 1.001,
                             "low": entry * 0.99}}

    def test_a_history_unavailable_stop_persists_obligation_then_recovers(
        self,
    ):
        """A (mandatory): TP1 committed -> +9h -> protective STOP with the
        rate observed while the close-leg history is UNREADABLE -> the
        close proceeds with a DURABLE obligation (nothing booked, no
        approximate funding) -> history restored -> the recovery pass
        settles exactly once, converging to the failure-free control."""
        settings = _settings()
        control = _scenario_a_control(settings)
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})    # TP1 committed
        clock.advance(hours=9)
        original_list = store.list_close_legs
        store.list_close_legs = _always_fail(
            "close-leg history unavailable at the close",
        )
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine

        # HISTORY_UNAVAILABLE with a KNOWN rate: close final for
        # protection, funding NOT booked (quantity unproven -> never an
        # approximate value), the rate durable in the obligation table.
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 0, (
            "unprovable quantity must never be charged",
        )
        assert row.funding_paid == 0.0, "no approximate funding ever written"
        ob = store.get_funding_obligation(key)
        assert ob is not None, "the observed rate must be DURABLE"
        assert ob.status == FUNDING_OBLIGATION_OPEN
        assert ob.observed_rate_percent == pytest.approx(0.01)
        assert ob.periods_due == 1
        assert ob.periods_charged_at_observation == 0
        assert key in engine.funding_obligation_keys
        assert engine.snapshot()["funding_obligations"] == 1
        # partial + terminal legs exactly once (staged with the close) вЂ”
        # read through the OUTAGE-FREE original reader (the fault is still
        # installed for the engine's own history reads)
        assert [leg.leg_seq for leg in original_list(key)] == [1, 3]

        # history restored -> the recovery pass settles EXACTLY ONCE
        store.list_close_legs = original_list
        contour.run_cycle({pos.symbol: entry})
        row2 = store.get_position(key)
        assert row2.funding_periods_charged == 1
        tp1_qty = _legs_by_reason(store, key)["TAKE_PROFIT_1"].quantity
        expected_qty = round(row2.position_size - tp1_qty, 12)
        expected_cost = -(
            row2.entry_price * expected_qty * (0.01 / 100.0)
        )
        assert row2.funding_paid == pytest.approx(expected_cost, abs=1e-9), (
            "recovery must charge the historically correct quantity",
        )
        assert row2.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row2.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        ob2 = store.get_funding_obligation(key)
        assert ob2.status == FUNDING_OBLIGATION_SETTLED
        assert engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        ), "recovered balance must equal the failure-free control"
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        assert engine.funding_obligation_keys == ()

        # exactly once: ten further cycles change nothing
        for _ in range(10):
            contour.run_cycle({pos.symbol: entry})
        row3 = store.get_position(key)
        assert row3.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-12,
        )
        assert row3.funding_periods_charged == 1
        assert row3.net_pnl == pytest.approx(control["net_pnl"], abs=1e-12)
        assert engine.balance == pytest.approx(
            control["balance"], abs=1e-12,
        )
        _assert_ledger_consistent(contour)

    def test_b_history_unavailable_timeout_persists_obligation(self):
        """B (mandatory): the same for the protective TIMEOUT close вЂ”
        15 due periods, observed rate durable, recovery settles exactly
        once to the control numbers."""
        settings = _settings()
        control = _timeout_control(settings)
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        clock.advance(hours=121)            # deadline passed, 15 periods due
        original_list = store.list_close_legs
        store.list_close_legs = _always_fail(
            "close-leg history unavailable at TIMEOUT",
        )
        cycle = contour.run_cycle(
            {pos.symbol: entry},
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["TIMEOUT"], cycle
        engine = contour.engine

        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 0
        assert row.funding_paid == 0.0, "never an approximate booking"
        ob = store.get_funding_obligation(key)
        assert ob is not None
        assert ob.status == FUNDING_OBLIGATION_OPEN
        assert ob.observed_rate_percent == pytest.approx(0.01)
        assert ob.periods_due == 15
        assert ob.periods_charged_at_observation == 0

        # history restored -> settled exactly once for the whole window
        store.list_close_legs = original_list
        contour.run_cycle({pos.symbol: entry})
        row2 = store.get_position(key)
        assert row2.funding_periods_charged == 15
        assert row2.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row2.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        assert store.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        assert engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        for _ in range(5):
            contour.run_cycle({pos.symbol: entry})
        row3 = store.get_position(key)
        assert row3.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-12,
        )
        assert row3.funding_periods_charged == 15

    def test_c_restart_before_funding_recovery_keeps_the_obligation(self):
        """C (mandatory): a restart BETWEEN the protective close and the
        funding recovery must not lose the observed rate вЂ” the DURABLE
        obligation is reloaded and settled exactly once after restart."""
        settings = _settings()
        control = _scenario_a_control(settings)
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})    # TP1 committed
        clock.advance(hours=9)
        original_list = store.list_close_legs
        store.list_close_legs = _always_fail("history down at the close")
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        assert store.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_OPEN
        )
        assert store.get_position(key).funding_periods_charged == 0

        # history restored BEFORE the restart вЂ” only durable state matters
        store.list_close_legs = original_list
        restarted = _restart(contour, settings, clock)
        r_engine = restarted.engine
        # the obligation survives the restart (never process-local)
        assert key in r_engine.funding_obligation_keys
        assert r_engine.snapshot()["funding_obligations"] == 1

        restarted.run_cycle({pos.symbol: entry})
        row = store.get_position(key)
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        assert store.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        assert r_engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        assert r_engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )

        # a SECOND restart changes nothing (restart-safe settlement)
        again = _restart(restarted, settings, clock)
        again.run_cycle({pos.symbol: entry})
        row2 = store.get_position(key)
        assert row2.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-12,
        )
        assert row2.funding_periods_charged == 1
        assert again.engine.balance == pytest.approx(
            control["balance"], abs=1e-12,
        )
        assert again.engine.accounting_consistent is True

    def test_d_ambiguous_funding_settlement_never_double_accrues(self):
        """D (mandatory): the obligation COMMIT applied with a lost ACK вЂ”
        the recovery pass must not book it twice; the balance resyncs from
        the durable facts and later cycles are no-ops."""
        settings = _settings()
        control = _scenario_a_control(settings)
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})    # TP1 committed
        clock.advance(hours=9)
        original_list = store.list_close_legs
        store.list_close_legs = _always_fail("history down at the close")
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine
        store.list_close_legs = original_list
        assert store.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_OPEN
        )

        # ambiguous settlement: APPLIED durably, acknowledgement lost
        original_settle = store.settle_funding_obligation

        def settle_losing_ack(*args, **kwargs):
            original_settle(*args, **kwargs)     # PostgreSQL applied it
            raise RuntimeError("settlement acknowledgement lost")

        store.settle_funding_obligation = settle_losing_ack
        contour.run_cycle({pos.symbol: entry})

        row = store.get_position(key)
        assert row.funding_periods_charged == 1, "the COMMIT was applied"
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert store.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        # the engine never drifts from the durable ledger (rule: an
        # ambiguous COMMIT is re-read, never re-booked)
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        assert engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )

        # healed: the recovery pass sees SETTLED and books NOTHING more
        store.settle_funding_obligation = original_settle
        for _ in range(5):
            contour.run_cycle({pos.symbol: entry})
        row2 = store.get_position(key)
        assert row2.funding_periods_charged == 1
        assert row2.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-12,
        )
        assert row2.net_pnl == pytest.approx(control["net_pnl"], abs=1e-12)
        assert engine.balance == pytest.approx(
            control["balance"], abs=1e-12,
        )
        assert store.list_funding_obligations(FUNDING_OBLIGATION_OPEN) == []

    def test_e_unknown_rate_is_a_distinct_state_from_history_unavailable(
        self,
    ):
        """E (mandatory): an UNKNOWN rate books NOTHING and creates NO
        obligation (gap stays on the row, no fabricated zero); a known rate
        with unreadable history creates an obligation with the rate вЂ”
        the two states are never conflated."""
        settings = _settings()

        # --- state 1: UNKNOWN rate (feed down), history readable --------
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})    # TP1 committed
        clock.advance(hours=9)
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            # NO funding rate observed at all
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 0, "never a fabricated zero"
        assert row.funding_paid == 0.0
        assert store.get_funding_obligation(key) is None, (
            "UNKNOWN rate must NOT create an obligation",
        )
        assert store.list_funding_obligations(FUNDING_OBLIGATION_OPEN) == []
        assert engine.snapshot()["funding_obligations"] == 0
        # the obligation itself (the GAP) stays visible on the row
        due = int((row.closed_at - row.entered_at).total_seconds() // 28800)
        assert due == 1 and row.funding_periods_charged == 0

        # --- state 2: HISTORY_UNAVAILABLE with a KNOWN rate -------------
        contour2, store2, clock2 = _contour(settings)
        pos2 = _open_one(contour2, store2, clock2)
        key2 = pos2.position_key
        entry2 = pos2.entry_price
        contour2.run_cycle({pos2.symbol: entry2 * 0.945})
        clock2.advance(hours=9)
        store2.list_close_legs = _always_fail("history unreadable")
        cycle2 = contour2.run_cycle(
            {pos2.symbol: entry2}, bars=self._stop_bars(pos2, entry2),
            funding_rates_percent={pos2.symbol: 0.01},
        )
        assert cycle2["exit_reasons"] == ["STOP_LOSS"], cycle2
        row2 = store2.get_position(key2)
        assert row2.funding_periods_charged == 0
        assert row2.funding_paid == 0.0, "still nothing booked"
        ob = store2.get_funding_obligation(key2)
        assert ob is not None, "KNOWN rate must be durable in an obligation"
        assert ob.status == FUNDING_OBLIGATION_OPEN
        assert ob.observed_rate_percent == pytest.approx(0.01)
        assert contour2.engine.snapshot()["funding_obligations"] == 1

    def test_f_partial_tp_exactly_at_the_funding_boundary(self):
        """F (mandatory): TP1 executes EXACTLY at the funding boundary вЂ”
        funding-before-exit keeps charging period 1 on the FULL original
        quantity, also when the settlement is deferred into a durable
        obligation and recovered later from the leg ledger."""
        settings = _settings()

        # --- control: history readable throughout ------------------------
        c_contour, c_store, c_clock = _contour(settings)
        c_pos = _open_one(c_contour, c_store, c_clock)
        c_entry = c_pos.entry_price
        c_clock.advance(hours=8)               # EXACTLY one boundary
        c_cycle = c_contour.run_cycle(
            {c_pos.symbol: c_entry * 0.945},
            funding_rates_percent={c_pos.symbol: 0.01},
        )
        assert c_pos.tp1_executed is True, c_cycle
        c_clock.advance(hours=1)
        c_cycle2 = c_contour.run_cycle(
            {c_pos.symbol: c_entry},
            bars=self._stop_bars(c_pos, c_entry),
            funding_rates_percent={c_pos.symbol: 0.01},
        )
        assert c_cycle2["exit_reasons"] == ["STOP_LOSS"], c_cycle2
        c_row = c_store.get_position(c_pos.position_key)
        assert c_row.funding_periods_charged == 1
        # boundary rule: the TP1 leg executed AT the boundary is accounted
        # AFTER that boundary's accrual -> FULL original quantity
        expected_qty = c_row.position_size
        expected_cost = -(
            c_row.entry_price * expected_qty * (0.01 / 100.0)
        )
        assert c_row.funding_paid == pytest.approx(expected_cost, abs=1e-9)

        # --- fault run: history unreadable across the boundary ---------
        contour, store, clock = _contour(settings)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        clock.advance(hours=8)
        original_list = store.list_close_legs
        store.list_close_legs = _always_fail("history down at the boundary")
        cycle = contour.run_cycle(
            {pos.symbol: entry * 0.945},
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert pos.tp1_executed is True, cycle
        # in-cycle settlement deferred (history unprovable): NOTHING
        # booked yet, no obligation yet вЂ” the position stays OPEN with the
        # gap reported and the rate re-observed every cycle
        row_open = store.get_position(key)
        assert row_open.status == POSITION_STATUS_OPEN
        assert row_open.funding_periods_charged == 0
        assert store.get_funding_obligation(key) is None

        clock.advance(hours=1)
        cycle2 = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle2["exit_reasons"] == ["STOP_LOSS"], cycle2
        # close with history STILL unreadable -> durable obligation
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 0
        ob = store.get_funding_obligation(key)
        assert ob is not None
        assert ob.status == FUNDING_OBLIGATION_OPEN
        assert ob.periods_due == 1

        # the TP1 leg really executed EXACTLY at the boundary (read via
        # the outage-free original reader вЂ” the fault is still installed)
        tp1_leg = {
            leg.exit_reason: leg for leg in original_list(key)
        }["TAKE_PROFIT_1"]
        assert tp1_leg.executed_at == pos.entered_at + timedelta(hours=8)

        # history restored -> recovery charges the FULL quantity (the leg
        # is not "strictly before" its boundary) == the control numbers
        store.list_close_legs = original_list
        contour.run_cycle({pos.symbol: entry})
        row2 = store.get_position(key)
        assert row2.funding_periods_charged == 1
        assert row2.funding_paid == pytest.approx(expected_cost, abs=1e-9), (
            "a TP exactly on the boundary must not shrink period 1's "
            "quantity (funding-before-exit)",
        )
        assert row2.funding_paid == pytest.approx(
            c_row.funding_paid, abs=1e-12,
        )
        assert row2.net_pnl == pytest.approx(c_row.net_pnl, abs=1e-12)
        assert store.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        assert contour.engine.balance == pytest.approx(
            c_contour.engine.balance, abs=1e-9,
        )
        assert contour.engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        # exactly once afterwards
        for _ in range(5):
            contour.run_cycle({pos.symbol: entry})
        row3 = store.get_position(key)
        assert row3.funding_paid == pytest.approx(
            c_row.funding_paid, abs=1e-12,
        )
        assert row3.funding_periods_charged == 1


# ---------------------------------------------------------------------------
# Round 7 (P1, Codex r4584484945) вЂ” a protective STOP/TIMEOUT whose OBSERVED
# funding rate could not be persisted must be RETAINED as a durable
# PENDING_PROTECTIVE_CLOSE, never dropped and never re-evaluated.
# Mandatory scenarios AвЂ“F.
# ---------------------------------------------------------------------------

class TestRound7P1RetainedProtectiveClose:
    """Mandatory scenarios AвЂ“F for Codex thread r4584484945."""

    @staticmethod
    def _stop_bars(pos, entry):
        return {pos.symbol: {"high": pos.stop_price * 1.001,
                             "low": entry * 0.99}}

    @staticmethod
    def _double_outage(store) -> tuple:
        """History UNREADABLE + obligation WRITE failing (both faults)."""
        original_list = store.list_close_legs
        original_save = store.save_funding_obligation
        store.list_close_legs = _always_fail("close-leg history unreadable")
        store.save_funding_obligation = _always_fail("obligation write failure")
        return original_list, original_save

    def test_a_stop_with_unreadable_history_and_failed_obligation(
        self, tmp_path,
    ):
        """A (mandatory): protective STOP + close-leg history UNREADABLE +
        obligation write FAILING -> the close is RETAINED as a durable
        pending protective close (frozen facts, position out of the exit
        algorithm, terminal UPDATE withheld, observed rate journaled,
        entries refused) instead of being dropped from ``open_positions``.
        """
        settings = _settings()
        control = _scenario_a_control(settings)
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})      # TP1 committed
        clock.advance(hours=9)
        original_list, original_save = self._double_outage(store)
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        engine = contour.engine

        # A1 вЂ” the protective exit FIRED and was reported (old HEAD: []).
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        # A2 вЂ” the position is TREATED AS PROTECTIVELY CLOSED: it left the
        #      exit algorithm, so STOP/TP/TIMEOUT can never re-evaluate it.
        assert key not in engine.open_positions
        # A3 вЂ” and it is RETAINED with the ORIGINAL frozen facts.
        assert key in engine.pending_closes
        snap = engine.pending_closes[key]
        assert snap.exit_reason == "STOP_LOSS"
        assert snap.remaining_size == 0.0
        assert snap.exit_price == pytest.approx(
            pos.stop_price * (1.0 + settings.slippage_percent),
        )
        assert snap.closed_at is not None

        # A4 вЂ” nothing durable is claimed: the terminal UPDATE is withheld
        #      (a CLOSED row must never freeze an unsettled period as 0
        #      while the rate lives outside PostgreSQL).
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_OPEN, "terminal UPDATE withheld"
        assert row.funding_periods_charged == 0
        assert store.get_funding_obligation(key) is None

        # A5 вЂ” the OBSERVED rate + every terminal fact are frozen into the
        #      OOS-2-only local durable JOURNAL (atomic file on disk).
        assert key in engine.pending_protective_close_keys
        files = sorted(p.name for p in tmp_path.glob("*.json"))
        assert files, "the journal record must exist on disk"
        record = engine._journal_records[key]
        assert record.exit_reason == "STOP_LOSS"
        assert record.triggered_at == snap.closed_at.isoformat()
        assert record.reference_price == pytest.approx(pos.stop_price)
        assert record.fill_price == pytest.approx(snap.exit_price)
        # the CLOSED remainder (the terminal leg's quantity), never the
        # already-zero post-close remaining_size
        assert record.quantity > 0.0, "the closed remainder must be frozen"
        assert record.terminal_leg["is_terminal"] is True
        assert record.terminal_leg["quantity"] == pytest.approx(record.quantity)
        assert record.owed_partial_legs == [] or all(
            int(leg["leg_seq"]) in (1, 2) for leg in record.owed_partial_legs
        )
        assert record.obligation is not None
        assert record.obligation["observed_rate_percent"] == pytest.approx(0.01)
        assert record.obligation["periods_due"] == 1
        assert record.obligation["periods_charged_at_observation"] == 0
        assert record.accounting_delta != 0.0
        assert record.recovery_ids["position_key"] == key
        # the on-disk payload is the same record (replay = byte-identical)
        on_disk = json.loads((tmp_path / files[0]).read_text("utf-8"))
        assert on_disk == record.to_dict()

        # A6 вЂ” new entries are refused while the rate is not durable.
        assert engine.open_entries({pos.symbol: entry}) == []
        snap_json = engine.snapshot()
        assert snap_json["pending_obligations"] == 1
        assert snap_json["pending_protective_closes"] == 1
        assert snap_json["pending_obligation_keys"] == [key]

        # A7 вЂ” recovery: the store accepts the retained rate, the close
        #      commits with the FROZEN facts and settles exactly once.
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        contour.run_cycle({pos.symbol: entry * 0.98})
        row2 = store.get_position(key)
        assert row2.status == POSITION_STATUS_CLOSED
        assert row2.exit_reason == "STOP_LOSS"
        assert row2.exit_price == pytest.approx(snap.exit_price)
        assert row2.closed_at == snap.closed_at
        # the frozen journal quantity is exactly the durable terminal leg
        assert _legs_by_reason(store, key)["STOP_LOSS"].quantity == (
            pytest.approx(record.quantity)
        )
        assert row2.funding_periods_charged == 1
        assert row2.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row2.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        ob = store.get_funding_obligation(key)
        assert ob is not None and ob.status == FUNDING_OBLIGATION_SETTLED
        assert engine.pending_closes == {}
        assert engine._pending_obligations == {}
        assert engine.pending_protective_close_keys == ()
        assert engine.journal.paths() == [], "journal record must be retired"
        assert engine.balance == pytest.approx(control["balance"], abs=1e-9)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        # A8 вЂ” entries are unblocked again once everything is durable.
        assert _capture(store, _candidate("ETHUSDT", 50.0, detected_at=clock())) \
            == "CAPTURED"
        assert contour.run_cycle({"ETHUSDT": 50.0})["entries"] == 1
        _assert_ledger_consistent(contour)

    def test_b_price_no_longer_touching_the_stop_never_loses_the_close(
        self, tmp_path,
    ):
        """B (mandatory): next cycle the price NO LONGER touches the STOP
        (old HEAD: the position stayed in ``open_positions`` and the exit
        simply never fired again, losing the close).  The retained close
        must complete with the ORIGINAL frozen trigger facts anyway вЂ” no
        recomputation from fresh inputs, ever."""
        settings = _settings()
        control = _scenario_a_control(settings)
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        clock.advance(hours=9)
        original_list, original_save = self._double_outage(store)
        cycle1 = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle1["exit_reasons"] == ["STOP_LOSS"]
        frozen = dict(contour.engine.pending_closes[key].to_dict())
        frozen_record = contour.engine._journal_records[key].to_dict()

        # --- cycle 2: price well BELOW the stop (would never trigger), no
        # bars, no funding rate вЂ” and the store is healthy again.
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        cycle2 = contour.run_cycle({pos.symbol: entry * 0.98})
        assert cycle2["exit_reasons"] == [], (
            "the retained position must not be re-evaluated at all",
        )
        assert key not in contour.engine.open_positions

        # the COMMITTED close carries the ORIGINAL trigger, byte for byte
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert row.exit_price == pytest.approx(frozen["exit_price"])
        assert row.closed_at.isoformat() == frozen["closed_at"]
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        assert contour.engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        # the frozen record was never mutated by the fresh cycle inputs
        assert frozen_record["exit_reason"] == "STOP_LOSS"
        assert frozen_record["obligation"]["observed_rate_percent"] == (
            pytest.approx(0.01)
        )
        assert contour.engine.journal.paths() == []
        _assert_ledger_consistent(contour)

    def test_c_observed_rate_survives_a_cycle_without_a_rate(self, tmp_path):
        """C (mandatory): the funding rate DISAPPEARS next cycle (no rate
        delivered, history still unreadable) вЂ” the rate observed at the
        STOP is the one that is eventually charged, never re-derived from
        a fresh (absent) input and never dropped."""
        settings = _settings()
        control = _scenario_a_control(settings)
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        clock.advance(hours=9)
        original_list, original_save = self._double_outage(store)
        cycle1 = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle1["exit_reasons"] == ["STOP_LOSS"]
        engine = contour.engine
        record = engine._journal_records[key]
        assert record.obligation["observed_rate_percent"] == pytest.approx(0.01)
        # deep snapshot (JSON round-trip) taken BEFORE the rate-less cycle
        frozen = json.loads(json.dumps(record.to_dict()))

        # cycle 2: history still unreadable, obligation STILL failing, and
        # NO funding rate is delivered at all.
        cycle2 = contour.run_cycle({pos.symbol: entry * 0.98})
        assert cycle2["exit_reasons"] == []
        assert key in engine.pending_closes, "the hold must persist"
        assert key in engine._pending_obligations
        assert store.get_funding_obligation(key) is None
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        # the retained observation is byte-identical (never re-derived from
        # the absent cycle-2 rate)
        assert json.loads(json.dumps(engine._journal_records[key].to_dict())) \
            == frozen
        assert engine.open_entries({pos.symbol: entry}) == []

        # cycle 3: everything restored -> the OBSERVED rate is persisted and
        # charged for exactly one period at 0.01%.
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        contour.run_cycle({pos.symbol: entry})
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        ob = store.get_funding_obligation(key)
        assert ob.observed_rate_percent == pytest.approx(0.01)
        assert ob.status == FUNDING_OBLIGATION_SETTLED
        assert engine.balance == pytest.approx(control["balance"], abs=1e-9)
        assert engine.journal.paths() == []
        _assert_ledger_consistent(contour)

    def test_d_restart_before_postgresql_recovery(self, tmp_path):
        """D (mandatory): RESTART while PostgreSQL still holds NOTHING of
        the close (row OPEN, no obligation).  The local journal must replay
        the frozen terminal facts AND the observed rate вЂ” both when the
        outbox half survived and when staging failed too (journal-only)."""
        settings = _settings()
        control = _scenario_a_control(settings)

        # --- variant 1: staging SUCCEEDED (outbox row exists) ----------
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        clock.advance(hours=9)
        original_list, original_save = self._double_outage(store)
        cycle1 = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle1["exit_reasons"] == ["STOP_LOSS"]
        assert [p.position_key for p in store.list_pending_closes()] == [key]

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        assert key in engine.pending_closes, "outbox half must resume"
        assert key in engine._pending_obligations, (
            "the observed rate must be replayed from the journal",
        )
        assert key not in engine.open_positions
        assert engine.open_entries({pos.symbol: entry}) == []
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        restarted.run_cycle({pos.symbol: entry * 0.98})
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert restarted.engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        assert restarted.engine.journal.paths() == []

        # --- variant 2: staging FAILED too -> the journal is the ONLY copy
        settings2 = _settings()
        contour2, store2, clock2 = _contour(settings2, journal_dir=tmp_path / "s")
        pos2 = _open_one(contour2, store2, clock2)
        key2 = pos2.position_key
        entry2 = pos2.entry_price
        contour2.run_cycle({pos2.symbol: entry2 * 0.945})
        clock2.advance(hours=9)
        ol2, os2 = self._double_outage(store2)
        original_stage = store2.stage_close
        store2.stage_close = _always_fail("outbox staging outage")
        cycle = contour2.run_cycle(
            {pos2.symbol: entry2}, bars=self._stop_bars(pos2, entry2),
            funding_rates_percent={pos2.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        assert store2.list_pending_closes() == [], "nothing staged at all"
        assert key2 in contour2.engine.pending_closes
        assert key2 in contour2.engine._unstaged_closes

        restarted2 = _restart(contour2, settings2, clock2, journal_dir=tmp_path / "s")
        engine2 = restarted2.engine
        assert key2 in engine2.pending_closes, "journal-only restore"
        assert key2 in engine2._unstaged_closes, "the outbox half must be owed"
        assert key2 in engine2._pending_obligations
        assert key2 not in engine2.open_positions
        store2.stage_close = original_stage
        store2.list_close_legs = ol2
        store2.save_funding_obligation = os2
        restarted2.run_cycle({pos2.symbol: entry2 * 0.98})
        row2 = store2.get_position(key2)
        assert row2.status == POSITION_STATUS_CLOSED
        assert row2.exit_reason == "STOP_LOSS"
        assert row2.funding_periods_charged == 1
        assert row2.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert restarted2.engine.journal.paths() == []

    def test_e_duplicate_delivery_and_ambiguous_commit_book_once(
        self, tmp_path,
    ):
        """E (mandatory): DUPLICATE delivery of the same terminal staging
        plus an AMBIGUOUS terminal COMMIT (write applied, acknowledgement
        lost) must book the close and the funding EXACTLY once, and a
        later duplicate of the superseded snapshot must never be resumed."""
        settings = _settings()
        control = _scenario_a_control(settings)
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        clock.advance(hours=9)
        original_list, original_save = self._double_outage(store)
        cycle1 = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle1["exit_reasons"] == ["STOP_LOSS"]
        retained = contour.engine.pending_closes[key]   # PRE-settlement facts

        # store healthy again, but the terminal UPDATE loses its ack AFTER
        # the write was applied.
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        real_update = store.update_position

        def _lost_ack(position):
            real_update(position)
            raise RuntimeError("terminal UPDATE acknowledgement lost")

        store.update_position = _lost_ack
        contour.run_cycle({pos.symbol: entry * 0.98})
        store.update_position = real_update

        engine = contour.engine
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 1, "AMBIGUOUS COMMIT confirmed"
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        assert engine.pending_closes == {}
        assert engine._uncommitted_deltas == {}
        assert engine._ambiguous_keys == set()
        assert engine.balance == pytest.approx(control["balance"], abs=1e-9)
        assert engine.journal.paths() == []

        # ten further cycles: exactly once, never re-booked
        for _ in range(10):
            contour.run_cycle({pos.symbol: entry * 0.98})
        row_b = store.get_position(key)
        assert row_b.funding_periods_charged == 1
        assert row_b.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-12,
        )
        assert row_b.net_pnl == pytest.approx(control["net_pnl"], abs=1e-12)
        assert contour.engine.balance == pytest.approx(
            control["balance"], abs=1e-12,
        )

        # DUPLICATE DELIVERY: the ORIGINAL (pre-settlement) staging arrives
        # again after everything already committed.
        store.stage_close(retained, None)
        assert [p.position_key for p in store.list_pending_closes()] == [key]
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        assert restarted.engine.pending_closes == {}, (
            "a superseded duplicate must never be resumed",
        )
        assert store.list_pending_closes() == [], (
            "the superseded duplicate must be retired",
        )
        row_c = store.get_position(key)
        assert row_c.funding_periods_charged == 1
        assert row_c.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-12,
        )
        assert restarted.engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        # one healthy cycle resyncs the ledger after the supersession retire
        restarted.run_cycle({pos.symbol: entry * 0.98})
        assert restarted.engine.accounting_consistent is True
        assert restarted.engine.durability_risk is False
        assert restarted.engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        assert restarted.engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )

    def test_f_timeout_variant_retains_the_protective_close(self, tmp_path):
        """F (mandatory): the protective TIMEOUT variant of the same defect
        вЂ” 15 due periods, both store faults, retained then recovered to the
        failure-free control numbers."""
        settings = _settings()
        control = _timeout_control(settings)
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        clock.advance(hours=121)
        original_list, original_save = self._double_outage(store)
        cycle1 = contour.run_cycle(
            {pos.symbol: entry}, funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle1["exit_reasons"] == ["TIMEOUT"], cycle1
        engine = contour.engine
        assert key not in engine.open_positions
        assert key in engine.pending_closes
        assert engine.pending_closes[key].exit_reason == "TIMEOUT"
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        assert store.get_funding_obligation(key) is None
        record = engine._journal_records[key]
        assert record.exit_reason == "TIMEOUT"
        assert record.obligation["periods_due"] == 15
        assert engine.open_entries({pos.symbol: entry}) == []

        # next cycle: price normal, NO rate delivered, store still broken вЂ”
        # the TIMEOUT close is never re-evaluated or lost.
        cycle2 = contour.run_cycle({pos.symbol: entry})
        assert cycle2["exit_reasons"] == []
        assert key in engine.pending_closes

        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        contour.run_cycle({pos.symbol: entry})
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "TIMEOUT"
        assert row.funding_periods_charged == 15
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        assert store.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        assert engine.balance == pytest.approx(control["balance"], abs=1e-9)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        assert engine.journal.paths() == []
        assert engine.pending_protective_close_keys == ()
        for _ in range(5):
            contour.run_cycle({pos.symbol: entry})
        row2 = store.get_position(key)
        assert row2.funding_periods_charged == 15
        assert row2.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-12,
        )
        _assert_ledger_consistent(contour)


# ---------------------------------------------------------------------------
# Round 7 (P2, Codex r4584484952) вЂ” a funding settlement must never leave a
# stale close outbox behind.  Mandatory scenarios AвЂ“F.
# ---------------------------------------------------------------------------

class TestRound7P2StaleCloseOutbox:
    """Mandatory scenarios AвЂ“F for Codex thread r4584484952."""

    @staticmethod
    def _stop_bars(pos, entry):
        return {pos.symbol: {"high": pos.stop_price * 1.001,
                             "low": entry * 0.99}}

    @staticmethod
    def _settled_with_stale_outbox(settings, journal_dir):
        """STOP close whose ``clear_pending_close()`` FAILED, then settled.

        Timeline (the defect): terminal staging + CLOSED update succeed ->
        the outbox clear raises -> the pre-settlement snapshot survives ->
        funding recovery advances the CLOSED row -> the snapshot is now
        STALE financial state that a restart must never replay.
        """
        control = _scenario_a_control(settings)
        contour, store, clock = _contour(settings, journal_dir=journal_dir)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})       # TP1 committed
        clock.advance(hours=9)

        original_list = store.list_close_legs
        original_clear = store.clear_pending_close
        store.list_close_legs = _always_fail("close-leg history unreadable")
        store.clear_pending_close = _always_fail("clear_pending_close outage")
        cycle = contour.run_cycle(
            {pos.symbol: entry},
            bars=TestRound7P2StaleCloseOutbox._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        store.list_close_legs = original_list

        # the CLOSED row committed but the clear FAILED -> stale snapshot
        row_closed = store.get_position(key)
        assert row_closed.status == POSITION_STATUS_CLOSED
        assert row_closed.funding_periods_charged == 0
        stale_rows = store.list_pending_closes()
        assert [p.position_key for p in stale_rows] == [key], (
            "clear_pending_close failed -> the snapshot must still be staged",
        )
        assert stale_rows[0].funding_periods_charged == 0

        # funding recovery advances the CLOSED row
        contour.run_cycle({pos.symbol: entry})
        row_settled = store.get_position(key)
        assert row_settled.funding_periods_charged == 1
        assert row_settled.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        store.clear_pending_close = original_clear
        return {
            "control": control, "contour": contour, "store": store,
            "clock": clock, "pos": pos, "key": key, "entry": entry,
            "stale": stale_rows[0], "row_settled": row_settled,
        }

    def test_a_settlement_retires_the_stale_outbox_atomically(self, tmp_path):
        """A (mandatory): terminal staging + CLOSED update succeed,
        ``clear_pending_close()`` fails, funding recovery then updates the
        CLOSED row вЂ” the superseded snapshot must be retired TOGETHER with
        that settlement (one transaction), and a restart must neither
        resume it nor replay it over the settled funding.  Entries must
        recover (never a permanent pending close)."""
        ctx = self._settled_with_stale_outbox(_settings(), tmp_path)
        store, key, control = ctx["store"], ctx["key"], ctx["control"]

        # THE FIX: the settlement retired the superseded snapshot in the
        # same step вЂ” a stale outbox can never be left behind.
        assert store.list_pending_closes() == [], (
            "funding settlement must retire the stale close outbox atomically",
        )

        restarted = _restart(ctx["contour"], _settings(), ctx["clock"],
                             journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.pending_closes == {}, (
            "a stale snapshot must never be resumed after a restart",
        )
        assert store.list_pending_closes() == []
        row = store.get_position(key)
        assert row.funding_periods_charged == 1, "settled funding must survive"
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        assert engine.balance == pytest.approx(control["balance"], abs=1e-9)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        assert engine.durability_risk is False

        # entries are NOT blocked forever by a pending close
        restarted.run_cycle({ctx["pos"].symbol: ctx["entry"] * 0.98})
        assert engine.accounting_consistent is True
        assert engine.pending_closes == {}
        assert _capture(store, _candidate("ETHUSDT", 50.0,
                                          detected_at=ctx["clock"]())) \
            == "CAPTURED"
        assert restarted.run_cycle({"ETHUSDT": 50.0})["entries"] == 1

    def test_b_stale_snapshot_can_never_overwrite_newer_confirmed_state(
        self, tmp_path,
    ):
        """B (mandatory): the stale snapshot must NEVER overwrite the newer
        confirmed state вЂ” neither through the engine (supersession is
        recognised and the snapshot retired) nor through a direct store
        write (the in-memory store now enforces the same monotone rule as
        the migration 089 PostgreSQL guard)."""
        ctx = self._settled_with_stale_outbox(_settings(), tmp_path)
        store, key, control = ctx["store"], ctx["key"], ctx["control"]
        stale = ctx["stale"]
        assert stale.funding_periods_charged == 0

        # (i) a DIRECT write of the stale snapshot is rejected вЂ” this is
        #     exactly what PostgreSQL's migration 089 guard already did and
        #     what the in-memory store used to accept silently.
        with pytest.raises(RuntimeError,
                           match="CLOSED_FUNDING_PERIODS_MAY_ONLY_ADVANCE"):
            store.update_position(stale)
        row = store.get_position(key)
        assert row.funding_periods_charged == 1, "settled state untouched"
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )

        # (ii) even if the snapshot is staged again, the engine recognises
        #      the supersession and retires it instead of replaying it.
        store.stage_close(stale, None)
        assert [p.position_key for p in store.list_pending_closes()] == [key]
        restarted = _restart(ctx["contour"], _settings(), ctx["clock"],
                             journal_dir=tmp_path)
        assert restarted.engine.pending_closes == {}
        assert store.list_pending_closes() == []
        row2 = store.get_position(key)
        assert row2.funding_periods_charged == 1
        assert row2.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert restarted.engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )

    def test_c_snapshot_kept_while_terminal_facts_are_unconfirmed(
        self, tmp_path,
    ):
        """C (mandatory): a superseded snapshot is NEVER silently deleted
        while unconfirmed terminal facts remain вЂ” the row must be CLOSED
        with IDENTICAL terminal facts (only the funding columns may have
        advanced)."""
        # (i) row still OPEN (retained protective close): never retired.
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})
        clock.advance(hours=9)
        original_list = store.list_close_legs
        original_save = store.save_funding_obligation
        store.list_close_legs = _always_fail("close-leg history unreadable")
        store.save_funding_obligation = _always_fail("obligation write failure")
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        engine = contour.engine
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        open_snap = engine.pending_closes[key]
        assert engine._retire_superseded_close(open_snap, source="test") is False
        assert [p.position_key for p in store.list_pending_closes()] == [key], (
            "an unconfirmed close must keep its staged snapshot",
        )
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        contour.run_cycle({pos.symbol: entry * 0.98})
        assert store.get_position(key).status == POSITION_STATUS_CLOSED

        # (ii) row CLOSED but the snapshot describes a DIFFERENT close
        #      (different exit facts) -> never mistaken for a stale twin.
        ctx = self._settled_with_stale_outbox(_settings(), tmp_path / "b")
        store2, key2 = ctx["store"], ctx["key"]
        engine2 = ctx["contour"].engine
        snap2 = ctx["stale"]
        other_exit = dataclasses.replace(snap2, exit_price=snap2.exit_price * 3.0)
        assert engine2._retire_superseded_close(other_exit, source="test") is False
        other_reason = dataclasses.replace(snap2, exit_reason="TIMEOUT")
        assert engine2._retire_superseded_close(other_reason,
                                                source="test") is False
        # (iii) the durable row is BEHIND the snapshot -> not supersession
        ahead = dataclasses.replace(snap2, funding_periods_charged=99)
        assert engine2._retire_superseded_close(ahead, source="test") is False
        # only the genuinely superseded twin is retired
        assert engine2._retire_superseded_close(snap2, source="test") is True

    def test_d_clear_is_idempotent_and_recovery_is_a_noop(self, tmp_path):
        """D (mandatory): ``clear_pending_close()`` is idempotent and a
        repeated recovery (many restarts + many cycles) is a pure no-op."""
        ctx = self._settled_with_stale_outbox(_settings(), tmp_path)
        store, key, control = ctx["store"], ctx["key"], ctx["control"]

        # idempotent: clearing an absent key is a no-op, never an error
        for _ in range(5):
            store.clear_pending_close(key)
        assert store.list_pending_closes() == []

        baseline = store.get_position(key)
        # repeated restarts + cycles change NOTHING
        for _ in range(3):
            restarted = _restart(ctx["contour"], _settings(), ctx["clock"],
                                 journal_dir=tmp_path)
            restarted.run_cycle({ctx["pos"].symbol: ctx["entry"] * 0.98})
            row = store.get_position(key)
            assert row.funding_periods_charged == \
                baseline.funding_periods_charged == 1
            assert row.funding_paid == pytest.approx(
                control["funding_paid"], abs=1e-12,
            )
            assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-12)
            assert restarted.engine.balance == pytest.approx(
                control["balance"], abs=1e-12,
            )
            assert restarted.engine.pending_closes == {}
            assert store.list_pending_closes() == []
            assert restarted.engine.accounting_consistent is True
            assert restarted.engine.durability_risk is False
        _assert_ledger_consistent(restarted)

    def test_e_migration_089_guards_are_not_weakened(self):
        """E (mandatory): the migration 089 closed-row guards are byte-for
        byte intact вЂ” nothing was relaxed to make the new recovery pass."""
        from app.paper.me_long_v2_inverse_short_oos2_v1 import (  # noqa: PLC0415
            store as store_module,
        )

        sql = store_module.SETTLE_POSITION_FUNDING_SQL
        assert "status = 'CLOSED'" in sql, "the settlement must target CLOSED"
        assert "funding_periods < %s" in sql, (
            "the monotone funding_periods guard must stay in place",
        )
        migration = (
            Path(__file__).resolve().parents[1] / "sql" / "migrations"
            / "089_me_long_v2_inverse_short_oos2_v1_funding_obligation.sql"
        ).read_text(encoding="utf-8")
        assert "funding_periods may only advance on a closed position" in migration
        assert "closed position is terminal" in migration
        assert "RAISE EXCEPTION" in migration

        # the engine still refuses to book an unprovable quantity
        engine_src = inspect.getsource(InverseShortPaperEngine._close)
        assert "funding_folded" in engine_src
        assert "_settle_funding_for_close" in engine_src
        exits_src = inspect.getsource(InverseShortPaperEngine.check_exits)
        assert exits_src.count("funding_rate_percent=funding_rate") == 4

    def test_f_no_regression_in_funding_periods_or_funding_paid(
        self, tmp_path,
    ):
        """F (mandatory): after the whole recovery the contour must match
        the failure-free control EXACTLY вЂ” never fewer
        ``funding_periods_charged``, never a lost ``funding_paid``."""
        settings = _settings()
        ctx = self._settled_with_stale_outbox(settings, tmp_path)
        store, key, control = ctx["store"], ctx["key"], ctx["control"]
        assert store.list_pending_closes() == []

        restarted = _restart(ctx["contour"], settings, ctx["clock"],
                             journal_dir=tmp_path)
        for _ in range(12):
            restarted.run_cycle({ctx["pos"].symbol: ctx["entry"] * 0.98})
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == control["funding_periods_charged"]
        assert row.funding_periods_charged == 1, "never fewer periods"
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        ), "funding_paid must never be lost"
        assert row.funding_paid != 0.0, "the settled funding must persist"
        assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
        assert restarted.engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        assert restarted.engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        assert restarted.engine.pending_closes == {}
        assert restarted.engine.accounting_consistent is True
        assert restarted.engine.durability_risk is False
        _assert_ledger_consistent(restarted)

    def test_g_identical_snapshot_is_never_retired(self, tmp_path):
        """G (extra, regression guard): a snapshot that still says exactly
        what the durable CLOSED row says is NOT superseded вЂ” it must keep
        replaying through the normal path.  That is the round-6 durable
        replay contract (pending close resumes, entries blocked, one clean
        flush) and the supersession guard must never swallow it."""
        settings = _settings()
        control = _scenario_a_control(settings)
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        key = pos.position_key
        entry = pos.entry_price
        contour.run_cycle({pos.symbol: entry * 0.945})        # TP1 committed
        clock.advance(hours=9)

        original_clear = store.clear_pending_close
        store.clear_pending_close = _always_fail("clear_pending_close outage")
        cycle = contour.run_cycle(
            {pos.symbol: entry},
            bars=self._stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        store.clear_pending_close = original_clear

        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 1
        staged = store.list_pending_closes()
        assert [p.position_key for p in staged] == [key], (
            "the failed clear must leave the snapshot staged",
        )

        engine = contour.engine
        assert engine._retire_superseded_close(staged[0], source="test") is False, (
            "an identical snapshot is NOT superseded вЂ” it must replay",
        )
        assert [p.position_key for p in store.list_pending_closes()] == [key], (
            "a non-superseded snapshot must never be silently deleted",
        )

        # the durable replay resumes exactly like round 6 required
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine2 = restarted.engine
        assert key in engine2.pending_closes, "the durable replay must resume"
        assert engine2.accounting_consistent is False
        restarted.run_cycle({pos.symbol: entry})
        assert engine2.pending_closes == {}
        assert store.list_pending_closes() == []
        assert engine2.accounting_consistent is True
        assert engine2.balance == pytest.approx(control["balance"], abs=1e-9)
        assert engine2.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        assert engine2.durability_risk is False
        _assert_ledger_consistent(restarted)


# ---------------------------------------------------------------------------
# Round 8 вЂ” Codex r4587604965 (failed recovery reads), r4587604973
# (journal before stage) and r4587604985 (journal directory fsync)
# ---------------------------------------------------------------------------

class SimulatedCrash(BaseException):
    """Process-kill model used by the crash-consistency scenarios.

    ``BaseException`` on purpose: every degradation path in the engine is
    ``except Exception``, so an ordinary Python exception would be swallowed
    and turned into a graceful retry вЂ” that is NOT a crash.  Raising this
    aborts the cycle at the exact injected instruction *after* that
    instruction's durable effect has been applied, and the test then throws
    the engine away and builds a fresh one over the SAME durable store вЂ”
    the verifiable equivalent of killing the process at that point.
    """


def _double_outage(store) -> tuple:
    """History UNREADABLE + obligation WRITE failing (both faults)."""
    original_list = store.list_close_legs
    original_save = store.save_funding_obligation
    store.list_close_legs = _always_fail("close-leg history unreadable")
    store.save_funding_obligation = _always_fail("obligation write failure")
    return original_list, original_save


def _kill_before_journal_write(journal) -> None:
    """Die before the recovery record reaches the disk."""
    original = journal.write

    def die(record):
        journal.write = original
        raise SimulatedCrash("killed before the journal write")

    journal.write = die


def _kill_before_stage(store) -> None:
    """Die after the recovery record is durable, before staging starts."""
    original = store.stage_close

    def die(*args, **kwargs):
        store.stage_close = original
        raise SimulatedCrash("killed before stage_close")

    store.stage_close = die


def _kill_after_stage(store) -> None:
    """Die immediately after the staging transaction COMMITTED."""
    original = store.stage_close

    def stage_then_die(position, terminal_leg=None, *, partial_legs=None):
        original(position, terminal_leg, partial_legs=partial_legs)
        store.stage_close = original
        raise SimulatedCrash("killed right after stage_close COMMIT")

    store.stage_close = stage_then_die


def _kill_after_closed_update(store) -> None:
    """Die immediately after the terminal CLOSED UPDATE applied (lost ack)."""
    original = store.update_position

    def update_then_die(position):
        result = original(position)
        if getattr(position, "status", None) == POSITION_STATUS_CLOSED:
            store.update_position = original
            raise SimulatedCrash("killed after the CLOSED UPDATE")
        return result

    store.update_position = update_then_die


def _kill_before_outbox_clear(store) -> None:
    """Die after the CLOSED UPDATE, before the outbox row is retired."""
    original = store.clear_pending_close

    def die(key):
        store.clear_pending_close = original
        raise SimulatedCrash("killed before clear_pending_close")

    store.clear_pending_close = die


def _kill_before_journal_delete(journal) -> None:
    """Die before the journal record is retired."""
    original = journal.delete

    def die(key):
        journal.delete = original
        raise SimulatedCrash("killed before the journal retire")

    journal.delete = die


def _lost_ack_then_crash(store, journal) -> None:
    """Stage COMMITS, the acknowledgement is lost, then the process dies.

    The engine sees ``stage_close`` fail (``except Exception``) and holds
    the close вЂ” yet the outbox and leg rows are already durable.  The
    process then dies before any retry, at the post-stage journal refresh.
    """
    original_stage = store.stage_close
    original_write = journal.write
    state = {"staged": False, "writes": 0}

    def stage_losing_ack(position, terminal_leg=None, *, partial_legs=None):
        original_stage(position, terminal_leg, partial_legs=partial_legs)
        state["staged"] = True
        raise RuntimeError("staging acknowledgement lost")

    def write_then_die(record):
        state["writes"] += 1
        if state["staged"] and state["writes"] >= 2:
            journal.write = original_write
            raise SimulatedCrash("killed after the lost staging ack")
        return original_write(record)

    store.stage_close = stage_losing_ack
    journal.write = write_then_die


def _journal_record(key: str) -> PendingProtectiveClose:
    """A self-contained journal payload used by the fsync scenarios."""
    return PendingProtectiveClose(
        position_key=key,
        exit_reason="STOP_LOSS",
        triggered_at=NOW.isoformat(),
        reference_price=100.0,
        fill_price=100.05,
        quantity=0.5,
        snapshot={"position_key": key, "status": "CLOSED"},
        terminal_leg={"position_key": key, "leg_seq": 3, "is_terminal": True},
        owed_partial_legs=[],
        obligation={
            "obligation_key": f"{key}:funding",
            "position_key": key,
            "symbol": "ETHUSDT",
            "observed_rate_percent": 0.01,
            "periods_due": 1,
            "periods_charged_at_observation": 0,
            "legs_ref_max_seq": 2,
            "state": FUNDING_OBLIGATION_OPEN,
        },
        accounting_delta=1.25,
        recovery_ids={"position_key": key},
        written_at=NOW.isoformat(),
        staged=False,
    )


def _round8_stop_setup(settings, tmp_path):
    """entry -> TP1 committed -> +9h, ready to fire the protective STOP."""
    control = _scenario_a_control(settings)
    contour, store, clock = _contour(settings, journal_dir=tmp_path)
    pos = _open_one(contour, store, clock)
    entry = pos.entry_price
    contour.run_cycle({pos.symbol: entry * 0.945})        # TP1 committed
    clock.advance(hours=9)
    return contour, store, clock, pos, pos.position_key, entry, control


def _round8_stop_bars(pos, entry):
    return {pos.symbol: {"high": pos.stop_price * 1.001, "low": entry * 0.99}}


def _assert_round8_recovered(
    restarted, store, key, pos, entry, control, *,
    expect_journal_retired: bool = True,
    expect_obligation: bool = True,
) -> None:
    """The financial invariants every Round 8 scenario must satisfy."""
    engine = restarted.engine
    # The STOP bars are passed so a scenario whose close was never staged
    # (crash before the journal write) re-executes the ORIGINAL exit from
    # the still-OPEN row вЂ” a retained close never sees them because the
    # position has already left the exit algorithm.
    restarted.run_cycle(
        {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        funding_rates_percent={pos.symbol: 0.01},
    )
    row = store.get_position(key)
    assert row.status == POSITION_STATUS_CLOSED, "the close must complete"
    assert row.exit_reason == "STOP_LOSS", row.exit_reason
    assert row.closed_at is not None
    # frozen trigger: STOP_LOSS at the ORIGINAL stop price, never recomputed
    assert row.exit_price == pytest.approx(
        pos.stop_price * (1.0 + restarted.settings.slippage_percent),
    )
    # observed funding preserved, never zeroed
    assert row.funding_periods_charged == 1, row.funding_periods_charged
    assert row.funding_paid == pytest.approx(control["funding_paid"], abs=1e-9)
    assert row.net_pnl == pytest.approx(control["net_pnl"], abs=1e-9)
    ob = store.get_funding_obligation(key)
    if expect_obligation:
        assert ob is not None and ob.status == FUNDING_OBLIGATION_SETTLED, ob
    else:
        # No obligation row is ever acceptable ONLY when the crash happened
        # before anything durable existed: the fresh exit re-reads the same
        # observed rate, settles the period and needs no obligation.  What
        # must never happen is a row left OPEN.
        assert ob is None or ob.status == FUNDING_OBLIGATION_SETTLED, ob
    # exactly one terminal leg, no orphan partials, no duplicates
    legs = store.list_close_legs(key)
    assert len([leg for leg in legs if leg.is_terminal]) == 1, legs
    assert sorted(leg.exit_reason for leg in legs) == [
        "STOP_LOSS", "TAKE_PROFIT_1",
    ], legs
    # clean recovery state
    assert engine.pending_closes == {}, engine.pending_closes
    assert store.list_pending_closes() == []
    assert engine._pending_obligations == {}
    if expect_journal_retired:
        assert not engine.journal.exists(key), "journal must be retired"
    assert engine.balance == pytest.approx(control["balance"], abs=1e-9)
    assert engine.balance == pytest.approx(
        store.balance_components()["balance"], abs=1e-9,
    )
    assert engine.durability_risk is False, engine.durability_risk
    _assert_ledger_consistent(restarted)


class TestRound8P1aFailedRecoveryRead:
    """Mandatory scenarios AвЂ“F for Codex thread r4587604965."""

    @staticmethod
    def _retained(settings, tmp_path):
        """A retained protective close: journal + outbox + OPEN row."""
        contour, store, clock, pos, key, entry, control = (
            _round8_stop_setup(settings, tmp_path)
        )
        original_list, original_save = _double_outage(store)
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        engine = contour.engine
        assert engine.journal.exists(key), "the recovery record must exist"
        assert engine.journal.read_all()[key].obligation is not None
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        return contour, store, clock, pos, key, entry, control

    def test_a_failed_read_is_never_treated_as_confirmed_absence(
        self, tmp_path,
    ):
        """A: ``get_position()`` RAISES during journal replay вЂ” the record,
        the frozen STOP/TIMEOUT facts and the observed funding must all be
        KEPT, new entries must be refused, and the read must be retried
        after the database comes back (old HEAD: the record was deleted as
        an "orphan" and the observed rate was lost)."""
        settings = _settings()
        contour, store, clock, pos, key, entry, control = self._retained(
            settings, tmp_path,
        )
        original_get = store.get_position
        store.get_position = _always_fail("PostgreSQL read unavailable")

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine2 = restarted.engine

        # 1) an unknown read NEVER deletes the durable record
        assert engine2.journal.exists(key), (
            "a failed read must not delete the journal record",
        )
        # 2) the observed funding obligation is preserved and retried
        assert key in engine2._pending_obligations, "observed rate lost"
        assert engine2._pending_obligations[
            key
        ].observed_rate_percent == pytest.approx(0.01)
        # 3) the frozen close is preserved and the position stays protected
        assert key in engine2.pending_closes, "frozen close lost"
        assert key not in engine2.open_positions
        assert engine2.pending_closes[key].exit_reason == "STOP_LOSS"
        # 4) new entries are refused while the outcome is unknown
        assert engine2.open_entries({pos.symbol: entry}) == []
        assert engine2.durability_risk is True
        # 5) no duplicate position / terminal leg was created
        assert len([
            leg for leg in store.list_close_legs(key) if leg.is_terminal
        ]) == 1

        # 6) PostgreSQL recovers -> the read-back is retried and completes
        store.get_position = original_get
        restarted.run_cycle({pos.symbol: entry * 0.98})
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

    def test_b_found_row_restores_the_record_exactly(self, tmp_path):
        """B (regression): a SUCCESSFUL read that finds the row keeps the
        established restore path вЂ” obligation first, then the close."""
        settings = _settings()
        contour, store, clock, pos, key, entry, control = self._retained(
            settings, tmp_path,
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine2 = restarted.engine
        assert key in engine2._pending_obligations
        assert key in engine2.pending_closes
        assert engine2.journal.exists(key)
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

    def test_c_confirmed_absent_is_the_only_state_that_retires_the_record(
        self, tmp_path,
    ):
        """C: the record may be deleted ONLY after a successful read chain
        proved there is no row AND no outbox, no leg and no obligation."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        fake_key = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:proven-absent"
        journal = contour.engine.journal
        journal.write(_journal_record(fake_key))
        assert journal.exists(fake_key)

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        assert not restarted.engine.journal.exists(fake_key), (
            "a proven orphan record must be retired",
        )
        assert fake_key not in restarted.engine._pending_obligations

    def test_d_absent_row_with_a_live_outbox_is_not_confirmed_absent(
        self, tmp_path,
    ):
        """D: ``get_position()`` answers "absent" while the staged outbox
        still carries the close вЂ” that is NOT proof of absence, so the
        record (and its obligation) must be kept."""
        settings = _settings()
        contour, store, clock, pos, key, entry, control = self._retained(
            settings, tmp_path,
        )
        assert store.list_pending_closes(), "the outbox must be staged"
        original_get = store.get_position
        store.get_position = lambda *a, **kw: None

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine2 = restarted.engine
        assert engine2.journal.exists(key), (
            "an outbox row proves the close exists вЂ” never delete",
        )
        assert key in engine2._pending_obligations, "observed rate lost"
        assert engine2.open_entries({pos.symbol: entry}) == []

        store.get_position = original_get
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

    def test_e_restart_while_both_journal_reads_are_down(self, tmp_path):
        """E: restart with the position AND obligation reads unavailable вЂ”
        everything is kept, entries stay shut, nothing is re-derived."""
        settings = _settings()
        contour, store, clock, pos, key, entry, control = self._retained(
            settings, tmp_path,
        )
        original_get = store.get_position
        original_ob = store.get_funding_obligation
        store.get_position = _always_fail("PostgreSQL read unavailable")
        store.get_funding_obligation = _always_fail(
            "PostgreSQL read unavailable",
        )

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine2 = restarted.engine
        assert engine2.journal.exists(key)
        assert key in engine2._pending_obligations
        assert key in engine2.pending_closes
        assert engine2.open_entries({pos.symbol: entry}) == []
        assert engine2.durability_risk is True

        store.get_position = original_get
        store.get_funding_obligation = original_ob
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

    def test_f_repeated_restarts_after_recovery_are_idempotent(
        self, tmp_path,
    ):
        """F: recovery then a SECOND restart вЂ” the settlement is exactly
        once, the journal stays retired and no duplicate leg appears."""
        settings = _settings()
        contour, store, clock, pos, key, entry, control = self._retained(
            settings, tmp_path,
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

        again = _restart(restarted, settings, clock, journal_dir=tmp_path)
        engine3 = again.engine
        assert engine3.pending_closes == {}
        assert engine3._pending_obligations == {}
        assert not engine3.journal.exists(key)
        assert key not in engine3.open_positions
        again.run_cycle({pos.symbol: entry * 0.97})
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(
            control["funding_paid"], abs=1e-9,
        )
        assert len([
            leg for leg in store.list_close_legs(key) if leg.is_terminal
        ]) == 1
        assert again.engine.balance == pytest.approx(
            control["balance"], abs=1e-9,
        )
        _assert_ledger_consistent(again)


class TestRound8P1bJournalBeforeStage:
    """Mandatory crash-point scenarios for Codex thread r4587604973."""

    @staticmethod
    def _crash_cycle(contour, store, pos, entry, kill):
        original_list, original_save = _double_outage(store)
        kill()
        with pytest.raises(SimulatedCrash):
            contour.run_cycle(
                {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
                funding_rates_percent={pos.symbol: 0.01},
            )
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save

    @staticmethod
    def _retained_then_crash(settings, tmp_path, install):
        """Retained close -> PostgreSQL recovers -> die DURING the recovery
        cycle at the exact point ``install`` selects (the recovery cycle is
        where the CLOSED UPDATE, the outbox clear and the journal retire
        actually happen)."""
        contour, store, clock, pos, key, entry, control = (
            _round8_stop_setup(settings, tmp_path)
        )
        original_list, original_save = _double_outage(store)
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
            funding_rates_percent={pos.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        assert restarted.engine.journal.exists(key), "record must exist"
        install(restarted)
        with pytest.raises(SimulatedCrash):
            restarted.run_cycle({pos.symbol: entry})
        return restarted, store, key, pos, entry, control

    def _run(self, tmp_path, kill):
        settings = _settings()
        control = _scenario_a_control(settings)
        contour, store, clock, pos, key, entry, control = (
            _round8_stop_setup(settings, tmp_path)
        )
        self._crash_cycle(contour, store, pos, entry, kill)
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        return restarted, store, key, pos, entry, control

    def test_a1_crash_before_the_journal_write(self, tmp_path):
        """A1: die BEFORE the recovery record hits the disk вЂ” nothing durable
        may claim to be final, so the still-OPEN row re-executes the exit."""
        settings = _settings()
        contour, store, clock, pos, key, entry, control = (
            _round8_stop_setup(settings, tmp_path)
        )
        self._crash_cycle(contour, store, pos, entry,
                          lambda: _kill_before_journal_write(
                              contour.engine.journal,
                          ))
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        # nothing durable exists at this point: no outbox row, no journal
        # record and no obligation вЂ” the whole close is re-executed.
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        assert not restarted.engine.journal.exists(key)
        assert store.get_funding_obligation(key) is None
        _assert_round8_recovered(
            restarted, store, key, pos, entry, control,
            expect_obligation=False,
        )

    def test_a2_crash_after_the_journal_write_before_staging(self, tmp_path):
        """A2: the record is durable, staging never ran вЂ” the replay must
        restore the frozen close and the observed rate, then stage."""
        settings = _settings()
        contour, store, clock, pos, key, entry, control = (
            _round8_stop_setup(settings, tmp_path)
        )
        self._crash_cycle(contour, store, pos, entry,
                          lambda: _kill_before_stage(store))
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        assert restarted.engine.journal.exists(key), "record must survive"
        assert restarted.engine.journal.read_all()[key].obligation is not None
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

    def test_a3_crash_immediately_after_the_stage_commit(self, tmp_path):
        """A3 (the Codex repro): die right after ``stage_close`` COMMITTED.
        The durable terminal snapshot may NEVER exist without its observed
        funding obligation вЂ” restart must find the record and settle the
        rate exactly once instead of closing at funding 0."""
        settings = _settings()
        contour, store, clock, pos, key, entry, control = (
            _round8_stop_setup(settings, tmp_path)
        )
        self._crash_cycle(contour, store, pos, entry,
                          lambda: _kill_after_stage(store))
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine2 = restarted.engine
        assert engine2.journal.exists(key), (
            "the observed obligation must be durable BEFORE the staging "
            "commit вЂ” otherwise restart finalises the close at funding 0",
        )
        record = engine2.journal.read_all()[key]
        assert record.obligation is not None, "observed rate missing"
        assert record.obligation["observed_rate_percent"] == pytest.approx(0.01)
        assert store.get_funding_obligation(key) is None, "not in PG yet"
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

    def test_a4_staging_ack_lost_then_the_process_dies(self, tmp_path):
        """A4: the staging transaction committed but the acknowledgement was
        lost (the engine believes staging failed) and the process dies
        before any retry вЂ” the recovery record must already be durable and
        the restart must finish the close exactly once."""
        settings = _settings()
        contour, store, clock, pos, key, entry, control = (
            _round8_stop_setup(settings, tmp_path)
        )
        original_list, original_save = _double_outage(store)
        _lost_ack_then_crash(store, contour.engine.journal)
        with pytest.raises(SimulatedCrash):
            contour.run_cycle(
                {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
                funding_rates_percent={pos.symbol: 0.01},
            )
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        assert restarted.engine.journal.exists(key)
        assert restarted.engine.journal.read_all()[key].obligation is not None
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

    def test_a5_crash_after_the_closed_update(self, tmp_path):
        """A5: die right after the terminal CLOSED UPDATE applied (lost ack)
        вЂ” restart must recognise the committed close, keep the observed
        rate and settle it exactly once."""
        settings = _settings()
        restarted, store, key, pos, entry, control = (
            self._retained_then_crash(
                settings, tmp_path,
                lambda restarted: _kill_after_closed_update(restarted.store),
            )
        )
        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert restarted.engine.journal.exists(key)
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

    def test_a6_crash_before_the_outbox_clear(self, tmp_path):
        """A6: die before the outbox row is retired вЂ” a stale snapshot must
        never roll back the confirmed funding and the record must retire."""
        settings = _settings()
        restarted, store, key, pos, entry, control = (
            self._retained_then_crash(
                settings, tmp_path,
                lambda restarted: _kill_before_outbox_clear(restarted.store),
            )
        )
        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert restarted.engine.journal.exists(key)
        _assert_round8_recovered(restarted, store, key, pos, entry, control)

    def test_a7_crash_before_the_journal_delete(self, tmp_path):
        """A7: die before the journal record is retired вЂ” a further restart
        must finish the retire without re-booking anything."""
        settings = _settings()
        restarted, store, key, pos, entry, control = (
            self._retained_then_crash(
                settings, tmp_path,
                lambda restarted: _kill_before_journal_delete(
                    restarted.engine.journal,
                ),
            )
        )
        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert restarted.engine.journal.exists(key), (
            "the record must survive the crash",
        )
        final = _restart(restarted, settings, restarted._clock,
                         journal_dir=tmp_path)
        _assert_round8_recovered(final, store, key, pos, entry, control)


class TestRound8P2JournalDirectoryFsync:
    """Mandatory scenarios for Codex thread r4587604985."""

    KEY = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:fsync-probe"

    @staticmethod
    def _journal(tmp_path) -> ProtectiveCloseJournal:
        # deliberately NOT the process working directory
        return ProtectiveCloseJournal(
            Path(tmp_path) / "release" / "var" / "me_inverse_short_oos2_journal",
        )

    def test_a_directory_fsync_targets_the_journal_directory(
        self, tmp_path, monkeypatch,
    ):
        """A: the directory that is fsync'ed is ``self.directory`` вЂ” never
        ``os.curdir`` (which in the supplied runtime is the release root,
        a completely different directory)."""
        monkeypatch.chdir(tmp_path)          # cwd != journal directory
        journal = self._journal(tmp_path)
        opened: list[str] = []
        real_open = os.open

        def recording_open(path, flags, *args, **kwargs):
            opened.append(str(path))
            # Windows cannot fsync a read-only handle, so the stand-in fd
            # is opened for writing (the real code opens the DIRECTORY
            # read-only; POSIX accepts that, Windows has no directory
            # fsync at all).
            return real_open(str(tmp_path / "handle.bin"), os.O_RDWR)

        (tmp_path / "handle.bin").write_text("x", encoding="utf-8")
        monkeypatch.setattr(os, "open", recording_open)
        journal.write(_journal_record(self.KEY))

        assert str(journal.directory) in opened, (
            f"the journal directory must be fsync'ed, opened={opened}",
        )
        assert os.curdir not in opened and "." not in opened, (
            "the process working directory must never be fsync'ed instead "
            f"of the journal directory, opened={opened}",
        )
        assert journal.exists(self.KEY)

    def test_b_file_fsync_failure_is_reported(self, tmp_path, monkeypatch):
        """B: a failed payload fsync must surface to the caller."""
        journal = self._journal(tmp_path)

        def failing_fsync(fd):
            raise OSError("payload fsync failed")

        monkeypatch.setattr(os, "fsync", failing_fsync)
        with pytest.raises(OSError):
            journal.write(_journal_record(self.KEY))
        assert not journal.exists(self.KEY)

    def test_c_directory_fsync_failure_is_reported(self, tmp_path, monkeypatch):
        """C: a failed DIRECTORY fsync must surface to the caller вЂ” the old
        code swallowed it and reported the record as durable anyway."""
        journal = self._journal(tmp_path)
        (tmp_path / "handle.bin").write_text("x", encoding="utf-8")
        real_open = os.open
        opened_path: list[str] = []
        real_fsync = os.fsync

        def counting_fsync(fd):
            # the payload fsync runs first; the FIRST fsync after the
            # journal directory itself was opened is the directory fsync
            if str(journal.directory) in opened_path:
                raise OSError("directory fsync failed")
            return real_fsync(fd)

        monkeypatch.setattr(
            os, "open",
            lambda path, flags, *a, **kw: (
                opened_path.append(str(path))
                or real_open(str(tmp_path / "handle.bin"), os.O_RDWR)
            ),
        )
        monkeypatch.setattr(os, "fsync", counting_fsync)
        with pytest.raises(OSError):
            journal.write(_journal_record(self.KEY))
        assert str(journal.directory) in opened_path

    def test_d_atomic_replace_failure_is_reported(self, tmp_path, monkeypatch):
        """D: a failed atomic replace must surface to the caller."""
        journal = self._journal(tmp_path)

        def failing_replace(src, dst):
            raise OSError("atomic replace failed")

        monkeypatch.setattr(os, "replace", failing_replace)
        with pytest.raises(OSError):
            journal.write(_journal_record(self.KEY))
        assert not journal.exists(self.KEY)

    def test_e_identical_facts_are_deduplicated(self, tmp_path):
        """E: rewriting byte-identical facts keeps exactly one record."""
        journal = self._journal(tmp_path)
        journal.write(_journal_record(self.KEY))
        journal.write(_journal_record(self.KEY))
        files = list(journal.paths())
        assert len(files) == 1, files
        assert list(journal.read_all()) == [self.KEY]

    def test_f_restart_after_a_completed_write_replays_the_record(
        self, tmp_path,
    ):
        """F: a completed write is replayed by a brand-new journal object."""
        journal = self._journal(tmp_path)
        journal.write(_journal_record(self.KEY))
        replayed = ProtectiveCloseJournal(journal.directory).read_all()
        assert list(replayed) == [self.KEY]
        assert replayed[self.KEY].obligation["observed_rate_percent"] == (
            pytest.approx(0.01)
        )

    def test_g_corrupt_record_is_skipped_never_merged(self, tmp_path):
        """G: a corrupt / torn payload is never merged; healthy records
        still load — but Round 10 (Codex r4597065733) adds the other half:
        the corruption must be an EXPLICIT recovery failure, not silence,
        and the file is never deleted or rewritten by the reader."""
        journal = self._journal(tmp_path)
        journal.write(_journal_record(self.KEY))
        good_key = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:other"
        journal.write(_journal_record(good_key))
        (journal.directory / "deadbeef.json").write_text(
            "{ not json", encoding="utf-8",
        )
        result = journal.read_checked()
        assert result.status == _journal_mod.JOURNAL_READ_FAILED, (
            "a corrupt record must be reported as a recovery failure",
        )
        assert sorted(result.records) == sorted([self.KEY, good_key])
        assert "deadbeef.json" in (result.reason or "")
        assert (journal.directory / "deadbeef.json").read_text(
            encoding="utf-8",
        ) == "{ not json", "the corrupt file is never deleted or rewritten"
        with pytest.raises(_journal_mod.JournalReadError):
            journal.read_all()

    def test_h_incomplete_temporary_file_after_a_crash_is_ignored(
        self, tmp_path,
    ):
        """H: a torn ``.json.tmp`` left by a crash between write and rename
        is never replayed as a record and never blocks the next write."""
        journal = self._journal(tmp_path)
        journal.write(_journal_record(self.KEY))
        torn = journal.paths()[0].with_name(
            journal.paths()[0].name + ".tmp",
        )
        torn.write_text('{"position_key": "ME_LONG', encoding="utf-8")

        records = journal.read_all()
        assert list(records) == [self.KEY], "torn payload must be ignored"

        journal.write(_journal_record(self.KEY))   # next write succeeds
        assert list(journal.read_all()) == [self.KEY]
        assert len(journal.paths()) == 1


# ---------------------------------------------------------------------------
# Codex review round 9 вЂ” P1 (r4590138816): a protective close must be
# durable EVEN WHEN no funding obligation is pending.
# ---------------------------------------------------------------------------


def _round9_protective_setup(settings, tmp_path, *, advance_hours=0):
    """entry -> protective STOP ready, with NO funding obligation pending.

    ``advance_hours=0`` keeps the position BEFORE the first 8h funding
    boundary, so ``_settle_funding_for_close`` books nothing and the close
    runs with ``obligation_pending=False`` вЂ” the exact premise of the
    finding.
    """
    contour, store, clock = _contour(settings, journal_dir=tmp_path)
    pos = _open_one(contour, store, clock)
    if advance_hours:
        clock.advance(hours=advance_hours)
    return contour, store, clock, pos, pos.position_key, pos.entry_price


def _stop_control(settings, tmp_path, *, advance_hours=0):
    """Failure-free reference run of the same protective-stop timeline."""
    contour, store, clock = _contour(
        settings, journal_dir=tmp_path / "control",
    )
    pos = _open_one(contour, store, clock)
    if advance_hours:
        clock.advance(hours=advance_hours)
    cycle = contour.run_cycle(
        {pos.symbol: pos.entry_price}, bars=_round8_stop_bars(
            pos, pos.entry_price,
        ),
    )
    assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
    assert store.get_position(pos.position_key).status == POSITION_STATUS_CLOSED
    assert contour.engine.journal.exists(pos.position_key) is False, (
        "a completed close retires its own recovery record",
    )
    return contour, store, pos


class TestRound9P1ProtectiveCloseWithoutObligation:
    """Mandatory scenarios AвЂ“H for Codex thread r4590138816."""

    def test_a_stop_before_the_first_boundary_is_journaled_with_pg_down(
        self, tmp_path,
    ):
        """A: STOP before the first funding boundary + PostgreSQL down
        (``stage_close`` raises) вЂ” a durable recovery record must exist
        even though NO funding obligation is pending."""
        settings = _settings()
        contour, store, clock, pos, key, entry = _round9_protective_setup(
            settings, tmp_path,
        )
        full_size = pos.remaining_size
        store.stage_close = _always_fail("terminal staging failure")

        cycle = contour.run_cycle({pos.symbol: entry}, bars=_round8_stop_bars(
            pos, entry,
        ))
        assert cycle["exit_reasons"] == ["STOP_LOSS"]

        # --- the premise: there is no funding obligation anywhere --------
        assert store.get_funding_obligation(key) is None
        assert contour.engine._pending_obligations == {}
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_OPEN
        assert row.funding_periods_charged == 0
        assert store.list_pending_closes() == []
        assert store.list_close_legs(key) == []

        # --- yet the frozen protective close IS durable ------------------
        record = contour.engine.journal.read_all().get(key)
        assert record is not None, (
            "a protective close must be journaled even with no obligation",
        )
        assert record.exit_reason == "STOP_LOSS"
        assert record.staged is False
        assert record.obligation is None, "no obligation may be invented"
        assert record.owed_partial_legs == []
        assert record.reference_price == pytest.approx(
            pos.stop_price, rel=1e-12,
        )
        assert record.fill_price == pytest.approx(
            pos.stop_price * (1.0 + settings.slippage_percent), rel=1e-12,
        )
        assert record.quantity == pytest.approx(full_size, rel=1e-12)
        assert record.recovery_ids.get("position_key") == key
        assert record.recovery_ids.get("event_key"), "idempotent replay ids"
        assert contour.engine.durability_risk is True
        assert key in contour.engine.unstaged_close_keys

    def test_b_restart_with_a_non_triggering_price_executes_the_original_stop(
        self, tmp_path,
    ):
        """B: after a restart a price at which the STOP would NOT fire must
        still execute the ORIGINAL STOP вЂ” never a fresh market evaluation."""
        settings = _settings()
        contour, store, clock, pos, key, entry = _round9_protective_setup(
            settings, tmp_path,
        )
        original_stage = store.stage_close
        store.stage_close = _always_fail("terminal staging failure")
        contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert store.get_position(key).status == POSITION_STATUS_OPEN

        # PostgreSQL is back, the process restarts.
        store.stage_close = original_stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)

        # A price at which the STOP would NOT fire (the high never reaches
        # the stop price): only the retained snapshot can close this trade.
        non_triggering = {
            pos.symbol: {"high": pos.stop_price * 0.5, "low": entry * 0.99},
        }
        cycle = restarted.run_cycle({pos.symbol: entry}, bars=non_triggering)
        assert cycle["exit_reasons"] == [], (
            "the STOP must come from the retained snapshot, not from a new "
            "evaluation of the market",
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert row.exit_price == pytest.approx(
            pos.stop_price * (1.0 + settings.slippage_percent), rel=1e-12,
        )
        assert row.funding_periods_charged == 0, "no period may be invented"
        assert store.get_funding_obligation(key) is None
        legs = store.list_close_legs(key)
        assert [leg.exit_reason for leg in legs] == ["STOP_LOSS"]
        assert len([leg for leg in legs if leg.is_terminal]) == 1
        assert restarted.engine.journal.exists(key) is False, "retired"
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    def test_c_unknown_rate_keeps_terminal_facts_without_a_fabricated_zero(
        self, tmp_path,
    ):
        """C: an UNKNOWN funding rate never becomes a booked 0.00 вЂ” the
        terminal facts stay intact and the STOP is still persisted."""
        settings = _settings()
        contour, store, clock, pos, key, entry = _round9_protective_setup(
            settings, tmp_path, advance_hours=8,
        )
        original_stage = store.stage_close
        full_size = pos.remaining_size
        store.stage_close = _always_fail("terminal staging failure")

        # No funding_rates_percent is supplied -> the rate is UNKNOWN.
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        assert store.get_funding_obligation(key) is None, (
            "an UNKNOWN rate must never be persisted as a booked obligation",
        )
        assert contour.engine._pending_obligations == {}
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_OPEN
        assert row.funding_periods_charged == 0, "no fabricated period"

        record = contour.engine.journal.read_all().get(key)
        assert record is not None, "the STOP must still be durable"
        assert record.exit_reason == "STOP_LOSS"
        assert record.obligation is None
        assert record.fill_price == pytest.approx(
            pos.stop_price * (1.0 + settings.slippage_percent), rel=1e-12,
        )
        assert record.quantity == pytest.approx(full_size, rel=1e-12)

        # Recovery executes the frozen STOP and books nothing invented.
        store.stage_close = original_stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert row.funding_periods_charged == 0
        assert row.exit_price == pytest.approx(
            pos.stop_price * (1.0 + settings.slippage_percent), rel=1e-12,
        )
        assert store.get_funding_obligation(key) is None
        assert restarted.engine.journal.exists(key) is False, "retired"
        _assert_durable_consistent(store, key)

    def test_d_timeout_is_journaled_without_an_obligation_and_survives(
        self, tmp_path,
    ):
        """D: the same guarantee for TIMEOUT вЂ” no obligation, still durable,
        and the original hold expiry closes after a restart."""
        settings = _settings()
        contour, store, clock, pos, key, entry = _round9_protective_setup(
            settings, tmp_path, advance_hours=MAX_HOLD_MINUTES / 60 + 1,
        )
        original_stage = store.stage_close
        store.stage_close = _always_fail("terminal staging failure")

        # Prices that trigger neither the STOP nor any TP: the hold expires.
        flat = {
            pos.symbol: {
                "high": pos.entry_price * 1.0001,
                "low": pos.entry_price * 0.9999,
            },
        }
        cycle = contour.run_cycle({pos.symbol: entry}, bars=flat)
        assert cycle["exit_reasons"] == ["TIMEOUT"], cycle

        assert store.get_funding_obligation(key) is None
        assert contour.engine._pending_obligations == {}
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        record = contour.engine.journal.read_all().get(key)
        assert record is not None, "a TIMEOUT must be journaled too"
        assert record.exit_reason == "TIMEOUT"
        assert record.staged is False
        assert record.obligation is None

        store.stage_close = original_stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        restarted.run_cycle(
            {pos.symbol: entry}, bars={
                pos.symbol: {
                    "high": pos.entry_price * 1.0001,
                    "low": pos.entry_price * 0.9999,
                },
            },
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "TIMEOUT"
        # the FROZEN fill price of the retained snapshot, never a new one
        assert row.exit_price == pytest.approx(record.fill_price, rel=1e-12)
        legs = store.list_close_legs(key)
        assert [leg.exit_reason for leg in legs] == ["TIMEOUT"]
        assert restarted.engine.journal.exists(key) is False, "retired"
        _assert_durable_consistent(store, key)

    def test_e_dirty_partial_leg_is_preserved_with_the_protective_stop(
        self, tmp_path,
    ):
        """E: a dirty TP1 leg and the terminal leg both survive a staging
        outage and are replayed exactly once on recovery."""
        settings = _settings()
        contour, store, clock, pos, key, entry = _round9_protective_setup(
            settings, tmp_path,
        )
        original_commit = store.commit_partial_exit
        original_stage = store.stage_close
        store.commit_partial_exit = _always_fail("ledger down")
        store.stage_close = _always_fail("terminal staging failure")

        contour.run_cycle({pos.symbol: entry * 0.945})       # TP1 stays dirty
        assert key in contour.engine.dirty_leg_keys
        contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )

        record = contour.engine.journal.read_all().get(key)
        assert record is not None, "the protective close must be durable"
        assert record.exit_reason == "STOP_LOSS"
        assert record.owed_partial_legs, "the owed TP1 leg rides the record"
        assert [int(leg["leg_seq"]) for leg in record.owed_partial_legs] == [1]
        assert bool(record.terminal_leg.get("is_terminal")) is True

        store.commit_partial_exit = original_commit
        store.stage_close = original_stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        legs = store.list_close_legs(key)
        assert sorted(leg.exit_reason for leg in legs) == [
            "STOP_LOSS", "TAKE_PROFIT_1",
        ]
        assert [leg.leg_seq for leg in legs] == [1, 3]
        assert len([leg for leg in legs if leg.is_terminal]) == 1
        assert store.list_pending_closes() == []
        assert restarted.engine.dirty_leg_keys == ()
        assert restarted.engine.journal.exists(key) is False, "retired"
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    def test_f_ambiguous_staging_commit_yields_exactly_one_terminal_leg(
        self, tmp_path,
    ):
        """F: a staging COMMIT whose acknowledgement is lost converges to
        exactly one terminal leg and never double-books PnL."""
        settings = _settings()
        control, control_store, control_pos = _stop_control(settings, tmp_path)
        control_balance = control.engine.balance

        contour, store, clock, pos, key, entry = _round9_protective_setup(
            settings, tmp_path,
        )
        original_stage = store.stage_close
        committed = {"n": 0}

        def commit_then_raise(*args, **kwargs):
            result = original_stage(*args, **kwargs)     # COMMITTED
            committed["n"] += 1
            raise RuntimeError("staging acknowledgement lost")

        store.stage_close = commit_then_raise
        contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert committed["n"] == 1
        assert store.list_pending_closes(), "the outbox row is durable"
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        record = contour.engine.journal.read_all()[key]
        assert record.staged is False, "an unconfirmed stage is never claimed"

        store.stage_close = original_stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        legs = store.list_close_legs(key)
        assert [leg.exit_reason for leg in legs] == ["STOP_LOSS"]
        assert len([leg for leg in legs if leg.is_terminal]) == 1
        assert store.list_pending_closes() == []
        assert restarted.engine.journal.exists(key) is False, "retired"
        assert restarted.engine.balance == pytest.approx(
            control_balance, abs=1e-9,
        ), "an ambiguous commit must never double-book PnL"
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    def test_g_repeated_restarts_before_the_restore_keep_the_frozen_facts(
        self, tmp_path,
    ):
        """G: any number of restarts before PostgreSQL recovers keep the
        journal record and every frozen fact byte-identical."""
        settings = _settings()
        contour, store, clock, pos, key, entry = _round9_protective_setup(
            settings, tmp_path,
        )
        original_stage = store.stage_close
        store.stage_close = _always_fail("terminal staging failure")
        contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        original = contour.engine.journal.read_all()[key]

        current = contour
        for round_no in range(3):
            store.stage_close = _always_fail("terminal staging failure")
            current = _restart(current, settings, clock, journal_dir=tmp_path)
            engine = current.engine
            record = engine.journal.read_all().get(key)
            assert record is not None, f"restart {round_no}: journal lost"
            assert record.exit_reason == original.exit_reason
            assert record.triggered_at == original.triggered_at
            assert record.reference_price == original.reference_price
            assert record.fill_price == original.fill_price
            assert record.quantity == original.quantity
            assert record.owed_partial_legs == original.owed_partial_legs
            assert record.obligation == original.obligation
            assert store.get_position(key).status == POSITION_STATUS_OPEN
            assert key in engine.pending_closes, "never re-evaluated"
            assert key not in engine.open_positions

        # Only now does PostgreSQL come back.
        store.stage_close = original_stage
        current.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_price == pytest.approx(original.fill_price, rel=1e-12)
        assert current.engine.journal.exists(key) is False, "retired"
        _assert_durable_consistent(store, key)

    def test_h_recovery_completes_consistently_and_equals_the_baseline(
        self, tmp_path,
    ):
        """H: recovery ends CLOSED with one terminal leg, a drained outbox,
        a retired journal record, no pending obligation and a balance equal
        to the failure-free baseline."""
        settings = _settings()
        control, control_store, control_pos = _stop_control(settings, tmp_path)
        control_balance = control.engine.balance

        contour, store, clock, pos, key, entry = _round9_protective_setup(
            settings, tmp_path,
        )
        original_stage = store.stage_close
        store.stage_close = _always_fail("terminal staging failure")
        contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert contour.engine.journal.exists(key)

        store.stage_close = original_stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )

        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        legs = store.list_close_legs(key)
        assert [leg.exit_reason for leg in legs] == ["STOP_LOSS"]
        assert len([leg for leg in legs if leg.is_terminal]) == 1

        engine = restarted.engine
        assert store.list_pending_closes() == [], "no outbox orphan"
        assert engine.pending_closes == {}
        assert engine._unstaged_closes == set()
        assert engine.journal.exists(key) is False, "record retired"
        assert engine._pending_obligations == {}, "nothing pending"
        assert store.get_funding_obligation(key) is None
        assert engine.durability_risk is False
        assert engine.balance == pytest.approx(control_balance, abs=1e-9)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)


# ---------------------------------------------------------------------------
# Codex review round 9 вЂ” P2 (r4590138839): a failed parent-directory fsync
# must never be forgotten by the next write.
# ---------------------------------------------------------------------------


class TestRound9P2AncestorDirectoryFsync:
    """Mandatory scenarios AвЂ“H for Codex thread r4590138839."""

    KEY = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:round9-p2"

    @staticmethod
    def _journal(tmp_path, *parts):
        return ProtectiveCloseJournal(tmp_path.joinpath(*parts))

    @staticmethod
    def _chain(root):
        """``root`` plus every ancestor, filesystem root excluded."""
        chain = [root]
        probe = root.parent
        while probe != probe.parent:
            chain.append(probe)
            probe = probe.parent
        return chain

    @staticmethod
    def _record_everything(monkeypatch):
        """Record ``os.open`` paths and the fsync/replace ordering while a
        flag is on, so unrelated filesystem traffic cannot pollute them."""
        state = {"on": False, "opened": [], "events": []}
        real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

        def open_recorder(path, flags, *args, **kwargs):
            if state["on"]:
                state["opened"].append(str(path))
            return real_open(path, flags, *args, **kwargs)

        def fsync_recorder(fd):
            if state["on"]:
                state["events"].append("fsync")
            return real_fsync(fd)

        def replace_recorder(src, dst):
            if state["on"]:
                state["events"].append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "open", open_recorder)
        monkeypatch.setattr(os, "fsync", fsync_recorder)
        monkeypatch.setattr(os, "replace", replace_recorder)
        return state

    @staticmethod
    def _record_synced(monkeypatch, journal):
        synced: list[str] = []
        monkeypatch.setattr(
            journal, "_fsync_directory",
            lambda directory: synced.append(str(directory)),
        )
        return synced

    @staticmethod
    def _failing_parent(journal, parent, once=True):
        state = {"failed": False}

        def flaky(directory):
            if str(directory) == str(parent) and (not once or not state["failed"]):
                state["failed"] = True
                raise OSError("injected directory fsync failure")

        return flaky, state

    def test_a_first_write_syncs_the_leaf_and_every_created_parent(
        self, tmp_path, monkeypatch,
    ):
        """A: create dirs -> temp file -> fsync temp -> atomic replace ->
        sync the journal dir -> sync the needed parents -> only then does
        the write report success."""
        root = tmp_path / "a" / "b"
        journal = self._journal(tmp_path, "a", "b")
        state = self._record_everything(monkeypatch)

        state["on"] = True
        path = journal.write(_journal_record(self.KEY))
        state["on"] = False

        # payload fsync BEFORE the atomic rename, then directory syncs
        assert state["events"][0] == "fsync"
        assert state["events"][1] == "replace"
        # every level of the required chain is opened for sync, leaf-first
        chain = self._chain(root)
        positions = [state["opened"].index(str(level)) for level in chain]
        assert positions == sorted(positions), "leaf-first order required"
        assert str(root) in state["opened"]
        assert str(root.parent) in state["opened"]
        assert str(tmp_path) in state["opened"]
        assert path.exists() and path.name.endswith(".json")
        assert journal.exists(self.KEY)

    def test_b_a_failed_parent_sync_makes_the_write_raise(
        self, tmp_path, monkeypatch,
    ):
        """B: a parent fsync failure means the write does NOT report
        durability вЂ” the error reaches the caller."""
        journal = self._journal(tmp_path, "jrn")
        flaky, state = self._failing_parent(journal, tmp_path)
        monkeypatch.setattr(journal, "_fsync_directory", flaky)

        with pytest.raises(OSError):
            journal.write(_journal_record(self.KEY))
        assert state["failed"] is True

    def test_c_retry_syncs_the_previously_unconfirmed_parent(
        self, tmp_path, monkeypatch,
    ):
        """C: the first write fails on a parent; the retry must synchronise
        that SAME parent even though the directory already exists."""
        journal = self._journal(tmp_path, "deep", "jrn")
        parent = tmp_path / "deep"
        flaky, state = self._failing_parent(journal, parent)
        monkeypatch.setattr(journal, "_fsync_directory", flaky)

        with pytest.raises(OSError):
            journal.write(_journal_record(self.KEY))
        assert state["failed"] is True
        assert journal.directory.exists(), "the tree was created"

        synced = self._record_synced(monkeypatch, journal)
        journal.write(_journal_record(self.KEY))     # identical facts, retry
        assert str(parent) in synced, (
            "the retry must re-synchronise the parent whose fsync never "
            "completed, even though the directory already exists",
        )
        assert str(journal.directory) in synced

    def test_d_an_identical_rewrite_still_synchronises_the_chain(
        self, tmp_path, monkeypatch,
    ):
        """D: de-duplicating byte-identical facts must not bypass the fsync
        obligations of the write being performed."""
        journal = self._journal(tmp_path, "deep", "jrn")
        journal.write(_journal_record(self.KEY))     # completes fully

        synced = self._record_synced(monkeypatch, journal)
        journal.write(_journal_record(self.KEY))     # identical facts
        assert str(journal.directory) in synced
        assert str(tmp_path / "deep") in synced, (
            "an identical rewrite must still sync the ancestor chain",
        )
        assert str(tmp_path) in synced
        assert list(journal.read_all()) == [self.KEY]

    def test_a_relative_path_is_anchored_not_cwd_dependent(
        self, tmp_path, monkeypatch,
    ):
        """E: the journal keeps correct absolute paths when it is started
        from a different working directory."""
        from app.paper.me_long_v2_inverse_short_oos2_v1 import journal as _mod

        foreign = tmp_path / "elsewhere"
        foreign.mkdir()
        monkeypatch.chdir(foreign)

        default = ProtectiveCloseJournal(None)
        assert default.directory.is_absolute()
        assert default.directory == _mod.DEFAULT_JOURNAL_DIR

        relative = ProtectiveCloseJournal("var/relative_journal")
        assert relative.directory.is_absolute(), (
            "a relative journal path must be resolved at construction "
            "time, never against a later process cwd",
        )
        assert relative.directory == foreign / "var" / "relative_journal"

        state = self._record_everything(monkeypatch)
        state["on"] = True
        relative.write(_journal_record(self.KEY))
        state["on"] = False
        assert str(relative.directory) in state["opened"]
        assert os.curdir not in state["opened"]
        assert relative.exists(self.KEY)

    def test_f_an_intermediate_level_failure_is_retried_too(
        self, tmp_path, monkeypatch,
    ):
        """F: several ancestor levels, the failure at an INTERMEDIATE one вЂ”
        the retry re-syncs that level as well."""
        journal = self._journal(tmp_path, "lvl1", "lvl2", "jrn")
        intermediate = tmp_path / "lvl1" / "lvl2"
        flaky, state = self._failing_parent(journal, intermediate)
        monkeypatch.setattr(journal, "_fsync_directory", flaky)

        with pytest.raises(OSError):
            journal.write(_journal_record(self.KEY))
        assert state["failed"] is True

        synced = self._record_synced(monkeypatch, journal)
        journal.write(_journal_record(self.KEY))
        assert str(intermediate) in synced, (
            "the intermediate level that was never confirmed must be "
            "synchronised by the retry",
        )
        assert str(tmp_path / "lvl1") in synced
        assert str(tmp_path) in synced

    def test_g_a_failed_payload_fsync_or_rename_is_never_reported(
        self, tmp_path, monkeypatch,
    ):
        """G: a failed file fsync or a failed atomic rename never produces a
        reported-durable record."""
        journal = self._journal(tmp_path, "jrn")
        real_fsync, real_replace = os.fsync, os.replace

        def fsync_boom(fd):
            raise OSError("payload fsync failed")

        monkeypatch.setattr(os, "fsync", fsync_boom)
        with pytest.raises(OSError):
            journal.write(_journal_record(self.KEY))
        assert not journal.exists(self.KEY)

        monkeypatch.setattr(os, "fsync", real_fsync)

        def replace_boom(src, dst):
            raise OSError("atomic replace failed")

        monkeypatch.setattr(os, "replace", replace_boom)
        with pytest.raises(OSError):
            journal.write(_journal_record(self.KEY))
        assert not journal.exists(self.KEY)
        monkeypatch.setattr(os, "replace", real_replace)

        # once the filesystem heals the very same write succeeds
        journal.write(_journal_record(self.KEY))
        assert list(journal.read_all()) == [self.KEY]

    def test_h_a_restart_after_a_failed_sync_keeps_the_immutable_facts(
        self, tmp_path, monkeypatch,
    ):
        """H: a restart after a failed parent sync replays the record with
        every immutable fact intact and redoes the outstanding fsync."""
        journal = self._journal(tmp_path, "deep", "jrn")
        parent = tmp_path / "deep"
        flaky, state = self._failing_parent(journal, parent)
        monkeypatch.setattr(journal, "_fsync_directory", flaky)

        original = _journal_record(self.KEY)
        with pytest.raises(OSError):
            journal.write(original)
        assert state["failed"] is True

        # ---- a brand-new journal object = a process restart ------------
        restarted = ProtectiveCloseJournal(journal.directory)
        synced = self._record_synced(monkeypatch, restarted)
        restarted.write(original)
        assert str(parent) in synced, (
            "the outstanding parent fsync must be redone after a restart",
        )

        restored = restarted.read_all()[self.KEY]
        assert restored.exit_reason == original.exit_reason
        assert restored.triggered_at == original.triggered_at
        assert restored.reference_price == original.reference_price
        assert restored.fill_price == original.fill_price
        assert restored.quantity == original.quantity
        assert restored.accounting_delta == original.accounting_delta
        assert restored.recovery_ids == original.recovery_ids
        assert restored.obligation == original.obligation


# ---------------------------------------------------------------------------
# Codex review round 10 — P1 (r4597065733): a recovery journal that cannot
# be READ is not a recovery journal that does not exist.
#
# The journal on disk is the ONLY durable copy of a protective STOP/TIMEOUT
# when PostgreSQL staging was down.  Reading it must therefore be a gate:
# read -> three-state verdict -> fail closed on anything but a clean read.
# ---------------------------------------------------------------------------


def _poison_journal_reads(monkeypatch, journal, exc_factory, *, once=False):
    """Make every journal RECORD read raise; all other I/O is untouched.

    The journal's own reader uses the builtin ``open`` for ``*.json``
    payloads, so that is the seam.  Writes (mode ``w``), temporary files
    (``*.tmp``) and everything outside the journal directory keep working —
    which is exactly the fault model: the record is there, only its READ
    fails.
    """
    import builtins as _builtins

    real_open = _builtins.open
    root = str(journal.directory)
    state = {"raised": 0, "enabled": True}

    def poisoned(file, mode="r", *args, **kwargs):
        if state["enabled"]:
            path, modes = str(file), str(mode)
            reading = "r" in modes and not any(
                flag in modes for flag in "wxa+"
            )
            if reading and path.startswith(root) and path.endswith(".json"):
                state["raised"] += 1
                if not (once and state["raised"] > 1):
                    raise exc_factory()
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", poisoned)
    return state


def _poison_journal_listing(monkeypatch, journal, exc_factory):
    """Make listing the journal directory raise (absence is never proved)."""
    real_scandir = os.scandir
    target = str(journal.directory)
    state = {"enabled": True}

    def poisoned(path=".", *args, **kwargs):
        if state["enabled"] and str(path) == target:
            raise exc_factory()
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", poisoned)
    return state


def _round10_stop_setup(
    settings, tmp_path, *, advance_hours=0, funding_rate=None,
    fail_obligation_write=False,
):
    """entry -> protective STOP journaled while PostgreSQL staging is down.

    Returns ``(contour, store, clock, pos, key, entry, original_stage)``.
    With ``advance_hours`` past the first 8h boundary a ``funding_rate`` of
    ``None`` leaves the rate UNKNOWN (no obligation pending — the Round 9
    premise), while a real rate produces an OBSERVED obligation — and
    ``fail_obligation_write`` forces that observed rate to live ONLY in the
    recovery journal, which is the premise of scenario J.
    """
    contour, store, clock = _contour(settings, journal_dir=tmp_path)
    pos = _open_one(contour, store, clock)
    entry = pos.entry_price
    if advance_hours:
        clock.advance(hours=advance_hours)
    original_stage = store.stage_close
    original_save = store.save_funding_obligation
    store.stage_close = _always_fail("terminal staging failure")
    if fail_obligation_write:
        store.save_funding_obligation = _always_fail(
            "obligation write failure",
        )
    rates = {pos.symbol: funding_rate} if funding_rate is not None else None
    cycle = contour.run_cycle(
        {pos.symbol: entry},
        funding_rates_percent=rates,
        bars=_round8_stop_bars(pos, entry),
    )
    if fail_obligation_write:
        store.save_funding_obligation = original_save
    assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
    key = pos.position_key
    assert contour.engine.journal.exists(key)
    assert store.get_position(key).status == POSITION_STATUS_OPEN
    assert store.list_close_legs(key) == []
    assert store.list_pending_closes() == []
    return contour, store, clock, pos, key, entry, original_stage


def _round10_control(
    settings, tmp_path, *, advance_hours=0, funding_rate=None,
    fail_obligation_write=False,
):
    """Failure-free reference for the same protective-stop timeline."""
    contour, store, clock = _contour(
        settings, journal_dir=tmp_path / "control",
    )
    pos = _open_one(contour, store, clock)
    if advance_hours:
        clock.advance(hours=advance_hours)
    original_save = store.save_funding_obligation
    if fail_obligation_write:
        store.save_funding_obligation = _always_fail(
            "obligation write failure",
        )
    rates = {pos.symbol: funding_rate} if funding_rate is not None else None
    cycle = contour.run_cycle(
        {pos.symbol: pos.entry_price},
        funding_rates_percent=rates,
        bars=_round8_stop_bars(pos, pos.entry_price),
    )
    if fail_obligation_write:
        store.save_funding_obligation = original_save
    assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
    assert store.get_position(pos.position_key).status == (
        POSITION_STATUS_CLOSED
    )
    return contour, store, pos


def _quiescent_bar(pos, entry):
    """A bar on which STOP, TP1 and TP3 would all stay idle."""
    return {pos.symbol: {"high": pos.stop_price * 0.9, "low": entry * 0.97}}


class TestRound10P1JournalReadFailureFailsClosed:
    """Mandatory scenarios A–L for Codex thread r4597065733."""

    # -- A ------------------------------------------------------------------
    def test_a_restart_with_an_unreadable_journal_keeps_the_gate_shut(
        self, tmp_path, monkeypatch,
    ):
        """A: baseline reproduction — a STOP journaled with staging down and
        then UNREADABLE at restart must keep the whole contour fail-closed."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        frozen = contour.engine.journal.read_all()[key]
        paths_before = list(contour.engine.journal.paths())
        clock.advance(minutes=5)      # a FRESH signal candle, never a dup
        assert _capture(
            store, _candidate(pos.symbol, entry, detected_at=clock()),
        ) == "CAPTURED"

        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine

        # --- the book must report the risk, not a clean restart -----------
        # (Codex fault injection on the baseline: durability_risk=False,
        # accounting_consistent=True — that is the defect these assert.)
        assert engine.durability_risk is True, (
            "an unreadable recovery journal must register an unresolved "
            "durability obligation",
        )
        assert engine.accounting_consistent is False, (
            "an unknown journal state is never a consistent book",
        )
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        assert engine.journal.exists(key), "the record is never deleted"

        # --- fail closed: no re-evaluation, no new entries ----------------
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == [], (
            "exit evaluation must be suspended while the frozen facts are "
            "unreadable — the STOP is never re-derived from market data",
        )
        assert cycle["entries"] == 0, "new entries must be refused"
        assert cycle["durability_risk"] is True
        assert cycle["accounting_consistent"] is False

        # --- the recovery gate itself -------------------------------------
        assert engine.journal_read_blocked is True, (
            "an unreadable journal must register an unresolved recovery",
        )
        assert engine.journal_read_failure
        assert engine._journal_unresolved == set(), (
            "the gate must not depend on _journal_unresolved — that empty "
            "set is exactly the original defect",
        )

        # --- the frozen facts survive byte-for-byte -----------------------
        assert engine.journal.paths() == paths_before, (
            "the unreadable record is still exactly where it was written",
        )
        state["enabled"] = False
        reread = engine.journal.read_all().get(key)
        state["enabled"] = True
        assert reread is not None
        assert reread.exit_reason == frozen.exit_reason
        assert reread.triggered_at == frozen.triggered_at
        assert reread.reference_price == frozen.reference_price
        assert reread.fill_price == frozen.fill_price
        assert reread.quantity == frozen.quantity
        assert reread.recovery_ids == frozen.recovery_ids

    # -- B ------------------------------------------------------------------
    def test_b_temporary_read_failure_blocks_entries_and_keeps_the_file(
        self, tmp_path, monkeypatch,
    ):
        """B: a transient ``OSError`` reports the risk, refuses entries, does
        not claim accounting consistency and never deletes the record."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        paths_before = list(contour.engine.journal.paths())
        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine

        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        assert engine.journal_read_blocked is True
        assert list(engine.journal.paths()) == paths_before, (
            "an unreadable record is never deleted",
        )
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        assert state["raised"] >= 1

        # entries stay refused on every subsequent cycle while unreadable
        for step in range(3):
            clock.advance(minutes=5)   # a FRESH signal candle each round
            assert _capture(
                store, _candidate(pos.symbol, entry, detected_at=clock()),
            ) == "CAPTURED", step
            cycle = restarted.run_cycle({pos.symbol: entry})
            assert cycle["entries"] == 0, cycle
            assert engine.journal_read_blocked is True

    # -- C ------------------------------------------------------------------
    def test_c_retry_after_the_filesystem_heals_restores_the_stop(
        self, tmp_path, monkeypatch,
    ):
        """C: once the journal is readable again the retry re-reads the real
        files and the ORIGINAL STOP is restored and completed."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage
        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.durability_risk is True, (
            "an unreadable recovery journal must report the durability risk",
        )
        assert engine.journal_read_blocked is True

        state["enabled"] = False          # the filesystem recovers
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert engine.journal_read_blocked is False, (
            "a clean read must open the recovery gate",
        )
        assert engine.journal_read_failure is None
        assert cycle["exit_reasons"] == [], "never re-evaluated"
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert engine.journal.exists(key) is False, "retired after recovery"
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True

    # -- D ------------------------------------------------------------------
    def test_d_a_price_that_no_longer_triggers_the_stop_still_closes_it(
        self, tmp_path, monkeypatch,
    ):
        """D: the restart market data no longer satisfies the STOP — the
        close must still happen, driven by the frozen terminal facts only."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        frozen = contour.engine.journal.read_all()[key]
        full = pos.remaining_size
        store.stage_close = stage
        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        assert restarted.engine.durability_risk is True, (
            "an unreadable recovery journal must report the durability risk",
        )
        assert restarted.engine.journal_read_blocked is True

        quiet = _quiescent_bar(pos, entry)
        state["enabled"] = False
        cycle = restarted.run_cycle({pos.symbol: entry}, bars=quiet)
        assert cycle["exit_reasons"] == []
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED, (
            "the frozen STOP must close the position even though the new "
            "price would never trigger it",
        )
        assert row.exit_reason == "STOP_LOSS"
        assert row.exit_price == pytest.approx(frozen.fill_price, rel=1e-12)
        assert frozen.triggered_at is not None, "trigger instant preserved"
        assert frozen.quantity == pytest.approx(row.position_size, rel=1e-12)
        legs = store.list_close_legs(key)
        assert [leg.exit_reason for leg in legs] == ["STOP_LOSS"]
        assert len(legs) == 1, "exactly one terminal leg"

    # -- E ------------------------------------------------------------------
    @pytest.mark.parametrize(
        "errno_code, message",
        [(13, "Permission denied"), (5, "Input/output error")],
        ids=["PermissionError", "EIO"],
    )
    def test_e_permission_error_and_eio_fail_closed_identically(
        self, tmp_path, monkeypatch, errno_code, message,
    ):
        """E: access and I/O errors behave exactly like any other unreadable
        journal — same gate, same refusals, same retry."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage
        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(errno_code, message),
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.durability_risk is True, (
            "an unreadable recovery journal must report the durability risk",
        )
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        assert engine.journal.exists(key)

        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == []
        assert cycle["entries"] == 0

        state["enabled"] = False
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert engine.journal_read_blocked is False
        assert store.get_position(key).status == POSITION_STATUS_CLOSED

    # -- F ------------------------------------------------------------------
    def test_f_a_failed_directory_listing_is_never_an_absence(
        self, tmp_path, monkeypatch,
    ):
        """F: a directory listing failure must never be reported as an empty
        journal — absence is not confirmed, the gate shuts, recovery waits."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage
        state = _poison_journal_listing(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.durability_risk is True, (
            "an unreadable directory listing must not be reported as a "
            "clean, empty journal",
        )
        assert engine.accounting_consistent is False
        assert engine.journal_read_blocked is True
        assert engine.journal.exists(key)

        result = engine.journal.read_checked()
        assert result.status == _journal_mod.JOURNAL_READ_FAILED
        assert result.records == {}
        assert "listing failed" in (result.reason or "")

        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == []
        assert cycle["entries"] == 0

        state["enabled"] = False
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None
        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert engine.journal.exists(key) is False, "retired after recovery"

    # -- G ------------------------------------------------------------------
    def test_g_a_corrupt_record_is_an_explicit_recovery_failure(
        self, tmp_path,
    ):
        """G: corrupt JSON / schema breach -> explicit recovery failure,
        entries banned, and the file is never deleted or rewritten."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        record_path = contour.engine.journal.paths()[0]
        record_path.write_text("{ not json at all", encoding="utf-8")

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.durability_risk is True, (
            "an unreadable recovery journal must report the durability risk",
        )
        assert engine.journal_read_blocked is True
        assert engine.journal_read_failure
        assert "corrupt record" in engine.journal_read_failure
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False

        clock.advance(minutes=5)          # a FRESH signal candle
        assert _capture(
            store, _candidate(pos.symbol, entry, detected_at=clock()),
        ) == "CAPTURED"
        cycle = restarted.run_cycle({pos.symbol: entry})
        assert cycle["entries"] == 0, "entries are banned"
        assert cycle["exit_reasons"] == []
        assert record_path.read_text(encoding="utf-8") == (
            "{ not json at all"
        ), "a corrupt record is never deleted or rewritten automatically"

    # -- H ------------------------------------------------------------------
    def test_h_repeated_restarts_keep_re_detecting_the_unreadable_journal(
        self, tmp_path, monkeypatch,
    ):
        """H: several restarts while the journal is unreadable keep the
        recovery unresolved on every one of them, and the frozen terminal
        snapshot is never lost in between."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        frozen = contour.engine.journal.read_all()[key]
        store.stage_close = stage
        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )

        current = contour
        for round_no in range(3):
            current = _restart(current, settings, clock, journal_dir=tmp_path)
            engine = current.engine
            assert engine.durability_risk is True, f"restart {round_no}"
            assert engine.journal_read_blocked is True, f"restart {round_no}"
            assert engine.durability_risk is True, f"restart {round_no}"
            assert engine.accounting_consistent is False, f"restart {round_no}"
            cycle = current.run_cycle({pos.symbol: entry})
            assert cycle["entries"] == 0, f"restart {round_no}"
            assert cycle["exit_reasons"] == [], f"restart {round_no}"

            state["enabled"] = False           # inspect without disturbing
            record = engine.journal.read_checked().records.get(key)
            state["enabled"] = True
            assert record is not None, f"restart {round_no}: record lost"
            assert record.exit_reason == frozen.exit_reason
            assert record.triggered_at == frozen.triggered_at
            assert record.fill_price == frozen.fill_price
            assert record.quantity == frozen.quantity
            assert record.owed_partial_legs == frozen.owed_partial_legs
            assert record.obligation == frozen.obligation
            assert store.get_position(key).status == POSITION_STATUS_OPEN

        # only now does the filesystem come back
        state["enabled"] = False
        current.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert current.engine.journal_read_blocked is False
        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert current.engine.journal.exists(key) is False

    # -- I ------------------------------------------------------------------
    def test_i_dirty_partial_tp_survives_a_journal_read_failure(
        self, tmp_path, monkeypatch,
    ):
        """I: a dirty TP1 plus the protective STOP — both legs and their
        quantities are preserved across an unreadable-journal restart."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        full = pos.remaining_size
        original_stage, original_commit = store.stage_close, (
            store.commit_partial_exit,
        )
        store.commit_partial_exit = _always_fail("partial leg write failed")
        store.stage_close = _always_fail("terminal staging failure")

        contour.run_cycle({pos.symbol: entry * 0.945})
        assert pos.position_key in contour.engine.dirty_leg_keys
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        key = pos.position_key
        frozen = contour.engine.journal.read_all()[key]
        assert frozen.owed_partial_legs, "the dirty TP1 is owed"

        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )
        store.commit_partial_exit, store.stage_close = (
            original_commit, original_stage,
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        assert restarted.engine.durability_risk is True, (
            "an unreadable recovery journal must report the durability risk",
        )
        assert restarted.engine.journal_read_blocked is True
        assert restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )["entries"] == 0

        state["enabled"] = False
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        legs = store.list_close_legs(key)
        assert [leg.leg_seq for leg in legs] == [1, 3]
        closed = sum(leg.quantity for leg in legs)
        assert round(full - row.remaining_size, 9) == round(closed, 9)
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- J ------------------------------------------------------------------
    def test_j_observed_funding_is_booked_exactly_once_after_recovery(
        self, tmp_path, monkeypatch,
    ):
        """J: the OBSERVED funding settlement of a protective STOP (accrued
        in-cycle, never yet durable in PostgreSQL — the position row flip was
        withheld) travels inside the frozen terminal snapshot, survives an
        UNREADABLE-journal restart and is applied exactly once on recovery."""
        settings = _settings()

        def run_timeline(journal_dir):
            """entry -> dirty TP1 -> +9h -> protective STOP whose staging is
            down (the observed funding sits only in memory + the journal)."""
            contour, store, clock = _contour(
                settings, journal_dir=journal_dir,
            )
            pos = _open_one(contour, store, clock)
            entry = pos.entry_price
            original_commit, original_stage = (
                store.commit_partial_exit, store.stage_close,
            )
            store.commit_partial_exit = _always_fail(
                "partial leg write failed",
            )
            contour.run_cycle({pos.symbol: entry * 0.945})   # TP1 DIRTY
            clock.advance(hours=9)                  # 1 funding period due
            store.stage_close = _always_fail("terminal staging failure")
            contour.run_cycle(
                {pos.symbol: entry},
                funding_rates_percent={pos.symbol: 0.01},
                bars=_round8_stop_bars(pos, entry),
            )
            store.commit_partial_exit = original_commit
            store.stage_close = original_stage
            return contour, store, clock, pos, entry

        # ---- failure-free reference: identical timeline, healthy journal --
        control, control_store, control_clock, control_pos, control_entry = (
            run_timeline(tmp_path / "control")
        )
        for _ in range(3):                 # the healthy journal converges
            control.run_cycle(
                {control_pos.symbol: control_entry},
                funding_rates_percent={control_pos.symbol: 0.01},
                bars=_round8_stop_bars(control_pos, control_entry),
            )
            if control_store.get_position(
                control_pos.position_key,
            ).status == POSITION_STATUS_CLOSED:
                break
        control_row = control_store.get_position(control_pos.position_key)
        assert control_row.status == POSITION_STATUS_CLOSED, control_row
        control_balance = control.engine.balance
        control_charged = control_row.funding_periods_charged

        # ---- fault run ----------------------------------------------------
        contour, store, clock, pos, entry = run_timeline(tmp_path)
        key = pos.position_key
        frozen = contour.engine.journal.read_all()[key]
        # The observed funding settlement travels inside the FROZEN terminal
        # snapshot — the durable position row was never written (the terminal
        # UPDATE was withheld), so the journal is its only surviving copy.
        assert frozen.snapshot.get("funding_periods") == 1, (
            "the observed funding period must be frozen into the record",
        )
        assert frozen.snapshot.get("funding_paid") not in (0, 0.0), (
            "the observed funding amount must be frozen into the record",
        )
        assert frozen.owed_partial_legs, "the dirty TP1 is still owed"

        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        assert restarted.engine.durability_risk is True, (
            "an unreadable recovery journal must report the durability risk",
        )
        assert restarted.engine.journal_read_blocked is True

        # while the journal is unreadable NOTHING may be booked or traded
        blocked = restarted.run_cycle(
            {pos.symbol: entry},
            funding_rates_percent={pos.symbol: 0.01},
            bars=_round8_stop_bars(pos, entry),
        )
        assert blocked["exit_reasons"] == []
        assert blocked["entries"] == 0
        assert store.get_position(key).funding_periods_charged == 0

        state["enabled"] = False
        for _ in range(3):                 # the journal is readable again
            restarted.run_cycle(
                {pos.symbol: entry},
                funding_rates_percent={pos.symbol: 0.01},
                bars=_round8_stop_bars(pos, entry),
            )
            if store.get_position(key).status == POSITION_STATUS_CLOSED:
                break

        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert restarted.engine.journal_read_blocked is False
        assert restarted.engine._pending_obligations == {}, "rate durable"
        assert row.funding_periods_charged == control_charged, (
            "the observed funding is applied exactly once",
        )
        assert row.funding_paid == pytest.approx(
            control_row.funding_paid, abs=1e-9,
        ), "the observed funding amount is identical to the reference"
        assert restarted.engine.balance == pytest.approx(
            control_balance, abs=1e-9,
        ), "an observed rate is never double-charged"
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- K ------------------------------------------------------------------
    def test_k_journal_read_failure_after_an_ambiguous_commit_never_doubles(
        self, tmp_path, monkeypatch,
    ):
        """K: re-reading the journal after an AMBIGUOUS staging COMMIT yields
        exactly one terminal leg and one PnL."""
        settings = _settings()
        control, control_store, control_pos = _round10_control(settings, tmp_path)
        control_balance = control.engine.balance

        contour, store, clock, pos, key, entry, original_stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        committed = {"n": 0}

        def commit_then_raise(*args, **kwargs):
            original_stage(*args, **kwargs)          # COMMITTED
            committed["n"] += 1
            raise RuntimeError("staging acknowledgement lost")

        store.stage_close = commit_then_raise
        contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert committed["n"] == 1
        assert store.list_pending_closes(), "the outbox row is durable"

        store.stage_close = original_stage
        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        assert restarted.engine.durability_risk is True, (
            "an unreadable recovery journal must report the durability risk",
        )
        assert restarted.engine.journal_read_blocked is True
        assert restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )["exit_reasons"] == []

        state["enabled"] = False
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        legs = store.list_close_legs(key)
        assert [leg.exit_reason for leg in legs] == ["STOP_LOSS"]
        assert len([leg for leg in legs if leg.is_terminal]) == 1
        assert store.list_pending_closes() == []
        assert restarted.engine.journal.exists(key) is False, "retired"
        assert restarted.engine.balance == pytest.approx(
            control_balance, abs=1e-9,
        ), "a re-read after an ambiguous COMMIT must never double-book"
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- L ------------------------------------------------------------------
    def test_l_full_recovery_restores_everything_and_reopens_entries(
        self, tmp_path, monkeypatch,
    ):
        """L: after the journal is readable again — CLOSED, exactly one
        terminal leg, outbox drained, record retired only after confirmation,
        balance equal to the failure-free reference, and entries allowed only
        while the book is fully consistent."""
        settings = _settings()
        control, control_store, control_pos = _round10_control(settings, tmp_path)
        control_balance = control.engine.balance

        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage
        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.durability_risk is True, (
            "an unreadable recovery journal must report the durability risk",
        )
        assert engine.journal_read_blocked is True

        state["enabled"] = False
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )

        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        legs = store.list_close_legs(key)
        assert [leg.exit_reason for leg in legs] == ["STOP_LOSS"]
        assert len([leg for leg in legs if leg.is_terminal]) == 1
        assert store.list_pending_closes() == [], "outbox drained"
        assert engine.pending_closes == {}
        assert engine._unstaged_closes == set()
        assert engine.journal.exists(key) is False, (
            "the record is retired only after a confirmed recovery",
        )
        assert engine._pending_obligations == {}
        assert store.get_funding_obligation(key) is None
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        assert engine.balance == pytest.approx(control_balance, abs=1e-9)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-9,
        )
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

        # entries reopen ONLY now, with a fully consistent book
        assert _capture(
            store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
        ) == "CAPTURED"
        reopened = restarted.run_cycle({"ETHUSDT": 100.0})
        assert reopened["entries"] == 1, reopened


# ---------------------------------------------------------------------------
# Round 11 (P2, Codex r4597992776): a syntactically VALID but NON-OBJECT
# JSON payload must become a controlled JOURNAL_READ_FAILED — never an
# unhandled AttributeError out of read_checked(), never an absence
# confirmation, never a bypass of the Round 10 fail-closed gate.
# ---------------------------------------------------------------------------
class TestRound11P2NonObjectJsonPayload:
    """Mandatory scenarios A–N for Codex thread r4597992776."""

    @staticmethod
    def _record_file(journal, key: str, payload_text: str) -> Path:
        """Write a VALID record for ``key``, then overwrite that exact file
        with the raw ``payload_text`` (the SHA-256 file name still belongs to
        ``key``, so the payload/file-key check stays meaningful)."""
        journal.write(_journal_record(key))
        path = journal._path(key)
        path.write_text(payload_text, encoding="utf-8")
        return path

    @staticmethod
    def _assert_failed(result, *, needle: str | None = None) -> None:
        assert result.status == _journal_mod.JOURNAL_READ_FAILED, (
            f"expected a controlled recovery failure, got {result.status!r} "
            f"({result.reason!r})",
        )
        assert result.reason, "a failed read must carry a diagnostic reason"
        if needle is not None:
            assert needle in result.reason, result.reason

    # -- A–E ----------------------------------------------------------------
    @pytest.mark.parametrize(
        ("payload", "case", "type_name", "leak"),
        [
            ("null", "A", "NoneType", "null"),
            ("[]", "B", "list", "[]"),
            ('"text"', "C", "str", "text"),
            ("123", "D", "int", None),       # digits also occur in file names
            ("true", "E_true", "bool", "true"),
            ("false", "E_false", "bool", "false"),
        ],
    )
    def test_non_object_json_is_a_controlled_recovery_failure(
        self, tmp_path, payload, case, type_name, leak,
    ):
        """A–E: ``null`` / ``[]`` / ``"text"`` / ``123`` / ``true`` /
        ``false`` reach JOURNAL_READ_FAILED with the payload TYPE recorded —
        without any exception escaping ``read_checked()`` and without the
        journal ever being declared absent."""
        journal = ProtectiveCloseJournal(tmp_path)
        key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:round11_{case}"
        path = self._record_file(journal, key, payload)

        result = journal.read_checked()          # must NOT raise

        self._assert_failed(result, needle="JOURNAL_INVALID_PAYLOAD_TYPE")
        assert path.name in result.reason, "the offending file is named"
        assert type_name in result.reason, (
            "the actual payload type is part of the diagnostic",
        )
        if leak is not None:
            assert leak not in result.reason, (
                "journal CONTENT is never echoed into diagnostics",
            )
        assert result.records == {}
        # never deleted, never rewritten, never reported absent
        assert path.exists()
        assert path.read_text(encoding="utf-8") == payload
        with pytest.raises(_journal_mod.JournalReadError):
            journal.read_all()

    # -- F ------------------------------------------------------------------
    def test_f_broken_json_syntax_is_a_controlled_failure(self, tmp_path):
        """F: a syntactically broken file is an explicit recovery failure and
        stays byte-identical on disk."""
        journal = ProtectiveCloseJournal(tmp_path)
        key = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:round11_F"
        path = self._record_file(journal, key, "{broken json")

        result = journal.read_checked()          # must NOT raise

        self._assert_failed(result, needle="corrupt record")
        assert result.records == {}
        assert path.read_text(encoding="utf-8") == "{broken json"
        with pytest.raises(_journal_mod.JournalReadError):
            journal.read_all()

    # -- G ------------------------------------------------------------------
    @pytest.mark.parametrize(
        "payload",
        ["{}", json.dumps({"schema": _journal_mod.JOURNAL_SCHEMA})],
        ids=["empty_object", "schema_without_position_key"],
    )
    def test_g_object_without_required_fields_fails_in_a_controlled_way(
        self, tmp_path, payload,
    ):
        """G: a JSON object that misses the required fields is a controlled
        failure — never an absence, never an unhandled exception."""
        journal = ProtectiveCloseJournal(tmp_path)
        key = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:round11_G"
        path = self._record_file(journal, key, payload)

        result = journal.read_checked()          # must NOT raise

        self._assert_failed(result, needle="corrupt record")
        assert result.records == {}
        assert path.read_text(encoding="utf-8") == payload

    # -- H ------------------------------------------------------------------
    @pytest.mark.parametrize(
        ("field", "value", "error"),
        [
            ("quantity", "not-a-number", "ValueError"),
            ("snapshot", 5, "TypeError"),
            ("owed_partial_legs", 5, "TypeError"),
        ],
    )
    def test_h_wrong_field_types_are_a_controlled_failure(
        self, tmp_path, field, value, error,
    ):
        """H: structurally valid JSON whose FIELDS carry the wrong types is a
        controlled failure naming the real deserialization error."""
        journal = ProtectiveCloseJournal(tmp_path)
        key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:round11_H_{field}"
        record = _journal_record(key).to_dict()
        record[field] = value
        path = self._record_file(journal, key, json.dumps(record))

        result = journal.read_checked()          # must NOT raise

        self._assert_failed(result, needle="corrupt record")
        assert error in result.reason, result.reason
        assert result.records == {}
        assert path.exists()

    # -- I ------------------------------------------------------------------
    def test_i_payload_file_key_mismatch_fails_integrity(self, tmp_path):
        """I: a valid payload stored under a file named for a DIFFERENT key
        is an integrity failure, not a replayable record."""
        journal = ProtectiveCloseJournal(tmp_path)
        key_a = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:round11_I_a"
        key_b = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:round11_I_b"
        journal.write(_journal_record(key_a))
        path = journal._path(key_a)
        path.write_text(
            json.dumps(_journal_record(key_b).to_dict()), encoding="utf-8",
        )

        result = journal.read_checked()          # must NOT raise

        self._assert_failed(result, needle="payload/file key mismatch")
        assert result.records == {}
        assert path.exists(), "the mismatching file is never deleted"

    # -- J ------------------------------------------------------------------
    def test_j_mixed_valid_and_invalid_records_stay_fail_closed(
        self, tmp_path,
    ):
        """J: one healthy record next to a damaged one — the healthy record
        may be restored, but the read as a whole NEVER reports success and
        the contour stays fail-closed."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage            # the healthy record may finish
        journal = contour.engine.journal
        damaged = journal._path(
            "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:round11_J_damaged",
        )
        damaged.write_text("null", encoding="utf-8")

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine

        # the healthy record IS available …
        result = engine.journal.read_checked()
        assert key in result.records
        # … but the mixed read as a whole FAILED and is never "fully valid"
        assert result.status == _journal_mod.JOURNAL_READ_FAILED
        with pytest.raises(_journal_mod.JournalReadError):
            engine.journal.read_all()

        # … so the whole contour stays fail-closed
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        assert damaged.exists(), "the damaged file is never deleted"

        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["entries"] == 0, "new entries stay refused"
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        # the healthy half really did recover (partial ≠ fully valid)
        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert journal.exists(key) is False, (
            "the healthy record retires after its close is proven",
        )
        assert damaged.exists(), "the damaged file never retires"

    # -- K ------------------------------------------------------------------
    def test_k_repairing_the_payload_then_retrying_recovers(
        self, tmp_path,
    ):
        """K: after the damaged file is repaired by hand, the next cycle's
        retry re-reads it and the ORIGINAL frozen STOP completes."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage
        journal = contour.engine.journal
        path = journal._path(key)
        original = path.read_bytes()
        path.write_text("null", encoding="utf-8")

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False

        # manual repair (never automatic): the original bytes come back
        path.write_bytes(original)
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None
        assert cycle["exit_reasons"] == [], "never re-evaluated"
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert row.exit_price == pytest.approx(
            json.loads(original)["fill_price"], rel=1e-12,
        ), "the frozen fill price, never recomputed"
        assert engine.journal.exists(key) is False, "retired after recovery"
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True

    # -- L ------------------------------------------------------------------
    def test_l_repeated_restarts_keep_the_damaged_journal_blocked(
        self, tmp_path,
    ):
        """L: every restart re-detects the damaged payload and keeps the
        recovery gate — the snapshot is never silently lost."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        journal = contour.engine.journal
        path = journal._path(key)
        path.write_text("[]", encoding="utf-8")

        for attempt in range(3):
            restarted = _restart(
                contour, settings, clock, journal_dir=tmp_path,
            )
            engine = restarted.engine
            assert engine.journal_read_blocked is True, attempt
            assert engine.durability_risk is True, attempt
            assert engine.accounting_consistent is False, attempt
            assert engine.journal_read_failure, attempt
            assert store.get_position(key).status == POSITION_STATUS_OPEN, (
                attempt,
            )
            assert path.exists(), attempt          # never deleted
            assert path.read_text(encoding="utf-8") == "[]", attempt

    # -- M ------------------------------------------------------------------
    def test_m_a_new_price_never_recomputes_the_frozen_stop(self, tmp_path):
        """M: while the damaged journal blocks recovery no market data is
        evaluated, and after the repair the close still comes from the
        frozen STOP at a price where it would no longer fire."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage
        journal = contour.engine.journal
        path = journal._path(key)
        original = path.read_bytes()
        frozen_fill = json.loads(original)["fill_price"]
        path.write_text("null", encoding="utf-8")

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True

        # a price that WOULD fire a fresh stop: nothing may be evaluated
        blocked = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert blocked["exit_reasons"] == [], (
            "exit evaluation stays suspended — the STOP is never re-derived",
        )
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        assert store.list_close_legs(key) == []

        path.write_bytes(original)
        non_triggering = _quiescent_bar(pos, entry)
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=non_triggering,
        )
        assert cycle["exit_reasons"] == [], "from the snapshot, not data"
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert row.exit_price == pytest.approx(frozen_fill, rel=1e-12)
        assert engine.journal_read_blocked is False

    # -- N ------------------------------------------------------------------
    def test_n_valid_journal_replays_normally(self, tmp_path):
        """N: a healthy journal is untouched by the new validation — the
        replay completes exactly as before Round 11."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None
        assert engine.durability_risk is True, "the close is still unstaged"

        result = engine.journal.read_checked()
        assert result.status == _journal_mod.JOURNAL_LOADED
        assert list(result.records) == [key]
        assert result.reason is None

        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == []
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert engine.journal.exists(key) is False, "retired after recovery"
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True


# ---------------------------------------------------------------------------
# Round 12 (P2, Codex r4598420146): ATOMIC journal replay + SAFE retirement.
#
# The reproduced defect: with a damaged NESTED snapshot the restore
# registered the funding obligation and the journal record FIRST and only
# then deserialized the snapshot, which raised.  ``pending_closes`` was
# therefore never populated, the next flush saw "nothing pending", swept the
# journal away and the following cycle re-opened the recovery gate on a
# journal that had silently vanished — the durable copy of the STOP was
# lost while the position stayed OPEN.
#
# The invariant under test: a journal record is NEVER treated as processed
# until every obligation it carries — protective close, partial legs and
# funding — has been restored AND confirmed.
# ---------------------------------------------------------------------------
class TestRound12AtomicJournalReplayAndSafeRetirement:
    """Mandatory scenarios A–T for Codex thread r4598420146."""

    # -- shared machinery -------------------------------------------------
    @staticmethod
    def _damaged_stop(settings, tmp_path, damage, **setup_kwargs):
        """A protective STOP journaled with staging down, then its durable
        payload damaged in place (the outer record and the file name stay
        valid).

        Returns ``(contour, store, clock, pos, key, entry, stage, path,
        original_bytes)``.
        """
        contour, store, clock, pos, key, entry, stage = _round10_stop_setup(
            settings, tmp_path, **setup_kwargs,
        )
        # staging is restored: the defect is about the JOURNAL, not about
        # PostgreSQL — the recovery after a hand repair must be able to
        # commit the very same close.
        store.stage_close = stage
        path = contour.engine.journal._path(key)
        original = path.read_bytes()
        payload = json.loads(original.decode("utf-8"))
        damage(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return contour, store, clock, pos, key, entry, stage, path, original

    @staticmethod
    def _assert_fail_closed(restarted, engine, store, pos, key, entry, path):
        """The mandatory negative-test contract (§4)."""
        assert engine.journal_read_blocked is True, (
            "a damaged nested snapshot must shut the recovery gate",
        )
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        reason = engine.journal_read_failure or ""
        assert "JOURNAL_SNAPSHOT_RESTORE_FAILED" in reason, reason
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        assert path.exists(), "the damaged record is never deleted"
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == [], "no re-evaluation of the STOP"
        assert cycle["entries"] == 0, "new entries stay refused"
        assert cycle["durability_risk"] is True
        assert cycle["accounting_consistent"] is False
        assert engine.journal_read_blocked is True
        assert path.exists(), "the damaged record survives every cycle"
        return cycle

    # -- A–G --------------------------------------------------------------
    @pytest.mark.parametrize(
        ("case", "damage"),
        [
            ("A", lambda p: p["snapshot"].pop("position_key", None)),
            ("B", lambda p: p.__setitem__("snapshot", None)),
            ("C", lambda p: p.__setitem__("snapshot", [])),
            ("D", lambda p: p["snapshot"].__setitem__("position_size", "abc")),
            (
                "E",
                lambda p: p["snapshot"].__setitem__(
                    "entry_price", "not-a-price",
                ),
            ),
            (
                "F",
                lambda p: p["snapshot"].__setitem__(
                    "position_key", "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:other",
                ),
            ),
            (
                "G",
                lambda p: p.__setitem__(
                    "owed_partial_legs",
                    [{"position_key": p["position_key"], "leg_seq": "x"}],
                ),
            ),
        ],
        ids=["A_no_position_key", "B_snapshot_null", "C_snapshot_list",
             "D_bad_quantity", "E_bad_prices", "F_key_mismatch",
             "G_bad_partial_legs"],
    )
    def test_damaged_nested_snapshot_fails_closed(
        self, tmp_path, case, damage,
    ):
        """A–G: a damaged NESTED snapshot is a controlled recovery failure
        that never mutates engine state and never retires the file."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, _ = (
            self._damaged_stop(settings, tmp_path, damage)
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine

        # the obligation/record registration must NOT have happened before
        # the snapshot was parsed (the reproduced defect: those two were set
        # and only then did from_dict() raise).  Behaviour first, so a
        # baseline run fails on evidence and not on a missing attribute.
        assert engine.pending_closes == {}
        assert engine._journal_records == {}

        self._assert_fail_closed(restarted, engine, store, pos, key, entry, path)
        assert path.read_bytes() != b"", "the file still holds the payload"
        # Round 12 bookkeeping (new on this branch)
        assert engine._journal_restore_ok == set()
        assert key in engine._journal_restore_failed, case

    # -- H ----------------------------------------------------------------
    @staticmethod
    def _valid_obligation(key: str, symbol: str, observed_at, entered_at):
        """A structurally VALID observed-funding obligation payload."""
        return {
            "obligation_key": f"{key}:round12",
            "position_key": key,
            "symbol": symbol,
            "event_key": "",
            "observed_rate_percent": 0.01,
            "observed_at": observed_at.isoformat(),
            "entered_at": entered_at.isoformat(),
            "periods_due": 1,
            "periods_charged_at_observation": 0,
            "legs_ref_max_seq": -1,
            "status": "OPEN",
        }

    def test_h_valid_funding_never_causes_premature_retirement(
        self, tmp_path,
    ):
        """H: a valid observed funding obligation next to a damaged
        snapshot must not let the flush retire the record."""
        settings = _settings()

        def damage(payload):
            # the FUNDING half stays perfectly valid …
            payload["obligation"] = self._valid_obligation(
                payload["position_key"], payload["snapshot"]["symbol"],
                datetime.now(timezone.utc), datetime.now(timezone.utc),
            )
            # … only the nested snapshot loses its identity
            payload["snapshot"].pop("position_key", None)

        contour, store, clock, pos, key, entry, stage, path, _ = (
            self._damaged_stop(settings, tmp_path, damage)
        )
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["obligation"] is not None, "the funding half is valid"

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True

        # TWO full flush cycles first — on the defect the obligation is
        # written to PostgreSQL and the sweep then retires the record even
        # though its nested snapshot never replayed (the reproduced bug).
        engine.flush_pending_closes()
        engine.flush_pending_closes()
        assert path.exists(), (
            "valid funding must never cause a premature retirement",
        )
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        # nothing was registered either: no obligation, no protective close
        assert key not in engine._pending_obligations
        assert engine.pending_closes == {}
        assert store.get_funding_obligation(key) is None, (
            "the obligation is never written from a record that could not "
            "be prepared",
        )

    # -- I ----------------------------------------------------------------
    def test_i_flush_after_failure_keeps_the_journal(self, tmp_path):
        """I: an explicit flush after the failure keeps the file."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, _ = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p.__setitem__("snapshot", None),
            )
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        flushed = engine.flush_pending_closes()
        assert flushed == 0, "nothing may be flushed from a damaged record"
        assert path.exists()
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False

    # -- J ----------------------------------------------------------------
    def test_j_repeated_flush_cycles_never_open_the_gate(self, tmp_path):
        """J: two or more flush cycles leave the gate shut."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, _ = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p.__setitem__("snapshot", []),
            )
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        for cycle_no in range(3):
            engine.flush_pending_closes()
            assert engine.journal_read_blocked is True, cycle_no
            assert engine.durability_risk is True, cycle_no
            assert engine.accounting_consistent is False, cycle_no
            assert path.exists(), cycle_no
            assert store.get_position(key).status == POSITION_STATUS_OPEN, (
                cycle_no,
            )

    # -- K ----------------------------------------------------------------
    def test_k_repeated_restarts_keep_the_damaged_record(self, tmp_path):
        """K: every restart re-detects the damage and keeps the file."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, _ = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p["snapshot"].pop("position_key", None),
            )
        )
        for attempt in range(3):
            restarted = _restart(
                contour, settings, clock, journal_dir=tmp_path,
            )
            engine = restarted.engine
            assert engine.journal_read_blocked is True, attempt
            assert engine.durability_risk is True, attempt
            assert engine.accounting_consistent is False, attempt
            assert "JOURNAL_SNAPSHOT_RESTORE_FAILED" in (
                engine.journal_read_failure or ""
            ), attempt
            assert path.exists(), attempt
            assert store.get_position(key).status == POSITION_STATUS_OPEN, (
                attempt,
            )

    # -- L ----------------------------------------------------------------
    def test_l_repair_then_retry_restores_the_original_stop(self, tmp_path):
        """L: after a HAND repair the retry restores the frozen STOP."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p.__setitem__("snapshot", None),
            )
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True

        path.write_bytes(original)          # manual repair, never automatic
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None
        assert cycle["exit_reasons"] == [], "from the snapshot, not data"
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert row.exit_price == pytest.approx(
            json.loads(original)["fill_price"], rel=1e-12,
        ), "the frozen fill price, never recomputed"
        assert engine.journal.exists(key) is False, "retired after recovery"
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True

    # -- M ----------------------------------------------------------------
    def test_m_a_new_price_never_reactivates_the_frozen_stop(self, tmp_path):
        """M: even a price that would no longer fire the STOP recovers the
        FROZEN stop after the repair."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p.__setitem__("snapshot", None),
            )
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True
        path.write_bytes(original)

        non_triggering = _quiescent_bar(pos, entry)
        cycle = restarted.run_cycle({pos.symbol: entry}, bars=non_triggering)
        assert cycle["exit_reasons"] == [], "from the snapshot, not data"
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert row.exit_price == pytest.approx(
            json.loads(original)["fill_price"], rel=1e-12,
        )

    # -- N ----------------------------------------------------------------
    def test_n_ambiguous_commit_never_duplicates_the_terminal_leg(
        self, tmp_path,
    ):
        """N: a terminal UPDATE whose acknowledgement is lost still yields
        exactly one terminal leg and one CLOSED row."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p.__setitem__("snapshot", None),
            )
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        path.write_bytes(original)

        real_update = store.update_position

        def _update_then_lose_ack(snapshot):
            real_update(snapshot)
            raise RuntimeError("injected lost acknowledgement (Round 12 N)")

        store.update_position = _update_then_lose_ack
        try:
            restarted.run_cycle(
                {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
            )
        finally:
            store.update_position = real_update

        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        legs = store.list_close_legs(key)
        assert [(leg.exit_reason, bool(leg.is_terminal)) for leg in legs] == [
            ("STOP_LOSS", True),
        ], "exactly one terminal leg"
        assert engine.pending_closes == {}
        assert engine.journal.exists(key) is False

        # a normal cycle afterwards must not add anything
        restarted.run_cycle({pos.symbol: entry}, bars=_quiescent_bar(pos, entry))
        assert store.list_close_legs(key) == legs
        assert store.get_position(key).status == POSITION_STATUS_CLOSED

    # -- O ----------------------------------------------------------------
    def test_o_partial_tp_plus_stop_keeps_quantity_conservation(
        self, tmp_path,
    ):
        """O: an unstaged partial TP frozen into the record is restored with
        the STOP and the quantities stay conserved."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        original_size = pos.position_size
        # staging is down from the very first partial TP: the TP leg stays
        # DIRTY in memory and is frozen into the protective-close record
        original_stage = store.stage_close
        store.stage_close = _always_fail("staging down for the whole run")

        # partial TP1 first (price falls through +5% for the inverse SHORT)
        tp1_cycle = contour.run_cycle({pos.symbol: entry}, bars={
            pos.symbol: {"high": entry * 1.05, "low": entry * 0.94},
        })
        assert pos.tp1_executed is True, tp1_cycle
        partial_size = pos.position_size - pos.remaining_size
        assert partial_size > 0.0, "the TP really consumed part of the size"
        assert pos.remaining_size < original_size

        # then the protective STOP, journaled with staging still down
        stop_cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert stop_cycle["exit_reasons"] == ["STOP_LOSS"], stop_cycle
        key = pos.position_key
        record_path = contour.engine.journal._path(key)
        record_payload = json.loads(record_path.read_text(encoding="utf-8"))
        assert record_payload["snapshot"]["remaining_size"] == 0.0, (
            "the frozen snapshot is the TERMINAL state of the close",
        )
        original = record_path.read_bytes()

        # damage the nested snapshot, restart, then hand-repair
        record_payload["snapshot"].pop("position_key", None)
        record_path.write_text(
            json.dumps(record_payload), encoding="utf-8",
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True
        assert engine.pending_closes == {}

        record_path.write_bytes(original)
        store.stage_close = original_stage
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert engine.journal_read_blocked is False
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        legs = store.list_close_legs(key)
        assert [leg.exit_reason for leg in legs] == [
            "TAKE_PROFIT_1", "STOP_LOSS",
        ], legs
        closed_total = sum(leg.quantity for leg in legs)
        assert closed_total == pytest.approx(original_size, abs=1e-9), (
            "quantity conservation: partial + terminal == original size",
        )
        assert row.remaining_size == pytest.approx(0.0, abs=1e-9)
        assert engine.journal.exists(key) is False
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- P ----------------------------------------------------------------
    def test_p_observed_funding_is_charged_exactly_once(self, tmp_path):
        """P: the OBSERVED funding frozen into the record reaches the
        CLOSED row exactly once when the damaged record is repaired — never
        re-derived, never charged twice."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p["snapshot"].pop("position_key", None),
                advance_hours=8, funding_rate=0.01,
                fail_obligation_write=True,
            )
        )
        record = contour.engine.journal.read_all()[key]
        frozen_periods = int(record.snapshot.get("funding_periods") or 0)
        frozen_paid = float(record.snapshot.get("funding_paid") or 0.0)
        assert frozen_periods >= 1, "an observed period must be frozen in"
        assert frozen_paid != 0.0

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True
        assert key not in engine._pending_obligations

        path.write_bytes(original)
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert engine.journal_read_blocked is False
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert int(row.funding_periods_charged) == frozen_periods, (
            "the frozen funding is restored exactly once, never recomputed",
        )
        assert row.funding_paid == pytest.approx(frozen_paid, rel=1e-12)
        assert store.list_pending_closes() == []
        assert engine.journal.exists(key) is False

        # further cycles must never charge it a second time
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        again = store.get_position(key)
        assert int(again.funding_periods_charged) == frozen_periods
        assert again.funding_paid == pytest.approx(frozen_paid, rel=1e-12)
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- Q ----------------------------------------------------------------
    def test_q_valid_journal_recovers_normally(self, tmp_path):
        """Q: an undamaged journal is untouched by the new validation."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None

        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == []
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.exit_reason == "STOP_LOSS"
        assert engine.journal.exists(key) is False
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True

    # -- R ----------------------------------------------------------------
    def test_r_mixed_records_never_report_global_recovery_success(
        self, tmp_path,
    ):
        """R: a healthy record next to a damaged one restores the healthy
        half but never reports the whole recovery as finished."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        victim = _open_one(contour, store, clock, symbol="BTCUSDT")
        other = _open_one(contour, store, clock, symbol="ETHUSDT")
        entry = victim.entry_price

        # the protective STOP with staging down -> the durable record
        original_stage = store.stage_close
        store.stage_close = _always_fail("terminal staging failure")
        cycle = contour.run_cycle(
            {victim.symbol: entry},
            bars=_round8_stop_bars(victim, entry),
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        store.stage_close = original_stage
        key = victim.position_key
        journal = contour.engine.journal

        # a SECOND record, valid on the outside, damaged in its NESTED
        # snapshot, bound to a position that really exists (so it is never
        # mistaken for a provable orphan)
        healthy_payload = json.loads(
            journal._path(key).read_text(encoding="utf-8"),
        )
        damaged_key = other.position_key
        damaged_payload = dict(healthy_payload)
        damaged_payload["position_key"] = damaged_key
        damaged_payload["snapshot"] = dict(healthy_payload["snapshot"])
        damaged_payload["snapshot"].pop("position_key", None)
        damaged = journal._path(damaged_key)
        damaged.write_text(json.dumps(damaged_payload), encoding="utf-8")

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        assert "JOURNAL_SNAPSHOT_RESTORE_FAILED" in (
            engine.journal_read_failure or ""
        ), engine.journal_read_failure
        assert key in engine.pending_closes, "the healthy half is restored"
        assert damaged_key not in engine.pending_closes
        assert damaged.exists(), "the damaged file is never deleted"

        cycle = restarted.run_cycle(
            {victim.symbol: entry}, bars=_round8_stop_bars(victim, entry),
        )
        assert cycle["entries"] == 0, "no false global recovery success"
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        assert store.get_position(key).status == POSITION_STATUS_CLOSED, (
            "the healthy record did recover",
        )
        assert store.get_position(damaged_key).status == POSITION_STATUS_OPEN

    # -- S ----------------------------------------------------------------
    def test_s_apply_failure_leaves_no_dangerous_partial_state(
        self, tmp_path,
    ):
        """S: an error INSIDE the apply phase is rolled back and reported as
        an explicit unresolved obligation — never as a finished recovery."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = (
            _round10_stop_setup(settings, tmp_path)
        )
        store.stage_close = stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is False
        assert key in engine.pending_closes, "the healthy replay applied"

        # rewind: drop the applied state, so the next replay has to go
        # through the whole APPLY phase again.  Every container is read
        # defensively so the scenario itself runs on pre-Round-12 sources
        # too (only the assertions afterwards differ).
        engine.pending_closes.clear()
        for _attr in (
            "_uncommitted_deltas", "_journal_records", "_journal_snapshots",
            "_terminal_legs",
        ):
            _container = getattr(engine, _attr, None)
            if _container is not None:
                _container.pop(key, None)
        for _attr in ("_journal_restore_ok", "_unstaged_closes"):
            _container = getattr(engine, _attr, None)
            if _container is not None:
                _container.discard(key)
        engine.open_positions.pop(key, None)

        class _ExplodingOpenPositions(dict):
            def pop(self, *args, **kwargs):  # noqa: D102 - fault injection
                raise RuntimeError("injected apply-phase failure (Round 12 S)")

        original_open = engine.open_positions
        engine.open_positions = _ExplodingOpenPositions(original_open)
        try:
            engine._replay_journal()
        finally:
            engine.open_positions = dict(original_open)

        # rollback: no half-applied containers may survive
        assert key not in engine.pending_closes, "pending_closes rolled back"
        assert key not in engine._uncommitted_deltas, "delta rolled back"
        assert key not in engine._unstaged_closes, "unstaged rolled back"
        assert key not in engine._journal_records, "record rolled back"
        assert key not in engine._journal_restore_ok, "restore-ok rolled back"
        # ... and the failure is EXPLICIT: fail closed + retried
        assert key in engine._journal_restore_failed
        assert key in engine._journal_unresolved
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        assert engine.journal.exists(key), "the record is never deleted"
        assert store.get_position(key).status == POSITION_STATUS_OPEN

    # -- T ----------------------------------------------------------------
    def test_t_retirement_happens_only_after_ledger_confirmation(
        self, tmp_path,
    ):
        """T: the record is deleted only once the durable row carries the
        confirmed terminal close and the ledger agrees."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p.__setitem__("snapshot", None),
            )
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True
        assert path.exists()

        # repair -> the close commits -> only THEN may the record go
        path.write_bytes(original)
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED, "close confirmed first"
        assert row.exit_reason == "STOP_LOSS"
        assert engine.journal.exists(key) is False, (
            "retirement only after the durable confirmation",
        )
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)
        # Round 12 bookkeeping (new on this branch) — after the behaviour
        # so a baseline run fails on evidence, not on a missing attribute
        assert engine._journal_records == {}
        assert engine._journal_snapshots == {}
        assert engine._journal_restore_ok == set()

        # the ledger must still agree after further cycles
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        _assert_ledger_consistent(restarted)


# ---------------------------------------------------------------------------
# Round 13 (P2, Codex r4599970100): REQUIRED financial snapshot validation
# and journal retirement integrity — mandatory scenarios A–R.
# ---------------------------------------------------------------------------
class TestRound13RequiredFinancialSnapshotValidation:
    """Mandatory scenarios A–R for Codex thread r4599970100.

    The reproduced defect: dropping ``snapshot.net_pnl`` made
    ``InverseShortPosition.from_dict()`` default it to 0.0, so Phase A
    accepted the record, recovery persisted a CLOSED row with
    ``net_pnl=0.0`` against a terminal leg of ≈ -50.85, the journal was
    deleted and ``accounting_consistent`` turned True.  Every negative
    test below asserts the full fail-closed contract: no unhandled
    exception, ``durability_risk=True``, ``accounting_consistent=False``,
    entries refused, no re-exit-evaluation, journal preserved.
    """

    # Round 12 machinery, reused unchanged (same damage + fail-closed
    # contract so both rounds stay directly comparable).
    @staticmethod
    def _damaged_stop(settings, tmp_path, damage, **setup_kwargs):
        return TestRound12AtomicJournalReplayAndSafeRetirement._damaged_stop(
            settings, tmp_path, damage, **setup_kwargs,
        )

    @staticmethod
    def _assert_fail_closed(restarted, engine, store, pos, key, entry, path):
        return (
            TestRound12AtomicJournalReplayAndSafeRetirement._assert_fail_closed(
                restarted, engine, store, pos, key, entry, path,
            )
        )

    # -- the reproduced Codex scenario (§2 of the finding) -----------------
    def test_a_codex_reproduced_zero_pnl_close_never_happens(self, tmp_path):
        """Codex's exact reproduction: only ``snapshot.net_pnl`` is removed.
        Before the fix the recovery closed the position with ``net_pnl=0.0``
        while the terminal leg kept ≈ -50.85, deleted the journal and
        reported ``accounting_consistent=True``.  After the fix the record
        stays fail-closed until it is repaired by hand — and the repaired
        recovery restores the FROZEN loss, never a substituted zero."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p["snapshot"].pop("net_pnl", None),
            )
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        leg_net = float(payload["terminal_leg"]["net_pnl"])
        assert "net_pnl" not in payload["snapshot"], "the field really is gone"
        assert leg_net < 0.0, "the frozen close is a real loss"

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        # trigger the replay first so the baseline failure is the DEFECT,
        # not a missing diagnostic
        restarted.run_cycle({pos.symbol: entry}, bars=_round8_stop_bars(pos, entry))
        row = store.get_position(key)
        assert not (
            row.status == POSITION_STATUS_CLOSED and row.net_pnl == 0.0
        ), (
            f"recovered net_pnl={row.net_pnl!r} while the frozen terminal "
            f"leg says {leg_net!r} — a damaged snapshot may never close "
            "with a substituted zero",
        )
        assert path.exists(), "the journal is deleted over a lost PnL only"
        assert engine.accounting_consistent is False
        assert engine.durability_risk is True
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        reason = engine.journal_read_failure or ""
        assert "JOURNAL_SNAPSHOT_RESTORE_FAILED" in reason, reason
        assert "JOURNAL_FINANCIAL_FIELD_MISSING:net_pnl" in reason, reason

        # hand repair -> the FROZEN loss is restored exactly once (the
        # POSITION net — never the 0.0 substitute, and never one leg's
        # amount: with a partial leg those legitimately differ)
        frozen_net = json.loads(original.decode("utf-8"))["snapshot"]["net_pnl"]
        path.write_bytes(original)
        restarted.run_cycle({pos.symbol: entry}, bars=_quiescent_bar(pos, entry))
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.net_pnl == pytest.approx(frozen_net, rel=1e-12, abs=1e-9), (
            "the frozen financial outcome, never 0.0",
        )
        assert row.net_pnl != 0.0
        assert engine.journal.exists(key) is False
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- A–H plus the audit-driven extras ---------------------------------
    @pytest.mark.parametrize(
        ("case", "damage", "code", "stage"),
        [
            (
                "A_net_pnl_missing",
                lambda p: p["snapshot"].pop("net_pnl", None),
                "JOURNAL_FINANCIAL_FIELD_MISSING:net_pnl",
                "snapshot_financial",
            ),
            (
                "B_net_pnl_null",
                lambda p: p["snapshot"].__setitem__("net_pnl", None),
                "JOURNAL_FINANCIAL_FIELD_INVALID:net_pnl",
                "snapshot_financial",
            ),
            (
                "C_net_pnl_nan",
                lambda p: p["snapshot"].__setitem__("net_pnl", float("nan")),
                "JOURNAL_FINANCIAL_FIELD_INVALID:net_pnl",
                "snapshot_financial",
            ),
            (
                "D_net_pnl_infinite",
                lambda p: p["snapshot"].__setitem__("net_pnl", float("inf")),
                "JOURNAL_FINANCIAL_FIELD_INVALID:net_pnl",
                "snapshot_financial",
            ),
            (
                "E_net_pnl_wrong_type",
                lambda p: p["snapshot"].__setitem__("net_pnl", "-50.85"),
                "JOURNAL_FINANCIAL_FIELD_INVALID:net_pnl",
                "snapshot_financial",
            ),
            (
                "F_gross_pnl_missing",
                lambda p: p["snapshot"].pop("gross_pnl", None),
                "JOURNAL_FINANCIAL_FIELD_MISSING:gross_pnl",
                "snapshot_financial",
            ),
            (
                "G_funding_paid_missing",
                lambda p: p["snapshot"].pop("funding_paid", None),
                "JOURNAL_FINANCIAL_FIELD_MISSING:funding_paid",
                "snapshot_financial",
            ),
            (
                "H_realized_legs_net_missing",
                lambda p: p["snapshot"].pop("realized_legs_net", None),
                "JOURNAL_FINANCIAL_FIELD_MISSING:realized_legs_net",
                "snapshot_financial",
            ),
            # audit-driven extras: every models.from_dict() default that a
            # CLOSED recovery snapshot must carry explicitly
            (
                "X_exit_price_missing",
                lambda p: p["snapshot"].pop("exit_price", None),
                "JOURNAL_FINANCIAL_FIELD_MISSING:exit_price",
                "snapshot_financial",
            ),
            (
                "X_entry_cost_allocated_missing",
                lambda p: p["snapshot"].pop("entry_cost_allocated", None),
                "JOURNAL_FINANCIAL_FIELD_MISSING:entry_cost_allocated",
                "snapshot_financial",
            ),
            (
                "X_entry_slippage_missing",
                lambda p: p["snapshot"].pop("entry_slippage", None),
                "JOURNAL_FINANCIAL_FIELD_MISSING:entry_slippage",
                "snapshot_financial",
            ),
            (
                "X_funding_periods_negative",
                lambda p: p["snapshot"].__setitem__("funding_periods", -1),
                "JOURNAL_FINANCIAL_FIELD_INVALID:funding_periods",
                "snapshot_financial",
            ),
            (
                "X_remaining_size_nonzero",
                lambda p: p["snapshot"].__setitem__(
                    "remaining_size",
                    p["snapshot"].get("position_size", 1.0),
                ),
                "JOURNAL_FINANCIAL_FIELD_INVALID:remaining_size",
                "snapshot_financial_consistency",
            ),
            (
                "X_terminal_leg_net_pnl_missing",
                lambda p: p["terminal_leg"].pop("net_pnl", None),
                "JOURNAL_FINANCIAL_FIELD_MISSING:terminal_leg.net_pnl",
                "terminal_leg_financial",
            ),
        ],
    )
    def test_required_financial_field_fails_closed(
        self, tmp_path, case, damage, code, stage,
    ):
        """A–H + audit extras: presence, type, null, NaN/±Infinity and
        value checks run in Phase A — before the permissive deserializer,
        before any engine state exists."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage_, path, original = (
            self._damaged_stop(settings, tmp_path, damage)
        )
        damaged_bytes = path.read_bytes()
        payload = json.loads(original.decode("utf-8"))
        leg_net = float(payload["terminal_leg"]["net_pnl"])
        assert leg_net < 0.0, "the frozen close is a real loss"

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        # behavioural contract first (so a baseline run fails on the
        # behaviour, not on a missing diagnostic)
        self._assert_fail_closed(
            restarted, engine, store, pos, key, entry, path,
        )
        reason = engine.journal_read_failure or ""
        assert code in reason, (case, reason)
        assert f"stage={stage}" in reason, (case, reason)
        # Phase A touched NOTHING
        assert engine.pending_closes == {}, case
        assert engine._journal_records == {}, case
        assert key not in engine._pending_obligations, case
        assert engine._journal_restore_ok == set(), case
        # a second flush cycle cannot change the verdict either
        engine.flush_pending_closes()
        assert path.read_bytes() == damaged_bytes, "the record is byte-stable"
        assert store.get_position(key).status == POSITION_STATUS_OPEN, (
            f"{case}: a damaged snapshot may never produce a CLOSED row",
        )

    # -- I ----------------------------------------------------------------
    def test_i_valid_financial_snapshot_recovers(self, tmp_path):
        """I: an untouched, fully valid snapshot replays, closes and
        retires — the strict validation never rejects healthy records."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(settings, tmp_path, lambda p: None)
        )
        frozen = json.loads(original.decode("utf-8"))["snapshot"]
        assert frozen["net_pnl"] < 0.0, "a real losing STOP"

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is False, engine.journal_read_failure
        assert engine.journal_read_failure is None
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == [], "from the snapshot, not data"
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.net_pnl == pytest.approx(frozen["net_pnl"], rel=1e-12)
        assert row.gross_pnl == pytest.approx(frozen["gross_pnl"], rel=1e-12)
        assert row.realized_legs_net == pytest.approx(
            frozen["realized_legs_net"], rel=1e-12,
        )
        assert engine.journal.exists(key) is False, "retired after recovery"
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- J ----------------------------------------------------------------
    def test_j_position_financial_mismatch_blocks_retirement(self, tmp_path):
        """J: a durable row whose financial outcome disagrees with the
        frozen record never retires the journal — and the record becomes
        restorable again the moment the mismatch is gone (no deadlock)."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(settings, tmp_path, lambda p: None)
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        real_get = store.get_position
        state = {"tampered": False}

        def tampered_get(position_key, *args, **kwargs):
            row = real_get(position_key, *args, **kwargs)
            if state["tampered"] and position_key == key and row is not None:
                return dataclasses.replace(row, net_pnl=0.0)
            return row

        store.get_position = tampered_get
        state["tampered"] = True
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        state["tampered"] = False
        store.get_position = real_get

        assert real_get(key).status == POSITION_STATUS_CLOSED, (
            "the close itself must commit",
        )
        assert path.exists(), "a financial mismatch never retires the record"
        assert key in engine._journal_unresolved
        assert engine.durability_risk is True
        blocked = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert blocked["entries"] == 0, "entries stay refused while unresolved"

        # the cause is removed -> the very same record retires (no deadlock)
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            "restorable after the error cause is removed",
        )
        assert engine.durability_risk is False
        _assert_ledger_consistent(restarted)

    # -- K ----------------------------------------------------------------
    def test_k_partial_leg_mismatch_blocks_retirement(self, tmp_path):
        """K: a durable PARTIAL leg whose financial amount disagrees with
        the frozen record blocks retirement too (owed legs included)."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        key = pos.position_key
        original_commit = store.commit_partial_exit
        original_stage = store.stage_close

        # (1) the TP1 commit fails -> the partial leg stays DIRTY in
        #     memory and is owed by the protective-close record
        store.commit_partial_exit = _always_fail("TP1 persistence failure")
        tp1_cycle = contour.run_cycle({pos.symbol: entry * 0.945})
        assert key in contour.engine.dirty_leg_keys, tp1_cycle

        # (2) staging down as well -> the STOP is journaled with the dirty
        #     TP1 owed and the terminal UPDATE is deferred (row stays OPEN)
        store.stage_close = _always_fail("staging down for the whole run")
        stop_cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert stop_cycle["exit_reasons"] == ["STOP_LOSS"], stop_cycle
        path = contour.engine.journal._path(key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["owed_partial_legs"], "the dirty TP1 is owed"
        assert store.get_position(key).status == POSITION_STATUS_OPEN, (
            "the deferred UPDATE keeps the row OPEN for the restart replay",
        )

        store.commit_partial_exit = original_commit
        store.stage_close = original_stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        real_leg = store.get_close_leg
        state = {"tampered": False}

        def tampered_get_close_leg(position_key, leg_seq, *args, **kwargs):
            leg = real_leg(position_key, leg_seq, *args, **kwargs)
            if state["tampered"] and leg is not None and int(leg.leg_seq) == 1:
                return dataclasses.replace(leg, net_pnl=leg.net_pnl + 5.0)
            return leg

        store.get_close_leg = tampered_get_close_leg
        state["tampered"] = True
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        state["tampered"] = False
        store.get_close_leg = real_leg

        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert path.exists(), "a partial-leg mismatch never retires the record"
        assert key in engine._journal_unresolved
        assert engine.durability_risk is True

        restarted.run_cycle(
            {pos.symbol: entry}, bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            "restorable after the leg mismatch is removed",
        )
        assert engine.durability_risk is False
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- L ----------------------------------------------------------------
    def test_l_funding_mismatch_fails_closed_without_loss_or_duplication(
        self, tmp_path,
    ):
        """L: a snapshot whose funding total contradicts its own frozen
        terminal leg fails closed — nothing booked (no duplication) and
        the record kept (no loss)."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p["snapshot"].__setitem__(
                    "funding_paid",
                    float(p["snapshot"].get("funding_paid") or 0.0) + 7.0,
                ),
            )
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        self._assert_fail_closed(
            restarted, engine, store, pos, key, entry, path,
        )
        reason = engine.journal_read_failure or ""
        assert "JOURNAL_FINANCIAL_FIELD_INVALID:funding_paid" in reason, reason
        assert "stage=snapshot_financial_consistency" in reason, reason
        # no loss: the record survives; no duplication: nothing was booked
        assert path.exists()
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_OPEN
        assert row.funding_paid == 0.0, "funding is never booked twice"
        assert row.funding_periods_charged == 0
        assert key not in engine._pending_obligations

    # -- M ----------------------------------------------------------------
    def test_m_two_flush_cycles_keep_the_record(self, tmp_path):
        """M: two consecutive flush cycles with an unresolved financial
        error keep the journal and the closed gate."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p["snapshot"].pop("net_pnl", None),
            )
        )
        damaged = path.read_bytes()
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        for cycle_no in range(2):
            assert engine.flush_pending_closes() == 0, cycle_no
            assert engine.journal_read_blocked is True, cycle_no
            assert engine.durability_risk is True, cycle_no
            assert engine.accounting_consistent is False, cycle_no
            assert path.read_bytes() == damaged, cycle_no
            assert store.get_position(key).status == POSITION_STATUS_OPEN, (
                cycle_no,
            )

    # -- N ----------------------------------------------------------------
    def test_n_repeated_restarts_keep_the_fail_closed_verdict(self, tmp_path):
        """N: every restart re-detects the financial damage and keeps it."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p["snapshot"].pop("net_pnl", None),
            )
        )
        for attempt in range(3):
            restarted = _restart(
                contour, settings, clock, journal_dir=tmp_path,
            )
            engine = restarted.engine
            assert engine.journal_read_blocked is True, attempt
            assert engine.durability_risk is True, attempt
            assert engine.accounting_consistent is False, attempt
            reason = engine.journal_read_failure or ""
            assert "JOURNAL_FINANCIAL_FIELD_MISSING:net_pnl" in reason, attempt
            assert path.exists(), attempt
            assert store.get_position(key).status == POSITION_STATUS_OPEN, (
                attempt,
            )

    # -- O ----------------------------------------------------------------
    def test_o_manual_repair_restores_the_frozen_financial_result(
        self, tmp_path,
    ):
        """O: after the hand repair the retry restores the frozen close —
        the original net_pnl, never a recomputed or zeroed one."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p["snapshot"].pop("net_pnl", None),
            )
        )
        frozen = json.loads(original.decode("utf-8"))["snapshot"]
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal_read_blocked is True

        path.write_bytes(original)          # manual repair, never automatic
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == [], "from the snapshot, not data"
        assert engine.journal_read_blocked is False
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        assert row.net_pnl == pytest.approx(frozen["net_pnl"], rel=1e-12)
        assert row.net_pnl != 0.0, "the frozen loss, never a substituted 0.0"
        assert row.exit_price == pytest.approx(
            json.loads(original)["fill_price"], rel=1e-12,
        )
        assert engine.journal.exists(key) is False
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- P ----------------------------------------------------------------
    def test_p_ambiguous_commit_keeps_the_frozen_result_exactly_once(
        self, tmp_path,
    ):
        """P: an ambiguous terminal COMMIT (ack lost after the write) still
        yields exactly one terminal leg with the frozen financial result."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p["snapshot"].pop("net_pnl", None),
            )
        )
        frozen = json.loads(original.decode("utf-8"))["snapshot"]
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        path.write_bytes(original)

        real_update = store.update_position
        state = {"ack_lost": False}

        def update_then_lose_ack(snapshot, *args, **kwargs):
            out = real_update(snapshot, *args, **kwargs)
            if not state["ack_lost"]:
                state["ack_lost"] = True
                raise RuntimeError("ambiguous COMMIT (ack lost after the write)")
            return out

        store.update_position = update_then_lose_ack
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        store.update_position = real_update
        assert state["ack_lost"] is True, "the ambiguity really fired"
        # the retry cycle confirms the identical write
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        legs = store.list_close_legs(key)
        assert [(leg.exit_reason, bool(leg.is_terminal)) for leg in legs] == [
            ("STOP_LOSS", True),
        ], legs
        assert row.net_pnl == pytest.approx(frozen["net_pnl"], rel=1e-12)
        assert engine.journal.exists(key) is False
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- Q ----------------------------------------------------------------
    def test_q_a_new_price_never_re_evaluates_the_frozen_stop(self, tmp_path):
        """Q: while the financial damage is unresolved, a NEW price that
        would breach the STOP never re-evaluates the frozen exit."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(
                settings, tmp_path,
                lambda p: p["snapshot"].pop("net_pnl", None),
            )
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        new_price_breach = {
            pos.symbol: {"high": pos.stop_price * 1.05, "low": entry * 0.97},
        }
        cycle = restarted.run_cycle(
            {pos.symbol: entry * 1.03}, bars=new_price_breach,
        )
        assert cycle["exit_reasons"] == [], "the STOP is never re-derived"
        assert cycle["entries"] == 0, "entries stay refused"
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_OPEN
        assert row.exit_price is None, "no exit may be invented from data"
        assert path.exists()
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False

    # -- R ----------------------------------------------------------------
    def test_r_retirement_waits_for_a_confirmed_read_back(self, tmp_path):
        """R: a successful recovery retires the record ONLY after the
        durable read-back confirms it — an unconfirmed row keeps the file."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(settings, tmp_path, lambda p: None)
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        real_get = store.get_position
        state = {"unconfirmed": False}

        def unconfirmed_get(position_key, *args, **kwargs):
            row = real_get(position_key, *args, **kwargs)
            if (
                state["unconfirmed"]
                and position_key == key
                and row is not None
                and row.status == POSITION_STATUS_CLOSED
            ):
                return dataclasses.replace(row, status=POSITION_STATUS_OPEN)
            return row

        store.get_position = unconfirmed_get
        state["unconfirmed"] = True
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        state["unconfirmed"] = False
        store.get_position = real_get

        assert real_get(key).status == POSITION_STATUS_CLOSED
        assert path.exists(), "no retirement without a confirmed read-back"
        assert engine.durability_risk is True

        restarted.run_cycle(
            {pos.symbol: entry}, bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            "retired only after the confirmation",
        )
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_ledger_consistent(restarted)


# ---------------------------------------------------------------------------
# Round 14 (P2, Codex r4601316564 / r4601316573 / r4601316586): the CLOSED
# replay never retires without the durable proof, Phase A validates the
# frozen financial ARITHMETIC, and the retirement proof compares EVERY
# frozen leg financial field — mandatory scenarios A–D.
# ---------------------------------------------------------------------------
class TestRound14RetirementProofAndFinancialInvariants:
    """Mandatory scenarios A–D for Codex threads r4601316564/73/86.

    The reproduced defects on the baseline (``43179a2``):

    * **A (r4601316564)** — Phase B's CLOSED branch called
      ``journal.delete(key)`` WITHOUT the durable proof the sweep
      requires: a second flush (or a restart) against a CLOSED row whose
      financials disagree retired the record anyway, dropped
      ``_journal_unresolved`` and reopened ``accounting_consistent``.
    * **B (r4601316573)** — Phase A validated field PRESENCE plus two
      position-level identities but never the frozen arithmetic of the
      legs: a terminal leg with ``net_pnl=12345.0`` (or a shifted
      ``gross_pnl``/``exit_fee``/``exit_slippage``/``entry_cost_share``)
      passed Phase A, was staged into the durable ledger and retired.
    * **C (r4601316586)** — the retirement proof compared only
      ``net_pnl``/``fill_price``/``quantity`` per leg: a durable terminal
      leg with a shifted ``exit_fee`` (or any other frozen field) retired
      the record, and a MISSING terminal leg was ``pass``-ed as degraded
      mode without proving migration 088 was actually absent.
    * **D** — the documented exceptions (ambiguous COMMIT, unreadable
      journal, migration-088 degraded mode, confirmed-absent) keep their
      exact existing behaviour.

    Every negative test asserts the full fail-closed contract: no
    unhandled exception, ``journal_read_blocked``/``durability_risk``
    True, ``accounting_consistent`` False, entries refused, no
    re-evaluation of the frozen STOP, the record preserved byte-for-byte,
    and retirement only after the cause is removed (no deadlock).
    """

    # Round 12 machinery, reused unchanged (same damage + fail-closed
    # contract so all rounds stay directly comparable).
    @staticmethod
    def _damaged_stop(settings, tmp_path, damage, **setup_kwargs):
        return TestRound12AtomicJournalReplayAndSafeRetirement._damaged_stop(
            settings, tmp_path, damage, **setup_kwargs,
        )

    # -- A: the CLOSED replay never retires without the durable proof -----
    def test_a1_closed_replay_never_retires_against_a_living_mismatch(
        self, tmp_path,
    ):
        """A1/A2: the SECOND flush against a CLOSED row whose financials
        disagree keeps the record, the unresolved obligation and the gate —
        never the baseline's proof-less ``journal.delete``."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(settings, tmp_path, lambda p: None)
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        real_get = store.get_position
        state = {"tampered": True}

        def tampered_get(position_key, *args, **kwargs):
            row = real_get(position_key, *args, **kwargs)
            if (
                state["tampered"] and position_key == key
                and row is not None and row.status == POSITION_STATUS_CLOSED
            ):
                return dataclasses.replace(row, net_pnl=0.0)
            return row

        store.get_position = tampered_get

        # cycle 1: the close commits; the sweep refuses the mismatch.
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert real_get(key).status == POSITION_STATUS_CLOSED, (
            "the close itself must commit",
        )
        assert path.exists(), "the sweep keeps the record over a mismatch"
        assert key in engine._journal_unresolved
        assert engine.durability_risk is True

        # a fresh event so "entries refused" below is a REAL assertion
        clock.advance(minutes=5)
        assert _capture(
            store, _candidate("ETHUSDT", 50.0, detected_at=clock()),
        ) == "CAPTURED"

        # cycle 2: the CLOSED replay must not retire against the mismatch
        # (baseline defect: journal.delete + _journal_unresolved discarded).
        blocked = restarted.run_cycle(
            {pos.symbol: entry, "ETHUSDT": 50.0},
            bars=_round8_stop_bars(pos, entry),
        )
        assert path.exists(), (
            "a CLOSED replay never retires the record against a financial "
            "mismatch",
        )
        assert key in engine._journal_unresolved, (
            "the unresolved obligation is never dropped by the replay",
        )
        assert engine.journal_read_blocked is True, (
            "the refusal must close the whole recovery gate",
        )
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        reason = engine.journal_read_failure or ""
        assert "JOURNAL_SNAPSHOT_RESTORE_FAILED" in reason, reason
        assert "JournalRetirementUnconfirmed" in reason, reason
        assert blocked["exit_reasons"] == [], "no re-evaluation of the STOP"
        assert blocked["entries"] == 0, "new entries stay refused"

        # C6: the verdict is byte-stable over a repeated flush
        damaged = path.read_bytes()
        engine.flush_pending_closes()
        assert path.read_bytes() == damaged, "the file is never rewritten"
        assert engine.journal_read_blocked is True
        assert store.get_position(key).status == POSITION_STATUS_CLOSED

        # A3/A5 (C7): the cause is removed -> the very same record retires
        # through the proof, the gate reopens and the entry resumes.
        state["tampered"] = False
        store.get_position = real_get
        recovered = restarted.run_cycle(
            {pos.symbol: entry, "ETHUSDT": 50.0},
            bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            "restorable after the mismatch is removed (no deadlock)",
        )
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None
        assert engine._journal_unresolved == set()
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        assert recovered["entries"] == 1, "the gate really reopened"
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    def test_a2_restart_against_a_closed_row_with_a_mismatch_fails_closed(
        self, tmp_path,
    ):
        """A3: a RESTART against a CLOSED row whose financials disagree
        fails closed at boot — the baseline deleted the record there."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(settings, tmp_path, lambda p: None)
        )
        first = _restart(contour, settings, clock, journal_dir=tmp_path)
        real_get = store.get_position
        state = {"tampered": True}

        def tampered_get(position_key, *args, **kwargs):
            row = real_get(position_key, *args, **kwargs)
            if (
                state["tampered"] and position_key == key
                and row is not None and row.status == POSITION_STATUS_CLOSED
            ):
                return dataclasses.replace(row, net_pnl=0.0)
            return row

        store.get_position = tampered_get
        first.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert real_get(key).status == POSITION_STATUS_CLOSED
        assert path.exists(), "the sweep keeps the record over a mismatch"
        clock.advance(minutes=5)
        assert _capture(
            store, _candidate("ETHUSDT", 50.0, detected_at=clock()),
        ) == "CAPTURED"

        # the mismatch SURVIVES a process restart: boot must fail closed
        # (baseline: the boot replay deleted the record outright).
        restarted = _restart(first, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert path.exists(), (
            "a restart never retires a record whose financials disagree",
        )
        assert engine.journal_read_blocked is True, (
            "the boot replay must close the recovery gate",
        )
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        reason = engine.journal_read_failure or ""
        assert "JOURNAL_SNAPSHOT_RESTORE_FAILED" in reason, reason
        assert "JournalRetirementUnconfirmed" in reason, reason

        cycle = restarted.run_cycle(
            {pos.symbol: entry, "ETHUSDT": 50.0},
            bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == [], "no re-evaluation of the STOP"
        assert cycle["entries"] == 0, "new entries stay refused"
        assert cycle["durability_risk"] is True
        assert cycle["accounting_consistent"] is False
        assert path.exists()

        # repair -> the boot gate reopens, the record retires, no deadlock
        state["tampered"] = False
        store.get_position = real_get
        recovered = restarted.run_cycle(
            {pos.symbol: entry, "ETHUSDT": 50.0},
            bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            "restorable after the mismatch is removed (no deadlock)",
        )
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None
        assert engine._journal_unresolved == set()
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        assert recovered["entries"] == 1, "the gate really reopened"
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    def test_a4_missing_terminal_leg_refuses_retirement_unless_degraded(
        self, tmp_path,
    ):
        """A4/C5: a CLOSED row whose terminal leg row is GONE keeps the
        record while the ledger is available — and retires only when the
        degraded mode is PROVEN (store probe) or happened in-process."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(settings, tmp_path, lambda p: None)
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        store.get_close_leg = lambda *a, **k: None

        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert store.get_position(key).status == POSITION_STATUS_CLOSED, (
            "the close itself must commit",
        )
        assert path.exists(), (
            "a missing terminal leg row is NEVER degraded mode while the "
            "ledger is available",
        )
        assert key in engine._journal_unresolved
        assert engine.durability_risk is True

        # the replay fails closed too (the gate, not only the sweep)
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert engine.journal_read_blocked is True
        assert engine.accounting_consistent is False
        assert engine.durability_risk is True
        assert path.exists()

        # PROVEN degraded mode (migration 088 genuinely absent): the
        # position row is the only durable half -> retirement allowed.
        store.close_leg_ledger_available = lambda: False
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            "a PROVEN migration-088 degraded mode retires as documented",
        )
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None
        assert engine._journal_unresolved == set()
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_ledger_consistent(restarted)

    # -- B: Phase A validates the frozen financial arithmetic --------------
    @pytest.mark.parametrize(
        ("case", "damage", "code"),
        [
            (
                "B1-terminal-net",
                lambda p: p["terminal_leg"].__setitem__("net_pnl", 12345.0),
                "JOURNAL_FINANCIAL_INVARIANT_MISMATCH:terminal_leg.net_pnl",
            ),
            (
                "B2-snapshot-gross",
                lambda p: p["snapshot"].__setitem__(
                    "gross_pnl", p["snapshot"]["gross_pnl"] + 100.0,
                ),
                "JOURNAL_FINANCIAL_INVARIANT_MISMATCH:net_pnl",
            ),
            (
                "B3-terminal-gross",
                lambda p: p["terminal_leg"].__setitem__(
                    "gross_pnl", p["terminal_leg"]["gross_pnl"] + 100.0,
                ),
                "JOURNAL_FINANCIAL_INVARIANT_MISMATCH:terminal_leg.net_pnl",
            ),
            (
                "B4-terminal-exit-fee",
                lambda p: p["terminal_leg"].__setitem__(
                    "exit_fee", p["terminal_leg"]["exit_fee"] + 0.01,
                ),
                "JOURNAL_FINANCIAL_INVARIANT_MISMATCH:terminal_leg.net_pnl",
            ),
            (
                "B5-terminal-exit-slippage",
                lambda p: p["terminal_leg"].__setitem__(
                    "exit_slippage",
                    p["terminal_leg"]["exit_slippage"] + 0.02,
                ),
                "JOURNAL_FINANCIAL_INVARIANT_MISMATCH:terminal_leg.net_pnl",
            ),
            (
                "B6-terminal-entry-cost-share",
                lambda p: p["terminal_leg"].__setitem__(
                    "entry_cost_share",
                    p["terminal_leg"]["entry_cost_share"] + 0.03,
                ),
                "JOURNAL_FINANCIAL_INVARIANT_MISMATCH:terminal_leg.net_pnl",
            ),
            (
                "B7-terminal-remaining-after",
                lambda p: p["terminal_leg"].__setitem__(
                    "remaining_after", 5.0,
                ),
                "JOURNAL_FINANCIAL_INVARIANT_MISMATCH:"
                "terminal_leg.remaining_after",
            ),
            (
                "B8-terminal-gross-and-net",
                lambda p: (
                    p["terminal_leg"].__setitem__(
                        "gross_pnl", p["terminal_leg"]["gross_pnl"] + 100.0,
                    ),
                    p["terminal_leg"].__setitem__(
                        "net_pnl", p["terminal_leg"]["net_pnl"] + 100.0,
                    ),
                ),
                "JOURNAL_FINANCIAL_INVARIANT_MISMATCH:gross_pnl",
            ),
        ],
    )
    def test_b_phase_a_rejects_broken_frozen_arithmetic(
        self, tmp_path, case, damage, code,
    ):
        """B1–B8: a record whose frozen arithmetic does not close is
        rejected in Phase A with the stable invariant code — nothing is
        staged, nothing is committed, the verdict is byte-stable."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(settings, tmp_path, damage)
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert engine.journal_read_blocked is True, case
        assert engine.durability_risk is True, case
        assert engine.accounting_consistent is False, case
        reason = engine.journal_read_failure or ""
        assert "JOURNAL_SNAPSHOT_RESTORE_FAILED" in reason, (case, reason)
        assert "stage=financial_invariant" in reason, (case, reason)
        assert code in reason, (case, reason)
        assert path.exists(), case
        assert store.get_position(key).status == POSITION_STATUS_OPEN, (
            f"{case}: a record whose arithmetic does not close is never "
            "applied",
        )
        assert cycle["exit_reasons"] == [], case
        assert cycle["entries"] == 0, case
        assert engine.pending_closes == {}, case
        assert engine._journal_restore_failed == {key}, case

        # second flush: byte-identical verdict, still refused
        damaged = path.read_bytes()
        engine.flush_pending_closes()
        assert path.read_bytes() == damaged, case
        assert engine.journal_read_blocked is True, case
        assert store.get_position(key).status == POSITION_STATUS_OPEN, case
        assert path.exists(), case

    # -- C: the COMPLETE durable leg financial comparison ------------------
    @pytest.mark.parametrize(
        ("field", "delta"),
        [
            ("exit_fee", 0.01),
            ("exit_slippage", 0.02),
            ("gross_pnl", 100.0),
            ("entry_cost_share", 0.03),
            ("funding_paid_to_date", 7.0),
            ("cumulative_realized_net", 0.05),
            ("remaining_after", 5.0),
            ("reference_price", 0.1),
        ],
        ids=[
            "exit_fee", "exit_slippage", "gross_pnl", "entry_cost_share",
            "funding_paid_to_date", "cumulative_realized_net",
            "remaining_after", "reference_price",
        ],
    )
    def test_c_terminal_leg_durable_field_mismatch_blocks_retirement(
        self, tmp_path, field, delta,
    ):
        """C1/C2: a durable TERMINAL leg whose frozen field was shifted
        keeps the record (baseline compared only net/fill/quantity) and the
        very same record retires once the row matches again."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(settings, tmp_path, lambda p: None)
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        real_leg = store.get_close_leg
        state = {"tampered": False}

        def tampered_get_close_leg(position_key, leg_seq, *args, **kwargs):
            leg = real_leg(position_key, leg_seq, *args, **kwargs)
            if (
                state["tampered"] and leg is not None
                and int(leg.leg_seq) == LEG_SEQ_TERMINAL
            ):
                return dataclasses.replace(
                    leg, **{field: getattr(leg, field) + delta},
                )
            return leg

        store.get_close_leg = tampered_get_close_leg
        state["tampered"] = True
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        state["tampered"] = False
        store.get_close_leg = real_leg

        assert store.get_position(key).status == POSITION_STATUS_CLOSED, (
            "the close itself must commit",
        )
        assert path.exists(), (
            f"a durable {field} mismatch never retires the record",
        )
        assert key in engine._journal_unresolved
        assert engine.durability_risk is True

        # C7: no deadlock — the same record retires after the cause leaves
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            f"restorable after the {field} mismatch is removed",
        )
        assert engine.journal_read_blocked is False
        assert engine._journal_unresolved == set()
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    def test_c2_owed_partial_leg_durable_mismatch_blocks_retirement(
        self, tmp_path,
    ):
        """C3: a durable OWED partial leg whose frozen exit fee was
        shifted keeps the record — the owed legs are compared field by
        field too, not only net/fill/quantity."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        key = pos.position_key
        original_commit = store.commit_partial_exit
        original_stage = store.stage_close

        # (1) the TP1 commit fails -> the partial leg stays DIRTY in
        #     memory and is owed by the protective-close record
        store.commit_partial_exit = _always_fail("TP1 persistence failure")
        contour.run_cycle({pos.symbol: entry * 0.945})
        assert key in contour.engine.dirty_leg_keys

        # (2) staging down as well -> the STOP is journaled with the dirty
        #     TP1 owed and the terminal UPDATE is deferred (row stays OPEN)
        store.stage_close = _always_fail("staging down for the whole run")
        stop_cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert stop_cycle["exit_reasons"] == ["STOP_LOSS"], stop_cycle
        path = contour.engine.journal._path(key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["owed_partial_legs"], "the dirty TP1 is owed"
        assert store.get_position(key).status == POSITION_STATUS_OPEN

        store.commit_partial_exit = original_commit
        store.stage_close = original_stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        real_leg = store.get_close_leg
        state = {"tampered": False}

        def tampered_get_close_leg(position_key, leg_seq, *args, **kwargs):
            leg = real_leg(position_key, leg_seq, *args, **kwargs)
            if state["tampered"] and leg is not None and int(leg.leg_seq) == 1:
                return dataclasses.replace(leg, exit_fee=leg.exit_fee + 0.01)
            return leg

        store.get_close_leg = tampered_get_close_leg
        state["tampered"] = True
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        state["tampered"] = False
        store.get_close_leg = real_leg

        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert path.exists(), "an owed-leg exit_fee mismatch never retires"
        assert key in engine._journal_unresolved
        assert engine.durability_risk is True

        restarted.run_cycle(
            {pos.symbol: entry}, bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            "restorable after the owed-leg mismatch is removed",
        )
        assert engine.durability_risk is False
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    def test_c3_missing_committed_partial_leg_blocks_retirement(
        self, tmp_path,
    ):
        """C4: a COMMITTED partial leg (tp1_executed, not owed) whose
        durable row is gone keeps the record — the retirement proof
        requires every leg the flags promise, not only the owed ones."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        key = pos.position_key
        engine = contour.engine

        # TP1 fires and commits durably (no failures at all)
        contour.run_cycle({pos.symbol: entry * 0.945})
        assert store.get_close_leg(key, 1) is not None, "TP1 is durable"
        assert key in engine.open_positions, "still open after the partial"

        # the durable TP1 row disappears (data loss) before the STOP
        real_leg = store.get_close_leg
        store.get_close_leg = lambda position_key, leg_seq, *a, **k: (
            None if int(leg_seq) == 1
            else real_leg(position_key, leg_seq, *a, **k)
        )
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        store.get_close_leg = real_leg
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert engine.journal.exists(key), (
            "a missing committed leg row never retires the record",
        )
        assert key in engine._journal_unresolved
        assert engine.durability_risk is True

        # the row reappears (repair) -> retirement proceeds, no deadlock
        contour.run_cycle(
            {pos.symbol: entry}, bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            "restorable after the missing leg row is back",
        )
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(contour)

    def test_c4_missing_owed_partial_leg_keeps_the_record(self, tmp_path):
        """C4 (owed variant): an owed partial leg row that is still
        missing keeps the record — the Round 8 refusal is unchanged by the
        Round 14 restructure of the leg block."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        key = pos.position_key
        original_commit = store.commit_partial_exit
        original_stage = store.stage_close

        # (1) the TP1 commit fails -> the partial leg stays DIRTY in
        #     memory and is owed by the protective-close record
        store.commit_partial_exit = _always_fail("TP1 persistence failure")
        contour.run_cycle({pos.symbol: entry * 0.945})
        assert key in contour.engine.dirty_leg_keys

        # (2) staging down as well -> the STOP is journaled with the dirty
        #     TP1 owed and the terminal UPDATE is deferred (row stays OPEN)
        store.stage_close = _always_fail("staging down for the whole run")
        stop_cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert stop_cycle["exit_reasons"] == ["STOP_LOSS"], stop_cycle
        path = contour.engine.journal._path(key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["owed_partial_legs"], "the dirty TP1 is owed"
        assert store.get_position(key).status == POSITION_STATUS_OPEN

        store.commit_partial_exit = original_commit
        store.stage_close = original_stage
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        real_leg = store.get_close_leg
        store.get_close_leg = lambda position_key, leg_seq, *a, **k: (
            None if int(leg_seq) == 1 else real_leg(position_key, leg_seq, *a, **k)
        )

        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        store.get_close_leg = real_leg
        assert store.get_position(key).status == POSITION_STATUS_CLOSED
        assert path.exists(), "a missing owed leg row keeps the record"
        assert key in engine._journal_unresolved
        assert engine.durability_risk is True

        restarted.run_cycle(
            {pos.symbol: entry}, bars=_quiescent_bar(pos, entry),
        )
        assert engine.journal.exists(key) is False, (
            "restorable once every owed leg row is durable",
        )
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    # -- D: the documented exceptions are unchanged ------------------------
    def test_d1_ambiguous_commit_is_still_classified_exactly_once(
        self, tmp_path,
    ):
        """D1: the ambiguous-COMMIT exception (Round 6/13) is unchanged —
        exactly one terminal leg with the frozen numbers, and the record
        retires only after the confirming read-back."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage, path, original = (
            self._damaged_stop(settings, tmp_path, lambda p: None)
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        frozen = json.loads(original.decode("utf-8"))["snapshot"]
        real_update = store.update_position
        state = {"ack_lost": False}

        def update_then_lose_ack(snapshot, *args, **kwargs):
            out = real_update(snapshot, *args, **kwargs)
            if not state["ack_lost"]:
                state["ack_lost"] = True
                raise RuntimeError(
                    "ambiguous COMMIT (ack lost after the write)",
                )
            return out

        store.update_position = update_then_lose_ack
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        store.update_position = real_update
        assert state["ack_lost"] is True, "the ambiguity really fired"
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        row = store.get_position(key)
        assert row.status == POSITION_STATUS_CLOSED
        legs = store.list_close_legs(key)
        assert [(leg.exit_reason, bool(leg.is_terminal)) for leg in legs] == [
            ("STOP_LOSS", True),
        ], legs
        assert row.net_pnl == pytest.approx(frozen["net_pnl"], rel=1e-12)
        assert engine.journal.exists(key) is False
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        _assert_durable_consistent(store, key)
        _assert_ledger_consistent(restarted)

    def test_d2_unreadable_journal_still_keeps_the_gate_shut(
        self, tmp_path, monkeypatch,
    ):
        """D2: an UNREADABLE journal still closes the gate on its own
        (Round 10), never deletes the record, and a later clean read
        resumes the replay."""
        settings = _settings()
        contour, store, clock, pos, key, entry, stage = _round10_stop_setup(
            settings, tmp_path,
        )
        paths_before = list(contour.engine.journal.paths())
        state = _poison_journal_reads(
            monkeypatch, contour.engine.journal,
            lambda: OSError(5, "Input/output error"),
        )
        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal.exists(key)
        assert engine.journal_read_blocked is True
        assert engine.durability_risk is True
        assert engine.accounting_consistent is False
        assert engine._journal_unresolved == set(), (
            "the gate must not depend on _journal_unresolved",
        )
        cycle = restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert cycle["exit_reasons"] == [], "no re-evaluation of the STOP"
        assert cycle["entries"] == 0, "new entries stay refused"
        assert engine.journal.paths() == paths_before, (
            "the unreadable record stays exactly where it was written",
        )

        state["enabled"] = False     # reads work again
        restarted.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        assert engine.journal_read_blocked is False, "the gate reopens"
        assert engine.journal.exists(key), (
            "the still-OPEN row never retires the record",
        )
        assert store.get_position(key).status == POSITION_STATUS_OPEN
        assert engine.durability_risk is True, (
            "the deferred close is still an unresolved durability risk",
        )

    def test_d3_migration_088_degraded_mode_still_retires_the_journal(
        self, tmp_path,
    ):
        """D3: the documented migration-088 degraded close (stage_close
        answers False, no raise) keeps its exact behaviour with the Round
        14 proof: the in-process degraded marker skips the leg half and the
        record retires — no false durability risk, no deadlock."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        pos = _open_one(contour, store, clock)
        entry = pos.entry_price
        key = pos.position_key
        engine = contour.engine
        original_stage = store.stage_close
        store.stage_close = lambda *args, **kwargs: False   # 088 absent

        written: list[str] = []
        real_write = engine.journal.write

        def spy_write(record):
            written.append(record.position_key)
            return real_write(record)

        engine.journal.write = spy_write
        cycle = contour.run_cycle(
            {pos.symbol: entry}, bars=_round8_stop_bars(pos, entry),
        )
        engine.journal.write = real_write
        store.stage_close = original_stage

        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        assert key in written, "the degraded close writes its record"
        assert store.get_position(key).status == POSITION_STATUS_CLOSED, (
            "the degraded terminal UPDATE still commits",
        )
        assert store.list_close_legs(key) == [], "no leg table, no legs"
        assert engine.journal.exists(key) is False, (
            "the degraded close retires exactly as documented",
        )
        assert engine._journal_unresolved == set()
        assert engine.journal_read_blocked is False
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
        # NOTE: no _assert_durable_consistent here — the documented
        # degraded mode has NO leg rows by definition (quantity
        # conservation lives in _assert_ledger_consistent instead).
        _assert_ledger_consistent(contour)

    def test_d4_confirmed_absent_still_retires_the_orphan_record(
        self, tmp_path,
    ):
        """D4: the confirmed-absent proof (Round 8) is unchanged — a
        synthetic orphan record is retired at boot through the read chain,
        without ever reaching the new retirement proof."""
        settings = _settings()
        contour, store, clock = _contour(settings, journal_dir=tmp_path)
        fake_key = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1:proven-absent-r14"
        journal = contour.engine.journal
        journal.write(_journal_record(fake_key))
        assert journal.exists(fake_key)

        restarted = _restart(contour, settings, clock, journal_dir=tmp_path)
        engine = restarted.engine
        assert engine.journal.exists(fake_key) is False, (
            "a proven orphan record must be retired",
        )
        assert engine.journal_read_blocked is False
        assert engine.journal_read_failure is None
        assert engine._journal_unresolved == set()
        assert fake_key not in engine._pending_obligations
        assert engine.durability_risk is False
        assert engine.accounting_consistent is True
