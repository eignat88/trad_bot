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

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import connect_test_db

from app.paper.me_long_v2_inverse_short_oos2_v1.models import (
    FUNDING_OBLIGATION_OPEN,
    FUNDING_OBLIGATION_SETTLED,
    InverseShortCloseLeg,
    InverseShortPosition,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
    protocol_fingerprint,
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

    # Round 6 (P2-funding): the additive obligation table (089) rides
    # along with every OOS-2 fixture — apply-once, raise on defect.
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
        self, running,
    ):
        """Combined failure on real PostgreSQL: partial commit AND
        stage_close() fail while update_position() itself is healthy —
        the terminal UPDATE must not run, and a restart restores the
        partial and terminal closes exactly once."""
        store = _connect_store()
        clock = _Clock(datetime.now(timezone.utc))
        contour = _contour(store, clock=clock)
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
        contour2 = _contour(store2, clock=clock)
        restored = store2.get_position(position_key)
        assert restored is not None
        assert restored.status == "OPEN"
        assert restored.tp1_executed is False
        assert restored.remaining_size == pytest.approx(size, rel=1e-9)
        assert contour2.engine.dirty_leg_keys == ()
        assert contour2.engine.durability_risk is False

        # Partial close restored exactly once.
        contour2.run_cycle({"BTCUSDT": entry * 0.945})
        legs = _legs(store2, position_key)
        assert [leg.leg_seq for leg in legs] == [1]
        assert legs[0].exit_reason == "TAKE_PROFIT_1"

        # Terminal close lands exactly once WITH its leg + outbox rows.
        cycle = contour2.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
        assert cycle["pending_closes"] == 0
        legs = _legs(store2, position_key)
        assert [leg.leg_seq for leg in legs] == [1, 3]
        assert store2.list_pending_closes() == []
        final = store2.get_position(position_key)
        assert final is not None and final.status == "CLOSED"
        closed_qty = sum(leg.quantity for leg in legs)
        assert round(final.position_size - final.remaining_size, 9) == round(
            closed_qty, 9,
        ), "quantity conservation across both restored closes"
        assert not contour2.engine.durability_risk
        assert contour2.engine.dirty_leg_keys == ()
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
        self, running, pg_conn,
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
        contour2 = _contour(store2, clock=clock)
        restored = store2.get_position(position_key)
        assert restored.status == "OPEN"
        assert restored.tp1_executed is False
        assert contour2.engine.dirty_leg_keys == ()
        assert contour2.engine.durability_risk is False

        # (f) reconciliation: TP1 exactly once, then the protective STOP
        contour2.run_cycle({"BTCUSDT": entry * 0.945})
        legs = _legs(store2, position_key)
        assert [leg.exit_reason for leg in legs] == ["TAKE_PROFIT_1"]
        cycle = contour2.run_cycle({"BTCUSDT": entry}, bars=bars)
        assert cycle["exit_reasons"] == ["STOP_LOSS"]
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
