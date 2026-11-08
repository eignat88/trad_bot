"""PR #267 — duplicate entry costs during adoption (Codex review P2-2).

After an ambiguous INSERT COMMIT the restored position may be folded into
the confirmed ledger by a reconcile_balance() BEFORE _adopt_stored_position()
runs.  The previous adoption logic then re-subtracted entry_fee +
entry_slippage_cost from the memory balance — a double booking that broke
the deterministic invariant

    memory_balance == confirmed_balance + sum(uncommitted_deltas)

and drifted away from the persisted ledger until some later reconciliation
happened to run.  The adoption now goes through a FRESH
reconcile_balance() and never subtracts entry costs by hand; while the
confirmed balance cannot be read, the adoption is deferred and new entries
stay blocked.

Required fault-injection scenarios and where they live:

  1. INSERT committed + ACK lost               ->
     test_ambiguous_insert_books_entry_costs_exactly_once;
  2. adoption BEFORE reconciliation            -> same test (explicit
     reconcile after adoption must be a no-op);
  3. adoption AFTER reconciliation             ->
     test_adoption_after_reconciliation_never_double_books (the bug);
  4. concurrent pending close of another symbol ->
     test_adoption_with_concurrent_pending_close_of_another_symbol;
  5. several failed reconciliations            ->
     test_adoption_deferred_across_several_failed_reconciliations;
  6. repeated delivery of the entry event      ->
     test_duplicate_delivery_after_adoption_never_rebooks;
  7. restart after adoption                    ->
     test_restart_after_adoption_preserves_ledger_equality;
  8. memory_balance == persisted_balance       -> asserted at the end of
     every scenario (store.balance_components());
  9. several idle cycles without drift         -> 3 idle cycles at the end
     of every scenario;
 10. funding and terminal close not counted twice ->
     test_funding_and_terminal_close_counted_exactly_once.

Entry facts (entered_at, deadline_at, entry price, TP, SL, lineage) are
never rewritten (req 6), pending closes / dirty funding / outbox replay /
EXIT_ONLY behaviour is untouched (req 7), and the frozen fingerprint is
re-asserted here.  No merge, no deploy, no migrations, no activation.
"""
from __future__ import annotations

import logging

import pg8000.dbapi as pg_dbapi
import pytest

from app.paper.me_long_v2_inverse_short_v1 import protocol_fingerprint
from app.paper.me_long_v2_inverse_short_v1.capture import CAPTURED
from app.paper.me_long_v2_inverse_short_v1.identity import (
    position_key as build_position_key,
)
from app.paper.me_long_v2_inverse_short_v1.models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_ENTERED,
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import deadline_for
from app.paper.me_long_v2_inverse_short_v1.store import (
    EVENT_TABLE,
    POSITION_TABLE,
)
from tests.test_me_long_v2_inverse_short_ambiguous_commit_v1 import (
    _TP_PRICE,
    _captured_event_key,
    _close_delta_for,
)
from tests.test_me_long_v2_inverse_short_pg_reconnect_v1 import (
    ENGINE_LOGGER,
    FROZEN_FINGERPRINT,
    SIGNAL_AT,
    _candidate,
    _capture,
    _contour,
    _FakeServer,
    _open,
)

# Precise statement matches (bare "UPDATE"/"SELECT" would over-match the
# outbox INSERT and the event/pending reads).
_BAL_MATCH = "WHEN status = 'CLOSED' THEN net_pnl"    # balance_components()
_MARK_MATCH = f"UPDATE {EVENT_TABLE} SET"             # mark_event_entered()
_UPDATE_ONLY = f"UPDATE {POSITION_TABLE} SET"         # funding/terminal UPDATE

_INITIAL = 10_000.0


def _entry_costs(row: dict) -> float:
    """entry_fee + entry_slippage exactly as persisted for an OPEN row."""
    return float(row["entry_fee"]) + float(row["entry_slippage"])


