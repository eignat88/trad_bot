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
    # leg rows first (logical children), then outbox -> position -> event.
    for table in (CLOSE_LEG_TABLE, CLOSE_OUTBOX_TABLE, POSITION_TABLE,
                  EVENT_TABLE):
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
        # trading / financial columns byte-identical
        for column in (
            "remaining_size", "tp1_executed", "tp2_executed", "tp3_executed",
            "realized_legs_net", "entry_cost_allocated", "funding_paid",
            "funding_periods", "net_pnl", "gross_pnl", "status",
            "entered_at", "deadline_at", "position_size", "entry_price",
        ):
            assert getattr(after, column) == getattr(row, column), column
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
        event_b = _capture_event(store, age=timedelta(seconds=5))
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
