"""ME_LONG_V2_INVERSE_SHORT_V2 — DB TTL under lock-wait (real PostgreSQL).

Review finding (P1-final): ``INSERT_POSITION_SQL`` judged the entry TTL
against ``statement_timestamp()``, which is stamped when the command
STARTS — but a one-statement admission WAITS for ``FOR UPDATE`` row locks
inside that same command.  An admission issued at 119m59s that then waited
for a lock held past 120 minutes would still be judged by its pre-wait
start time and could create a position after the TTL had actually
elapsed.

``save_position`` now runs a lock-first protocol: it locks the EVENT row
and then the ACTIVATION row in their OWN statements (all waiting happens
there) and only then issues the guarded INSERT, whose
``statement_timestamp()`` is therefore strictly after lock acquisition —
the TTL is judged at the real decision instant.  This suite proves, on a
real PostgreSQL server with migration 087:

* an admission whose event-row lock is held across the TTL boundary is
  REFUSED (no position row, the event stays ``CAPTURED`` and remains
  available for the terminal disposition);
* the same holds when it is the ACTIVATION row that is held;
* a concurrent EXPIRE and a concurrent RUNNING -> DRAINING both win over
  the waiting admission (serialization preserved);
* a fresh signal with no lock wait is admitted as before;
* a PROTOCOLLESS single statement (the pre-fix admission shape) would
  indeed admit past the TTL — documenting the bug this protocol fixes
  (characterization of the old behaviour, not of production code);
* the SQL-contract: lock statements are pure row locks without any clock,
  ``save_position`` executes them in order BEFORE the INSERT, the TTL
  gate stays in the INSERT, and the frozen fingerprints (V1 and V2) are
  byte-identical to the audited values.

Static tests need no database.  PostgreSQL tests are gated on
``TEST_DB_HOST`` / ``TEST_DB_UNIX_SOCK`` and refuse a production-named
database — this suite never writes to the production database.
"""
from __future__ import annotations

import ast
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import connect_test_db