def _cycle_ambiguous_insert_deferred(
    server, contour, store, clock, caplog, *, symbol="ETHUSDT", bal_times=1,
):
    """One cycle: INSERT COMMITTED + ACK lost AND a failed confirm-read.

    The INSERT applies server-side but its acknowledgement is lost; the
    idempotent retry sees the committed row (rowcount 0), so
    save_position() returns False and adoption is attempted — but the
    adoption's fresh balance_components() read fails (``bal_times``), so
    the adoption DEFERS: nothing opened, nothing booked, accounting
    untrusted, the event stays CAPTURED for the next cycle.

    Returns ``(event_key, position_key, cycle)``.
    """
    assert _capture(
        store, _candidate(symbol, 100.0, detected_at=clock()), clock=clock,
    ) == CAPTURED
    ev_key = _captured_event_key(store)
    key = build_position_key(ev_key)
    server.commit_ack_lost = True
    server.commit_ack_lost_on = "INSERT_POSITION"
    server.fail_execute_with = pg_dbapi.IntegrityError(
        "confirmed_balance_unavailable",
    )
    server.fail_execute_match = _BAL_MATCH
    server.fail_execute_times = bal_times
    with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
        cycle = contour.run_cycle({symbol: 100.0})
    assert cycle["entries"] == 0, cycle
    assert key not in contour.engine.open_positions
    assert server.positions[key]["status"] == POSITION_STATUS_OPEN
    assert server.apply_log.count(("INSERT_POSITION", key)) == 1
    assert "adoption DEFERRED" in caplog.text
    return ev_key, key, cycle


# ======================================================================
# Adoption entry-cost accounting
# ======================================================================

