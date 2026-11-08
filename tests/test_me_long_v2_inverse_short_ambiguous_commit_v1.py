"""PR #267 — ambiguous PostgreSQL COMMIT outcomes (fault injection, review P4).

Ten mandatory scenarios, all driven through the REAL engine + store code
on the scriptable fake PostgreSQL (shared harness):

 1. INSERT COMMITTED with a lost acknowledgement  → stored row ADOPTED,
    no re-open, entry fees booked exactly once;
 2. duplicate delivery after such an ambiguous INSERT → original
    ``entered_at`` / ``deadline_at`` / TP / SL preserved, one row only;
 3. protective STOP fires immediately after the position is restored;
 4. funding UPDATE COMMITTED with a lost acknowledgement → verified
    against PostgreSQL, no double funding, no dirty/marker state;
 5. reconnect attempts exhausted → bounded attempts, state retained,
    entries fail-closed (incl. the dirty-funding gate) until recovery;
 6. recovery completes while ANOTHER pending close sits in the outbox;
 7. several consecutive reconciliation cycles: markers survive every
    failed read and clear only after a confirmed successful read;
 8. after full recovery the memory balance equals the persisted ledger;
 9. no re-accrual of entry fees / funding / close PnL anywhere;
10. RUNNING and DRAINING / EXIT_ONLY both behave correctly.

Terminal-close COMMIT follow-up (Codex review P2-2):

 11. a terminal UPDATE applied with a lost acknowledgement is verified
     against the stored row (status=CLOSED + the exact first terminal
     facts) and settled WITHOUT an uncommitted close delta — a later
     reconcile_balance() can never double-count the realized PnL;
 12. an unverifiable terminal outcome (reconnect attempts exhausted)
     keeps the pending close + local delta, blocks new entries until a
     reconciliation succeeds, and converges byte-for-byte on replay;
 13. several pending closes together with uncommitted funding deltas
     converge in one recovery cycle with no drift afterwards;
 14. an ambiguous COMMIT inside flush_pending_closes() (replay path)
     settles through the same verification.

Forbidden by the review (and honoured here): no fingerprint change, no
production Paper, no trading-gate changes, no migrations, no VPS.
"""
from __future__ import annotations

import json
import logging

import pg8000.dbapi as pg_dbapi
import pg8000.exceptions as pg_exceptions
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
from app.paper.me_long_v2_inverse_short_v1.store import POSITION_TABLE
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


def _captured_event_key(store) -> str:
    """The single CAPTURED event waiting for entry."""
    events = store.list_events(status=EVENT_STATUS_CAPTURED)
    assert len(events) == 1, events
    return events[0].event_key


def _stage_outbox_close(server, store, contour, clock):
    """Open + TIMEOUT-close a position, then re-stage its outbox row.

    Simulates a crash after the terminal snapshot was staged but before
    the outbox row was cleared — a durable pending close the next process
    must recover (used by the pending-close / marker scenarios).
    """
    position = _open(contour, store, clock)
    clock.advance(hours=128)                 # MAX_HOLD deadline reached
    cycle = contour.run_cycle({position.symbol: 100.0})
    assert cycle["exits"] == 1 and cycle["exit_reasons"] == ["TIMEOUT"], cycle
    closed = store.list_positions(POSITION_STATUS_CLOSED)
    assert len(closed) == 1
    row = closed[0]
    server.outbox[row.position_key] = {
        "position_key": row.position_key,
        "symbol": row.symbol,
        "exit_reason": row.exit_reason,
        "closed_at": row.closed_at,
        "snapshot": json.dumps(row.to_dict()),
    }
    return position, row


# ======================================================================
# P1 — ambiguous INSERT COMMIT
# ======================================================================