from app.paper.me_long_v2_inverse_short_v1.protocol import (
    protocol_fingerprint as v1_protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v2.models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_EXPIRED,
)
from app.paper.me_long_v2_inverse_short_v2.protocol import (
    ENTRY_MAX_AGE_MINUTES,
    EXPERIMENT_ID,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v2 import store as store_module
from app.paper.me_long_v2_inverse_short_v2.store import (
    ACTIVATION_TABLE,
    EVENT_TABLE,
    INSERT_POSITION_SQL,
    LOCK_ACTIVATION_FOR_ADMISSION_SQL,
    LOCK_EVENT_FOR_ADMISSION_SQL,
    POSITION_TABLE,
)

# Sibling integration suite: shared fixtures/helpers (same directory, no
# package __init__, so pytest puts this directory on sys.path).
from test_me_long_v2_inverse_short_v2_pg_admission_it_v1 import (  # noqa: E402
    MIGRATION_087,
    PINNED_V2_FINGERPRINT,
    _admission_params,
    _build_position,
    _capture_event,
    _cleanup,
    _connect_store,
    _contour,
    _count,
    _event_row,
    _guard_not_production,
    _literalize,
    _read_activation,
    _restore_activation,
    _write_activation,
)

PINNED_V1_FINGERPRINT = (
    "4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c"
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Static SQL / source contract (no database)
# ---------------------------------------------------------------------------
class TestLockFirstProtocolContract:
    def test_lock_statements_are_pure_row_locks_without_any_clock(self):
        for sql in (LOCK_EVENT_FOR_ADMISSION_SQL, LOCK_ACTIVATION_FOR_ADMISSION_SQL):
            assert "FOR UPDATE" in sql
            assert sql.count("FOR UPDATE") == 1
            # No clock may appear in a lock statement: the TTL decision
            # belongs exclusively to the guarded INSERT.
            assert "statement_timestamp()" not in sql
            assert "clock_timestamp()" not in sql
            assert "now()" not in sql
            assert "interval '" not in sql
        assert "event_key = %s" in LOCK_EVENT_FOR_ADMISSION_SQL
        assert "status = %s" in LOCK_EVENT_FOR_ADMISSION_SQL      # CAPTURED gate
        assert "experiment_id = %s" in LOCK_ACTIVATION_FOR_ADMISSION_SQL

    def test_ttl_gate_stays_in_the_guarded_insert(self):
        assert "statement_timestamp()" in INSERT_POSITION_SQL
        assert f"interval '{ENTRY_MAX_AGE_MINUTES} minutes'" in INSERT_POSITION_SQL
        assert "FOR UPDATE OF ev, act" in INSERT_POSITION_SQL
        assert "clock_timestamp()" not in INSERT_POSITION_SQL

    def test_save_position_locks_event_then_activation_then_inserts(self):
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # The abstract base also declares ``save_position`` — pick the
        # implementation that carries the admission protocol.
        segment = next(
            ast.get_source_segment(source, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "save_position"
            and "LOCK_EVENT_FOR_ADMISSION_SQL" in (ast.get_source_segment(source, node) or "")
        )
        i_event = segment.index("LOCK_EVENT_FOR_ADMISSION_SQL")
        i_activation = segment.index("LOCK_ACTIVATION_FOR_ADMISSION_SQL")
        i_insert = segment.index("INSERT_POSITION_SQL")
        assert i_event < i_activation < i_insert, (
            "save_position must lock event, then activation, then INSERT"
        )
        # Both lock steps short-circuit BEFORE the insert when no row locks.
        assert segment.count("fetchone() is None") >= 2
        assert segment.index("fetchone() is None") < i_insert

    def test_frozen_fingerprints_are_unchanged(self):
        assert protocol_fingerprint() == PINNED_V2_FINGERPRINT
        assert v1_protocol_fingerprint() == PINNED_V1_FINGERPRINT


# ---------------------------------------------------------------------------
# PostgreSQL fixtures (same conventions as the admission suite)
# ---------------------------------------------------------------------------
_original_activation: tuple | None = None


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
    if not cursor.fetchone()[0]:
        try:
            cursor.execute(MIGRATION_087.read_text(encoding="utf-8"))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.close()
            pytest.skip(f"migration 087 not applicable: {exc}")
        cursor.execute("SELECT to_regclass(%s)", (EVENT_TABLE,))
        if not cursor.fetchone()[0]:
            conn.close()
            pytest.skip("V2 tables missing after migration 087 attempt")

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
    """Activation = RUNNING with the pinned fingerprint + a fresh freeze."""
    freeze = datetime.now(timezone.utc) - timedelta(hours=3)
    _write_activation(pg_conn, "RUNNING", protocol_fingerprint(), freeze)
    return freeze


# ---------------------------------------------------------------------------
# Lock-wait helpers
# ---------------------------------------------------------------------------
def _hold_lock(sql: str, params: tuple):
    """Open a SECOND PostgreSQL session that holds ``FOR UPDATE`` on the row.

    The transaction is deliberately left open — the lock is released by
    ``_release`` (commit of a read-only transaction).
    """
    conn = connect_test_db()
    _guard_not_production(conn)
    cursor = conn.cursor()
    cursor.execute(sql, params)
    row = cursor.fetchone()
    assert row is not None, "lock target row missing in the TEST database"
    return conn


def _release(conn) -> None:
    try:
        conn.commit()  # no writes; ending the txn releases the row lock
    finally:
        conn.close()


def _start_admission(store, position) -> tuple[threading.Thread, dict]:
    """Run ``save_position`` on a background thread; return (thread, outcome)."""
    outcome: dict = {"result": None, "error": None, "done_at": None}

    def target() -> None:
        try:
            outcome["result"] = store.save_position(position)
        except BaseException as exc:  # noqa: BLE001 - surfaced via outcome
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        outcome["done_at"] = time.monotonic()

    thread = threading.Thread(target=target, daemon=True, name="v2-admission")
    thread.start()
    return thread, outcome


def _age_at(signal_time: datetime) -> timedelta:
    return datetime.now(timezone.utc) - signal_time


# ---------------------------------------------------------------------------
# The mandatory scenario: lock held ACROSS the TTL boundary
# ---------------------------------------------------------------------------
class TestLockWaitAcrossTtlBoundary:
    def test_event_lock_held_past_ttl_refuses_admission(
        self, pg_conn, running,
    ):
        """Age ~119m59s + event row locked by a second session until the TTL
        has elapsed: the waiting ``save_position`` must be REFUSED, leave no
        position, and keep the event available for terminal disposition."""
        store = _connect_store()
        contour = _contour(store)
        key = _capture_event(
            store, age=timedelta(minutes=119, seconds=59), symbol="BTCUSDT",
        )
        position = _build_position(contour, store, key)
        signal_time = store.get_event(key).signal_time
        assert signal_time is not None

        blocker = _hold_lock(
            LOCK_EVENT_FOR_ADMISSION_SQL, (key, EVENT_STATUS_CAPTURED),
        )
        hold_s = 6.0
        t0 = time.monotonic()
        thread, outcome = _start_admission(store, position)
        time.sleep(hold_s)
        # The TTL has certainly elapsed while the admission is still waiting:
        assert _age_at(signal_time) > timedelta(minutes=ENTRY_MAX_AGE_MINUTES)
        _release(blocker)
        thread.join(timeout=60)
        assert outcome["done_at"] is not None, outcome["error"]

        # 1) It really waited for the row lock (it could not finish inside
        #    the hold window)...
        assert outcome["done_at"] - t0 >= hold_s, (
            "admission did not wait for the event-row lock"
        )
        # 2) ...and was REFUSED after the lock arrived past the TTL.
        assert outcome["error"] is None, outcome["error"]
        assert outcome["result"] is False
        # 3) No position row exists.
        assert _count(pg_conn, POSITION_TABLE) == 0
        # 4) The event is still CAPTURED — available for the terminal
        #    disposition, which now succeeds:
        assert _event_row(pg_conn, key)["status"] == EVENT_STATUS_CAPTURED
        assert store.mark_event_expired(
            key, EVENT_STATUS_EXPIRED, reason="ENTRY_TTL",
            decided_at=datetime.now(timezone.utc),
        ) is True
        assert _event_row(pg_conn, key)["status"] == EVENT_STATUS_EXPIRED
        assert _count(pg_conn, POSITION_TABLE) == 0

    def test_activation_lock_held_past_ttl_refuses_admission(
        self, pg_conn, running,
    ):
        """Same boundary, but the second session holds the ACTIVATION row:
        the admission waits at its second lock and must be refused once the
        TTL elapsed during the wait."""
        store = _connect_store()
        contour = _contour(store)
        key = _capture_event(
            store, age=timedelta(minutes=119, seconds=59), symbol="ETHUSDT",
        )
        position = _build_position(contour, store, key)
        signal_time = store.get_event(key).signal_time
        assert signal_time is not None

        blocker = _hold_lock(
            LOCK_ACTIVATION_FOR_ADMISSION_SQL, (EXPERIMENT_ID,),
        )
        hold_s = 6.0
        t0 = time.monotonic()
        thread, outcome = _start_admission(store, position)
        time.sleep(hold_s)
        assert _age_at(signal_time) > timedelta(minutes=ENTRY_MAX_AGE_MINUTES)
        _release(blocker)
        thread.join(timeout=60)
        assert outcome["done_at"] is not None, outcome["error"]

        assert outcome["done_at"] - t0 >= hold_s, (
            "admission did not wait for the activation-row lock"
        )
        assert outcome["error"] is None, outcome["error"]
        assert outcome["result"] is False
        assert _count(pg_conn, POSITION_TABLE) == 0
        assert _event_row(pg_conn, key)["status"] == EVENT_STATUS_CAPTURED

    def test_fresh_signal_without_lock_wait_admits(self, pg_conn, running):
        """The protocol must not disturb the normal path: a fresh signal
        with nobody holding a lock is admitted immediately."""
        store = _connect_store()
        contour = _contour(store)
        key = _capture_event(store, age=timedelta(seconds=90), symbol="SOLUSDT")
        position = _build_position(contour, store, key)

        started = time.monotonic()
        assert store.save_position(position) is True
        assert time.monotonic() - started < 10.0  # no unexplained waiting
        assert _count(pg_conn, POSITION_TABLE) == 1
        assert _event_row(pg_conn, key)["status"] == EVENT_STATUS_CAPTURED


# ---------------------------------------------------------------------------
# Serialization with the competing terminal operations
# ---------------------------------------------------------------------------
class TestCompetingDispositionDuringLockWait:
    def test_competing_expire_beats_a_waiting_admission(self, pg_conn, running):
        """While the admission waits on the event row, the holder performs
        the EXPIRE itself and commits: the admission must lose and refuse."""
        store = _connect_store()
        contour = _contour(store)
        key = _capture_event(store, age=timedelta(seconds=90), symbol="BTCUSDT")
        position = _build_position(contour, store, key)

        blocker = _hold_lock(
            LOCK_EVENT_FOR_ADMISSION_SQL, (key, EVENT_STATUS_CAPTURED),
        )
        t0 = time.monotonic()
        thread, outcome = _start_admission(store, position)
        time.sleep(1.0)  # the admission is now waiting on the event row
        cursor = blocker.cursor()
        cursor.execute(
            f"UPDATE {EVENT_TABLE} SET status = %s, status_reason = %s, "
            "status_at = now(), updated_at = now() "
            "WHERE event_key = %s AND status = %s",
            (EVENT_STATUS_EXPIRED, "ENTRY_TTL", key, EVENT_STATUS_CAPTURED),
        )
        assert cursor.rowcount == 1
        blocker.commit()
        blocker.close()
        thread.join(timeout=60)
        assert outcome["done_at"] is not None, outcome["error"]

        assert outcome["done_at"] - t0 >= 1.0, "admission did not wait"
        assert outcome["error"] is None, outcome["error"]
        assert outcome["result"] is False
        assert _count(pg_conn, POSITION_TABLE) == 0
        assert _event_row(pg_conn, key)["status"] == EVENT_STATUS_EXPIRED

    def test_competing_drain_beats_a_waiting_admission(self, pg_conn, running):
        """While the admission waits on the activation row, the holder moves
        RUNNING -> DRAINING and commits: the admission must lose and refuse,
        leaving its event CAPTURED (no position, no forced disposition)."""
        store = _connect_store()
        contour = _contour(store)
        key = _capture_event(store, age=timedelta(seconds=90), symbol="ETHUSDT")
        position = _build_position(contour, store, key)

        blocker = _hold_lock(
            LOCK_ACTIVATION_FOR_ADMISSION_SQL, (EXPERIMENT_ID,),
        )
        t0 = time.monotonic()
        thread, outcome = _start_admission(store, position)
        time.sleep(1.0)  # the admission is now waiting on the activation row
        cursor = blocker.cursor()
        cursor.execute(
            f"UPDATE {ACTIVATION_TABLE} SET status = %s, updated_at = now() "
            "WHERE experiment_id = %s",
            ("DRAINING", EXPERIMENT_ID),
        )
        assert cursor.rowcount == 1
        blocker.commit()
        blocker.close()
        thread.join(timeout=60)
        assert outcome["done_at"] is not None, outcome["error"]

        assert outcome["done_at"] - t0 >= 1.0, "admission did not wait"
        assert outcome["error"] is None, outcome["error"]
        assert outcome["result"] is False
        assert _count(pg_conn, POSITION_TABLE) == 0
        assert _event_row(pg_conn, key)["status"] == EVENT_STATUS_CAPTURED


# ---------------------------------------------------------------------------
# Why the protocol exists (characterization of the PRE-FIX admission shape)
# ---------------------------------------------------------------------------
class TestWhyTheLockFirstProtocolIsNeeded:
    def test_protocolless_single_statement_admits_after_the_lock_wait(
        self, pg_conn, running,
    ):
        """PROTOCOLLESS control: executing ``INSERT_POSITION_SQL`` directly —
        the pre-fix single-statement admission — stamps
        ``statement_timestamp()`` when the command STARTS, then waits for
        the event-row lock.  Released past 120 minutes, the stale stamp still
        satisfies the TTL gate and a position IS admitted although the TTL
        had already elapsed.  Production ``save_position`` never executes the
        INSERT this way (lock-first protocol), which the tests above prove.

        Timing margins: signal age starts 10 s before the TTL (stamp must be
        pre-TTL), the lock is held 15 s (release ≥ 5 s post-TTL).
        """
        store = _connect_store()
        contour = _contour(store)
        key = _capture_event(
            store, age=timedelta(minutes=119, seconds=50), symbol="SOLUSDT",
        )
        position = _build_position(contour, store, key)
        signal_time = store.get_event(key).signal_time
        assert signal_time is not None
        script = _literalize(INSERT_POSITION_SQL, _admission_params(store, position))

        blocker = _hold_lock(
            LOCK_EVENT_FOR_ADMISSION_SQL, (key, EVENT_STATUS_CAPTURED),
        )

        def _release_later() -> None:
            time.sleep(15.0)
            blocker.commit()
            blocker.close()

        releaser = threading.Thread(target=_release_later, daemon=True)
        releaser.start()

        raw = connect_test_db()
        _guard_not_production(raw)
        cursor = raw.cursor()
        t0 = time.monotonic()
        cursor.execute(script)   # blocks: stamped BEFORE the wait
        waited = time.monotonic() - t0
        try:
            # It really waited for the row lock...
            assert waited >= 10.0, f"raw statement did not wait ({waited:.2f}s)"
            # ...the decision moment is already past the TTL...
            assert _age_at(signal_time) > timedelta(
                minutes=ENTRY_MAX_AGE_MINUTES,
            )
            # ...and the protocolless statement ADMITTED anyway (the bug).
            assert cursor.rowcount == 1, (
                "expected the pre-fix single-statement shape to admit"
            )
            raw.commit()
            assert _count(pg_conn, POSITION_TABLE) == 1
        finally:
            raw.close()
            releaser.join(timeout=5)
