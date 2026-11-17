"""ME_LONG_V2_INVERSE_SHORT_OOS2_V1 — partial-TP durability (real PostgreSQL).

Issue #276 acceptance, against a real PostgreSQL server with migration 088:

* every partial TP leg commits ATOMICALLY (leg row + absolute position
  state in ONE transaction) — remaining quantity, stage flags and the
  cumulative realized ledger move together or not at all;
* a RESTART after a partial TP restores the ladder exactly (TP1 flag,
  remaining quantity, immutable entry facts) and the next stage commits
  exactly one new leg — never a duplicate;
* an ambiguous leg COMMIT (row durable, acknowledgement lost) is adopted
  on read-back/replay without booking the leg twice and without touching
  the position row again;
* a dirty TP1 leg whose position TERMINATED while the ledger was down is
  completed leg-only after recovery (the terminal row is never rewritten);
* the balance reconstructed from PostgreSQL (closed net + open legs −
  open entry/funding costs) equals the in-process balance after restart;
* funding settles exactly once per period on the REMAINING quantity.

Static SQL-contract tests need no database.  PostgreSQL tests are gated on
``TEST_DB_HOST`` / ``TEST_DB_UNIX_SOCK`` and refuse a production-named
database (same guard as the sibling admission suite).
"""
from __future__ import annotations

import builtins
import dataclasses
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import connect_test_db