class TestAmbiguousInsertCommit:
    def test_insert_committed_with_lost_ack_is_adopted_not_reopened(
        self, caplog,
    ):
        """(1) INSERT applied + ACK lost → adopted, never re-opened."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        assert _capture(
            store, _candidate("BTCUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        event_key = _captured_event_key(store)
        key = build_position_key(event_key)

        # The INSERT is applied server-side; only the ACK is lost.
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "INSERT_POSITION"
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle({"BTCUSDT": 100.0})

        # adopted (entries counted), not suppressed, not re-opened:
        assert cycle["entries"] == 1, cycle
        assert key in engine.open_positions
        adopted = engine.open_positions[key]
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert store.reconnect_count == 1        # the retry healed the socket

        # original COMMITTED entry facts kept verbatim:
        row = server.positions[key]
        assert row["status"] == POSITION_STATUS_OPEN
        assert adopted.entered_at == row["entered_at"] == SIGNAL_AT
        assert adopted.deadline_at == row["deadline_at"]
        assert adopted.deadline_at == deadline_for(adopted.entered_at)
        assert adopted.stop_price == row["stop_price"]
        assert adopted.target_price == row["target_price"]
        assert adopted.entry_price == row["entry_price"]

        # event marked entered ONCE, fees booked ONCE:
        assert server.events[event_key]["status"] == EVENT_STATUS_ENTERED
        assert server.apply_log.count(("MARK_EVENT_ENTERED", event_key)) == 1
        assert engine.balance == pytest.approx(
            10_000.0 - adopted.entry_fee - adopted.entry_slippage_cost,
        )
        assert "ADOPTED committed position" in caplog.text

        # memory and persisted ledger already agree (the row existed):
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

    def test_duplicate_delivery_after_ambiguous_insert_keeps_original_facts(
        self, caplog,
    ):
        """(2)+(5) ACK lost + reconnect exhausted → fail-closed entry, then
        recovery adopts the committed row with its ORIGINAL facts."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        assert _capture(
            store, _candidate("BTCUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        event_key = _captured_event_key(store)
        key = build_position_key(event_key)

        server.commit_ack_lost = True
        server.commit_ack_lost_on = "INSERT_POSITION"
        server.connect_failures = 100           # reconnects refused
        cycle1 = contour.run_cycle({"BTCUSDT": 100.0})

        # (5) bounded: the unit failed closed, NOTHING healed:
        assert cycle1["entries"] == 0
        assert cycle1["pending_events_unavailable"] is True
        assert store.reconnect_count == 0
        # the row WAS committed by the first attempt:
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert key not in engine.open_positions
        assert server.events[event_key]["status"] == EVENT_STATUS_CAPTURED
        original = dict(server.positions[key])
        assert original["status"] == POSITION_STATUS_OPEN

        # PostgreSQL recovers an hour later; a re-build would now produce a
        # DIFFERENT entered_at — adoption must keep the ORIGINAL ones.
        server.connect_failures = 0
        clock.advance(hours=1)
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle({"BTCUSDT": 100.0})

        assert cycle2["entries"] == 1, cycle2
        adopted = engine.open_positions[key]
        assert adopted.entered_at == original["entered_at"]
        assert adopted.deadline_at == original["deadline_at"]
        assert adopted.stop_price == original["stop_price"]
        assert adopted.target_price == original["target_price"]
        assert adopted.entry_price == original["entry_price"]
        assert adopted.entered_at != clock()      # NOT rebuilt at recovery
        assert store.reconnect_count >= 1         # healed on recovery

        # still ONE row / ONE insert / ONE mark / ONE fee booking:
        assert len(server.positions) == 1
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert server.apply_log.count(("MARK_EVENT_ENTERED", event_key)) == 1
        assert server.events[event_key]["status"] == EVENT_STATUS_ENTERED
        assert engine.balance == pytest.approx(
            10_000.0 - adopted.entry_fee - adopted.entry_slippage_cost,
        )
        assert "ADOPTED committed position" in caplog.text

        # a further duplicate delivery never re-opens or re-books:
        cycle3 = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle3["entries"] == 0
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert engine.balance == pytest.approx(
            10_000.0 - adopted.entry_fee - adopted.entry_slippage_cost,
        )

    def test_protective_stop_fires_immediately_after_adoption(self, caplog):
        """(3) STOP works on the very next cycle after restoration."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        assert _capture(
            store, _candidate("BTCUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        event_key = _captured_event_key(store)
        key = build_position_key(event_key)

        server.commit_ack_lost = True
        server.commit_ack_lost_on = "INSERT_POSITION"
        cycle1 = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle1["entries"] == 1
        position = engine.open_positions[key]
        assert position.entered_at == SIGNAL_AT

        # Next cycle: price exactly AT the stop → protective STOP, no gap.
        clock.advance(hours=1)
        cycle2 = contour.run_cycle({position.symbol: position.stop_price})
        assert cycle2["exits"] == 1
        assert cycle2["exit_reasons"] == ["STOP_LOSS"]
        assert key not in engine.open_positions
        row = server.positions[key]
        assert row["status"] == POSITION_STATUS_CLOSED
        # exit fill = STOP + SHORT cover slippage (settings: 0.05%):
        assert row["exit_price"] == pytest.approx(
            position.stop_price * (1.0 + 0.0005),
        )
        assert row["entered_at"] == SIGNAL_AT       # entry facts untouched
        assert server.outbox == {}                  # close fully persisted


# ======================================================================
# P2 — ambiguous funding UPDATE COMMIT
# ======================================================================

class TestAmbiguousFundingCommit:
    def test_funding_committed_with_lost_ack_is_confirmed_not_reaccrued(
        self, caplog,
    ):
        """(4) UPDATE applied + ACK lost, retry fails with a SQL error →
        read-back CONFIRMS it: no dirty state, no delta, one accrual."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        position = _open(contour, store, clock)
        key = position.position_key

        clock.advance(hours=8)
        # Attempt #1 APPLIES the funding and loses only its ACK; the
        # retried statement then fails with a SQL error (no reconnect, no
        # further retry) — exactly the window where the engine must VERIFY
        # instead of assuming the write failed.
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "UPDATE_POSITION"
        server.fail_execute_with = pg_dbapi.IntegrityError("chk_funding")
        server.fail_execute_match = "UPDATE"
        server.fail_execute_skip = 1
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle(
                {position.symbol: 100.0}, {position.symbol: 0.01},
            )

        notional = position.entry_price * position.position_size
        expected_paid = -(notional * (0.01 / 100.0))   # SHORT sign convention
        # applied EXACTLY once server-side (the retry died before staging):
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1
        assert server.positions[key]["funding_periods"] == 1
        assert server.positions[key]["funding_paid"] == pytest.approx(
            expected_paid,
        )
        # confirmation via read-back: NO dirty state, NO local delta:
        assert key not in engine._dirty_funding
        assert dict(engine.uncommitted_deltas) == {}
        assert "CONFIRMED applied" in caplog.text
        # memory and persisted ledger identical — no double booking:
        assert position.funding_paid == pytest.approx(expected_paid)
        assert position.funding_periods_charged == 1
        assert engine.balance == pytest.approx(
            10_000.0 - position.entry_fee - position.entry_slippage_cost
            - position.funding_paid,
        )
        assert cycle["accounting_consistent"] is True
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

        # an idle cycle (no new interval) performs NO further write:
        updates = server.apply_log.count(("UPDATE_POSITION", key))
        cycle2 = contour.run_cycle(
            {position.symbol: 100.0}, {position.symbol: 0.01},
        )
        assert cycle2["entries"] == 0
        assert server.apply_log.count(("UPDATE_POSITION", key)) == updates

    def test_reconnect_exhaustion_keeps_state_fail_closed_until_recovery(
        self, caplog,
    ):
        """(5) every funding statement AND every reconnect fails → bounded
        attempts, dirty/delta retained, entries fail-closed; recovery
        commits the dirty snapshot first and only then reopens entries."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        position = _open(contour, store, clock)
        key = position.position_key
        # a pending event that must NOT open while funding is unconfirmed:
        assert _capture(
            store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED

        clock.advance(hours=8)
        server.fail_execute_with = pg_exceptions.InterfaceError("reset")
        server.fail_execute_match = "UPDATE"
        server.fail_execute_times = 10
        server.connect_failures = 100
        attempts_before = server.connect_attempts
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle(
                {position.symbol: 100.0}, {position.symbol: 0.01},
            )

        # (5) bounded reconnects (3 attempts), nothing healed, no write:
        assert server.connect_attempts - attempts_before == 3
        assert store.reconnect_count == 0
        assert key in engine._dirty_funding
        assert key in engine.uncommitted_deltas
        assert server.positions[key]["funding_periods"] == 0
        assert server.positions[key]["funding_paid"] == 0.0
        # fail-closed for NEW entries while the write is unconfirmed —
        # even though the balance itself is still trusted:
        assert cycle["entries"] == 0
        assert cycle["pending_events"] == 1          # event available, refused
        assert cycle["accounting_consistent"] is True
        assert "not yet confirmed by PostgreSQL" in caplog.text

        # Recovery: the dirty snapshot commits FIRST, then the gate opens.
        server.fail_execute_with = None
        server.fail_execute_match = None
        server.connect_failures = 0
        cycle2 = contour.run_cycle(
            {position.symbol: 100.0, "ETHUSDT": 100.0},
            {position.symbol: 0.01},
        )
        assert key not in engine._dirty_funding
        assert dict(engine.uncommitted_deltas) == {}
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1
        assert server.positions[key]["funding_periods"] == 1
        assert server.positions[key]["funding_paid"] == pytest.approx(
            -(position.entry_price * position.position_size) * (0.01 / 100.0),
        )
        assert cycle2["entries"] == 1                # ETH opened after recovery
        assert cycle2["pending_events"] == 0


# ======================================================================
# Recovery with another pending close / sequential reconciliations
# ======================================================================

class TestRecoveryWithPendingCloseAndMarkers:
    def test_recovery_completes_while_another_pending_close_exists(
        self, caplog,
    ):
        """(6) an ambiguous INSERT is adopted in the SAME cycle that
        recovers a DIFFERENT pending close from the outbox."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        _, closed_y = _stage_outbox_close(server, store, contour, clock)
        y_key = closed_y.position_key

        # restart on the same server with the staged close still present:
        store2, contour2, clock2 = _contour(server)
        engine2 = contour2.engine
        assert y_key in engine2.balance_recovery_pending

        clock2.advance(minutes=5)                 # a DIFFERENT signal candle
        assert _capture(
            store2, _candidate("BTCUSDT", 100.0, detected_at=clock2()),
            clock=clock2,
        ) == CAPTURED
        event_key = _captured_event_key(store2)
        key = build_position_key(event_key)
        assert key != y_key

        # ambiguous INSERT for the new event while Y's pending close is
        # still queued (the STAGE_CLOSE commit must NOT consume it):
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "INSERT_POSITION"
        cycle = contour2.run_cycle({"BTCUSDT": 100.0})

        # the OTHER pending close recovered in the SAME cycle (flush runs
        # before entries) and its balance marker reconciled away:
        assert y_key not in server.outbox
        assert y_key not in engine2.pending_closes
        assert server.positions[y_key]["status"] == POSITION_STATUS_CLOSED
        assert engine2.balance_recovery_pending == ()
        # the ambiguous INSERT adopted:
        assert cycle["entries"] == 1, cycle
        assert key in engine2.open_positions
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        adopted = engine2.open_positions[key]
        # restart ledger = initial + realized PnL of Y, minus X's fees once:
        assert engine2.balance == pytest.approx(
            10_000.0 + closed_y.net_pnl
            - adopted.entry_fee - adopted.entry_slippage_cost,
        )
        assert engine2.reconcile_balance() is True
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"],
        )

    def test_balance_markers_survive_several_failed_reconciliation_cycles(
        self, caplog,
    ):
        """(7) markers survive MULTIPLE consecutive failed reconciliation
        cycles and clear only after a confirmed successful read; the
        ledger never drifts across the subsequent stable cycles."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        _, closed_y = _stage_outbox_close(server, store, contour, clock)
        y_key = closed_y.position_key

        store2, contour2, clock2 = _contour(server)
        engine2 = contour2.engine
        assert y_key in engine2.balance_recovery_pending

        # cycles 1 and 2: EVERY balance read fails (writes still work):
        server.fail_execute_with = pg_dbapi.InternalError("ledger unreadable")
        server.fail_execute_match = "WHEN status = 'CLOSED' THEN net_pnl"
        server.fail_execute_times = 10
        for _ in range(2):
            cycle = contour2.run_cycle({"BTCUSDT": 100.0})
            # the close flushed, but the marker SURVIVED the failed reads:
            assert y_key not in server.outbox
            assert y_key in engine2.balance_recovery_pending
            assert cycle["accounting_consistent"] is False
            assert cycle["entries"] == 0
        assert engine2.balance_recovery_pending == (y_key,)

        # cycle 3: reads heal → a CONFIRMED reconciliation clears the marker:
        server.fail_execute_with = None
        server.fail_execute_match = None
        cycle3 = contour2.run_cycle({"BTCUSDT": 100.0})
        assert engine2.balance_recovery_pending == ()
        assert cycle3["accounting_consistent"] is True

        # cycles 4..6: stable ledger — no drift, marker never returns:
        balance_after = engine2.balance
        for _ in range(3):
            cycle = contour2.run_cycle({"BTCUSDT": 100.0})
            assert cycle["accounting_consistent"] is True
            assert engine2.balance_recovery_pending == ()
        assert engine2.balance == pytest.approx(balance_after)
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"],
        )


# ======================================================================
# Ledger consistency / no re-accrual
# ======================================================================

class TestLedgerConsistency:
    def test_memory_balance_equals_persisted_ledger_after_full_recovery(
        self, caplog,
    ):
        """(8) adoption + an UNCONFIRMABLE funding write + full recovery →
        memory balance == persisted ledger, markers and deltas empty."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine

        # (a) ambiguous INSERT → adoption (fees booked once):
        assert _capture(
            store, _candidate("BTCUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        event_key = _captured_event_key(store)
        key = build_position_key(event_key)
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "INSERT_POSITION"
        assert contour.run_cycle({"BTCUSDT": 100.0})["entries"] == 1
        position = engine.open_positions[key]

        # (b) funding write that CANNOT be confirmed (store unreachable):
        clock.advance(hours=8)
        server.fail_execute_with = pg_exceptions.InterfaceError("reset")
        server.fail_execute_match = "UPDATE"
        server.fail_execute_times = 10
        server.connect_failures = 100
        contour.run_cycle({position.symbol: 100.0}, {position.symbol: 0.01})
        assert key in engine._dirty_funding
        assert key in engine.uncommitted_deltas
        # NOT applied server-side, NOT claimed durable:
        assert server.positions[key]["funding_periods"] == 0

        # (c) full recovery — the dirty snapshot commits exactly once:
        server.fail_execute_with = None
        server.fail_execute_match = None
        server.connect_failures = 0
        contour.run_cycle({position.symbol: 100.0}, {position.symbol: 0.01})
        assert key not in engine._dirty_funding
        assert dict(engine.uncommitted_deltas) == {}
        assert server.positions[key]["funding_periods"] == 1

        # (d) the ledger reconciles EXACTLY:
        assert engine.reconcile_balance() is True
        assert engine.balance_recovery_pending == ()
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )
        expected = (
            10_000.0 - position.entry_fee - position.entry_slippage_cost
            - position.funding_paid
        )
        assert engine.balance == pytest.approx(expected)
        assert dict(engine.uncommitted_deltas) == {}
        assert engine.accounting_consistent is True

    def test_no_reaccrual_of_fees_funding_or_pnl(self, caplog):
        """(9) across adoption, an ambiguous funding commit, three idle
        cycles and a final TIMEOUT close: every number is booked ONCE and
        memory stays equal to the persisted ledger throughout."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine

        # ambiguous INSERT → adoption (fees booked exactly ONCE):
        assert _capture(
            store, _candidate("BTCUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        event_key = _captured_event_key(store)
        key = build_position_key(event_key)
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "INSERT_POSITION"
        assert contour.run_cycle({"BTCUSDT": 100.0})["entries"] == 1
        position = engine.open_positions[key]
        fee_once = 10_000.0 - position.entry_fee - position.entry_slippage_cost
        assert engine.balance == pytest.approx(fee_once)

        # ambiguous funding: attempt applies, the retried statement
        # re-applies IDENTICAL absolute values (absorbed) — the ledger
        # must hold exactly ONE accrual:
        clock.advance(hours=8)
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "UPDATE_POSITION"
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            contour.run_cycle(
                {position.symbol: 100.0}, {position.symbol: 0.01},
            )

        notional = position.entry_price * position.position_size
        expected_paid = -(notional * (0.01 / 100.0))
        assert position.funding_paid == pytest.approx(expected_paid)
        assert position.funding_periods_charged == 1
        assert server.positions[key]["funding_paid"] == pytest.approx(
            expected_paid,
        )
        assert server.positions[key]["funding_periods"] == 1
        assert engine.balance == pytest.approx(fee_once - position.funding_paid)
        assert key not in engine._dirty_funding
        assert dict(engine.uncommitted_deltas) == {}
        assert engine.accounting_consistent is True

        # three idle cycles: NO re-accrual, NO extra writes, no drift:
        updates = server.apply_log.count(("UPDATE_POSITION", key))
        for _ in range(3):
            cycle = contour.run_cycle(
                {position.symbol: 100.0}, {position.symbol: 0.01},
            )
            assert cycle["entries"] == 0
        assert position.funding_periods_charged == 1
        assert position.funding_paid == pytest.approx(expected_paid)
        assert server.apply_log.count(("UPDATE_POSITION", key)) == updates
        assert len(server.positions) == 1
        assert server.apply_log.count(("INSERT_POSITION", key)) == 1
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )

        # final TIMEOUT close: the whole funding backlog settles ONCE and
        # the realized PnL lands in both ledgers identically:
        clock.advance(hours=120)                  # total 128h = deadline
        cycle = contour.run_cycle(
            {position.symbol: 100.0}, {position.symbol: 0.01},
        )
        assert cycle["exit_reasons"] == ["TIMEOUT"]
        assert position.funding_periods_charged == 16   # 16 x 8h periods
        assert server.positions[key]["funding_periods"] == 16
        assert len(server.positions) == 1
        assert engine.reconcile_balance() is True
        closed = engine.closed_positions()[0]
        assert closed.position_key == key
        assert engine.balance == pytest.approx(10_000.0 + closed.net_pnl)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"],
        )


# ======================================================================
# RUNNING vs DRAINING / EXIT_ONLY
# ======================================================================

class TestRunningAndExitOnly:
    def test_exit_only_ambiguous_funding_and_protective_exit(self, caplog):
        """(10) under DRAINING / EXIT_ONLY: the ambiguous funding commit is
        confirmed exactly once, no entry is ever attempted, and the
        protective TIMEOUT exit still runs and persists."""
        server = _FakeServer()
        store, contour, clock = _contour(server)     # RUNNING
        engine = contour.engine
        position = _open(contour, store, clock)
        key = position.position_key
        server.set_status("DRAINING")

        # ambiguous funding commit while already in EXIT_ONLY:
        clock.advance(hours=8)
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "UPDATE_POSITION"
        server.fail_execute_with = pg_dbapi.IntegrityError("chk_funding")
        server.fail_execute_match = "UPDATE"
        server.fail_execute_skip = 1
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle(
                {position.symbol: 100.0}, {position.symbol: 0.01},
            )

        assert cycle["activated"] is True
        assert cycle["exit_only"] is True
        assert cycle["entries"] == 0                    # NEW_ENTRIES refused
        assert cycle["pending_events"] == 0             # never even queried
        # funding confirmed via read-back, applied exactly once:
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1
        assert position.funding_periods_charged == 1
        assert key not in engine._dirty_funding
        assert dict(engine.uncommitted_deltas) == {}
        assert "CONFIRMED applied" in caplog.text
        assert engine.accounting_consistent is True

        # protective exits CONTINUE under EXIT_ONLY:
        clock.advance(hours=120)                        # total 128h deadline
        cycle2 = contour.run_cycle(
            {position.symbol: 100.0}, {position.symbol: 0.01},
        )
        assert cycle2["exit_only"] is True
        assert cycle2["entries"] == 0
        assert cycle2["exit_reasons"] == ["TIMEOUT"]
        assert engine.open_positions == {}
        row = server.positions[key]
        assert row["status"] == POSITION_STATUS_CLOSED
        assert row["funding_periods"] == 16             # backlog settled once
        assert server.outbox == {}                      # close fully durable


# ======================================================================
# P2 — ambiguous terminal close COMMIT (review follow-up)
# ======================================================================

# Precise injection target: ONLY the terminal/funding position UPDATE
# (the outbox INSERT contains "DO UPDATE" and the event mark starts with
# "UPDATE <event table>", so a bare "UPDATE" match would over-match).
_UPDATE_ONLY = f"UPDATE {POSITION_TABLE} SET"
_TP_PRICE = 89.0            # TP of the 99.95 entry is 89.955: no gap up


def _close_delta_for(engine, position) -> float:
    """The exact close_delta the engine books into memory (mirror of _close).

    TP closes use the TARGET price as the fill reference (review-fixed
    conservative fill), so the mirror must too.
    """
    ref_price = float(position.target_price)
    slip = float(engine.settings.slippage_percent)
    fill = ref_price * (1.0 + slip)
    gross = (position.entry_market_price - ref_price) * position.position_size
    exit_fee = fill * position.position_size * float(engine.settings.taker_fee)
    exit_slip = (fill - ref_price) * position.position_size
    return gross - exit_fee - exit_slip


class TestAmbiguousTerminalCloseCommit:
    def test_terminal_commit_ack_lost_confirmed_without_close_delta(
        self, caplog,
    ):
        """(11) terminal UPDATE applied + ACK lost → verified against the
        stored row and settled WITHOUT an uncommitted close delta, so a
        later reconcile_balance() can never double-count the PnL."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        position = _open(contour, store, clock)
        key = position.position_key
        clock.advance(hours=1)                    # < funding interval: no I/O
        # Force the ambiguity the review targets: the first commit APPLIES
        # the terminal facts and loses the ACK; the unit-level idempotent
        # retry re-executes the statement, which is rejected — so the
        # engine must verify the outcome against the stored row itself.
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "UPDATE_POSITION"
        server.fail_execute_with = pg_dbapi.IntegrityError("ack_retry_blocked")
        server.fail_execute_match = _UPDATE_ONLY
        server.fail_execute_skip = 1              # the FIRST apply goes through
        server.fail_execute_times = 1              # its retried twin fails
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle({position.symbol: _TP_PRICE})

        assert cycle["exits"] == 1, cycle
        assert cycle["exit_reasons"] == ["TAKE_PROFIT_1"], cycle
        row = server.positions[key]
        assert row["status"] == POSITION_STATUS_CLOSED   # committed server-side
        assert store.reconnect_count == 1               # verify read healed the socket
        assert "CONFIRMED applied despite a lost acknowledgement" in caplog.text
        # nothing left to replay and NO local close delta to double-count:
        assert dict(engine.pending_closes) == {}
        assert dict(engine.uncommitted_deltas) == {}
        assert dict(engine._dirty_funding) == {}
        assert engine._balance_trusted is True          # entries stay open
        assert server.outbox == {}                      # staged, then cleared
        # exactly ONE application of the terminal facts (no replay happened):
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1

        # (5) an explicit reconciliation must NOT move the balance:
        before = engine.balance
        assert engine.reconcile_balance() is True
        assert engine.balance == before
        # (8) memory == persisted ledger, derived independently:
        components = store.balance_components()
        assert components["balance"] == pytest.approx(10_000.0 + row["net_pnl"])
        assert engine.balance == pytest.approx(components["balance"])

        # no drift across idle cycles, no further writes for this key:
        for _ in range(3):
            clock.advance(minutes=30)
            idle = contour.run_cycle({position.symbol: 100.0})
            assert idle["exits"] == 0 and idle["entries"] == 0, idle
        assert engine.balance == pytest.approx(components["balance"])
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1

    def test_terminal_ack_lost_unverifiable_fails_closed_until_replay(
        self, caplog,
    ):
        """(12) applied terminal COMMIT + lost ACK + exhausted reconnects →
        the outcome stays UNVERIFIABLE: pending close + local delta are
        kept, new entries fail closed, and one idempotent replay of the
        ORIGINAL snapshot converges everything byte-for-byte."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        position = _open(contour, store, clock)
        key = position.position_key
        clock.advance(hours=1)
        assert _capture(
            store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "UPDATE_POSITION"
        server.connect_failures = 100
        attempts_before = server.connect_attempts
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle(
                {position.symbol: _TP_PRICE, "ETHUSDT": 100.0},
            )

        assert cycle["exits"] == 1, cycle
        assert cycle["exit_reasons"] == ["TAKE_PROFIT_1"], cycle
        # every store unit burns exactly the bounded 3 reconnect attempts:
        delta = server.connect_attempts - attempts_before
        assert delta >= 3 and delta % 3 == 0, delta
        assert store.reconnect_count == 0              # nothing healed
        row = server.positions[key]
        assert row["status"] == POSITION_STATUS_CLOSED # applied, ACK lost
        # UNVERIFIABLE → pending close + close delta retained, never assumed:
        assert key in engine.pending_closes
        assert key in engine.uncommitted_deltas
        assert engine.uncommitted_deltas[key] == pytest.approx(
            _close_delta_for(engine, position),
        )
        assert engine._balance_trusted is False
        # new entries fail closed (balance not trustworthy while the
        # terminal outcome is unconfirmed):
        assert cycle["entries"] == 0
        assert cycle["pending_events_unavailable"] is True
        assert "not trustworthy" in caplog.text

        # Recovery: ONE idempotent replay of the original snapshot settles
        # the close, the delta and the trust gate in the same cycle:
        snap = engine.pending_closes[key]
        snap_facts = (
            snap.closed_at, snap.exit_reason, snap.exit_price,
            snap.net_pnl, snap.gross_pnl, snap.funding_periods_charged,
        )
        server.connect_failures = 0
        cycle2 = contour.run_cycle(
            {position.symbol: 100.0, "ETHUSDT": 100.0},
        )
        assert cycle2["entries"] == 1                  # gate reopened
        assert dict(engine.pending_closes) == {}
        assert dict(engine.uncommitted_deltas) == {}
        assert engine._balance_trusted is True
        row = server.positions[key]
        assert row["status"] == POSITION_STATUS_CLOSED
        closed_at, exit_reason, exit_price, net_pnl, gross_pnl, periods = (
            snap_facts
        )
        assert row["closed_at"] == closed_at           # byte-for-byte replay
        assert row["exit_reason"] == exit_reason
        assert row["exit_price"] == pytest.approx(exit_price)
        assert row["net_pnl"] == pytest.approx(net_pnl)
        assert row["gross_pnl"] == pytest.approx(gross_pnl)
        assert row["funding_periods"] == periods
        assert server.outbox == {}
        # original application + the single replay, nothing more:
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 2

        # (8) memory == persisted ledger; no drift across idle cycles:
        assert len(engine.open_positions) == 1
        eth = next(iter(engine.open_positions.values()))
        assert eth.symbol == "ETHUSDT"
        components = store.balance_components()
        assert components["balance"] == pytest.approx(
            10_000.0 + net_pnl - eth.entry_fee - eth.entry_slippage_cost,
        )
        assert engine.balance == pytest.approx(components["balance"])
        for _ in range(3):
            clock.advance(minutes=30)
            contour.run_cycle({position.symbol: 100.0, "ETHUSDT": 100.0})
        assert engine.balance == pytest.approx(components["balance"])
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 2

    def test_multiple_pending_closes_and_funding_deltas_converge(
        self, caplog,
    ):
        """(13) TWO positions with uncommitted funding deltas and TWO
        pending closes converge in one recovery cycle; afterwards the
        memory balance equals the persisted ledger with no drift."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        assert _capture(
            store, _candidate("BTCUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        assert _capture(
            store, _candidate("ETHUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        cycle0 = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle0["entries"] == 2, cycle0
        btc, eth = list(engine.open_positions.values())
        assert {btc.symbol, eth.symbol} == {"BTCUSDT", "ETHUSDT"}
        b_key, e_key = btc.position_key, eth.position_key
        assert _capture(
            store, _candidate("XRPUSDT", 100.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED                       # refused below by the trust gate

        # ONE cycle: funding fails for BOTH positions (dirty snapshot +
        # local delta each), then BOTH terminal updates fail as well
        # (pending close + close delta each) — 4 attempts per unit:
        clock.advance(hours=8)
        server.fail_execute_with = pg_exceptions.InterfaceError("reset")
        server.fail_execute_match = _UPDATE_ONLY
        server.fail_execute_times = 16       # funding x2 + close x2 = 4x4
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle(
                {"BTCUSDT": _TP_PRICE, "ETHUSDT": _TP_PRICE},
                {"BTCUSDT": 0.01, "ETHUSDT": 0.01},   # observed rates
            )
        assert server.fail_execute_with is None        # exhausted exactly
        assert cycle["exits"] == 2, cycle
        assert cycle["entries"] == 0                  # refused (dirty gate)
        assert cycle["pending_events"] == 1           # XRP captured, refused
        # the refusal is the fail-closed gate: unconfirmed funding writes
        # block new entries until accounting consistency is restored:
        assert "not yet confirmed by PostgreSQL" in caplog.text
        assert "write NOT confirmed by PostgreSQL" in caplog.text
        assert set(engine.pending_closes) == {b_key, e_key}
        assert set(engine._dirty_funding) == {b_key, e_key}
        assert set(engine.uncommitted_deltas) == {b_key, e_key}
        # The store is reachable, so the in-cycle reconciliation PROVES
        # memory == confirmed + deltas and reopens the gate immediately;
        # the unconfirmed closes themselves stay retained (pending + delta):
        assert engine._balance_trusted is True
        assert server.positions[b_key]["status"] == POSITION_STATUS_OPEN
        assert server.positions[e_key]["status"] == POSITION_STATUS_OPEN
        snap_facts = {
            k: (
                v.closed_at, v.exit_reason, v.exit_price, v.net_pnl,
                v.gross_pnl, v.funding_paid, v.funding_periods_charged,
            )
            for k, v in engine.pending_closes.items()
        }

        # Recovery: both pending closes replay byte-for-byte, both funding
        # deltas land exactly once, the trust gate reopens for XRP:
        cycle2 = contour.run_cycle(
            {"BTCUSDT": 100.0, "ETHUSDT": 100.0, "XRPUSDT": 100.0},
        )
        assert cycle2["entries"] == 1, cycle2
        assert dict(engine.pending_closes) == {}
        assert dict(engine.uncommitted_deltas) == {}
        assert dict(engine._dirty_funding) == {}
        assert engine._balance_trusted is True
        xrp = next(
            p for p in engine.open_positions.values()
            if p.symbol == "XRPUSDT"
        )
        expected_net = 0.0
        for k in (b_key, e_key):
            row = server.positions[k]
            assert row["status"] == POSITION_STATUS_CLOSED
            closed_at, exit_reason, exit_price, net_pnl, gross_pnl, paid, per = (
                snap_facts[k]
            )
            assert row["closed_at"] == closed_at        # byte-for-byte replay
            assert row["exit_reason"] == exit_reason
            assert row["exit_price"] == pytest.approx(exit_price)
            assert row["net_pnl"] == pytest.approx(net_pnl)
            assert row["gross_pnl"] == pytest.approx(gross_pnl)
            assert row["funding_paid"] == pytest.approx(paid)   # delta landed
            assert row["funding_periods"] == per
            # only the replay applied (the failed attempts never did):
            assert server.apply_log.count(("UPDATE_POSITION", k)) == 1
            expected_net += net_pnl
        assert server.outbox == {}

        # (8) memory == persisted ledger, derived independently:
        components = store.balance_components()
        assert components["balance"] == pytest.approx(
            10_000.0 + expected_net
            - xrp.entry_fee - xrp.entry_slippage_cost,
        )
        assert engine.balance == pytest.approx(components["balance"])
        # no drift across idle cycles, no extra writes:
        for _ in range(3):
            clock.advance(minutes=30)
            contour.run_cycle(
                {"BTCUSDT": 100.0, "ETHUSDT": 100.0, "XRPUSDT": 100.0},
            )
        assert engine.balance == pytest.approx(components["balance"])
        for k in (b_key, e_key):
            assert server.apply_log.count(("UPDATE_POSITION", k)) == 1

    def test_flush_replay_ack_lost_settles_via_verification(self, caplog):
        """(14) an ambiguous COMMIT INSIDE flush_pending_closes() (the
        replay path) is verified and settled through the same
        confirmation: no duplicate delta, no pending close left, the
        original terminal facts preserved byte-for-byte."""
        server = _FakeServer()
        store, contour, clock = _contour(server)
        engine = contour.engine
        position = _open(contour, store, clock)
        key = position.position_key
        clock.advance(hours=1)
        # 1) the terminal UPDATE is rejected outright (SQL error): the
        #    pending close + local delta are retained, balance not trusted:
        server.fail_execute_with = pg_dbapi.IntegrityError("terminal_rejected")
        server.fail_execute_match = _UPDATE_ONLY
        server.fail_execute_times = 1         # SQL errors raise at once
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle({position.symbol: _TP_PRICE})
        assert cycle["exits"] == 1, cycle
        assert server.fail_execute_with is None        # exhausted exactly
        assert key in engine.pending_closes
        assert key in engine.uncommitted_deltas
        # The store is reachable: the in-cycle reconciliation proves
        # memory == confirmed + delta (the row is still OPEN, so the delta
        # is exactly what is missing) and reopens the gate at once — the
        # unconfirmed close itself stays retained until the replay:
        assert engine._balance_trusted is True
        assert server.positions[key]["status"] == POSITION_STATUS_OPEN
        snap = engine.pending_closes[key]
        snap_facts = (
            snap.closed_at, snap.exit_reason, snap.exit_price,
            snap.net_pnl, snap.gross_pnl, snap.funding_periods_charged,
        )

        # 2) the RETRY applies the snapshot but loses the ACK — and its
        #    own idempotent re-execution is rejected, so the outcome is
        #    genuinely ambiguous and must be verified against the row:
        server.commit_ack_lost = True
        server.commit_ack_lost_on = "UPDATE_POSITION"
        server.fail_execute_with = pg_dbapi.IntegrityError("ack_retry_blocked")
        server.fail_execute_match = _UPDATE_ONLY
        server.fail_execute_skip = 1              # the FIRST apply goes through
        server.fail_execute_times = 1              # its retried twin fails
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle({position.symbol: 100.0})
        assert cycle2["exits"] == 0, cycle2
        assert server.fail_execute_with is None
        assert (
            "flush CONFIRMED applied despite a lost acknowledgement"
            in caplog.text
        )
        assert dict(engine.pending_closes) == {}
        assert dict(engine.uncommitted_deltas) == {}
        assert engine._balance_trusted is True         # reconciliation succeeded
        assert store.reconnect_count == 1             # verify healed the socket
        row = server.positions[key]
        assert row["status"] == POSITION_STATUS_CLOSED
        closed_at, exit_reason, exit_price, net_pnl, gross_pnl, periods = (
            snap_facts
        )
        assert row["closed_at"] == closed_at           # byte-for-byte replay
        assert row["exit_reason"] == exit_reason
        assert row["exit_price"] == pytest.approx(exit_price)
        assert row["net_pnl"] == pytest.approx(net_pnl)
        assert row["gross_pnl"] == pytest.approx(gross_pnl)
        assert row["funding_periods"] == periods
        assert server.outbox == {}
        # only the replay ever applied (the rejected attempt never did):
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1

        # (8) memory == persisted ledger; no drift across idle cycles:
        components = store.balance_components()
        assert components["balance"] == pytest.approx(10_000.0 + net_pnl)
        assert engine.balance == pytest.approx(components["balance"])
        for _ in range(3):
            clock.advance(minutes=30)
            contour.run_cycle({position.symbol: 100.0})
        assert engine.balance == pytest.approx(components["balance"])
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1


# ======================================================================
# Frozen protocol (review guard)
# ======================================================================

def test_frozen_protocol_fingerprint_unchanged():
    assert protocol_fingerprint() == FROZEN_FINGERPRINT
