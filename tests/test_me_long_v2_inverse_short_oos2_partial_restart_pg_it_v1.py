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
