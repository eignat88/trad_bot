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


def _cycle_ambiguous_insert_unbooked(
    server, contour, store, clock, caplog, *, symbol="ETHUSDT", bal_times=1,
):
    """One cycle: INSERT COMMITTED + ACK lost + FAILED adoption confirm-read.

    The INSERT applies server-side but its acknowledgement is lost; the
    idempotent retry sees the committed row (rowcount 0), so
    save_position() returns False and adoption runs — but the adoption's
    fresh balance_components() read fails (``bal_times``).  Review P1
    semantics: the VERIFIED row is adopted ANYWAY (protective STOP/TP/
    TIMEOUT active from the very next cycle), NOTHING is booked (the
    balance stays untouched; the entry costs land exactly once at the
    next successful reconciliation) and accounting stays untrusted, so
    new entries are blocked.

    Returns ``(event_key, position_key, cycle)``.
    """
    assert _capture(
        store, _candidate(symbol, 100.0, detected_at=clock()), clock=clock,
    ) == CAPTURED
    ev_key = _captured_event_key(store)
    key = build_position_key(ev_key)
    engine = contour.engine
    server.commit_ack_lost = True
    server.commit_ack_lost_on = "INSERT_POSITION"
    server.fail_execute_with = pg_dbapi.IntegrityError(
        "confirmed_balance_unavailable",
    )
    server.fail_execute_match = _BAL_MATCH
    server.fail_execute_times = bal_times
    with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
        cycle = contour.run_cycle({symbol: 100.0})
    # Adopted for protective management despite the failed confirm-read:
    assert cycle["entries"] == 1, cycle
    assert key in engine.open_positions
    assert "accounting UNCONFIRMED" in caplog.text
    assert "ADOPTED committed position" in caplog.text
    # …but NOT booked and NOT trusted:
    assert engine.balance == pytest.approx(_INITIAL)
    assert engine._balance_trusted is False
    assert cycle["accounting_consistent"] is False
    # The committed row verbatim, exactly one INSERT, event marked entered:
    assert server.positions[key]["status"] == POSITION_STATUS_OPEN
    assert server.apply_log.count(("INSERT_POSITION", key)) == 1
    assert server.apply_log.count(("MARK_EVENT_ENTERED", ev_key)) == 1
    assert len(store.list_events(status=EVENT_STATUS_ENTERED)) == 1
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
        """THE BUG (Codex P2-2): the costs must land exactly ONCE.

        Cycle 1 adopts the committed row with a FAILED confirm-read (P1:
        protective exits active, NOTHING booked).  Cycle 2's reconciliation
        at the top of open_entries folds the committed entry costs into
        memory exactly once — and no adoption step may ever subtract them
        again (the old code deferred the adoption AND later re-subtracted
        by hand, double-counting the costs against the confirmed ledger).
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        ev_key, key, cycle1 = _cycle_ambiguous_insert_unbooked(
            server, contour, store, clock, caplog,
        )
        # Adopted for protection, but nothing booked while unconfirmed:
        assert engine.balance == pytest.approx(_INITIAL)
        assert engine._balance_trusted is False
        assert cycle1["accounting_consistent"] is False
        assert len(store.list_events(status=EVENT_STATUS_ENTERED)) == 1

        # Cycle 2: reads heal -> the reconciliation folds the committed
        # costs in ONCE; the adoption itself never re-runs (the event is
        # marked and the position key is guarded).
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle2["entries"] == 0, cycle2
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

    def test_adoption_blocked_across_several_failed_reconciliations(
        self, caplog,
    ):
        """Several failed confirm-reads keep NEW entries blocked, then heal.

        Cycle 1: adoption (unbooked, protective) with the first failure.
        Cycle 2: both reconciliation retries fail again -> accounting stays
        untrusted and the gate refuses the SECOND candidate while the
        adopted position keeps its protective management.  Cycle 3: reads
        succeed -> the reconciliation folds the committed entry costs in
        exactly once, then the blocked candidate may open.
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        # The failed-read budget covers THREE reconciliation attempts:
        # cycle 1's adoption confirm-read, cycle 2's recovery retry inside
        # check_exits(), and cycle 2's retry inside open_entries() — the
        # gate then blocks the whole second cycle.
        ev_key, key, cycle1 = _cycle_ambiguous_insert_unbooked(
            server, contour, store, clock, caplog, bal_times=3,
        )
        # A second candidate waits behind the blocked accounting:
        assert _capture(
            store, _candidate("SOLUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        sol_key = build_position_key(_captured_event_key(store))
        assert engine.balance == pytest.approx(_INITIAL)
        assert engine._balance_trusted is False

        # Cycle 2: both reconciliation retries fail -> entries refused,
        # the adopted position stays under protective management.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle(
                {"ETHUSDT": 100.0, "SOLUSDT": 100.0},
            )
        assert cycle2["entries"] == 0, cycle2
        assert "not trustworthy" in caplog.text
        assert engine._balance_trusted is False
        assert key in engine.open_positions          # P1: exits stay active
        assert sol_key not in engine.open_positions  # new entry blocked
        assert engine.balance == pytest.approx(_INITIAL)
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert server.apply_log.count(("MARK_EVENT_ENTERED", ev_key)) == 1

        # Cycle 3: injection exhausted -> the reconciliation lands the
        # committed ETH entry costs exactly once, then SOL opens normally.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle3 = contour.run_cycle(
                {"ETHUSDT": 100.0, "SOLUSDT": 100.0},
            )
        assert cycle3["entries"] == 1, cycle3
        eth_costs = _entry_costs(server.positions[key])
        sol_costs = _entry_costs(server.positions[sol_key])
        assert engine.balance == pytest.approx(_INITIAL - eth_costs - sol_costs)
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

        Cycle 1: the INSERT commits with a lost ACK, the adoption
        reconciles and books the entry costs ONCE, but the entry MARK is
        rejected (fail-open) — the event stays CAPTURED and is offered
        again.  The position guard must answer every repetition without a
        second INSERT, a second mark or any balance movement.
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
        server.fail_execute_with = pg_dbapi.IntegrityError("mark_rejected")
        server.fail_execute_match = _MARK_MATCH
        server.fail_execute_times = 1
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle1 = contour.run_cycle({"ETHUSDT": 100.0})
        # Adopted and booked exactly once; the MARK was rejected:
        assert cycle1["entries"] == 1, cycle1
        assert key in engine.open_positions
        assert len(store.list_events(status=EVENT_STATUS_CAPTURED)) == 1
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert server.apply_log.count(("MARK_EVENT_ENTERED", ev_key)) == 0
        row = server.positions[key]
        costs = _entry_costs(row)
        assert engine.balance == pytest.approx(_INITIAL - costs)
        assert engine._balance_trusted is True

        # Cycle 2: the same event is delivered again — position guard
        # returns it, nothing is inserted, marked or rebooked:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle2["entries"] == 0, cycle2
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
        ev_key, key, _ = _cycle_ambiguous_insert_unbooked(
            server, contour, store, clock, caplog,
        )
        assert engine.balance == pytest.approx(_INITIAL)  # adopted, unbooked
        cycle2 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle2["entries"] == 0, cycle2  # event already marked ENTERED
        row = server.positions[key]
        costs = _entry_costs(row)
        # The reconciliation folded the committed entry costs in exactly once:
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
        _ev_key, key, _ = _cycle_ambiguous_insert_unbooked(
            server, contour, store, clock, caplog,
        )
        cycle2 = contour.run_cycle({"ETHUSDT": 100.0})
        assert cycle2["entries"] == 0, cycle2  # event already marked ENTERED
        # The reconciliation folded the committed entry costs in exactly once:
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

    def test_protective_exits_survive_failed_adoption_reconciliation(
        self, caplog,
    ):
        """Review P1 chain: INSERT committed -> ACK lost -> repeated failed
        reconciliations -> STOP still fires -> terminal persistence and
        recovery -> balance reconciled, entry costs charged exactly once.

        The verified OPEN row is adopted for protective management even
        while balance_components() keeps failing; nothing is booked in the
        meantime and NEW entries stay blocked (the second candidate is
        refused every cycle until the balance is confirmed); afterwards the
        final ledger equals exactly ``initial + realized net PnL`` (entry
        costs landed once, inside the closed row's net) with no drift over
        idle cycles.
        """
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine

        # 1) INSERT COMMITTED + ACK lost + first failed confirm-read:
        #    adopted UNBOOKED — protective exits are now ACTIVE (P1).
        ev_key, key, cycle1 = _cycle_ambiguous_insert_unbooked(
            server, contour, store, clock, caplog, bal_times=3,
        )
        assert key in engine.open_positions
        # A second candidate waits behind the blocked accounting:
        assert _capture(
            store, _candidate("SOLUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        sol_key = build_position_key(_captured_event_key(store))

        # 2) Cycle 2: BOTH remaining reconciliation retries fail — the
        #    second candidate stays blocked for the WHOLE cycle and the
        #    adopted position keeps protective management (still unbooked).
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle(
                {"ETHUSDT": 100.0, "SOLUSDT": 100.0},
            )
        assert cycle2["entries"] == 0, cycle2            # blocked (untrusted)
        assert "not trustworthy" in caplog.text          # SOL refused
        assert key in engine.open_positions              # protective still on
        assert sol_key not in engine.open_positions      # new entry blocked
        assert engine.balance == pytest.approx(_INITIAL) # still unbooked
        assert engine._balance_trusted is False
        # The DETERMINISTIC invariant holds against the last CONFIRMED
        # read (the construction-time one here — every fresh read fails):
        # the committed entry costs are deliberately NOT in memory yet and
        # land exactly once at the next successful reconciliation.
        assert engine.balance == pytest.approx(
            engine.confirmed_balance
            + sum(engine._uncommitted_deltas.values()),
        )

        # 3) Cycle 3: the PROTECTIVE STOP fires (price gapped beyond the
        #    stop) and the terminal write is rejected once — persistence
        #    pending, close delta local; the balance confirmed earlier in
        #    the cycle unblocks the second candidate afterwards.
        clock.advance(minutes=15)
        server.fail_execute_with = pg_dbapi.IntegrityError("terminal_rejected")
        server.fail_execute_match = _UPDATE_ONLY
        server.fail_execute_times = 1
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle3 = contour.run_cycle(
                {"ETHUSDT": 110.0, "SOLUSDT": 100.0},
            )
        assert cycle3["exits"] == 1, cycle3
        assert cycle3["exit_reasons"] == ["STOP_LOSS_GAP"], cycle3
        assert key not in engine.open_positions          # closed out of memory
        assert key in engine.pending_closes              # terminal persistence
        assert engine._uncommitted_deltas[key]           # close delta local
        # The failed terminal write was re-confirmed in the SAME cycle by
        # the healthy store read (the row is still OPEN server-side), so
        # accounting is trustworthy again and the second candidate unblocks:
        assert engine._balance_trusted is True
        assert cycle3["entries"] == 1, cycle3            # balance confirmed
        assert sol_key in engine.open_positions

        # 4) Cycle 4: healthy again — the flush replays the close, the
        #    reconciliation folds EVERYTHING in exactly once (entry costs
        #    via the closed row's net) and nothing is pending any more.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle4 = contour.run_cycle(
                {"ETHUSDT": 100.0, "SOLUSDT": 100.0},
            )
        assert cycle4["entries"] == 0 and not cycle4["closed"], cycle4
        assert not engine.pending_closes and not engine._uncommitted_deltas
        assert engine._balance_trusted is True
        closed_row = server.positions[key]
        assert closed_row["status"] == POSITION_STATUS_CLOSED

        # THE identity: entry costs charged EXACTLY once (inside net_pnl):
        net = float(closed_row["net_pnl"])
        sol_costs = _entry_costs(server.positions[sol_key])
        assert engine.balance == pytest.approx(_INITIAL + net - sol_costs)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )
        # Exactly one INSERT and one terminal UPDATE for the adopted key:
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1

        # Original entry facts verbatim through the whole chain:
        assert closed_row["entered_at"] == SIGNAL_AT
        assert closed_row["deadline_at"] == deadline_for(SIGNAL_AT)
        assert closed_row["exit_reason"] == "STOP_LOSS_GAP"
        assert closed_row["event_key"] == ev_key

        # The deterministic invariant re-armed against a fresh confirmed
        # read: memory_balance == confirmed_balance + Σ uncommitted_deltas.
        assert engine.reconcile_balance() is True
        assert not engine._uncommitted_deltas
        assert engine.balance == pytest.approx(engine.confirmed_balance)

        for _ in range(3):
            cycle = contour.run_cycle({"ETHUSDT": 100.0, "SOLUSDT": 100.0})
            assert cycle["entries"] == 0 and not cycle["closed"], cycle
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

    def test_frozen_protocol_fingerprint_unchanged(self):
        assert protocol_fingerprint() == FROZEN_FINGERPRINT