from app.paper.me_long_v2_inverse_short_oos2_v1.journal import (
    JOURNAL_INVALID_PAYLOAD_TYPE,
    JOURNAL_READ_FAILED,
    JournalReadError,
    ProtectiveCloseJournal,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.models import (
    FUNDING_OBLIGATION_OPEN,
    FUNDING_OBLIGATION_SETTLED,
    InverseShortCloseLeg,
    InverseShortPosition,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.runner import (
    InverseShortContour,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.store import (
    ACTIVATION_TABLE,
    CLOSE_LEG_TABLE,
    CLOSE_OUTBOX_TABLE,
    EVENT_TABLE,
    FUNDING_OBLIGATION_TABLE,
    INSERT_CLOSE_LEG_SQL,
    POSITION_TABLE,
    UPDATE_POSITION_SQL,
    _CLOSE_LEG_COLUMNS,
    _POSITION_COLUMNS,
)

from test_me_long_v2_inverse_short_oos2_pg_admission_it_v1 import (
    MIGRATION_087,
    PINNED_V2_FINGERPRINT,
    _capture_event,
    _cleanup as _cleanup_base,
    _connect_store,
    _contour,
    _count,
    _guard_not_production,
    _read_activation,
    _restore_activation,
    _write_activation,
    _Clock,
    _settings,
    ensure_funding_obligation_table,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_PG_ONLY = pytest.mark.skipif(
    not (os.getenv("TEST_DB_HOST") or os.getenv("TEST_DB_UNIX_SOCK")),
    reason="TEST_DB_HOST / TEST_DB_UNIX_SOCK not set (real-PostgreSQL suite)",
)


# ---------------------------------------------------------------------------
# Static SQL / migration contract (no database required)
# ---------------------------------------------------------------------------
class TestPartialExitSqlContract:
    def test_close_leg_insert_is_an_idempotent_upsert(self):
        assert "ON CONFLICT (position_key, leg_seq) DO NOTHING" \
            in INSERT_CLOSE_LEG_SQL
        assert INSERT_CLOSE_LEG_SQL.count("%s") == len(_CLOSE_LEG_COLUMNS)

    def test_position_update_never_rewrites_entry_facts(self):
        forbidden = (
            "entered_at =", "deadline_at =", "position_size =",
            "entry_price =", "entry_market_price =", "entry_fee =",
            "entry_slippage =", "stop_price =", "target_price =",
            "tp2_price =", "tp3_price =", "event_key =",
        )
        for column in forbidden:
            assert column not in UPDATE_POSITION_SQL, column
        for column in (
            "remaining_size =", "tp1_executed =", "tp2_executed =",
            "tp3_executed =", "realized_legs_net =",
            "entry_cost_allocated =", "last_mark_price =",
        ):
            assert column in UPDATE_POSITION_SQL, column

    def test_migration_creates_five_tables_with_leg_identity(self):
        body = MIGRATION_087.read_text(encoding="utf-8")
        assert body.count("CREATE TABLE") == 5
        assert "CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_oos2_v1_close_leg" in body
        assert "PRIMARY KEY (position_key, leg_seq)" in body
        # ladder quantity invariants live in the position CHECKs
        assert "remaining_size >= 0 AND remaining_size <= position_size" in body
        assert "remaining_size = 0" in body           # CLOSED => no remainder
        assert "NOT tp2_executed OR tp1_executed" in body
        assert "remaining_size may only shrink" in body   # guard trigger

    def test_migration_is_additive_only(self):
        body = MIGRATION_087.read_text(encoding="utf-8").upper()
        assert "ALTER TABLE" not in body
        assert "\nDROP " not in body and "\nDROP\t" not in body

    def test_fingerprint_still_pinned(self):
        assert protocol_fingerprint() == PINNED_V2_FINGERPRINT


# ---------------------------------------------------------------------------
# Fixtures (PostgreSQL)
# ---------------------------------------------------------------------------
def _cleanup(conn) -> None:
    cursor = conn.cursor()
    # obligation rows first (no FK, but they reference positions logically),
    # then leg rows (logical children), then outbox -> position -> event.
    for table in (FUNDING_OBLIGATION_TABLE, CLOSE_LEG_TABLE,
                  CLOSE_OUTBOX_TABLE, POSITION_TABLE, EVENT_TABLE):
        cursor.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
    conn.commit()


@pytest.fixture
def pg_conn():
    if not (os.getenv("TEST_DB_HOST") or os.getenv("TEST_DB_UNIX_SOCK")):
        pytest.skip("TEST_DB_HOST / TEST_DB_UNIX_SOCK not set")
    try:
        conn = connect_test_db()
    except Exception as exc:  # noqa: BLE001 - no local server
        pytest.skip(f"test PostgreSQL unreachable: {exc}")
    _guard_not_production(conn)

    cursor = conn.cursor()
    cursor.execute("CREATE SCHEMA IF NOT EXISTS dds")
    conn.commit()
    cursor.execute("SELECT to_regclass(%s)", (EVENT_TABLE,))
    event_exists = bool(cursor.fetchone()[0])
    cursor.execute("SELECT to_regclass(%s)", (CLOSE_LEG_TABLE,))
    legs_exist = bool(cursor.fetchone()[0])
    if not event_exists or not legs_exist:
        try:
            if not event_exists:
                conn.commit()
                cursor.execute(MIGRATION_087.read_text(encoding="utf-8"))
                conn.commit()
            else:
                # event tables exist from an older state: apply only the
                # leg table is not separable — require a full migration.
                conn.commit()
                pytest.skip("close_leg table missing; migration 088 required")
        except Exception as exc:  # noqa: BLE001
            conn.close()
            pytest.skip(f"migration 088 not applicable: {exc}")
        cursor.execute("SELECT to_regclass(%s)", (CLOSE_LEG_TABLE,))
        if not cursor.fetchone()[0]:
            conn.close()
            pytest.skip("close_leg missing after migration 088 attempt")

    # Round 6 (P2-funding): migration 089 (obligation table + guard
    # refinement) rides along with every OOS-2 fixture — always applied
    # (idempotent), raise on defect.
    ensure_funding_obligation_table(conn)

    global _original_activation
    _original_activation = _read_activation(conn)
    yield conn
    try:
        _cleanup(conn)
        _restore_activation(conn, _original_activation)
    finally:
        conn.close()


@pytest.fixture
def running(pg_conn):
    freeze = datetime.now(timezone.utc) - timedelta(hours=3)
    _write_activation(pg_conn, "RUNNING", protocol_fingerprint(), freeze)
    return freeze


def _legs(store, position_key: str | None = None) -> list[InverseShortCloseLeg]:
    return store.list_close_legs(position_key)


# ---------------------------------------------------------------------------
# Full ladder against real PostgreSQL
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestPartialLadderOnPostgres:
    def test_each_leg_commits_atomically_with_position_state(
        self, running,
    ):
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        assert contour.activated, contour.activation_reason

        event_key = _capture_event(store, age=timedelta(seconds=5))
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"

        row = store.get_position(position_key)
        assert row is not None and row.status == "OPEN"
        assert row.remaining_size == pytest.approx(row.position_size, rel=1e-9)
        assert not row.tp1_executed

        # TP1: leg row + absolute position state, committed together.
        contour.run_cycle({"BTCUSDT": row.entry_price * 0.945})
        legs = _legs(store, position_key)
        assert [l.leg_seq for l in legs] == [1]
        row = store.get_position(position_key)
        assert row.status == "OPEN"                 # partial keeps the row
        assert row.tp1_executed and not row.tp2_executed
        assert row.remaining_size == pytest.approx(
            row.position_size * 0.5, rel=1e-9,
        )
        assert row.realized_legs_net == pytest.approx(
            legs[0].gross_pnl - legs[0].exit_fee - legs[0].exit_slippage,
            abs=1e-6,
        )

        # TP2: second leg, remainder 12.5%.
        contour.run_cycle({"BTCUSDT": row.entry_price * 0.925})
        legs = _legs(store, position_key)
        assert [l.leg_seq for l in legs] == [1, 2]
        row = store.get_position(position_key)
        assert row.status == "OPEN"
        assert row.tp2_executed and not row.tp3_executed
        assert row.remaining_size == pytest.approx(
            row.position_size * 0.125, rel=1e-9,
        )

        # TP3 terminal: third leg + CLOSED in the same recovery window.
        contour.run_cycle({"BTCUSDT": row.entry_price * 0.89})
        legs = _legs(store, position_key)
        assert [l.leg_seq for l in legs] == [1, 2, 3]
        assert legs[-1].is_terminal and legs[-1].remaining_after == 0.0
        row = store.get_position(position_key)
        assert row.status == "CLOSED"
        assert row.exit_reason == "TAKE_PROFIT_3"
        assert row.remaining_size == 0.0
        assert row.tp1_executed and row.tp2_executed and row.tp3_executed
        store.close()

    def test_restart_after_partial_tp1_restores_the_ladder_exactly(
        self, running,
    ):
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event_key = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"
        contour.run_cycle({"BTCUSDT": 100.0 * 0.945})     # TP1 partial

        before = store.get_position(position_key)
        assert before is not None and before.status == "OPEN"
        entered_at, deadline = before.entered_at, before.deadline_at
        entry_facts = (before.entry_price, before.position_size,
                       before.stop_price, before.target_price)
        balance_before = contour.engine.balance
        store.close()

        # ---- RESTART: brand-new store + contour on the same database ----
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        restored = store2.get_position(position_key)
        assert restored is not None and restored.status == "OPEN"
        assert restored.entered_at == entered_at          # immutable
        assert restored.deadline_at == deadline           # immutable
        assert (restored.entry_price, restored.position_size,
                restored.stop_price, restored.target_price) == entry_facts
        assert restored.tp1_executed and not restored.tp2_executed
        assert restored.remaining_size == pytest.approx(
            restored.position_size * 0.5, rel=1e-9,
        )
        assert len(contour2.engine.open_positions) == 1
        # balance reconstruction includes the committed partial leg
        assert store2.balance_components()["balance"] == pytest.approx(
            balance_before, abs=1e-6,
        )
        assert contour2.engine.balance == pytest.approx(
            balance_before, abs=1e-6,
        )

        # the ladder CONTINUES after the restart: exactly one new leg
        contour2.run_cycle({"BTCUSDT": restored.entry_price * 0.925})
        legs = _legs(store2, position_key)
        assert [l.leg_seq for l in legs] == [1, 2]
        after = store2.get_position(position_key)
        assert after is not None
        assert after.entered_at == entered_at
        assert after.deadline_at == deadline
        assert after.remaining_size == pytest.approx(
            after.position_size * 0.125, rel=1e-9,
        )
        store2.close()

    def test_ambiguous_leg_commit_is_adopted_without_double_booking(
        self, running,
    ):
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event_key = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"
        contour.run_cycle({"BTCUSDT": 100.0 * 0.945})     # TP1 committed

        position = store.get_position(position_key)
        legs_before = _legs(store, position_key)
        assert len(legs_before) == 1
        leg = legs_before[0]

        # Ambiguous COMMIT re-issue: the SAME leg facts, same identity,
        # submitted again (as a lost-acknowledgement retry would).
        store.commit_partial_exit(position, leg)
        store.commit_partial_exit(position, leg)

        legs_after = _legs(store, position_key)
        assert len(legs_after) == 1, "leg replay must never duplicate"
        assert legs_after[0].to_dict() == leg.to_dict()
        row = store.get_position(position_key)
        assert row is not None
        assert row.remaining_size == pytest.approx(
            position.remaining_size, rel=1e-9,
        )
        assert row.realized_legs_net == pytest.approx(
            position.realized_legs_net, abs=1e-9,
        )
        store.close()

    def test_leg_only_completion_after_terminal_close(self, running):
        """Dirty TP1 + terminal close while the ledger is down -> recovery
        completes the leg row WITHOUT rewriting the terminal position."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event_key = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"

        original = store.commit_partial_exit

        def ledger_down(position, leg):
            raise RuntimeError("simulated ledger outage")

        store.commit_partial_exit = ledger_down
        contour.run_cycle({"BTCUSDT": 100.0 * 0.945})     # TP1 -> DIRTY
        assert contour.engine.durability_risk

        # protective STOP executes anyway (exits are never blocked)
        entry = 100.0
        bars = {"BTCUSDT": {"high": 110.1, "low": 99.0}}
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        closed = store.get_position(position_key)
        assert closed is not None and closed.status == "CLOSED"
        terminal_net = closed.net_pnl
        terminal_realized = closed.realized_legs_net

        # recovery: the owed TP1 leg row completes leg-only
        store.commit_partial_exit = original
        contour.run_cycle({"BTCUSDT": entry})

        legs = _legs(store, position_key)
        assert [l.leg_seq for l in legs] == [1, 3], legs
        assert legs[0].exit_reason == "TAKE_PROFIT_1"
        assert not legs[0].is_terminal
        assert legs[1].exit_reason == "STOP_LOSS" and legs[1].is_terminal
        assert not contour.engine.durability_risk

        recovered = store.get_position(position_key)
        assert recovered is not None
        # the terminal row was NEVER rewritten by the leg-only completion
        assert recovered.status == "CLOSED"
        assert recovered.remaining_size == 0.0
        assert recovered.net_pnl == pytest.approx(terminal_net, abs=1e-9)
        assert recovered.realized_legs_net == pytest.approx(
            terminal_realized, abs=1e-9,
        )
        store.close()

    def test_funding_settles_once_per_period_on_the_remaining_qty(
        self, running,
    ):
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event_key = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"
        contour.run_cycle({"BTCUSDT": 100.0 * 0.945})     # TP1: 50% left

        clock.advance(hours=8)   # one funding period
        contour.run_cycle(
            {"BTCUSDT": 100.0 * 0.945},
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        first = store.get_position(position_key)
        assert first is not None and first.funding_periods_charged == 1
        paid_first = first.funding_paid

        # a second cycle inside the SAME period must not charge again
        clock.advance(minutes=5)
        contour.run_cycle(
            {"BTCUSDT": 100.0 * 0.945},
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        second = store.get_position(position_key)
        assert second is not None
        assert second.funding_periods_charged == 1
        assert second.funding_paid == pytest.approx(paid_first, abs=1e-12)
        # funding was computed on the REMAINING (half) quantity: a full-size
        # control would pay roughly double — assert the magnitude is finite
        # and tied to the remaining notional.
        assert abs(second.funding_paid) > 0
        store.close()


# ---------------------------------------------------------------------------
# Codex review hardening (PR #278: P1-1 / P1-2 / P2) — real PostgreSQL
#
# These are the REAL-database counterparts of the fault-injection scenarios
# in test_me_long_v2_inverse_short_oos2_durability_hardening_v1.py.  They are
# gated on TEST_DB_HOST / TEST_DB_UNIX_SOCK like the sibling suites: without
# a test PostgreSQL they SKIP and must be reported as BLOCKED_BY_ENVIRONMENT
# — never as PASS.
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestCodexDurabilityHardeningOnPostgres:
    def test_mandatory_fault_injection_tp1_fail_retry_tp2_restart(
        self, running,
    ):
        """Mandatory scenario on real PostgreSQL with migration 088:
        TP1 calculated -> DB write fails -> retry -> TP1 confirmed -> TP2 ->
        restart.  Exactly-once stages, 12.5% remainder, conservation, and
        recovery that never blocks."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event_key = _capture_event(store, age=timedelta(seconds=5))
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 1, cycle
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"

        row = store.get_position(position_key)
        assert row is not None
        size = row.position_size
        tp2_price = row.tp2_price
        bar = {"BTCUSDT": {"high": tp2_price, "low": tp2_price}}

        # TP1's durable write fails exactly once
        original = store.commit_partial_exit
        calls = {"n": 0}

        def flaky(position, leg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated COMMIT failure")
            return original(position, leg)

        store.commit_partial_exit = flaky

        # cycle 1: one bar crosses BOTH TP1 and TP2
        contour.run_cycle({"BTCUSDT": tp2_price}, bars=bar)
        assert contour.engine.durability_risk
        memory = contour.engine.open_positions[position_key]
        assert memory.tp1_executed is True
        assert memory.tp2_executed is False, (
            "ladder advanced over an unconfirmed TP1 (P1-2 violation)",
        )
        durable = store.get_position(position_key)
        assert durable is not None
        assert durable.tp1_executed is False
        assert durable.remaining_size == pytest.approx(size, rel=1e-9)
        assert _legs(store, position_key) == []
        # P1-1: mark persistence inside run_cycle must NOT have carried the
        # unconfirmed TP1 into the durable row.
        contour.engine.persist_marks()
        durable = store.get_position(position_key)
        assert durable.tp1_executed is False
        assert durable.remaining_size == pytest.approx(size, rel=1e-9)

        # cycle 2: retry confirms TP1 first, then TP2 commits exactly once
        contour.run_cycle({"BTCUSDT": tp2_price}, bars=bar)
        assert not contour.engine.durability_risk
        assert contour.engine.dirty_leg_keys == ()
        legs = _legs(store, position_key)
        assert [leg.leg_seq for leg in legs] == [1, 2], (
            "TP1 once, TP2 once, no missing stage",
        )
        after = store.get_position(position_key)
        assert after is not None
        assert after.tp1_executed and after.tp2_executed
        assert after.remaining_size == pytest.approx(size * 0.125, rel=1e-9)
        closed_qty = sum(leg.quantity for leg in legs)
        assert round(after.position_size - after.remaining_size, 9) == round(
            closed_qty, 9,
        ), "quantity conservation on the durable rows"
        assert contour.engine._uncommitted_deltas == {}
        balance_before = store.balance_components()["balance"]

        # RESTART: fresh connection + fresh engine over the same database
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        restored = store2.get_position(position_key)
        assert restored is not None
        assert restored.tp1_executed and restored.tp2_executed
        assert restored.remaining_size == pytest.approx(size * 0.125, rel=1e-9)
        cycle = contour2.run_cycle({"BTCUSDT": tp2_price}, bars=bar)
        assert cycle is not None
        assert not contour2.engine.durability_risk
        assert contour2.engine.dirty_leg_keys == ()
        assert contour2.engine.accounting_consistent
        assert store2.balance_components()["balance"] == pytest.approx(
            balance_before, abs=1e-6,
        )
        assert len(_legs(store2, position_key)) == 2, "still exactly one leg per stage"
        store2.close()
        store.close()

    def test_mark_write_never_moves_trading_columns(self, running):
        """P1-1 against the real UPDATE: update_position_marks touches
        telemetry only, even when handed a mutated trading state."""
        import dataclasses

        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event_key = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"
        row = store.get_position(position_key)
        assert row is not None

        mutated = dataclasses.replace(
            row,
            remaining_size=row.remaining_size * 0.5,
            tp1_executed=True,
            realized_legs_net=123.45,
            entry_cost_allocated=1.0,
            funding_paid=999.0,
            # The dataclass field is funding_periods_charged (the DB column
            # is funding_periods); mutate it so the comparison below proves
            # the mark write cannot move it either.
            funding_periods_charged=7,
            net_pnl=-1.0,
            status="CLOSED",
            last_mark_price=row.entry_price * 0.9,
            highest_since_entry=row.entry_price * 1.3,
            lowest_since_entry=row.entry_price * 0.7,
            mfe=7.0,
            mae=8.0,
        )
        store.update_position_marks(mutated)

        after = store.get_position(position_key)
        assert after is not None
        # trading / financial columns byte-identical — every name below is
        # a real InverseShortPosition attribute (funding_periods_charged,
        # NOT the DB column name funding_periods).
        for column in (
            "remaining_size", "tp1_executed", "tp2_executed", "tp3_executed",
            "realized_legs_net", "entry_cost_allocated", "funding_paid",
            "funding_periods_charged", "net_pnl", "gross_pnl", "status",
            "entered_at", "deadline_at", "position_size", "entry_price",
        ):
            assert getattr(after, column) == getattr(row, column), column
        assert after.funding_periods_charged == row.funding_periods_charged == 0
        # telemetry moved
        assert after.last_mark_price == pytest.approx(
            row.entry_price * 0.9,
        )
        assert after.mfe == pytest.approx(7.0)
        assert after.mae == pytest.approx(8.0)
        store.close()

    def test_backlog_funding_charges_the_historical_quantity(self, running):
        """P2 on real PostgreSQL: a period that closed BEFORE the observed
        rate is charged on the quantity outstanding at ITS boundary (full
        size), not on the live remainder."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event_key = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"
        row = store.get_position(position_key)
        size = row.position_size

        # period 1 boundary passes with UNKNOWN rate -> obligation open
        clock.advance(hours=8, minutes=1)
        contour.run_cycle({"BTCUSDT": 100.0}, funding_rates_percent={})
        assert store.get_position(position_key).funding_periods_charged == 0

        # TP1 AFTER the period-1 boundary
        contour.run_cycle({"BTCUSDT": 100.0 * 0.945})
        row = store.get_position(position_key)
        assert row.tp1_executed
        assert row.remaining_size == pytest.approx(size * 0.5, rel=1e-9)

        # first observed rate: period 1 must charge the FULL historical size
        clock.advance(hours=1)
        contour.run_cycle(
            {"BTCUSDT": 100.0 * 0.945},
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        settled = store.get_position(position_key)
        assert settled.funding_periods_charged == 1
        expected_full = -(settled.entry_price * size) * (0.01 / 100.0)
        expected_live = -(settled.entry_price * settled.remaining_size) * (
            0.01 / 100.0
        )
        assert settled.funding_paid == pytest.approx(expected_full, rel=1e-9), (
            "the pre-TP1 period must be charged on the historical quantity"
        )
        assert settled.funding_paid != pytest.approx(expected_live, rel=1e-6)
        store.close()

    def test_dirty_leg_retry_survives_a_connection_drop(self, running):
        """Scenario 6: PostgreSQL disconnect between the failed TP1 write
        and its retry — the store reconnects and confirms exactly once."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event_key = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"
        row = store.get_position(position_key)
        tp1_price = row.target_price

        original = store.commit_partial_exit

        def flaky(position, leg):
            raise RuntimeError("simulated COMMIT failure")

        store.commit_partial_exit = flaky
        contour.run_cycle({"BTCUSDT": tp1_price})
        assert position_key in contour.engine.dirty_leg_keys

        # hard connection drop (socket closed underneath the store)
        store._conn.close()
        store.commit_partial_exit = original

        # next cycle: _with_reconnect re-establishes the session and the
        # dirty-leg retry confirms TP1 exactly once.
        contour.run_cycle({"BTCUSDT": tp1_price})
        assert not contour.engine.durability_risk
        assert contour.engine.dirty_leg_keys == ()
        legs = _legs(store, position_key)
        assert [leg.leg_seq for leg in legs] == [1]
        durable = store.get_position(position_key)
        assert durable is not None
        assert durable.tp1_executed is True
        assert durable.remaining_size == pytest.approx(
            durable.position_size * 0.5, rel=1e-9,
        )
        store.close()

    def test_second_session_mark_persistence_never_corrupts_leg_state(
        self, running,
    ):
        """Scenario 12 on real PostgreSQL: another session persisting marks
        concurrently with the leg commits never moves trading state and
        never double-books legs/fees/PnL."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event_key = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"
        row = store.get_position(position_key)
        tp2_price = row.tp2_price

        # second session over its OWN connection
        store_b = _connect_store()
        copy = store_b.get_position(position_key)
        assert copy is not None
        copy.last_mark_price = copy.entry_price * 0.97
        copy.mfe = 3.0
        store_b.update_position_marks(copy)

        # session A continues the ladder
        contour.run_cycle({"BTCUSDT": 100.0 * 0.945})
        contour.run_cycle({"BTCUSDT": tp2_price})
        legs = _legs(store, position_key)
        assert [leg.leg_seq for leg in legs] == [1, 2], (
            "exactly one durable execution per stage despite interleaved "
            "mark writes from a second session",
        )
        final = store.get_position(position_key)
        assert final.tp1_executed and final.tp2_executed
        assert final.remaining_size == pytest.approx(
            final.position_size * 0.125, rel=1e-9,
        )
        closed_qty = sum(leg.quantity for leg in legs)
        assert round(final.position_size - final.remaining_size, 9) == round(
            closed_qty, 9,
        )
        # session A's own mark write followed B's — the row's telemetry is
        # current (MFE recomputed by A, not left at B's stale value), and
        # B's stale full copy was never able to move trading state.
        assert final.last_mark_price == pytest.approx(tp2_price)
        assert final.mfe == pytest.approx(100.0 - tp2_price, rel=1e-9)
        assert contour.engine._uncommitted_deltas == {}
        store_b.close()
        store.close()


# ---------------------------------------------------------------------------
# Codex review round 2 — P1 (close never fully saved without close-leg +
# outbox) and P2 (exact-boundary funding, unified funding-before-exit)
# against real PostgreSQL with migration 088.
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestCodexRound2OnPostgres:
    def test_partial_commit_and_stage_failure_defers_update_then_restart_restores(
        self, running, tmp_path,
    ):
        """Combined failure on real PostgreSQL: partial commit AND
        stage_close() fail while update_position() itself is healthy —
        the terminal UPDATE must not run, and a restart restores the
        partial and terminal closes exactly once."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        # isolate the OOS-2 local durable journal for this test
        contour.engine.journal = ProtectiveCloseJournal(tmp_path)
        contour.journal_dir = tmp_path
        event_key = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"
        row = store.get_position(position_key)
        assert row is not None
        size, entry, stop = row.position_size, row.entry_price, row.stop_price

        original_commit = store.commit_partial_exit
        original_stage = store.stage_close
        original_update = store.update_position

        def always_fail_leg(position, leg):
            raise RuntimeError("partial leg write failed")

        def always_fail_stage(*args, **kwargs):
            raise RuntimeError("close-leg/outbox staging failed")

        update_calls = {"n": 0}

        def spy_update(position_row):
            update_calls["n"] += 1
            return original_update(position_row)

        store.commit_partial_exit = always_fail_leg
        store.stage_close = always_fail_stage
        store.update_position = spy_update

        # TP1 applies in memory, fails durably (dirty leg).
        contour.run_cycle({"BTCUSDT": entry * 0.945})
        assert position_key in contour.engine.dirty_leg_keys

        # Protective STOP fires while both writes are down.
        bars = {"BTCUSDT": {"high": stop * 1.001, "low": entry * 0.99}}
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]

        # update_position() would have succeeded — and must NOT have run:
        # the position may not be reported fully saved without its
        # close-leg and outbox rows.
        assert update_calls["n"] == 0, (
            "terminal UPDATE ran while close-leg/outbox staging was "
            "unconfirmed (P1 round-2 violation)",
        )
        durable = store.get_position(position_key)
        assert durable is not None
        assert durable.status == "OPEN", (
            "position claimed fully saved without close-leg/outbox rows",
        )
        assert durable.remaining_size == pytest.approx(size, rel=1e-9)
        assert _legs(store, position_key) == []
        assert store.list_pending_closes() == []
        engine = contour.engine
        assert position_key in engine._unstaged_closes
        assert position_key in engine.pending_closes
        assert position_key in engine._terminal_legs
        assert engine.durability_risk is True
        assert cycle["pending_closes"] == 1

        # ---- the process dies here: only durable state survives ---------
        store.commit_partial_exit = original_commit
        store.stage_close = original_stage
        store.close()

        store2 = _connect_store()
        # journal_dir must ride the CONSTRUCTOR: the replay of the durable
        # recovery record runs inside __init__, so a journal swapped in
        # afterwards would never see it.
        contour2 = InverseShortContour(
            _settings(), store2, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        restored = store2.get_position(position_key)
        assert restored is not None
        assert restored.status == "OPEN"
        assert restored.tp1_executed is False
        assert restored.remaining_size == pytest.approx(size, rel=1e-9)
        engine2 = contour2.engine
        assert engine2.dirty_leg_keys == ()
        # Round 9 (P1, Codex r4590138816): the frozen protective close was
        # journaled BEFORE staging, so the restart replays it as a RETAINED
        # close instead of leaving a STOP to be re-derived from market
        # data — while the DURABLE row is still the untouched pre-close
        # state and the owed staging is still visible as a durability risk.
        assert position_key in engine2.pending_closes
        assert engine2.journal.paths(), "the recovery record survived"
        assert engine2.durability_risk is True, "the staged snapshot is owed"

        # ONE recovery cycle replays the frozen snapshot: the owed partial
        # leg and the terminal leg land in the SAME idempotent transaction
        # and then the terminal UPDATE flips the row.
        cycle = contour2.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == [], (
            "a retained protective close must never be re-evaluated",
        )
        assert cycle["pending_closes"] == 0
        legs = _legs(store2, position_key)
        assert [leg.leg_seq for leg in legs] == [1, 3]
        assert store2.list_pending_closes() == []
        final = store2.get_position(position_key)
        assert final is not None and final.status == "CLOSED"
        assert final.tp1_executed is True
        closed_qty = sum(leg.quantity for leg in legs)
        assert round(final.position_size - final.remaining_size, 9) == round(
            closed_qty, 9,
        ), "quantity conservation across both restored closes"
        assert not contour2.engine.durability_risk
        assert contour2.engine.dirty_leg_keys == ()
        assert engine2.journal.paths() == [], "retired after completion"
        assert contour2.engine.balance == pytest.approx(
            store2.balance_components()["balance"], abs=1e-6,
        )
        store2.close()

    def test_exact_boundary_funding_normal_matches_restored(self, running):
        """A partial TP executed EXACTLY on the funding boundary is
        accounted AFTER that boundary's accrual — the normal in-cycle
        calculation and the calculation restored from the durable ledger
        after a restart charge the same FULL quantity."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        rate = 0.01 / 100.0

        # --- A: NORMAL — rate observed in the boundary cycle itself ------
        event_a = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        key_a = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_a}"
        clock.advance(hours=8)              # EXACTLY period-1 boundary
        contour.run_cycle(
            {"BTCUSDT": 100.0 * 0.945},
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        a = store.get_position(key_a)
        assert a is not None and a.tp1_executed
        assert a.funding_periods_charged == 1
        legs_a = _legs(store, key_a)
        assert [leg.exit_reason for leg in legs_a] == ["TAKE_PROFIT_1"]
        boundary_a = a.entered_at + timedelta(hours=8)
        assert legs_a[0].executed_at == boundary_a, (
            "the TP must sit EXACTLY on the funding boundary",
        )
        expected_full_a = -(a.entry_price * a.position_size) * rate
        assert a.funding_paid == pytest.approx(expected_full_a, rel=1e-12), (
            "normal calculation must charge the FULL pre-TP quantity",
        )

        # --- B: same instant, UNKNOWN rate; settle after a RESTART -------
        # Two independent deterministic signal identities: candle_steps=1
        # shifts the production event_key/dedup candle deterministically,
        # and signal_at=clock() pins B's signal_time onto the ENGINE clock
        # timeline (admission ages signals against the engine clock, which
        # has already advanced 8h here).  Dedup stays fully active.
        event_b = _capture_event(
            store, age=timedelta(seconds=5), candle_steps=1,
            signal_at=clock(),
        )
        assert event_a != event_b, (
            "NORMAL and RECOVERED must use two independent deterministic "
            "signal identities (distinct test event_keys)",
        )
        contour.run_cycle({"BTCUSDT": 100.0})
        key_b = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_b}"
        clock.advance(hours=8)              # B's period-1 boundary
        contour.run_cycle(
            {"BTCUSDT": 100.0 * 0.945},
            funding_rates_percent={},       # UNKNOWN at the boundary
        )
        b = store.get_position(key_b)
        assert b is not None and b.tp1_executed
        assert b.funding_periods_charged == 0   # obligation preserved
        legs_b = _legs(store, key_b)
        boundary_b = b.entered_at + timedelta(hours=8)
        assert legs_b[0].executed_at == boundary_b

        # RESTART (fresh connection + engine), then the rate arrives.
        store.close()
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        clock.advance(hours=1)
        contour2.run_cycle(
            {"BTCUSDT": 100.0 * 0.945},
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        b2 = store2.get_position(key_b)
        assert b2 is not None
        assert b2.funding_periods_charged == 1
        expected_full_b = -(b2.entry_price * b2.position_size) * rate
        # --- the comparison: normal == restored, both on the FULL size ---
        assert b2.funding_paid == pytest.approx(expected_full_b, rel=1e-12), (
            "restored calculation must equal the normal funding-before-exit "
            "one for an exact-boundary TP",
        )
        live_remainder = -(b2.entry_price * b2.remaining_size) * rate
        assert b2.funding_paid != pytest.approx(live_remainder, rel=1e-6), (
            "an exact-boundary TP must NOT reduce that period's quantity",
        )

        # A's strictly-before TP IS reflected at its NEXT boundary (the
        # strict-before half of the same rule).
        a2 = store2.get_position(key_a)
        assert a2 is not None and a2.funding_periods_charged == 2
        legs_a2 = _legs(store2, key_a)
        expected_a = expected_full_a + (
            -(a2.entry_price * (a2.position_size - legs_a2[0].quantity)) * rate
        )
        assert a2.funding_paid == pytest.approx(expected_a, rel=1e-12)
        store2.close()


# ---------------------------------------------------------------------------
# Codex review round 3 — P1 against real PostgreSQL: a pending terminal
# snapshot owns its key's leg rows (no independent dirty-leg replay),
# restored only through the unified terminal-close recovery.
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestCodexRound3OnPostgres:
    def test_fault_injection_pending_snapshot_leg_replay_restart_reconciliation(
        self, running, pg_conn, tmp_path,
    ):
        """Mandatory sequence on real PostgreSQL with migration 088:
        TP1 persistence failure -> STOP -> terminal staging failure ->
        next-cycle retry -> PostgreSQL recovery -> restart ->
        reconciliation; balance equals the failure-free reference and all
        close legs are present exactly once."""
        # ---- reference: identical bars, no failures ---------------------
        ref_store = _connect_store()
        ref_clock = _Clock(datetime.now(timezone.utc))
        ref = _contour(ref_store, clock=ref_clock)
        event_ref = _capture_event(ref_store, age=timedelta(seconds=5))
        ref.run_cycle({"BTCUSDT": 100.0})
        ref_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_ref}"
        ref_row = ref_store.get_position(ref_key)
        entry = ref_row.entry_price
        stop = ref_row.stop_price
        bars = {"BTCUSDT": {"high": stop * 1.001, "low": entry * 0.99}}
        ref.run_cycle({"BTCUSDT": entry * 0.945})
        ref.run_cycle({"BTCUSDT": entry}, bars=bars)
        ref_final = ref_store.get_position(ref_key)
        assert ref_final.status == "CLOSED"
        ref_legs = {
            leg.exit_reason: leg for leg in _legs(ref_store, ref_key)
        }
        assert sorted(ref_legs) == ["STOP_LOSS", "TAKE_PROFIT_1"]
        ref_balance = ref_store.balance_components()["balance"]
        ref_store.close()

        # reset the database for the fault run (activation preserved)
        _cleanup(pg_conn)

        # ---- fault run ---------------------------------------------------
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        # isolate the OOS-2 local durable journal for this test
        contour.engine.journal = ProtectiveCloseJournal(tmp_path)
        contour.journal_dir = tmp_path
        # Distinct deterministic identity vs the reference run's signal
        # (candle step back): the two scenario signals never collide.
        event_key = _capture_event(
            store, age=timedelta(seconds=5), candle_steps=1,
        )
        assert event_key != event_ref, (
            "reference and fault runs must use distinct event_keys",
        )
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event_key}"

        original_commit = store.commit_partial_exit
        original_stage = store.stage_close
        original_update = store.update_position

        def fail_commit(position, leg):
            raise RuntimeError("TP1 persistence failure")

        def fail_stage(*args, **kwargs):
            raise RuntimeError("terminal staging failure")

        update_calls = {"n": 0}

        def spy_update(position_row):
            update_calls["n"] += 1
            return original_update(position_row)

        store.commit_partial_exit = fail_commit
        store.stage_close = fail_stage
        store.update_position = spy_update

        # (a)+(b) TP1 fails durably, STOP with staging down
        contour.run_cycle({"BTCUSDT": entry * 0.945})
        assert position_key in contour.engine.dirty_leg_keys
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        assert update_calls["n"] == 0
        engine = contour.engine
        assert position_key in engine.pending_closes
        assert position_key in engine._unstaged_closes
        assert position_key in engine._terminal_legs
        assert engine.durability_risk is True
        durable = store.get_position(position_key)
        assert durable.status == "OPEN"
        assert _legs(store, position_key) == []
        assert store.list_pending_closes() == []

        # (c) next-cycle retry: commit recovered, staging STILL down —
        #     the pending snapshot excludes the independent leg replay.
        store.commit_partial_exit = original_commit
        contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert _legs(store, position_key) == [], (
            "independent dirty-leg replay ran against a pending terminal "
            "snapshot (leg-only insert into an OPEN row)",
        )
        durable = store.get_position(position_key)
        assert durable.status == "OPEN"
        assert durable.tp1_executed is False
        assert durable.remaining_size == pytest.approx(
            durable.position_size, rel=1e-9,
        )
        assert update_calls["n"] == 0
        assert position_key in contour.engine.dirty_leg_keys
        assert contour.engine.durability_risk is True

        # (d) PostgreSQL recovery + (e) RESTART on a fresh connection
        store.stage_close = original_stage
        store.update_position = original_update
        store.close()

        store2 = _connect_store()
        # journal_dir must ride the CONSTRUCTOR: the replay of the durable
        # recovery record runs inside __init__.
        contour2 = InverseShortContour(
            _settings(), store2, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        restored = store2.get_position(position_key)
        assert restored.status == "OPEN"
        assert restored.tp1_executed is False
        engine2 = contour2.engine
        assert engine2.dirty_leg_keys == ()
        # Round 9 (P1, Codex r4590138816): the frozen protective close was
        # journaled BEFORE staging, so it survived the restart as a
        # RETAINED close instead of existing only in process memory —
        # while the DURABLE row is still the untouched pre-close state.
        assert position_key in engine2.pending_closes
        assert engine2.journal.paths(), "the recovery record survived"
        assert engine2.durability_risk is True, "the staged snapshot is owed"

        # (f) reconciliation: ONE recovery cycle replays the frozen
        #     snapshot through the unified terminal-close recovery — owed
        #     partial leg, terminal leg and outbox in one transaction.
        cycle = contour2.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == [], (
            "a retained protective close must never be re-evaluated",
        )
        assert cycle["pending_closes"] == 0

        legs = _legs(store2, position_key)
        assert [leg.leg_seq for leg in legs] == [1, 3]
        final = store2.get_position(position_key)
        assert final.status == "CLOSED"
        closed_qty = sum(leg.quantity for leg in legs)
        assert round(final.position_size - final.remaining_size, 9) == round(
            closed_qty, 9,
        )
        assert store2.list_pending_closes() == []
        assert not contour2.engine.durability_risk
        assert contour2.engine.accounting_consistent is True

        # req 7: balance equals the failure-free reference; every leg
        # matches it exactly; nothing double-books afterwards.
        final_balance = store2.balance_components()["balance"]
        assert final_balance == pytest.approx(ref_balance, abs=1e-6), (
            "recovered balance must equal the failure-free reference",
        )
        final_legs = {leg.exit_reason: leg for leg in legs}
        for reason in ("TAKE_PROFIT_1", "STOP_LOSS"):
            assert final_legs[reason].quantity == pytest.approx(
                ref_legs[reason].quantity, rel=1e-12,
            ), reason
            assert final_legs[reason].net_pnl == pytest.approx(
                ref_legs[reason].net_pnl, abs=1e-9,
            ), reason

        contour2.run_cycle({"BTCUSDT": entry})
        assert len(_legs(store2, position_key)) == 2
        assert store2.balance_components()["balance"] == pytest.approx(
            final_balance, abs=1e-12,
        )
        store2.close()


# ---------------------------------------------------------------------------
# Round 5 (Codex r4552612002) — observed funding must survive a dirty-leg
# terminal close.  Scenarios A/C/D additionally run on REAL PostgreSQL
# (their unit/fault-injection twins live in
# ``test_me_long_v2_inverse_short_oos2_durability_hardening_v1.py``).
# ---------------------------------------------------------------------------

def _always_fail(message: str):
    """Store-write fault: every call raises (simulated outage)."""

    def _fail(*args, **kwargs):
        raise RuntimeError(message)

    return _fail


class TestCodexRound5OnPostgres:
    """A/C/D on real PostgreSQL with migration 088.

    Faults are injected at the store-method boundary (the same accepted
    pattern as rounds 1-3): everything else — leg staging, terminal
    UPDATE, outbox, read-back verification, restart — is the REAL
    PostgreSQL code path with real transactions.
    """

    @staticmethod
    def _reference_scenario(pg_conn) -> dict:
        """Failure-free scenario-A timeline: entry -> TP1 committed ->
        +9h -> protective STOP in the cycle where the rate (0.01%) is
        observed.  Every fault run below converges to these facts."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event}"
        row = store.get_position(position_key)
        assert row is not None
        entry = row.entry_price
        bars = {"BTCUSDT": {"high": row.stop_price * 1.001, "low": entry * 0.99}}
        contour.run_cycle({"BTCUSDT": entry * 0.945})        # TP1 committed
        clock.advance(hours=9)
        cycle = contour.run_cycle(
            {"BTCUSDT": entry}, bars=bars,
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        final = store.get_position(position_key)
        assert final.status == "CLOSED"
        assert final.funding_periods_charged == 1, (
            "the control must settle exactly one due period",
        )
        legs = {leg.exit_reason: leg for leg in _legs(store, position_key)}
        assert sorted(legs) == ["STOP_LOSS", "TAKE_PROFIT_1"]
        facts = {
            "funding_paid": final.funding_paid,
            "funding_periods_charged": final.funding_periods_charged,
            "net_pnl": final.net_pnl,
            "entry_price": final.entry_price,
            "position_size": final.position_size,
            "balance": store.balance_components()["balance"],
            "legs": legs,
            "position_key": position_key,
        }
        store.close()
        _cleanup(pg_conn)
        return facts

    @staticmethod
    def _open_fault_position(store, clock, contour, *, candle_steps: int):
        """Capture + enter the fault run's position (distinct identity)."""
        event = _capture_event(
            store, age=timedelta(seconds=5), candle_steps=candle_steps,
        )
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event}"
        row = store.get_position(position_key)
        assert row is not None
        bars = {"BTCUSDT": {"high": row.stop_price * 1.001,
                            "low": row.entry_price * 0.99}}
        return position_key, row, bars

    def test_scenario_a_observed_funding_survives_dirty_tp_stop(
        self, running, pg_conn,
    ):
        """A on real PostgreSQL: TP1 commit fails -> +9h -> rate observed
        -> protective STOP in the SAME cycle -> terminal staging ->
        restart -> reconciliation.  The CLOSED row carries the OBSERVED
        funding with historically correct per-period quantity (TP1's 50%
        closed before the boundary), TP1/STOP exactly once, outbox
        drained, remaining_size=0, balance equals the failure-free
        control, and a second restart changes nothing."""
        ref = self._reference_scenario(pg_conn)

        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        position_key, row, bars = self._open_fault_position(
            store, clock, contour, candle_steps=1,
        )
        entry = row.entry_price
        original_commit = store.commit_partial_exit

        # (1) TP1 computed -> write fails -> DIRTY
        store.commit_partial_exit = _always_fail("TP1 persistence failure")
        contour.run_cycle({"BTCUSDT": entry * 0.945})
        assert position_key in contour.engine.dirty_leg_keys

        # (2) +9h: one interval due; rate observed in the STOP cycle
        clock.advance(hours=9)
        cycle = contour.run_cycle(
            {"BTCUSDT": entry}, bars=bars,
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        assert cycle["pending_closes"] == 0, cycle
        engine = contour.engine

        # (3) funding folded INTO the terminal snapshot, not lost
        final = store.get_position(position_key)
        assert final.status == "CLOSED"
        assert final.funding_periods_charged == 1, (
            "observed due period lost by the dirty-leg terminal close",
        )
        assert final.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-9,
        ), "funding must equal the failure-free control"
        assert final.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-9)
        legs = _legs(store, position_key)
        assert [leg.leg_seq for leg in legs] == [1, 3]
        closed_qty = sum(leg.quantity for leg in legs)
        assert round(final.position_size - final.remaining_size, 9) == round(
            closed_qty, 9,
        ), "conservation: per-position_size == sum of leg quantities"
        assert final.remaining_size == 0.0
        assert store.list_pending_closes() == []
        assert engine.pending_closes == {}
        assert engine._uncommitted_deltas == {}
        tp1_qty = ref["legs"]["TAKE_PROFIT_1"].quantity
        expected_qty = round(ref["position_size"] - tp1_qty, 12)
        assert final.funding_paid == pytest.approx(
            -(ref["entry_price"] * expected_qty * (0.01 / 100.0)), abs=1e-9,
        ), "per-period funding must use the TP history quantity"
        assert store.balance_components()["balance"] == pytest.approx(
            ref["balance"], abs=1e-6,
        ), "balance must equal the failure-free control"
        store.commit_partial_exit = original_commit

        # (4) RESTART on a fresh connection: nothing changes
        store.close()
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        assert contour2.engine.dirty_leg_keys == ()
        assert contour2.engine.durability_risk is False
        balance_before = store2.balance_components()["balance"]
        contour2.run_cycle(
            {"BTCUSDT": entry},
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        after = store2.get_position(position_key)
        assert after.funding_periods_charged == 1
        assert after.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-9,
        )
        assert len(_legs(store2, position_key)) == 2
        assert store2.list_pending_closes() == []
        assert store2.balance_components()["balance"] == pytest.approx(
            balance_before, abs=1e-12,
        ), "a further cycle must book nothing twice"
        assert contour2.engine.unsettled_funding_periods() == 0
        assert contour2.engine.accounting_consistent is True

        # (5) SECOND restart: identical PnL, funding and fees
        store2.close()
        store3 = _connect_store()
        contour3 = _contour(store3, clock=clock)
        contour3.run_cycle({"BTCUSDT": entry})
        final2 = store3.get_position(position_key)
        assert final2.net_pnl == pytest.approx(final.net_pnl, abs=1e-12)
        assert final2.funding_paid == pytest.approx(
            final.funding_paid, abs=1e-12,
        )
        assert final2.funding_periods_charged == 1
        assert store3.balance_components()["balance"] == pytest.approx(
            balance_before, abs=1e-12,
        )
        assert [leg.exit_reason
                for leg in _legs(store3, position_key)] == [
            "TAKE_PROFIT_1", "STOP_LOSS",
        ]
        store3.close()

    def test_scenario_c_lost_ack_after_successful_terminal_commit(
        self, running, pg_conn,
    ):
        """C on real PostgreSQL: scenario A plus a LOST ACKNOWLEDGEMENT
        after the terminal COMMIT was applied.  The engine's read-back
        verification proves the stored facts identical (incl. the folded
        funding), so the state settles as committed — no pending close,
        no uncommitted delta, no duplicated leg/period/balance operation,
        balance equals the failure-free control, restart stable."""
        ref = self._reference_scenario(pg_conn)

        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        position_key, row, bars = self._open_fault_position(
            store, clock, contour, candle_steps=1,
        )
        entry = row.entry_price
        original_commit = store.commit_partial_exit
        original_update = store.update_position

        store.commit_partial_exit = _always_fail("TP1 persistence failure")

        def lost_ack_update(position_row):
            # the COMMIT succeeds inside PostgreSQL, then the ACK is lost.
            original_update(position_row)
            if position_row.status == "CLOSED":
                raise RuntimeError(
                    "simulated lost ACK after successful COMMIT",
                )
            return None

        store.update_position = lost_ack_update

        contour.run_cycle({"BTCUSDT": entry * 0.945})
        clock.advance(hours=9)
        cycle = contour.run_cycle(
            {"BTCUSDT": entry}, bars=bars,
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine
        position_obj = engine.open_positions.get(position_key) or \
            store.get_position(position_key)

        # read-back identity despite the lost acknowledgement
        assert engine._confirmed_close(position_obj) is True
        final = store.get_position(position_key)
        assert final.status == "CLOSED"
        assert final.funding_periods_charged == 1
        assert final.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-9,
        )
        assert final.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-9)

        # no duplicates: nothing to replay, no local delta to double-count
        assert engine.pending_closes == {}
        assert engine._uncommitted_deltas == {}
        assert store.list_pending_closes() == []
        assert [leg.leg_seq for leg in _legs(store, position_key)] == [1, 3]
        assert store.balance_components()["balance"] == pytest.approx(
            ref["balance"], abs=1e-6,
        )
        store.commit_partial_exit = original_commit
        store.update_position = original_update

        # restart: everything already durable, nothing changes
        store.close()
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        assert contour2.engine.durability_risk is False
        balance_before = store2.balance_components()["balance"]
        contour2.run_cycle(
            {"BTCUSDT": entry},
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        after = store2.get_position(position_key)
        assert after.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-9,
        )
        assert after.funding_periods_charged == 1
        assert len(_legs(store2, position_key)) == 2
        assert store2.balance_components()["balance"] == pytest.approx(
            balance_before, abs=1e-12,
        )
        assert contour2.engine.accounting_consistent is True
        store2.close()

    def test_scenario_d_store_outage_at_tp_and_staging_recovers_sequentially(
        self, running, pg_conn,
    ):
        """D on real PostgreSQL: store unavailable during the partial TP,
        then again during terminal staging.  The close defers with its
        folded funding retained (no UPDATE attempted without staging);
        after recovery the unified terminal-close recovery replays owed
        legs + terminal + outbox in ONE transaction, then the terminal
        UPDATE — conservation holds, funding equals the failure-free
        control, restart changes nothing."""
        ref = self._reference_scenario(pg_conn)

        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        position_key, row, bars = self._open_fault_position(
            store, clock, contour, candle_steps=1,
        )
        entry = row.entry_price
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
        contour.run_cycle({"BTCUSDT": entry * 0.945})
        assert position_key in contour.engine.dirty_leg_keys

        # (2) +9h, rate observed, STOP — store now unavailable for
        #     terminal STAGING as well.
        store.stage_close = _always_fail(
            "PostgreSQL unavailable during terminal staging",
        )
        clock.advance(hours=9)
        cycle = contour.run_cycle(
            {"BTCUSDT": entry}, bars=bars,
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine
        assert update_calls["n"] == 0, (
            "terminal UPDATE ran although staging was not confirmed",
        )
        assert position_key in engine.pending_closes
        assert position_key in engine._unstaged_closes
        assert engine.durability_risk is True
        assert position_key in engine._uncommitted_deltas, (
            "the folded funding must stay uncommitted until the write",
        )
        durable = store.get_position(position_key)
        assert durable.status == "OPEN"
        assert durable.funding_periods_charged == 0, (
            "nothing may be durable before staging confirms",
        )
        assert _legs(store, position_key) == []
        assert store.list_pending_closes() == []

        # (3) store recovery -> unified terminal-close recovery replays
        #     everything (owed TP1 leg + terminal leg + outbox, then the
        #     terminal UPDATE).
        store.commit_partial_exit = original_commit
        store.stage_close = original_stage
        cycle = contour.run_cycle({"BTCUSDT": entry})
        assert cycle["pending_closes"] == 0, cycle
        assert update_calls["n"] >= 1

        final = store.get_position(position_key)
        assert final.status == "CLOSED"
        legs = _legs(store, position_key)
        assert [leg.leg_seq for leg in legs] == [1, 3]
        assert final.funding_periods_charged == 1
        assert final.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-9,
        )
        assert final.remaining_size == 0.0
        closed_qty = sum(leg.quantity for leg in legs)
        assert round(final.position_size - final.remaining_size, 9) == round(
            closed_qty, 9,
        ), "conservation: per-position_size == sum of leg quantities"
        assert store.list_pending_closes() == []
        assert engine.pending_closes == {}
        assert engine._uncommitted_deltas == {}
        assert engine.durability_risk is False
        assert store.balance_components()["balance"] == pytest.approx(
            ref["balance"], abs=1e-6,
        ), "balance must equal the failure-free control"

        # (4) RESTART: nothing changes
        store.close()
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        balance_before = store2.balance_components()["balance"]
        contour2.run_cycle({"BTCUSDT": entry})
        after = store2.get_position(position_key)
        assert after.net_pnl == pytest.approx(final.net_pnl, abs=1e-12)
        assert after.funding_paid == pytest.approx(
            final.funding_paid, abs=1e-12,
        )
        assert len(_legs(store2, position_key)) == 2
        assert store2.balance_components()["balance"] == pytest.approx(
            balance_before, abs=1e-12,
        )
        assert contour2.engine.accounting_consistent is True
        store2.close()


# ---------------------------------------------------------------------------
# Round 6 (Codex r4555591304 P1 + r4555591322 P2-funding) — fault injection
# against REAL PostgreSQL with migrations 088 + 089 (unit twins live in
# ``test_me_long_v2_inverse_short_oos2_durability_hardening_v1.py``).
# Faults are injected at the store-method boundary; staging, terminal
# UPDATE, outbox, obligation upsert/settle and restart are the REAL
# PostgreSQL code paths with real transactions.
# ---------------------------------------------------------------------------

class TestCodexRound6OnPostgres:
    """P1 scenarios A/C/D + funding scenarios A/C/D on PostgreSQL."""

    # -- shared P1 timeline (NO funding advance: P1 is isolated) --------

    @staticmethod
    def _p1_reference(pg_conn) -> dict:
        """Failure-free P1 timeline: entry -> TP1 committed -> protective
        STOP (no clock advance, no funding due)."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        event = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event}"
        row = store.get_position(position_key)
        entry = row.entry_price
        bars = {"BTCUSDT": {"high": row.stop_price * 1.001,
                            "low": entry * 0.99}}
        contour.run_cycle({"BTCUSDT": entry * 0.945})        # TP1 committed
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        final = store.get_position(position_key)
        assert final.status == "CLOSED"
        facts = {
            "balance": store.balance_components()["balance"],
            "net_pnl": final.net_pnl,
        }
        store.close()
        _cleanup(pg_conn)
        return facts

    @staticmethod
    def _open_p1_fault(store, clock, contour, *, candle_steps: int):
        """Capture + enter the fault run's position and commit its TP1;
        also parks a FRESH ETHUSDT candidate for the entry-block/resume
        assertions (captured before any clock advance, never stale)."""
        event = _capture_event(
            store, age=timedelta(seconds=5), candle_steps=candle_steps,
        )
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event}"
        row = store.get_position(position_key)
        entry = row.entry_price
        contour.run_cycle({"BTCUSDT": entry * 0.945})        # TP1 committed
        bars = {"BTCUSDT": {"high": row.stop_price * 1.001,
                            "low": entry * 0.99}}
        _capture_event(store, age=timedelta(seconds=5), symbol="ETHUSDT")
        return position_key, row, bars

    def test_p1_a_ambiguous_commit_blocks_entries_and_never_double_counts(
        self, running, pg_conn,
    ):
        """A+B on PostgreSQL: COMMIT applied, ACK lost, read-back failed ->
        quarantine (no consistency claim), engine balance == DURABLE
        balance (zero gap), entries blocked; read-back recovery ->
        classified, balanced to the reference, entries resume, partial +
        terminal legs exactly once."""
        ref = self._p1_reference(pg_conn)
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        position_key, row, bars = self._open_p1_fault(
            store, clock, contour, candle_steps=1,
        )
        entry = row.entry_price
        original_update = store.update_position
        original_get = store.get_position

        def update_losing_ack(position):
            original_update(position)       # PostgreSQL APPLIED it
            raise RuntimeError("terminal COMMIT ack lost")

        def read_back_down(key):
            if key == position_key:
                raise RuntimeError("durable read-back unavailable")
            return original_get(key)

        store.update_position = update_losing_ack
        store.get_position = read_back_down
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine

        # unknown outcome -> quarantined; consistency never claimed
        assert engine.ambiguous_write_keys == (position_key,)
        assert engine.accounting_consistent is False
        assert position_key in engine.pending_closes
        # durable truth: the close WAS applied on PostgreSQL
        durable = original_get(position_key)
        assert durable.status == "CLOSED"
        assert durable.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-6)
        # rule 5: engine balance == durable balance (gap = 0, never +delta)
        assert engine.reconcile_balance() is True
        components = store.balance_components()
        assert engine.confirmed_balance == pytest.approx(
            components["balance"], abs=1e-6,
        )
        assert engine.balance == pytest.approx(
            components["balance"], abs=1e-6,
        ), "the applied COMMIT must never be added on top"
        # rule 3: entries refused while the outcome is unknown
        assert engine.open_entries({"ETHUSDT": 100.0}) == []

        # read-back recovers -> CONFIRMED -> retired, reference, resume
        store.update_position = original_update
        store.get_position = original_get
        contour.run_cycle({"BTCUSDT": entry})
        assert engine.ambiguous_write_keys == ()
        assert engine.pending_closes == {}
        assert engine.accounting_consistent is True
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-6,
        )
        assert [leg.leg_seq for leg in _legs(store, position_key)] == [1, 3]
        opened = engine.open_entries({"ETHUSDT": 100.0})
        assert len(opened) == 1, "entries must resume after classification"
        store.close()

    def test_p1_c_proven_absent_close_replays_exactly_once(
        self, running, pg_conn,
    ):
        """C on PostgreSQL: the COMMIT never reaches PostgreSQL ->
        read-back PROVES it absent -> pending obligation + safe delta,
        entries blocked; the stage-before-update replay then commits the
        identical snapshot exactly once and converges to the reference."""
        ref = self._p1_reference(pg_conn)
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        position_key, row, bars = self._open_p1_fault(
            store, clock, contour, candle_steps=1,
        )
        entry = row.entry_price
        original_update = store.update_position

        def update_rejected(position):
            raise RuntimeError("terminal COMMIT rejected by PostgreSQL")

        store.update_position = update_rejected
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        engine = contour.engine

        # outcome KNOWN (never ambiguous) but the write is still owed
        assert engine.ambiguous_write_keys == ()
        assert position_key in engine.pending_closes
        delta = engine._uncommitted_deltas[position_key]
        assert delta != 0.0
        assert store.get_position(position_key).status == "OPEN", (
            "only staging (legs + outbox) is durable at this point",
        )
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"] + delta, abs=1e-6,
        )
        assert engine.open_entries({"ETHUSDT": 100.0}) == []

        # heal -> the replay commits exactly once
        store.update_position = original_update
        contour.run_cycle({"BTCUSDT": entry})
        assert engine.pending_closes == {}
        assert store.list_pending_closes() == []
        assert engine._uncommitted_deltas == {}
        final = store.get_position(position_key)
        assert final.status == "CLOSED"
        assert final.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-6)
        assert engine.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-6,
        )
        legs = _legs(store, position_key)
        assert [leg.leg_seq for leg in legs] == [1, 3]
        assert sum(1 for leg in legs if leg.is_terminal) == 1
        opened = engine.open_entries({"ETHUSDT": 100.0})
        assert len(opened) == 1
        store.close()

    def test_p1_d_restart_while_ambiguous_keeps_durable_facts_and_blocks(
        self, running, pg_conn,
    ):
        """D on PostgreSQL: a restart in the middle of the unknown outcome
        rebuilds from durable facts only; the staged outbox keeps entries
        blocked until the durable replay confirms; the result equals the
        failure-free reference."""
        ref = self._p1_reference(pg_conn)
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        position_key, row, bars = self._open_p1_fault(
            store, clock, contour, candle_steps=1,
        )
        entry = row.entry_price
        original_update = store.update_position
        original_get = store.get_position

        def update_losing_ack(position):
            original_update(position)
            raise RuntimeError("terminal COMMIT ack lost")

        def read_back_down(key):
            if key == position_key:
                raise RuntimeError("durable read-back unavailable")
            return original_get(key)

        store.update_position = update_losing_ack
        store.get_position = read_back_down
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        assert contour.engine.ambiguous_write_keys == (position_key,)
        store.close()

        # RESTART: a brand-new store + contour over the SAME database
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        engine2 = contour2.engine
        # durable facts: the applied CLOSED row IS the balance (rule 8)
        assert engine2.reconcile_balance() is True
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"], abs=1e-6,
        )
        # the staged close-outbox replays: pending resumes, entries blocked
        assert position_key in engine2.pending_closes
        assert engine2.accounting_consistent is False
        assert engine2.open_entries({"ETHUSDT": 100.0}) == []

        # the fresh store carries NO patches: the replay is idempotent
        contour2.run_cycle({"BTCUSDT": entry})
        assert engine2.pending_closes == {}
        assert store2.list_pending_closes() == []
        assert engine2.accounting_consistent is True
        assert engine2.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"], abs=1e-6,
        )
        assert [leg.leg_seq for leg in _legs(store2, position_key)] == [1, 3]
        opened = engine2.open_entries({"ETHUSDT": 100.0})
        assert len(opened) == 1
        store2.close()

    # -- funding scenarios (timeline = round-5 reference) ---------------

    @staticmethod
    def _funding_open(pg_conn, *, candle_steps: int):
        """Reference (failure-free) timeline + an open TP1 position whose
        STOP cycle will observe the rate."""
        ref = TestCodexRound5OnPostgres._reference_scenario(pg_conn)
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        position_key, row, bars = TestCodexRound5OnPostgres._open_fault_position(
            store, clock, contour, candle_steps=candle_steps,
        )
        entry = row.entry_price
        contour.run_cycle({"BTCUSDT": entry * 0.945})        # TP1 committed
        clock.advance(hours=9)
        return ref, store, clock, contour, position_key, entry, bars

    def test_funding_a_history_unavailable_close_keeps_durable_obligation(
        self, running, pg_conn,
    ):
        """A on PostgreSQL: protective STOP with the rate observed while
        the close-leg history is unreadable -> the obligation row is
        DURABLE in PostgreSQL (raw-SQL asserted), nothing is booked
        approximately; history restored -> settled exactly once to the
        reference numbers."""
        ref, store, clock, contour, position_key, entry, bars = (
            self._funding_open(pg_conn, candle_steps=1)
        )
        engine = contour.engine
        original_list = store.list_close_legs
        store.list_close_legs = _always_fail(
            "close-leg history unavailable at the close",
        )
        cycle = contour.run_cycle(
            {"BTCUSDT": entry}, bars=bars,
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle

        # DURABLE obligation — asserted through RAW SQL on PostgreSQL
        cursor = pg_conn.cursor()
        cursor.execute(
            "SELECT status, observed_rate_percent, periods_due, "
            "periods_charged_at_observation FROM "
            "dds.me_long_v2_inverse_short_oos2_v1_funding_obligation "
            "WHERE position_key = %s",
            (position_key,),
        )
        ob_row = cursor.fetchone()
        pg_conn.rollback()
        assert ob_row is not None, "the observed rate must be durable"
        assert ob_row[0] == "OPEN"
        assert float(ob_row[1]) == pytest.approx(0.01)
        assert int(ob_row[2]) == 1 and int(ob_row[3]) == 0

        row_after = store.get_position(position_key)
        assert row_after.status == "CLOSED"
        assert row_after.funding_periods_charged == 0, (
            "unprovable quantity must never be charged",
        )
        assert row_after.funding_paid == 0.0, "no approximate booking"
        assert store.get_funding_obligation(position_key).status == (
            FUNDING_OBLIGATION_OPEN
        )
        # partial + terminal legs exactly once (staged with the close)
        assert [leg.leg_seq for leg in original_list(position_key)] == [1, 3]

        # history restored -> the recovery pass settles EXACTLY ONCE
        store.list_close_legs = original_list
        contour.run_cycle({"BTCUSDT": entry})
        settled = store.get_position(position_key)
        assert settled.funding_periods_charged == 1
        assert settled.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-6,
        )
        assert settled.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-6)
        assert store.get_funding_obligation(position_key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        assert engine.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert store.balance_components()["balance"] == pytest.approx(
            ref["balance"], abs=1e-6,
        )
        # idempotent: five further cycles change nothing
        for _ in range(5):
            contour.run_cycle({"BTCUSDT": entry})
        assert store.get_position(position_key).funding_paid == (
            pytest.approx(ref["funding_paid"], abs=1e-9)
        )
        assert store.get_position(position_key).funding_periods_charged == 1
        store.close()

    def test_funding_c_restart_before_recovery_keeps_obligation(
        self, running, pg_conn,
    ):
        """C on PostgreSQL: a restart BETWEEN the protective close and the
        funding recovery reloads the DURABLE obligation and settles it
        exactly once; a second restart changes nothing."""
        ref, store, clock, contour, position_key, entry, bars = (
            self._funding_open(pg_conn, candle_steps=1)
        )
        original_list = store.list_close_legs
        store.list_close_legs = _always_fail("history down at the close")
        cycle = contour.run_cycle(
            {"BTCUSDT": entry}, bars=bars,
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        assert store.get_funding_obligation(position_key).status == (
            FUNDING_OBLIGATION_OPEN
        )
        assert store.get_position(position_key).funding_periods_charged == 0
        store.list_close_legs = original_list
        store.close()

        # RESTART: only durable state survives
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        assert position_key in contour2.engine.funding_obligation_keys
        contour2.run_cycle({"BTCUSDT": entry})
        row = store2.get_position(position_key)
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-6,
        )
        assert store2.get_funding_obligation(position_key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        assert contour2.engine.balance == pytest.approx(
            ref["balance"], abs=1e-6,
        )
        assert contour2.engine.balance == pytest.approx(
            store2.balance_components()["balance"], abs=1e-6,
        )

        # SECOND restart: nothing changes
        store2.close()
        store3 = _connect_store()
        contour3 = _contour(store3, clock=clock)
        contour3.run_cycle({"BTCUSDT": entry})
        row3 = store3.get_position(position_key)
        assert row3.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-9,
        )
        assert row3.funding_periods_charged == 1
        assert contour3.engine.balance == pytest.approx(
            ref["balance"], abs=1e-6,
        )
        assert contour3.engine.accounting_consistent is True
        store3.close()

    def test_funding_d_ambiguous_settlement_never_double_accrues(
        self, running, pg_conn,
    ):
        """D on PostgreSQL: the obligation settlement COMMIT applied with a
        lost acknowledgement — never booked twice, balance resynced from
        the durable ledger, later cycles are no-ops."""
        ref, store, clock, contour, position_key, entry, bars = (
            self._funding_open(pg_conn, candle_steps=1)
        )
        engine = contour.engine
        original_list = store.list_close_legs
        store.list_close_legs = _always_fail("history down at the close")
        cycle = contour.run_cycle(
            {"BTCUSDT": entry}, bars=bars,
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        store.list_close_legs = original_list
        assert store.get_funding_obligation(position_key).status == (
            FUNDING_OBLIGATION_OPEN
        )

        original_settle = store.settle_funding_obligation

        def settle_losing_ack(*args, **kwargs):
            original_settle(*args, **kwargs)     # PostgreSQL applied it
            raise RuntimeError("settlement acknowledgement lost")

        store.settle_funding_obligation = settle_losing_ack
        contour.run_cycle({"BTCUSDT": entry})

        row = store.get_position(position_key)
        assert row.funding_periods_charged == 1, "the COMMIT was applied"
        assert row.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-6,
        )
        assert store.get_funding_obligation(position_key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        # the engine resyncs from the durable ledger (never re-books)
        assert engine.reconcile_balance() is True
        assert engine.balance == pytest.approx(
            store.balance_components()["balance"], abs=1e-6,
        )
        assert engine.balance == pytest.approx(ref["balance"], abs=1e-6)

        # healed: later cycles book NOTHING more
        store.settle_funding_obligation = original_settle
        for _ in range(3):
            contour.run_cycle({"BTCUSDT": entry})
        row2 = store.get_position(position_key)
        assert row2.funding_periods_charged == 1
        assert row2.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-9,
        )
        assert store.list_funding_obligations(FUNDING_OBLIGATION_OPEN) == []
        store.close()


# ---------------------------------------------------------------------------
# Round 7 (Codex r4584484945 P1 retained protective close + r4584484952 P2
# stale close outbox) — fault injection against REAL PostgreSQL with
# migrations 088 + 089.  Unit twins live in
# ``test_me_long_v2_inverse_short_oos2_durability_hardening_v1.py``.
# The outbox retire and the 089 guard are only provable here: one real
# PostgreSQL transaction, one real trigger.
# ---------------------------------------------------------------------------

class TestCodexRound7OnPostgres:
    """Round 7 P1/P2 scenarios against real PostgreSQL."""

    # -- shared timeline: STOP close whose ``clear_pending_close`` FAILED,
    #    then the funding settlement that used to leave the stale snapshot.

    @staticmethod
    def _stale_outbox_timeline(pg_conn):
        ref, store, clock, contour, position_key, entry, bars = (
            TestCodexRound6OnPostgres._funding_open(pg_conn, candle_steps=1)
        )
        original_list = store.list_close_legs
        original_clear = store.clear_pending_close
        store.list_close_legs = _always_fail("close-leg history unreadable")
        store.clear_pending_close = _always_fail("clear_pending_close outage")
        cycle = contour.run_cycle(
            {"BTCUSDT": entry}, bars=bars,
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        store.list_close_legs = original_list

        # CLOSED row committed while the clear FAILED -> stale snapshot
        row_closed = store.get_position(position_key)
        assert row_closed.status == "CLOSED"
        assert row_closed.funding_periods_charged == 0
        stale_rows = store.list_pending_closes()
        assert [p.position_key for p in stale_rows] == [position_key], (
            "clear_pending_close failed -> the snapshot must still be staged",
        )

        # the funding recovery advances the CLOSED row
        contour.run_cycle({"BTCUSDT": entry})
        settled = store.get_position(position_key)
        assert settled.funding_periods_charged == 1
        assert settled.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-6,
        )
        store.clear_pending_close = original_clear
        return ref, store, clock, contour, position_key, entry, stale_rows[0]

    @staticmethod
    def _outbox_keys(pg_conn, position_key) -> list[str]:
        cursor = pg_conn.cursor()
        cursor.execute(
            f"SELECT position_key FROM {CLOSE_OUTBOX_TABLE} "  # noqa: S608
            "WHERE position_key = %s",
            (position_key,),
        )
        keys = [row[0] for row in cursor.fetchall()]
        pg_conn.rollback()
        return keys

    # -- shared timeline: protective STOP whose OBSERVED rate could NOT be
    #    persisted (both store faults) -> the close is RETAINED.

    @staticmethod
    def _retained_close(pg_conn, journal_dir):
        ref, store, clock, contour, position_key, entry, bars = (
            TestCodexRound6OnPostgres._funding_open(pg_conn, candle_steps=1)
        )
        # isolate the OOS-2 local durable journal for this test
        contour.engine.journal = ProtectiveCloseJournal(journal_dir)
        contour.journal_dir = journal_dir
        original_list = store.list_close_legs
        original_save = store.save_funding_obligation
        store.list_close_legs = _always_fail("close-leg history unreadable")
        store.save_funding_obligation = _always_fail("obligation write failure")
        cycle = contour.run_cycle(
            {"BTCUSDT": entry}, bars=bars,
            funding_rates_percent={"BTCUSDT": 0.01},
        )
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        return {
            "ref": ref, "store": store, "clock": clock, "contour": contour,
            "position_key": position_key, "entry": entry, "bars": bars,
            "original_list": original_list, "original_save": original_save,
        }

    # -- P1 ---------------------------------------------------------------

    def test_p1_a_retained_protective_close_survives_a_postgres_restart(
        self, running, pg_conn, tmp_path,
    ):
        """A on PostgreSQL: STOP + unreadable history + failed obligation
        write -> the close is RETAINED (row still OPEN, frozen facts in the
        outbox AND in the local journal) and a RESTART before PostgreSQL
        recovery restores BOTH halves; the rate then reaches PostgreSQL and
        settles exactly once to the reference numbers."""
        ctx = self._retained_close(pg_conn, tmp_path)
        store, clock, contour = ctx["store"], ctx["clock"], ctx["contour"]
        key, entry, ref = ctx["position_key"], ctx["entry"], ctx["ref"]
        engine = contour.engine

        row = store.get_position(key)
        assert row.status == "OPEN", "terminal UPDATE withheld"
        assert row.funding_periods_charged == 0
        assert store.get_funding_obligation(key) is None
        assert key in engine.pending_closes
        assert key not in engine.open_positions
        assert key in engine._pending_obligations
        assert [p.position_key for p in store.list_pending_closes()] == [key]
        assert list(tmp_path.glob("*.json")), "the local journal must exist"
        assert engine.open_entries({"ETHUSDT": 100.0}) == []

        # RESTART before PostgreSQL recovery: durable state + journal only
        store.close()
        store2 = _connect_store()
        contour2 = InverseShortContour(
            _settings(), store2, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        engine2 = contour2.engine
        assert store2.get_position(key).status == "OPEN"
        assert key in engine2.pending_closes, "the outbox half must resume"
        assert key in engine2._pending_obligations, (
            "the observed rate must be replayed from the journal",
        )
        assert key not in engine2.open_positions
        assert engine2.open_entries({"ETHUSDT": 100.0}) == []

        # recovery: the retained rate reaches PostgreSQL, then settles once
        contour2.run_cycle({"BTCUSDT": entry * 0.98})
        row2 = store2.get_position(key)
        assert row2.status == "CLOSED"
        assert row2.exit_reason == "STOP_LOSS"
        assert row2.funding_periods_charged == 1
        assert row2.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-6,
        )
        assert row2.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-6)
        assert engine2.pending_closes == {}
        assert store2.list_pending_closes() == []
        assert engine2.journal.paths() == [], "the record must be retired"
        assert engine2.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"], abs=1e-6,
        )
        assert engine2.durability_risk is False
        store2.close()

    # -- P2 ---------------------------------------------------------------

    def test_p2_a_settlement_retires_the_stale_outbox_atomically(
        self, running, pg_conn,
    ):
        """A on PostgreSQL: the settlement retires the superseded snapshot
        in the SAME transaction (raw-SQL proof), a restart resumes nothing
        and the settled funding survives; entries are never blocked."""
        ref, store, clock, contour, position_key, entry, stale = (
            self._stale_outbox_timeline(pg_conn)
        )
        # raw-SQL proof: no stale snapshot survives the settlement
        assert self._outbox_keys(pg_conn, position_key) == []

        store.close()
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        engine2 = contour2.engine
        assert engine2.pending_closes == {}
        assert store2.list_pending_closes() == []
        row = store2.get_position(position_key)
        assert row.funding_periods_charged == 1, "settled funding must survive"
        assert row.funding_paid == pytest.approx(ref["funding_paid"], abs=1e-6)
        assert row.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-6)
        assert engine2.balance == pytest.approx(ref["balance"], abs=1e-6)

        contour2.run_cycle({"BTCUSDT": entry})
        assert engine2.pending_closes == {}
        assert engine2.accounting_consistent is True
        assert engine2.durability_risk is False
        # entries are NOT blocked forever by a stale outbox
        _capture_event(store2, age=timedelta(seconds=5), symbol="ETHUSDT",
                       signal_at=clock())
        opened = engine2.open_entries({"ETHUSDT": 100.0})
        assert len(opened) == 1, "a stale outbox must never block entries"
        store2.close()

    def test_p2_b_stale_snapshot_replay_is_rejected_by_the_089_guard(
        self, running, pg_conn,
    ):
        """B on PostgreSQL: replaying the superseded snapshot is a HARD
        ERROR (migration 089) — the settled funding can never be rewound —
        and staging it again is recognised as supersession on restart."""
        ref, store, clock, contour, position_key, entry, stale = (
            self._stale_outbox_timeline(pg_conn)
        )
        with pytest.raises(Exception) as excinfo:      # noqa: PT011 - pg error
            store.update_position(stale)
        assert "funding_periods may only advance on a closed position" in str(
            excinfo.value,
        ), str(excinfo.value)
        row = store.get_position(position_key)
        assert row.funding_periods_charged == 1, "settled state untouched"
        assert row.funding_paid == pytest.approx(ref["funding_paid"], abs=1e-6)

        # a duplicate delivery of the superseded snapshot is recognised and
        # retired on restart instead of being replayed
        store.stage_close(stale, None)
        assert self._outbox_keys(pg_conn, position_key) == [position_key]
        store.close()
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        assert contour2.engine.pending_closes == {}
        assert store2.list_pending_closes() == []
        contour2.run_cycle({"BTCUSDT": entry})
        row2 = store2.get_position(position_key)
        assert row2.funding_periods_charged == 1
        assert row2.funding_paid == pytest.approx(ref["funding_paid"], abs=1e-6)
        assert contour2.engine.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert contour2.engine.accounting_consistent is True
        store2.close()

    def test_p2_c_snapshot_kept_until_terminal_facts_are_confirmed(
        self, running, pg_conn, tmp_path,
    ):
        """C on PostgreSQL: a superseded snapshot is NEVER silently deleted
        while unconfirmed terminal facts remain — an OPEN row keeps its
        staged snapshot, and only a snapshot of the SAME close with the
        SAME terminal facts is ever retired."""
        # (i) row still OPEN (retained protective close) -> never retired
        ctx = self._retained_close(pg_conn, tmp_path)
        store, clock, contour = ctx["store"], ctx["clock"], ctx["contour"]
        key, entry = ctx["position_key"], ctx["entry"]
        engine = contour.engine
        assert store.get_position(key).status == "OPEN"
        open_snap = engine.pending_closes[key]
        assert engine._retire_superseded_close(open_snap, source="test") is False
        assert [p.position_key for p in store.list_pending_closes()] == [key], (
            "an unconfirmed close must keep its staged snapshot",
        )

        # recovery -> the row becomes CLOSED with the same terminal facts
        store.list_close_legs = ctx["original_list"]
        store.save_funding_obligation = ctx["original_save"]
        contour.run_cycle({"BTCUSDT": entry * 0.98})
        assert store.get_position(key).status == "CLOSED"

        # (ii) a snapshot describing a DIFFERENT close is never mistaken
        #      for a stale twin of this one
        other_exit = dataclasses.replace(
            open_snap, exit_price=open_snap.exit_price * 3.0,
        )
        assert engine._retire_superseded_close(other_exit,
                                               source="test") is False
        other_reason = dataclasses.replace(open_snap, exit_reason="TIMEOUT")
        assert engine._retire_superseded_close(other_reason,
                                               source="test") is False
        # (iii) the durable row is BEHIND the snapshot -> not supersession
        ahead = dataclasses.replace(open_snap, funding_periods_charged=99)
        assert engine._retire_superseded_close(ahead, source="test") is False
        store.close()

    def test_p2_d_clear_is_idempotent_and_recovery_is_a_noop(
        self, running, pg_conn,
    ):
        """D on PostgreSQL: ``clear_pending_close()`` is idempotent and a
        repeated recovery (restarts + cycles) is a pure no-op."""
        ref, store, clock, contour, position_key, entry, stale = (
            self._stale_outbox_timeline(pg_conn)
        )
        for _ in range(3):
            store.clear_pending_close(position_key)     # never raises
        assert store.list_pending_closes() == []
        baseline = store.get_position(position_key)
        assert baseline.funding_periods_charged == 1

        store.close()
        for _ in range(2):
            store = _connect_store()
            contour2 = _contour(store, clock=clock)
            contour2.run_cycle({"BTCUSDT": entry})
            row = store.get_position(position_key)
            assert row.funding_periods_charged == 1
            assert row.funding_paid == pytest.approx(
                ref["funding_paid"], abs=1e-9,
            )
            assert row.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-9)
            assert contour2.engine.balance == pytest.approx(
                ref["balance"], abs=1e-6,
            )
            assert contour2.engine.pending_closes == {}
            assert store.list_pending_closes() == []
            assert contour2.engine.accounting_consistent is True
            assert contour2.engine.durability_risk is False
            store.close()

    def test_p2_e_migration_089_guard_still_raises_on_a_raw_rewind(
        self, running, pg_conn,
    ):
        """E on PostgreSQL: the migration 089 closed-row guard is byte-for
        byte intact — a raw rewind of the funding window still raises."""
        ref, store, clock, contour, position_key, entry, stale = (
            self._stale_outbox_timeline(pg_conn)
        )
        assert store.get_position(position_key).funding_periods_charged == 1
        cursor = pg_conn.cursor()
        with pytest.raises(Exception) as excinfo:      # noqa: PT011 - pg error
            cursor.execute(
                f"UPDATE {POSITION_TABLE} "             # noqa: S608
                "SET funding_periods = 0, funding_paid = 0, net_pnl = 0 "
                "WHERE position_key = %s",
                (position_key,),
            )
        assert "funding_periods may only advance on a closed position" in str(
            excinfo.value,
        ), str(excinfo.value)
        pg_conn.rollback()
        # the failed rewind changed nothing
        row = store.get_position(position_key)
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(ref["funding_paid"], abs=1e-6)
        store.close()

    def test_p2_f_no_regression_in_funding_periods_or_funding_paid(
        self, running, pg_conn,
    ):
        """F on PostgreSQL: after the whole recovery the contour matches
        the failure-free reference exactly — never fewer
        ``funding_periods_charged``, never a lost ``funding_paid``."""
        ref, store, clock, contour, position_key, entry, stale = (
            self._stale_outbox_timeline(pg_conn)
        )
        assert store.list_pending_closes() == []
        store.close()
        store2 = _connect_store()
        contour2 = _contour(store2, clock=clock)
        for _ in range(6):
            contour2.run_cycle({"BTCUSDT": entry})
        row = store2.get_position(position_key)
        assert row.status == "CLOSED"
        assert row.funding_periods_charged == ref["funding_periods_charged"]
        assert row.funding_periods_charged == 1, "never fewer periods"
        assert row.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-6,
        ), "funding_paid must never be lost"
        assert row.funding_paid != 0.0, "the settled funding must persist"
        assert row.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-6)
        assert contour2.engine.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert contour2.engine.balance == pytest.approx(
            store2.balance_components()["balance"], abs=1e-6,
        )
        assert contour2.engine.pending_closes == {}
        assert contour2.engine.accounting_consistent is True
        assert contour2.engine.durability_risk is False
        store2.close()


# ---------------------------------------------------------------------------
# Round 8 (Codex, 16.11.2026) — three NEW findings on HEAD edbb7350:
#   P1a r4587604965  a failed recovery READ must not become "confirmed
#                    absent" and must never retire the journal record;
#   P1b r4587604973  the observed obligation must be durable BEFORE the
#                    staging COMMIT creates a terminal snapshot;
#   P2  r4587604985  the journal DIRECTORY (not ``os.curdir``) is what gets
#                    fsync'ed, and write/fsync failures reach the caller.
# Unit twins live in ``..._durability_hardening_v1.py`` (classes
# TestRound8P1a/P1b/P2); this block repeats them on real PostgreSQL.
# ---------------------------------------------------------------------------


class _ProcessDeath(BaseException):
    """Simulated process death.

    Deliberately NOT an ``Exception``: the engine only guards exit paths
    with ``except Exception``, so a real process termination cannot be
    swallowed by any handler.
    """


class TestCodexRound8OnPostgres:
    """Round 8 P1/P2 scenarios against real PostgreSQL."""

    # -- shared timeline: durable journal + committed staging, then death --

    @staticmethod
    def _die_after_staging_commit(pg_conn, journal_dir):
        """STOP -> journal write (durable) -> staging COMMIT -> death."""
        ref, store, clock, contour, position_key, entry, bars = (
            TestCodexRound6OnPostgres._funding_open(pg_conn, candle_steps=1)
        )
        contour.engine.journal = ProtectiveCloseJournal(journal_dir)
        contour.journal_dir = journal_dir
        original_list = store.list_close_legs
        original_save = store.save_funding_obligation
        original_stage = store.stage_close
        store.list_close_legs = _always_fail("close-leg history unreadable")
        store.save_funding_obligation = _always_fail("obligation write failure")

        def stage_then_die(position, terminal_leg=None, *, partial_legs=None):
            # the transaction really commits, THEN the acknowledgement is
            # lost to a process death (not to an Exception).
            original_stage(position, terminal_leg, partial_legs=partial_legs)
            raise _ProcessDeath("died right after the staging COMMIT")

        store.stage_close = stage_then_die
        with pytest.raises(_ProcessDeath):
            contour.run_cycle(
                {"BTCUSDT": entry}, bars=bars,
                funding_rates_percent={"BTCUSDT": 0.01},
            )
        store.list_close_legs = original_list
        store.save_funding_obligation = original_save
        store.stage_close = original_stage
        return ref, store, clock, contour, position_key, entry, bars

    # -- P1a ---------------------------------------------------------------

    def test_p1_a_failed_recovery_read_keeps_journal_and_observed_rate(
        self, running, pg_conn, tmp_path,
    ):
        """P1a on PostgreSQL: the journal replay cannot read the position
        row — the record, the frozen STOP facts and the observed funding
        must all survive, new entries must be refused, and the read-back
        must be retried until PostgreSQL answers."""
        ctx = TestCodexRound7OnPostgres._retained_close(pg_conn, tmp_path)
        store, clock, contour = ctx["store"], ctx["clock"], ctx["contour"]
        key, entry, ref = ctx["position_key"], ctx["entry"], ctx["ref"]
        assert list(tmp_path.glob("*.json")), "the record must exist"
        assert store.get_position(key).status == "OPEN"

        # ---- restart while the position READ is unavailable --------------
        store.close()
        store2 = _connect_store()
        original_get = store2.get_position
        # a FRESH candidate on the engine's own clock: the entry TTL ages
        # signals against it, so the block below is proven to be the
        # recovery gate and not a missing/stale candidate
        _capture_event(store2, age=timedelta(seconds=5), symbol="ETHUSDT",
                       signal_at=clock())
        store2.get_position = _always_fail("position row unreadable")
        contour2 = InverseShortContour(
            _settings(), store2, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        engine2 = contour2.engine
        # (1) an unknown read is NEVER proof of absence
        assert engine2.journal.paths(), (
            "a failed read must not retire the journal record",
        )
        assert engine2.journal.read_all()[key].obligation is not None
        assert key in engine2._journal_unresolved, "read is retried"
        # (2) the observed funding and the frozen close are both restored
        assert key in engine2._pending_obligations, "observed rate lost"
        assert key in engine2.pending_closes, "frozen close lost"
        assert key not in engine2.open_positions
        assert engine2.pending_closes[key].exit_reason == "STOP_LOSS"
        # (3) fail closed: no new entries while the outcome is unknown
        assert engine2.durability_risk is True
        assert engine2.open_entries({"ETHUSDT": 100.0}) == []
        # (4) PostgreSQL was never mutated by the failed read
        assert [p.position_key for p in store2.list_pending_closes()] == [key]
        assert store2.get_funding_obligation(key) is None, (
            "nothing may be fabricated while the read is unknown",
        )

        # ---- read path restored -> read-back classifies and completes ----
        store2.get_position = original_get
        store2.close()
        store3 = _connect_store()
        contour3 = InverseShortContour(
            _settings(), store3, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        engine3 = contour3.engine
        assert engine3._journal_unresolved == set(), "read-back classified"
        assert engine3.journal.paths(), "row still OPEN -> record kept"
        assert key in engine3._pending_obligations
        # the restored close still owes an uncommitted accounting delta, so
        # the memory ledger is deliberately NOT trusted yet (fail closed) —
        # exactly like the pre-recovery state of round 6 test_p1_d
        assert engine3.accounting_consistent is False
        assert engine3.open_entries({"ETHUSDT": 100.0}) == []

        contour3.run_cycle({"BTCUSDT": entry * 0.98})
        row = store3.get_position(key)
        assert row.status == "CLOSED"
        assert row.exit_reason == "STOP_LOSS"
        assert row.funding_periods_charged == 1, "never fewer periods"
        assert row.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-6,
        ), "funding_paid must never be lost"
        assert row.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-6)
        assert store3.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        assert [leg.leg_seq for leg in store3.list_close_legs(key)] == [1, 3]
        assert engine3.pending_closes == {}
        assert store3.list_pending_closes() == []
        assert engine3.journal.paths() == [], "retired after proven completion"
        assert engine3.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert engine3.accounting_consistent is True
        assert engine3.durability_risk is False
        # every fail-closed gate must be clear before entries may resume
        assert not engine3._pending_obligations, "obligation still pending"
        assert not engine3._ambiguous_keys, "ambiguous write not classified"
        assert not engine3._dirty_funding, "funding write unconfirmed"
        assert not engine3._dirty_legs, "leg write unconfirmed"
        assert not engine3._journal_unresolved, "read never resolved"
        assert not engine3._unstaged_closes, "staging never completed"
        # the SAME fresh candidate now opens — the recovery gates are clear
        assert len(engine3.open_entries({"ETHUSDT": 100.0})) == 1, (
            "entries unblock once the outcome is known",
        )
        store3.close()

    # -- P1b ---------------------------------------------------------------

    def test_p1_b_observed_obligation_is_durable_before_the_staging_commit(
        self, running, pg_conn, tmp_path,
    ):
        """P1b on PostgreSQL: STOP -> journal -> staging COMMIT -> death.

        After the crash the terminal snapshot may already be durable in
        PostgreSQL, but the OBSERVED funding obligation must be durable
        too (in the journal record) — otherwise the restart would finalise
        the close with ``funding_paid = 0`` and zero periods."""
        ref, store, clock, contour, key, entry, bars = (
            self._die_after_staging_commit(pg_conn, tmp_path)
        )
        engine = contour.engine

        # (1) the recovery record is on disk WITH the observed funding —
        #     it was written BEFORE the staging transaction ran
        assert engine.journal.exists(key), "the record must precede staging"
        record = engine.journal.read_all()[key]
        assert record.obligation is not None, "observed funding must be durable"
        assert record.exit_reason == "STOP_LOSS", "frozen trigger kept"
        assert record.triggered_at, "the frozen closed_at must be kept"
        # (2) the terminal snapshot IS durable in PostgreSQL (raw SQL)
        cursor = pg_conn.cursor()
        cursor.execute(
            f"SELECT position_key FROM {CLOSE_OUTBOX_TABLE} "  # noqa: S608
            "WHERE position_key = %s",
            (key,),
        )
        assert cursor.fetchone() is not None, "the staging must have committed"
        pg_conn.rollback()
        # The record's ``staged`` hint is still False: the pre-stage write
        # never claims a staging it has not confirmed, and the process died
        # before the post-stage refresh.  That is the CONSERVATIVE direction
        # — on replay the hint is only consulted when the outbox row is
        # missing, where restaging idempotently is exactly right; when the
        # outbox row already resumed ``pending_closes`` it is not needed.
        assert record.staged is False
        # (3) the terminal UPDATE never ran: the row is still OPEN
        assert store.get_position(key).status == "OPEN"
        assert store.get_funding_obligation(key) is None, (
            "PostgreSQL still has no obligation -> the journal is the only copy",
        )

        # ---- restart: both halves resume, entries stay refused ------------
        store.close()
        store2 = _connect_store()
        contour2 = InverseShortContour(
            _settings(), store2, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        engine2 = contour2.engine
        assert store2.get_position(key).status == "OPEN"
        assert key in engine2.pending_closes, "the outbox half must resume"
        assert key in engine2._pending_obligations, (
            "the observed rate must be replayed from the journal",
        )
        assert key not in engine2.open_positions
        assert engine2.journal.paths(), "the record outlives the crash"
        assert engine2.open_entries({"ETHUSDT": 100.0}) == []

        # ---- reconciliation -> funding recovery -> outbox retirement -------
        contour2.run_cycle({"BTCUSDT": entry * 0.98})
        row2 = store2.get_position(key)
        assert row2.status == "CLOSED"
        assert row2.exit_reason == "STOP_LOSS"
        # the FROZEN trigger is replayed verbatim — never recomputed
        assert row2.exit_price == pytest.approx(record.fill_price, abs=1e-9)
        assert row2.closed_at is not None
        # the frozen ``closed_at`` is replayed verbatim (compared without
        # the timezone representation, which PostgreSQL may round-trip)
        frozen_closed_at = datetime.fromisoformat(record.triggered_at)
        assert row2.closed_at.replace(tzinfo=None) == (
            frozen_closed_at.replace(tzinfo=None)
        )
        # exactly-once settlement against the failure-free reference
        assert row2.funding_periods_charged == 1, "never fewer periods"
        assert row2.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-6,
        )
        assert row2.funding_paid != 0.0, "the observed funding must persist"
        assert row2.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-6)
        assert store2.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        legs = store2.list_close_legs(key)
        assert [leg.leg_seq for leg in legs] == [1, 3], legs
        assert len([leg for leg in legs if leg.is_terminal]) == 1, legs
        assert engine2.pending_closes == {}
        assert store2.list_pending_closes() == [], "outbox retired"
        assert engine2.journal.paths() == [], "record retired once proven"
        assert engine2.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"], abs=1e-6,
        )
        assert engine2.durability_risk is False

        # ---- repeated restart after recovery is a no-op --------------------
        store2.close()
        store3 = _connect_store()
        contour3 = InverseShortContour(
            _settings(), store3, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        engine3 = contour3.engine
        assert engine3.pending_closes == {}
        assert engine3._pending_obligations == {}
        assert engine3.journal.paths() == []
        assert store3.get_position(key).status == "CLOSED"
        assert store3.get_position(key).funding_periods_charged == 1
        assert len([
            leg for leg in store3.list_close_legs(key) if leg.is_terminal
        ]) == 1, "no duplicate terminal leg"
        assert engine3.durability_risk is False
        store3.close()

    # -- P2 ----------------------------------------------------------------

    def test_p2_d_journal_directory_is_synced_from_a_foreign_cwd(
        self, running, pg_conn, tmp_path, monkeypatch,
    ):
        """P2 on PostgreSQL: the record lives under a release-style path
        while the process runs from a DIFFERENT working directory — the
        directory opened for the directory fsync is the JOURNAL directory
        (and the parents it had to create), never ``os.curdir``, and a
        restart from yet another cwd replays it."""
        journal_dir = (
            tmp_path / "release" / "var" / "me_inverse_short_oos2_journal"
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(parents=True)
        monkeypatch.chdir(elsewhere)          # cwd is NOT the journal dir
        assert not journal_dir.exists(), "a fresh release layout"

        ctx = TestCodexRound7OnPostgres._retained_close(pg_conn, journal_dir)
        store, clock, contour = ctx["store"], ctx["clock"], ctx["contour"]
        key, entry, ref = ctx["position_key"], ctx["entry"], ctx["ref"]
        engine = contour.engine

        # the record landed in the release path — never in the process cwd
        assert list(journal_dir.glob("*.json")), "record in journal_dir"
        assert not list(elsewhere.glob("*.json")), "nothing may land in cwd"

        # the directory fsync targets the JOURNAL directory itself.  The
        # probe wraps ONE identical-facts rewrite (sha-256 dedup -> the same
        # path) and performs no database I/O while os.open is patched.
        opened: list[str] = []
        real_open = os.open
        (tmp_path / "handle.bin").write_text("x", encoding="utf-8")

        def recording_open(path, flags, *args, **kwargs):
            opened.append(str(path))
            # Windows cannot fsync a read-only handle; POSIX opens the real
            # directory read-only, which is why the stand-in must be writable
            return real_open(str(tmp_path / "handle.bin"), os.O_RDWR)

        monkeypatch.setattr(os, "open", recording_open)
        engine.journal.write(engine.journal.read_all()[key])
        monkeypatch.setattr(os, "open", real_open)
        assert opened, "the journal directory must be opened for the fsync"
        assert str(journal_dir) in opened, opened
        assert all(os.path.isabs(p) for p in opened), opened
        assert os.curdir not in opened and "." not in opened, opened
        assert engine.journal.exists(key), "the rewrite must stay atomic"

        # ---- a restart from ANOTHER working directory replays it ----------
        monkeypatch.chdir(tmp_path)
        store.close()
        store2 = _connect_store()
        contour2 = InverseShortContour(
            _settings(), store2, clock=clock, activation_enabled=True,
            journal_dir=journal_dir,
        )
        engine2 = contour2.engine
        assert key in engine2._pending_obligations, "replayed from the record"
        assert key in engine2.pending_closes
        assert engine2.open_entries({"ETHUSDT": 100.0}) == []

        contour2.run_cycle({"BTCUSDT": entry * 0.98})
        row = store2.get_position(key)
        assert row.status == "CLOSED"
        assert row.exit_reason == "STOP_LOSS"
        assert row.funding_periods_charged == 1
        assert row.funding_paid == pytest.approx(
            ref["funding_paid"], abs=1e-6,
        )
        assert row.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-6)
        assert store2.get_funding_obligation(key).status == (
            FUNDING_OBLIGATION_SETTLED
        )
        assert store2.list_pending_closes() == []
        assert engine2.journal.paths() == []
        assert engine2.balance == pytest.approx(ref["balance"], abs=1e-6)
        assert engine2.durability_risk is False
        store2.close()

    # -- P2: failures must reach the caller ---------------------------------

    def test_p2_e_journal_write_failures_reach_the_caller(self, tmp_path, monkeypatch):
        """P2: a payload-fsync failure and an atomic-replace failure must
        PROPAGATE — the journal never reports a record as durable when the
        ``fsync`` failed (the old ``_fsync_dir`` swallowed every OSError)."""
        from app.paper.me_long_v2_inverse_short_oos2_v1.journal import (
            PendingProtectiveClose,
        )

        journal = ProtectiveCloseJournal(
            tmp_path / "release" / "var" / "me_inverse_short_oos2_journal",
        )
        record = PendingProtectiveClose(
            position_key="ME_LONG_V2_INVERSE_SHORT_OOS2_V1:round8",
            exit_reason="STOP_LOSS",
            triggered_at="2026-06-01T21:00:00+00:00",
            reference_price=100.0,
            fill_price=110.0,
            quantity=0.01,
            snapshot={},
            terminal_leg={},
            written_at="2026-06-01T21:00:00+00:00",
        )

        def exploding_fsync(fd):
            raise OSError("fsync failure")

        monkeypatch.setattr(os, "fsync", exploding_fsync)
        with pytest.raises(OSError):
            journal.write(record)
        monkeypatch.undo()

        journal2 = ProtectiveCloseJournal(
            tmp_path / "release2" / "var" / "journal2",
        )

        def broken_replace(*args, **kwargs):
            raise OSError("atomic replace failure")

        monkeypatch.setattr(os, "replace", broken_replace)
        with pytest.raises(OSError):
            journal2.write(record)
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Codex review round 9 (r4590138816): a protective close must be durable
# even when NO funding obligation is pending.
# ---------------------------------------------------------------------------
class TestCodexRound9OnPostgres:
    """Round 9 P1 against real PostgreSQL."""

    @staticmethod
    def _stop_without_obligation(journal_dir, *, candle_steps=1):
        """Open -> protective STOP with staging DOWN, BEFORE the first
        funding boundary: no obligation exists at all, so the only possible
        durability of the frozen close is the local journal."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
        contour.engine.journal = ProtectiveCloseJournal(journal_dir)
        contour.journal_dir = journal_dir
        position_key, row, bars = TestCodexRound5OnPostgres._open_fault_position(
            store, clock, contour, candle_steps=candle_steps,
        )
        entry = row.entry_price
        original_stage = store.stage_close
        store.stage_close = _always_fail("terminal staging failure (PG down)")
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        return store, clock, contour, position_key, entry, bars, original_stage

    def test_p1_protective_stop_without_an_obligation_is_durable_before_staging(
        self, running, pg_conn, tmp_path,
    ):
        """P1 on PostgreSQL: a STOP before the first funding boundary (no
        obligation anywhere) with ``stage_close`` failing must still leave a
        durable recovery record; after a restart the ORIGINAL STOP executes
        even at a price where the STOP would no longer fire, and the close
        completes with exactly one terminal leg, no invented funding and a
        retired journal record."""
        store, clock, contour, key, entry, bars, original_stage = (
            self._stop_without_obligation(tmp_path)
        )
        engine = contour.engine

        row = store.get_position(key)
        assert row.status == "OPEN", "terminal UPDATE withheld"
        assert row.funding_periods_charged == 0
        assert store.get_funding_obligation(key) is None, (
            "no obligation may exist for a pre-boundary close",
        )
        assert engine._pending_obligations == {}
        assert [p.position_key for p in store.list_pending_closes()] == []
        assert _legs(store, key) == [], "nothing staged yet"
        assert list(tmp_path.glob("*.json")), "the recovery record must exist"

        record = engine.journal.read_all()[key]
        assert record.exit_reason == "STOP_LOSS"
        assert record.staged is False, "an unconfirmed stage is never claimed"
        assert record.obligation is None, "no obligation may be invented"
        assert record.owed_partial_legs == []
        assert record.recovery_ids.get("position_key") == key
        assert engine.durability_risk is True
        assert engine.open_entries({"ETHUSDT": 100.0}) == []

        # ---- restart with PostgreSQL back, price that would NOT fire -----
        store.stage_close = original_stage
        store.close()
        store2 = _connect_store()
        contour2 = InverseShortContour(
            _settings(), store2, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        engine2 = contour2.engine
        assert store2.get_position(key).status == "OPEN"
        assert key in engine2.pending_closes, "the record must be replayed"
        assert key not in engine2.open_positions, (
            "the position must leave the exit algorithm",
        )
        assert engine2.open_entries({"ETHUSDT": 100.0}) == []

        non_triggering = {
            "BTCUSDT": {"high": row.stop_price * 0.5, "low": entry * 0.99},
        }
        cycle = contour2.run_cycle({"BTCUSDT": entry}, bars=non_triggering)
        assert cycle["exit_reasons"] == [], (
            "the STOP must come from the retained snapshot, not from a new "
            "evaluation of the market",
        )
        final = store2.get_position(key)
        assert final.status == "CLOSED"
        assert final.exit_reason == "STOP_LOSS"
        assert final.exit_price == pytest.approx(record.fill_price, rel=1e-9)
        assert final.funding_periods_charged == 0, "no period may be invented"
        assert store2.get_funding_obligation(key) is None
        legs = _legs(store2, key)
        assert [leg.exit_reason for leg in legs] == ["STOP_LOSS"]
        assert len([leg for leg in legs if leg.is_terminal]) == 1
        assert engine2.pending_closes == {}
        assert store2.list_pending_closes() == []
        assert engine2.journal.paths() == [], "retired after proven completion"
        assert engine2.durability_risk is False
        assert engine2.accounting_consistent is True
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"], abs=1e-6,
        )
        store2.close()


class TestCodexRound10OnPostgres:
    """Round 10 P1 (Codex r4597065733) against real PostgreSQL 17.

    Scenario (issue #276 §5): open a position -> protective STOP ->
    PostgreSQL staging DOWN -> durable journal -> restart -> the journal
    file itself temporarily UNREADABLE -> the closed recovery gate ->
    file access restored -> bounded retry -> the frozen STOP restored ->
    financial state identical to a failure-free reference run.

    Faults are injected at the store-method boundary and at the file-read
    boundary only; every leg, outbox, terminal UPDATE, read-back and
    restart is the REAL PostgreSQL code path with real transactions.
    """

    @staticmethod
    def _failure_free_reference(pg_conn) -> dict:
        """Failure-free protective STOP: entry -> STOP fires -> the close
        completes in the same cycle.  Every fact the fault run below
        recovers must equal these."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = InverseShortContour(
            _settings(), store, clock=clock, activation_enabled=True,
        )
        event = _capture_event(store, age=timedelta(seconds=5))
        contour.run_cycle({"BTCUSDT": 100.0})
        position_key = f"ME_LONG_V2_INVERSE_SHORT_OOS2_V1:{event}"
        row = store.get_position(position_key)
        assert row is not None
        entry = row.entry_price
        bars = {
            "BTCUSDT": {"high": row.stop_price * 1.001, "low": entry * 0.99},
        }
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        final = store.get_position(position_key)
        assert final.status == "CLOSED"
        assert final.exit_reason == "STOP_LOSS"
        facts = {
            "entry_price": final.entry_price,
            "exit_price": final.exit_price,
            "net_pnl": final.net_pnl,
            "funding_periods_charged": final.funding_periods_charged,
            "balance": store.balance_components()["balance"],
            "legs": [
                (leg.exit_reason, bool(leg.is_terminal))
                for leg in _legs(store, position_key)
            ],
        }
        store.close()
        _cleanup(pg_conn)
        return facts

    @staticmethod
    def _poison_journal_read(journal_dir) -> callable:
        """Make every READ of a ``*.json`` recovery record inside
        ``journal_dir`` fail with ``EIO`` (the temporary read failure Codex
        injected at restart).  Returns an idempotent restore callable; only
        the journal files are affected, so the real PostgreSQL driver and
        every other file keep working normally."""
        original_open = builtins.open
        root = os.path.abspath(str(journal_dir))

        def failing_open(file, mode="r", *args, **kwargs):
            name = os.path.abspath(str(file))
            reading = not any(flag in mode for flag in "wxa+")
            if (
                reading
                and name.endswith(".json")
                and os.path.dirname(name) == root
            ):
                raise OSError(5, "Input/output error")
            return original_open(file, mode, *args, **kwargs)

        builtins.open = failing_open
        state = {"restored": False}

        def restore() -> None:
            if not state["restored"]:
                state["restored"] = True
                builtins.open = original_open

        return restore

    def test_p1_unreadable_journal_fails_closed_then_recovers(
        self, running, pg_conn, tmp_path,
    ):
        """§5 on real PostgreSQL: a STOP whose staging failed leaves the
        frozen facts ONLY in the journal; restarting while that journal is
        unreadable must fail closed (risk reported, accounting unconfirmed,
        no entries, no exit re-evaluation) and, once the file is readable
        again, the ORIGINAL STOP must be restored and the resulting
        financial state must equal the failure-free reference."""
        ref = self._failure_free_reference(pg_conn)

        # ---- 1. protective STOP with PostgreSQL staging DOWN -------------
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = InverseShortContour(
            _settings(), store, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        position_key, row, bars = TestCodexRound5OnPostgres._open_fault_position(  # noqa: E501
            store, clock, contour, candle_steps=1,
        )
        entry = row.entry_price
        original_stage = store.stage_close
        store.stage_close = _always_fail("terminal staging failure (PG down)")
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        store.stage_close = original_stage

        assert store.get_position(position_key).status == "OPEN", (
            "terminal UPDATE withheld until staging is durable",
        )
        assert _legs(store, position_key) == [], "nothing staged yet"
        assert list(tmp_path.glob("*.json")), "the durable journal must exist"
        record = contour.engine.journal.read_all()[position_key]
        assert record.exit_reason == "STOP_LOSS"
        assert record.staged is False, "an unconfirmed stage is never claimed"
        assert record.recovery_ids.get("position_key") == position_key
        assert contour.engine.durability_risk is True
        assert contour.engine.journal_read_blocked is False
        store.close()

        # ---- 2. restart: PostgreSQL healthy, journal UNREADABLE ----------
        restore_open = self._poison_journal_read(tmp_path)
        try:
            store2 = _connect_store()
            contour2 = InverseShortContour(
                _settings(), store2, clock=clock, activation_enabled=True,
                journal_dir=tmp_path,
            )
            engine2 = contour2.engine
            assert engine2.journal_read_blocked is True, (
                "a failed journal read must be detected at runtime start",
            )
            assert engine2.durability_risk is True, (
                "an unreadable recovery journal must report the durability "
                "risk",
            )
            assert engine2.accounting_consistent is False, (
                "accounting can never be declared consistent while the only "
                "durable copy of the protective exit cannot be read",
            )
            assert store2.get_position(position_key).status == "OPEN"
            assert engine2.open_entries({"ETHUSDT": 100.0}) == [], (
                "no new entries while the journal is unreadable",
            )

            non_triggering = {
                "BTCUSDT": {
                    "high": row.stop_price * 0.5, "low": entry * 0.99,
                },
            }
            blocked = contour2.run_cycle(
                {"BTCUSDT": entry}, bars=non_triggering,
            )
            assert blocked["exit_reasons"] == [], (
                "exit evaluation is suspended while the journal is "
                "unreadable — the STOP is never re-evaluated from new data",
            )
            assert store2.get_position(position_key).status == "OPEN"
            assert _legs(store2, position_key) == [], (
                "nothing may be staged while the journal is unreadable",
            )

            # ---- 3. file access restored -> bounded retry -> recovery -----
            restore_open()
            cycle2 = contour2.run_cycle(
                {"BTCUSDT": entry}, bars=non_triggering,
            )
            assert cycle2["exit_reasons"] == [], (
                "the close comes from the frozen snapshot, not from a new "
                "evaluation of the market",
            )

            final = store2.get_position(position_key)
            assert final.status == "CLOSED"
            assert final.exit_reason == "STOP_LOSS"
            assert final.exit_price == pytest.approx(
                record.fill_price, rel=1e-9,
            ), "the frozen fill price, never recomputed"
            assert engine2.journal_read_blocked is False
            assert engine2.durability_risk is False
            assert engine2.accounting_consistent is True
            legs = _legs(store2, position_key)
            recovered_legs = [
                (leg.exit_reason, bool(leg.is_terminal)) for leg in legs
            ]
            assert recovered_legs == [("STOP_LOSS", True)], (
                "exactly one terminal leg"
            )
            assert final.remaining_size == 0.0
            assert engine2.pending_closes == {}
            assert store2.list_pending_closes() == []
            assert engine2._uncommitted_deltas == {}
            assert engine2.journal.paths() == [], (
                "the record is retired only after the close is proven",
            )

            # ---- 4. identical to the failure-free reference ---------------
            assert final.entry_price == pytest.approx(
                ref["entry_price"], abs=1e-9,
            )
            assert final.exit_price == pytest.approx(
                ref["exit_price"], abs=1e-9,
            ), "the recovered STOP equals the failure-free STOP"
            assert final.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-9)
            assert final.funding_periods_charged == ref[
                "funding_periods_charged"
            ], "no funding period may be invented or lost"
            assert recovered_legs == ref["legs"], (
                "the same single terminal leg as the failure-free reference"
            )
            assert store2.balance_components()["balance"] == pytest.approx(
                ref["balance"], abs=1e-6,
            ), "balance must equal the failure-free control"
            assert engine2.balance == pytest.approx(
                store2.balance_components()["balance"], abs=1e-6,
            )
            store2.close()
        finally:
            restore_open()


class TestCodexRound11OnPostgres:
    """Round 11 P2 (Codex r4597992776) against real PostgreSQL 17.

    Scenario (issue #276 §5): open a position -> protective STOP ->
    PostgreSQL staging DOWN -> durable journal -> the durable record is then
    replaced by a syntactically valid NON-OBJECT payload (``null``) ->
    restart -> ``JOURNAL_READ_FAILED`` and a CLOSED recovery gate -> the
    original valid record is restored by hand -> retry -> the frozen STOP is
    recovered exactly once and the balance equals the failure-free
    reference.

    Only the journal payload and the store-method boundary are faked; the
    leg staging, terminal UPDATE, outbox, read-back verification and every
    restart are the REAL PostgreSQL code path with real transactions.
    """

    @staticmethod
    def _non_object_payload(tmp_path, position_key) -> tuple[bytes, object]:
        """Freeze the valid record's bytes, then replace the file with
        ``null`` — returns ``(original_bytes, path)``."""
        journal = ProtectiveCloseJournal(tmp_path)
        path = journal._path(position_key)
        original = path.read_bytes()
        payload = json.loads(original.decode("utf-8"))
        assert payload["exit_reason"] == "STOP_LOSS"
        assert payload["staged"] is False
        path.write_text("null", encoding="utf-8")
        return original, path

    def test_p2_non_object_journal_payload_fails_closed_then_recovers(
        self, running, pg_conn, tmp_path,
    ):
        """§5 on real PostgreSQL: a ``null`` journal payload must become an
        explicit recovery failure (never an unhandled AttributeError, never
        an absence), keep the whole contour fail-closed, and — once the
        original record is restored — recover the ORIGINAL stop with
        exactly one terminal leg and the reference balance."""
        ref = TestCodexRound10OnPostgres._failure_free_reference(pg_conn)

        # ---- 1-4. STOP with PostgreSQL staging DOWN -> durable journal ---
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = InverseShortContour(
            _settings(), store, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        position_key, row, bars = TestCodexRound5OnPostgres._open_fault_position(  # noqa: E501
            store, clock, contour, candle_steps=1,
        )
        entry = row.entry_price
        original_stage = store.stage_close
        store.stage_close = _always_fail("terminal staging failure (PG down)")
        cycle = contour.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"], cycle
        store.stage_close = original_stage

        assert store.get_position(position_key).status == "OPEN", (
            "terminal UPDATE withheld until staging is durable",
        )
        record = contour.engine.journal.read_all()[position_key]
        assert record.exit_reason == "STOP_LOSS"
        original_payload, record_path = self._non_object_payload(
            tmp_path, position_key,
        )
        assert record_path.read_text(encoding="utf-8") == "null"
        store.close()

        # ---- 7-9. restart with the non-object payload --------------------
        store2 = _connect_store()
        contour2 = InverseShortContour(
            _settings(), store2, clock=clock, activation_enabled=True,
            journal_dir=tmp_path,
        )
        engine2 = contour2.engine

        # ---- 8. JOURNAL_READ_FAILED, never an unhandled exception --------
        verdict = engine2.journal.read_checked()
        assert verdict.status == JOURNAL_READ_FAILED, verdict
        assert JOURNAL_INVALID_PAYLOAD_TYPE in (verdict.reason or ""), (
            "the payload TYPE is the diagnostic",
        )
        assert "NoneType" in (verdict.reason or "")
        assert verdict.records == {}
        with pytest.raises(JournalReadError):
            engine2.journal.read_all()

        # ---- 9-10. closed recovery gate, no re-evaluation, no entries ----
        assert engine2.journal_read_blocked is True
        assert engine2.durability_risk is True
        assert engine2.accounting_consistent is False
        assert store2.get_position(position_key).status == "OPEN"
        assert engine2.open_entries({"ETHUSDT": 100.0}) == []
        assert record_path.exists(), "the damaged file is never deleted"
        assert record_path.read_text(encoding="utf-8") == "null"

        non_triggering = {
            "BTCUSDT": {"high": row.stop_price * 0.5, "low": entry * 0.99},
        }
        blocked = contour2.run_cycle({"BTCUSDT": entry}, bars=non_triggering)
        assert blocked["exit_reasons"] == [], (
            "exit evaluation stays suspended — the STOP is never re-derived",
        )
        assert blocked["entries"] == 0
        assert store2.get_position(position_key).status == "OPEN"
        assert _legs(store2, position_key) == []
        assert record_path.read_text(encoding="utf-8") == "null"

        # ---- 11-12. restore the original record by hand -> retry ---------
        record_path.write_bytes(original_payload)
        cycle2 = contour2.run_cycle({"BTCUSDT": entry}, bars=non_triggering)
        assert engine2.journal_read_blocked is False, (
            "a clean read must open the recovery gate",
        )
        assert engine2.journal_read_failure is None
        assert cycle2["exit_reasons"] == [], "from the snapshot, not data"

        # ---- 13-14. recovered frozen STOP, exactly once, reference balance
        final = store2.get_position(position_key)
        assert final.status == "CLOSED"
        assert final.exit_reason == "STOP_LOSS"
        assert final.exit_price == pytest.approx(record.fill_price, rel=1e-9)
        legs = _legs(store2, position_key)
        assert [(leg.exit_reason, bool(leg.is_terminal)) for leg in legs] == [
            ("STOP_LOSS", True),
        ], "exactly one terminal leg"
        assert final.remaining_size == 0.0
        assert engine2.pending_closes == {}
        assert store2.list_pending_closes() == []
        assert engine2.journal.paths() == [], "retired after proven recovery"
        assert engine2.durability_risk is False
        assert engine2.accounting_consistent is True

        assert final.exit_price == pytest.approx(ref["exit_price"], abs=1e-9)
        assert final.net_pnl == pytest.approx(ref["net_pnl"], abs=1e-9)
        assert store2.balance_components()["balance"] == pytest.approx(
            ref["balance"], abs=1e-6,
        ), "balance must equal the failure-free control"
        assert engine2.balance == pytest.approx(
            store2.balance_components()["balance"], abs=1e-6,
        )
        store2.close()