class TestAdoptionEntryCostAccounting:
    def test_ambiguous_insert_books_entry_costs_exactly_once(self, caplog):
        """INSERT committed + ACK lost; adoption before any reconciliation.

        The adoption's own fresh reconciliation folds the committed entry
        costs into memory exactly once; a later reconcile_balance() is a
        no-op (memory stays == persisted, no drift over idle cycles).
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        assert _capture(
            store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        ev_key = _captured_event_key(store)
        key = build_position_key(ev_key)

        server.commit_ack_lost = True
        server.commit_ack_lost_on = "INSERT_POSITION"
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle1 = contour.run_cycle({"ETHUSDT": 100.0})
        # Adopted within the same cycle, one reconnect absorbed the lost ACK:
        assert cycle1["entries"] == 1, cycle1
        assert store.reconnect_count == 1
        assert key in engine.open_positions
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1

        row = server.positions[key]
        costs = _entry_costs(row)
        components = store.balance_components()
        assert components["balance"] == pytest.approx(_INITIAL - costs)
        assert engine.balance == pytest.approx(components["balance"])
        assert engine.balance == pytest.approx(_INITIAL - costs)
        assert engine._balance_trusted is True
        assert not engine._uncommitted_deltas and not engine.pending_closes

        # Adoption BEFORE reconciliation: the reconcile afterwards must not
        # move anything (entry costs already carried by the confirmed leg).
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(_INITIAL - costs)

        # Several idle cycles: no drift.
        for _ in range(3):
            cycle = contour.run_cycle({"ETHUSDT": 100.0})
            assert cycle["entries"] == 0 and not cycle["closed"], cycle
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

    def test_adoption_after_reconciliation_never_double_books(self, caplog):
        """THE BUG (Codex P2-2): reconcile first, adoption afterwards.

        Cycle 1 defers the adoption (failed confirmation read), so the
        committed row sits in PostgreSQL with nothing booked.  Cycle 2's
        reconciliation at the top of open_entries folds the committed
        entry costs into memory FIRST — the adoption that follows must
        only verify and open, never subtract the same costs again.
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        ev_key, key, cycle1 = _cycle_ambiguous_insert_deferred(
            server, contour, store, clock, caplog,
        )
        # Nothing booked yet while the outcome is unverifiable:
        assert engine.balance == pytest.approx(_INITIAL)
        assert engine._balance_trusted is False
        assert cycle1["accounting_consistent"] is False
        assert len(store.list_events(status=EVENT_STATUS_CAPTURED)) == 1

        # Cycle 2: reads heal -> reconciliation runs BEFORE the adoption.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle2["entries"] == 1, cycle2
        assert key in engine.open_positions
        assert engine._balance_trusted is True

        row = server.positions[key]
        costs = _entry_costs(row)
        components = store.balance_components()
        # Persisted ledger: exactly ONE subtraction of the entry costs…
        assert components["balance"] == pytest.approx(_INITIAL - costs)
        # …and memory matches it — a second subtraction would fail both:
        assert engine.balance == pytest.approx(components["balance"])
        assert engine.balance == pytest.approx(_INITIAL - costs)
        assert not engine._uncommitted_deltas and not engine.pending_closes

        # Requirement: original entry facts verbatim, never re-derived.
        adopted = engine.open_positions[key]
        assert adopted.entered_at == SIGNAL_AT
        assert adopted.deadline_at == deadline_for(SIGNAL_AT)
        assert adopted.entry_price == pytest.approx(float(row["entry_price"]))
        assert adopted.stop_price == pytest.approx(float(row["stop_price"]))
        assert adopted.target_price == pytest.approx(float(row["target_price"]))
        assert adopted.event_key == row["event_key"]
        assert adopted.source_scanner == row["source_scanner"]
        assert adopted.source_setup_id == row["source_setup_id"]

        # One INSERT, one entry mark, no re-open:
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert server.apply_log.count(("MARK_EVENT_ENTERED", ev_key)) == 1

        for _ in range(3):
            cycle = contour.run_cycle({"ETHUSDT": 100.0})
            assert cycle["entries"] == 0 and not cycle["closed"], cycle
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

    def test_adoption_deferred_across_several_failed_reconciliations(
        self, caplog,
    ):
        """Two consecutive failed confirm-reads block everything, then heal.

        Cycle 1: adoption deferred (first failure).  Cycle 2: the
        reconciliation itself fails again -> accounting stays untrusted and
        the gate refuses new entries.  Cycle 3: reads succeed -> reconcile
        then adopt, booked exactly once, invariant restored.
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        # The failed-read budget covers THREE reconciliation attempts:
        # cycle 1's adoption confirm-read, cycle 2's recovery retry inside
        # check_exits(), and cycle 2's retry inside open_entries() — the
        # gate then blocks the whole second cycle.
        ev_key, key, cycle1 = _cycle_ambiguous_insert_deferred(
            server, contour, store, clock, caplog, bal_times=3,
        )
        assert engine.balance == pytest.approx(_INITIAL)
        assert engine._balance_trusted is False

        # Cycle 2: the reconciliation at the top of open_entries fails
        # again -> entries refused, nothing adopted, balance untouched.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle2["entries"] == 0, cycle2
        assert "not trustworthy" in caplog.text
        assert engine._balance_trusted is False
        assert key not in engine.open_positions
        assert engine.balance == pytest.approx(_INITIAL)
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert server.apply_log.count(("MARK_EVENT_ENTERED", ev_key)) == 0

        # Cycle 3: injection exhausted -> reconcile + adoption succeed.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle3 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle3["entries"] == 1, cycle3
        row = server.positions[key]
        costs = _entry_costs(row)
        assert engine.balance == pytest.approx(_INITIAL - costs)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )
        assert engine._balance_trusted is True
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1

        # Explicit reconcile after adoption: a no-op (adoption happened
        # before this reconciliation):
        before = engine.balance
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(before)

        for _ in range(3):
            cycle = contour.run_cycle({"ETHUSDT": 100.0})
            assert cycle["entries"] == 0 and not cycle["closed"], cycle
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

    def test_adoption_with_concurrent_pending_close_of_another_symbol(
        self, caplog,
    ):
        """BTC's close is pending while ETH's ambiguous INSERT adopts.

        The pending close keeps its local delta, the adoption reconciles
        against the confirmed ledger with that delta still present, and
        after the close replays everything converges: pendings/deltas
        empty, memory == persisted, no drift.
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        btc = _open(contour, store, clock)            # BTCUSDT, normal open
        b_key = btc.position_key
        assert _capture(
            store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        e_key = build_position_key(_captured_event_key(store))

        # Cycle 1: BTC's terminal UPDATE is rejected (pending close + local
        # delta) AND ETH's INSERT commits with a lost acknowledgement.
        clock.advance(minutes=15)
        server.fail_execute_with = pg_dbapi.IntegrityError("terminal_rejected")
        server.fail_execute_match = _UPDATE_ONLY
        server.fail_execute_times = 1
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "INSERT_POSITION"
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle1 = contour.run_cycle(
                {"BTCUSDT": _TP_PRICE, "ETHUSDT": 100.0},
            )
        assert cycle1["exits"] == 1, cycle1
        assert cycle1["exit_reasons"] == ["TAKE_PROFIT_1"]
        assert b_key in engine.pending_closes
        delta_b = engine._uncommitted_deltas[b_key]
        assert delta_b == pytest.approx(
            _close_delta_for(engine, engine.pending_closes[b_key]),
        )
        # The other symbol adopted despite the pending close (req 7):
        assert cycle1["entries"] == 1, cycle1
        assert e_key in engine.open_positions
        assert engine._balance_trusted is True

        # Invariant with the delta still local:
        components = store.balance_components()
        assert engine.balance == pytest.approx(
            components["balance"]
            + sum(engine._uncommitted_deltas.values()),
        )
        # …which is exactly the independent arithmetic (both rows OPEN):
        btc_costs = _entry_costs(server.positions[b_key])
        eth_costs = _entry_costs(server.positions[e_key])
        assert components["balance"] == pytest.approx(
            _INITIAL - btc_costs - eth_costs,
        )
        assert engine.balance == pytest.approx(
            _INITIAL - btc_costs - eth_costs + delta_b,
        )
        assert server.apply_log.count(("INSERT_POSITION", e_key)) == 1

        # Cycle 2: the close replays and lands once; ETH re-entries stay off.
        # (A flush replay is not counted as a new exit — the STATE proves it.)
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle2["entries"] == 0, cycle2
        assert not engine.pending_closes and not engine._uncommitted_deltas
        assert server.positions[b_key]["status"] == POSITION_STATUS_CLOSED
        assert server.positions[e_key]["status"] == POSITION_STATUS_OPEN
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )
        # The close was applied exactly once (no double accounting):
        assert server.apply_log.count(("UPDATE_POSITION", b_key)) == 1

        for _ in range(3):
            cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
            assert cycle["entries"] == 0 and not cycle["closed"], cycle
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

    def test_duplicate_delivery_after_adoption_never_rebooks(self, caplog):
        """A repeated delivery of the adopted event never rebooks costs.

        The entry mark fails after a successful adoption (fail-open), so
        the event stays CAPTURED and is offered again — the position guard
        must return it without a second INSERT, a second mark or any
        balance movement.
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        ev_key, key, cycle1 = _cycle_ambiguous_insert_deferred(
            server, contour, store, clock, caplog,
        )

        # Cycle 2: adoption succeeds, the entry mark is rejected:
        server.fail_execute_with = pg_dbapi.IntegrityError("mark_rejected")
        server.fail_execute_match = _MARK_MATCH
        server.fail_execute_times = 1
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle2["entries"] == 1, cycle2
        assert key in engine.open_positions
        assert len(store.list_events(status=EVENT_STATUS_CAPTURED)) == 1
        row = server.positions[key]
        costs = _entry_costs(row)
        assert engine.balance == pytest.approx(_INITIAL - costs)

        # Cycle 3: the same event is delivered again — position guard
        # returns it, nothing is inserted, marked or rebooked:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle3 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle3["entries"] == 0, cycle3
        assert engine.balance == pytest.approx(_INITIAL - costs)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert server.apply_log.count(("MARK_EVENT_ENTERED", ev_key)) == 0

        # A repeated CAPTURE of the identical signal changes nothing:
        assert _capture(
            store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) != CAPTURED or len(store.list_events()) == 1
        assert len(store.list_positions()) == 1
        assert engine.balance == pytest.approx(_INITIAL - costs)

        for _ in range(3):
            cycle = contour.run_cycle({"ETHUSDT": 100.0})
            assert cycle["entries"] == 0 and not cycle["closed"], cycle
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

    def test_restart_after_adoption_preserves_ledger_equality(self, caplog):
        """A restarted process loads the adopted row, balances agree.

        Nothing is re-booked across the restart: the fresh engine's memory
        balance comes from the persisted ledger, the original entry facts
        survive verbatim, and idle cycles drift nowhere.
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        ev_key, key, _ = _cycle_ambiguous_insert_deferred(
            server, contour, store, clock, caplog,
        )
        cycle2 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle2["entries"] == 1, cycle2
        row = server.positions[key]
        costs = _entry_costs(row)
        assert engine.balance == pytest.approx(_INITIAL - costs)
        assert store.list_events(status=EVENT_STATUS_ENTERED)

        # Restart on the same healthy server:
        store2, contour2, _clock2 = _contour(server)
        engine2 = contour2.engine
        assert key in engine2.open_positions
        assert engine2.balance == pytest.approx(engine.balance)
        assert engine2.balance == pytest.approx(_INITIAL - costs)
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"],
        )
        # Original entry facts verbatim across the restart:
        loaded = engine2.open_positions[key]
        assert loaded.entered_at == SIGNAL_AT
        assert loaded.deadline_at == deadline_for(SIGNAL_AT)
        assert loaded.entry_price == pytest.approx(float(row["entry_price"]))
        assert loaded.stop_price == pytest.approx(float(row["stop_price"]))
        assert loaded.target_price == pytest.approx(float(row["target_price"]))
        assert loaded.event_key == row["event_key"]
        assert store2.get_event(ev_key).status == EVENT_STATUS_ENTERED

        # No re-open, no rebooking after the restart:
        cycle = contour2.run_cycle({"ETHUSDT": 100.0})
        assert cycle["entries"] == 0 and not cycle["closed"], cycle
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert server.apply_log.count(("MARK_EVENT_ENTERED", ev_key)) == 1

        for _ in range(3):
            cycle = contour2.run_cycle({"ETHUSDT": 100.0})
            assert cycle["entries"] == 0 and not cycle["closed"], cycle
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"],
        )

    def test_funding_and_terminal_close_counted_exactly_once(self, caplog):
        """After adoption: funding accrues once, the close lands once.

        The final balance equals exactly initial + closed net PnL — any
        double-counted entry cost, funding delta or close delta would break
        both that identity and memory == persisted over the idle cycles.
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        _ev_key, key, _ = _cycle_ambiguous_insert_deferred(
            server, contour, store, clock, caplog,
        )
        cycle2 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle2["entries"] == 1, cycle2
        assert engine.balance == pytest.approx(
            _INITIAL - _entry_costs(server.positions[key]),
        )

        # Funding: one 8h period, committed exactly once, no dirty state.
        clock.advance(hours=8)
        cycle_f = contour.run_cycle({"ETHUSDT": 100.0}, {"ETHUSDT": 0.01})
        assert not cycle_f["closed"], cycle_f
        row = server.positions[key]
        # One 8h period accrued exactly once (SHORT receives funding when
        # the rate is positive — the direction is irrelevant here, the
        # COUNT and the ledger identity are what matter):
        assert float(row["funding_paid"]) != 0.0
        assert int(row["funding_periods"]) == 1
        assert not engine._uncommitted_deltas
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

        # Terminal close: lands once on top of the funding accrual.
        cycle_c = contour.run_cycle({"ETHUSDT": _TP_PRICE})
        assert len(cycle_c["closed"]) == 1, cycle_c
        assert cycle_c["exit_reasons"] == ["TAKE_PROFIT_1"]
        closed_row = server.positions[key]
        assert closed_row["status"] == POSITION_STATUS_CLOSED
        assert int(closed_row["funding_periods"]) == 1
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 2  # fund+close
        assert not engine.pending_closes and not engine._uncommitted_deltas

        # THE identity: memory == persisted == initial + realized net PnL.
        net = float(closed_row["net_pnl"])
        assert engine.balance == pytest.approx(_INITIAL + net)
        assert store.balance_components()["balance"] == pytest.approx(
            _INITIAL + net,
        )
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

        for _ in range(3):
            cycle = contour.run_cycle({"ETHUSDT": _TP_PRICE})
            assert cycle["entries"] == 0 and not cycle["closed"], cycle
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

    def test_frozen_protocol_fingerprint_unchanged(self):
        assert protocol_fingerprint() == FROZEN_FINGERPRINT
