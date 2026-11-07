"""Codex review P1 for PR #267: PostgreSQL reconnection + state recovery.

Requirements under test (fault injection, NO real PostgreSQL):

1. connection-level pg8000 errors (``InterfaceError`` / socket errors) are
   detected and DISTINGUISHED from SQL/constraint errors (``DatabaseError``
   family) — SQL errors never trigger a reconnect;
2. a dropped connection is replaced with a NEW connection using limited
   attempts and exponential backoff;
3. the ``InverseShortPaperEngine`` is NEVER recreated — ``open_positions``,
   ``pending_closes``, ``dirty_funding``, accumulated PnL and staged
   terminal snapshots survive the outage untouched;
4. after reconnection the idempotent writes (funding, close outbox,
   terminal UPDATE) are retried and commit EXACTLY once;
5. no funding/PnL double counting anywhere;
6. new entries fail closed until BOTH the connection and the accounting
   state are restored;
7. protective STOP/TP/TIMEOUT keeps being evaluated on market data while
   PostgreSQL is down;
8. nothing is claimed durable before the actual commit (a commit lost to a
   dropped socket re-executes the whole unit);
9. DRAINING / EXIT_ONLY keeps working through the outage;
10. fault scenarios: connection drop, database restart, reconnect,
    repeated writes, preserved exit facts, exactly-once application.

The harness is a TRANSACTIONAL fake connection — mutations are staged per
connection and applied only on ``commit`` (a dead socket takes its staged
transaction with it), with scripted drops — driving the REAL
``PgInverseShortStore._with_reconnect`` / ``_reconnect`` code paths.

Ambiguous-COMMIT extensions (review P4, consumed by
``test_me_long_v2_inverse_short_ambiguous_commit_v1.py``):

* ``commit_ack_lost`` / ``commit_ack_lost_on`` — the NEXT write commit of
  the given statement kind APPLIES its transaction and then loses the
  acknowledgement (socket dies after the server committed); reads (empty
  transactions) never consume it;
* ``fail_execute_match`` / ``fail_execute_times`` / ``fail_execute_skip`` —
  inject a statement error into ONE statement kind, N times, after
  skipping the first K matching statements (e.g. let the first funding
  UPDATE through and fail only its retried twin);
* staged mutations are TAGGED by statement kind so a targeted ambiguous
  commit cannot be consumed by an unrelated write (e.g. an outbox flush).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pg8000.dbapi as pg_dbapi
import pg8000.exceptions as pg_exceptions
import pytest

from app.config import Settings
from app.paper.me_long_v2_inverse_short_v1 import (
    ActivationRecord,
    InverseShortContour,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_v1.capture import (
    CAPTURED,
    CAPTURE_ERROR,
)
from app.paper.me_long_v2_inverse_short_v1.models import (
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
)
from app.paper.me_long_v2_inverse_short_v1.store import (
    ACTIVATION_TABLE,
    DELETE_CLOSE_OUTBOX_SQL,
    EVENT_TABLE,
    INSERT_CLOSE_OUTBOX_SQL,
    INSERT_EVENT_SQL,
    INSERT_POSITION_SQL,
    LIST_CLOSE_OUTBOX_SQL,
    MARK_EVENT_ENTERED_SQL,
    POSITION_TABLE,
    UPDATE_POSITION_SQL,
    PgInverseShortStore,
    _EVENT_COLUMNS,
    _POSITION_COLUMNS,
    is_connection_error,
)
from app.scanners.models import SetupCandidate, SetupState

ENGINE_LOGGER = "app.paper.me_long_v2_inverse_short_v1.engine"
FROZEN_FINGERPRINT = (
    "4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c"
)

FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)

# Normalized SQL for constant statements (whitespace-insensitive matching).
_NORM = lambda sql: " ".join(sql.split())  # noqa: E731
SQL_INSERT_POSITION = _NORM(INSERT_POSITION_SQL)
SQL_UPDATE_POSITION = _NORM(UPDATE_POSITION_SQL)
SQL_MARK_EVENT_ENTERED = _NORM(MARK_EVENT_ENTERED_SQL)
SQL_INSERT_EVENT = _NORM(INSERT_EVENT_SQL)
SQL_INSERT_OUTBOX = _NORM(INSERT_CLOSE_OUTBOX_SQL)
SQL_LIST_OUTBOX = _NORM(LIST_CLOSE_OUTBOX_SQL)
SQL_DELETE_OUTBOX = _NORM(DELETE_CLOSE_OUTBOX_SQL)
SQL_LOAD_ACTIVATION = _NORM(
    "SELECT experiment_id, status, protocol_fingerprint, freeze_ts "
    f"FROM {ACTIVATION_TABLE} WHERE experiment_id = %s",
)
SQL_GET_EVENT = _NORM(f"SELECT * FROM {EVENT_TABLE} WHERE event_key = %s")
SQL_LIST_EVENTS_STATUS = _NORM(
    f"SELECT * FROM {EVENT_TABLE} WHERE status = %s "
    "ORDER BY signal_time, event_key",
)
SQL_LIST_EVENTS_ALL = _NORM(
    f"SELECT * FROM {EVENT_TABLE} ORDER BY signal_time, event_key",
)
SQL_POSITIONS_STATUS = _NORM(
    f"SELECT * FROM {POSITION_TABLE} WHERE status = %s ORDER BY entered_at",
)
SQL_POSITIONS_ALL = _NORM(
    f"SELECT * FROM {POSITION_TABLE} ORDER BY entered_at",
)
SQL_GET_POSITION = _NORM(
    f"SELECT * FROM {POSITION_TABLE} WHERE position_key = %s",
)
SQL_HAS_POSITION = _NORM(
    f"SELECT 1 FROM {POSITION_TABLE} WHERE position_key = %s",
)


def _tag_of(sql_norm: str) -> str:
    """Statement kind of an already-normalized statement (staged txn tags)."""
    if sql_norm == SQL_INSERT_POSITION:
        return "INSERT_POSITION"
    if sql_norm == SQL_UPDATE_POSITION:
        return "UPDATE_POSITION"
    if sql_norm == SQL_MARK_EVENT_ENTERED:
        return "MARK_EVENT_ENTERED"
    if sql_norm == SQL_INSERT_EVENT:
        return "INSERT_EVENT"
    if sql_norm == SQL_INSERT_OUTBOX:
        return "STAGE_CLOSE"
    if sql_norm == SQL_DELETE_OUTBOX:
        return "CLEAR_CLOSE"
    return "WRITE"

# The mutable column order of UPDATE_POSITION_SQL (must stay in sync with
# PgInverseShortStore.update_position).
_UPDATE_COLUMNS = (
    "funding_paid", "funding_periods", "highest_since_entry",
    "lowest_since_entry", "mfe", "mae", "status", "exit_price",
    "exit_reason", "exit_fee", "slippage", "gross_pnl", "net_pnl",
    "closed_at", "duration_sec",
)

# Timestamp columns: real PostgreSQL returns ``datetime`` objects for
# ``timestamp with time zone`` — the fake mimics that by parsing the ISO
# strings the store passes in as parameters.
_DATETIME_COLUMNS = frozenset({
    "entered_at", "deadline_at", "closed_at", "signal_time",
    "captured_at", "activation_freeze_ts",
})


def _pg_value(column: str, value):
    if column in _DATETIME_COLUMNS and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


# ======================================================================
# Shared helpers (mirrors the other contour suites)
# ======================================================================

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
        max_open_positions=3,
        taker_fee=0.00055,
        slippage_percent=0.0005,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )
    defaults.update(overrides)
    return dataclasses.replace(Settings(), **defaults)


def _candidate(symbol: str = "BTCUSDT", price: float = 100.0,
               detected_at: datetime = SIGNAL_AT) -> SetupCandidate:
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
        features={"exhaustion_magnitude": 0.01, "rsi_14": 70.0},
    )


def _capture(store, candidate, *, clock=None) -> str:
    from app.paper.me_long_v2_inverse_short_v1 import capture_with_status

    return capture_with_status(
        store, candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"}, clock=clock,
    )


# ======================================================================
# Fake PostgreSQL: transactional connections + scripted failures
# ======================================================================

class _FakeServer:
    """A scriptable fake PostgreSQL 'server' (data + failure injection).

    * ``restart_window()`` — database restart: every open session dies and
      new connections are refused until ``recover()``;
    * ``connect_failures``   — the next N connection attempts fail (even
                               while the server is up);
    * ``fail_execute_with``  — the next statement raises this error ONCE
                               (SQL/constraint error injection), narrowed
                               by ``fail_execute_match`` (SQL substring),
                               ``fail_execute_times`` (N matching raises)
                               and ``fail_execute_skip`` (K matches pass);
    * ``fail_commit_with``   — the next commit raises this error ONCE and
                               kills the socket (commit lost: the staged
                               transaction dies with it);
    * ``commit_ack_lost`` / ``commit_ack_lost_on`` — AMBIGUOUS COMMIT: the
      next write commit of that statement kind applies its transaction and
      THEN loses the acknowledgement (applied, ack lost);
    * ``apply_log``          — every MUTATION actually applied to the
                               committed state, for exactly-once asserts.
    """

    def __init__(self) -> None:
        self.activation: tuple | None = None
        self.positions: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.outbox: dict[str, dict] = {}
        self.down = False
        self.connect_failures = 0
        self.fail_execute_with: BaseException | None = None
        # Optional SQL substring the injected execute error must match
        # (None = any statement).  Lets a test fail ONE statement kind
        # (e.g. only the funding UPDATE) without touching reads.
        self.fail_execute_match: str | None = None
        # How many matching statements raise before the injection clears.
        self.fail_execute_times = 1
        # Matching statements to SKIP before the injection starts raising
        # (e.g. let the FIRST funding UPDATE through, fail only its retry).
        self.fail_execute_skip = 0
        self.fail_commit_with: BaseException | None = None
        # Ambiguous COMMIT: the NEXT commit that carries writes APPLIES the
        # transaction and then loses the acknowledgement (socket dies
        # after the server committed).  Reads (empty transactions) never
        # consume it; ``commit_ack_lost_on`` restricts it to one statement
        # kind (e.g. only the position INSERT).
        self.commit_ack_lost = False
        self.commit_ack_lost_on: str | None = None
        self.connections: list[_FakeConnection] = []
        self.apply_log: list[tuple[str, str]] = []
        self.connect_attempts = 0
        self.commit_count = 0
        self.event_selects = 0

    # -- lifecycle --------------------------------------------------------
    def connect(self, **kwargs) -> "_FakeConnection":
        self.connect_attempts += 1
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise pg_exceptions.InterfaceError(
                "Can't create a connection (scripted transient failure)",
            )
        if self.down:
            raise pg_exceptions.InterfaceError(
                "Can't create a connection to host fake and port 5432",
            )
        conn = _FakeConnection(self, len(self.connections))
        self.connections.append(conn)
        return conn

    def restart_window(self) -> None:
        """Database restart: sessions die, new connections are refused."""
        self.down = True
        for conn in self.connections:
            conn.broken = True

    def recover(self) -> None:
        """PostgreSQL is reachable again (dead sessions stay dead)."""
        self.down = False

    def connection_error(self) -> pg_exceptions.InterfaceError:
        return pg_exceptions.InterfaceError("network error")

    def set_status(self, status: str) -> None:
        assert self.activation is not None
        experiment_id, _, fingerprint, freeze_ts = self.activation
        self.activation = (experiment_id, status, fingerprint, freeze_ts)

    # -- statement dispatch ---------------------------------------------
    def plan(
        self, sql: str, params: tuple,
    ) -> tuple[list[str] | None, list[tuple], int, object | None]:
        """Map ONE statement onto the data model.

        Returns ``(columns, rows, rowcount, mutation)`` — the mutation is
        STAGED on the calling connection and applied only on commit.
        """
        s = " ".join(sql.split())

        # ---------------- writes (staged mutations) ---------------------
        if s == SQL_INSERT_POSITION:
            row = {
                c: _pg_value(c, v) for c, v in zip(_POSITION_COLUMNS, params)
            }
            key = row["position_key"]
            inserted = key not in self.positions

            def insert_position() -> None:
                self.positions[key] = dict(row)
                self.apply_log.append(("INSERT_POSITION", key))

            return (None, [], 1 if inserted else 0,
                    insert_position if inserted else None)

        if s == SQL_UPDATE_POSITION:
            values = {
                c: _pg_value(c, v)
                for c, v in zip(_UPDATE_COLUMNS, params[:15])
            }
            key = str(params[15])
            matched = key in self.positions

            def update_position() -> None:
                self.positions[key].update(values)
                self.apply_log.append(("UPDATE_POSITION", key))

            return (None, [], 1 if matched else 0,
                    update_position if matched else None)

        if s == SQL_MARK_EVENT_ENTERED:
            new_status, position_key, event_key, expected = params
            event = self.events.get(event_key)
            matched = event is not None and event["status"] == expected

            def mark_entered() -> None:
                assert event is not None
                event["status"] = new_status
                event["position_key"] = position_key
                self.apply_log.append(("MARK_EVENT_ENTERED", event_key))

            return (None, [], 1 if matched else 0,
                    mark_entered if matched else None)

        if s == SQL_INSERT_EVENT:
            row = {
                c: _pg_value(c, v)
                for c, v in zip(_EVENT_COLUMNS, params[:-2])
            }
            experiment_id, fingerprint = params[-2], params[-1]
            guard = (
                self.activation is not None
                and self.activation[0] == experiment_id
                and self.activation[1] == "RUNNING"
                and self.activation[2] == fingerprint
                and self.activation[3] is not None
            )
            key = row["event_key"]
            inserted = guard and key not in self.events

            def insert_event() -> None:
                self.events[key] = dict(row)
                self.apply_log.append(("INSERT_EVENT", key))

            return (None, [], 1 if inserted else 0,
                    insert_event if inserted else None)

        if s == SQL_INSERT_OUTBOX:
            key, symbol, exit_reason, closed_at, snapshot = params

            def stage_close() -> None:
                self.outbox[key] = {
                    "position_key": key, "symbol": symbol,
                    "exit_reason": exit_reason,
                    "closed_at": _pg_value("closed_at", closed_at),
                    "snapshot": snapshot,
                }
                self.apply_log.append(("STAGE_CLOSE", key))

            return (None, [], 1, stage_close)

        if s == SQL_DELETE_OUTBOX:
            key = str(params[0])
            existed = key in self.outbox

            def clear_close() -> None:
                self.outbox.pop(key, None)
                self.apply_log.append(("CLEAR_CLOSE", key))

            return (None, [], 1 if existed else 0,
                    clear_close if existed else None)

        # ---------------- reads ------------------------------------------
        if s == SQL_LIST_OUTBOX:
            rows = [
                (row["position_key"], row["snapshot"])
                for row in self.outbox.values()
            ]
            return (["position_key", "snapshot"], rows, len(rows), None)

        if s == SQL_LOAD_ACTIVATION:
            rows: list[tuple] = []
            if self.activation is not None and self.activation[0] == params[0]:
                rows = [tuple(self.activation)]
            return (
                ["experiment_id", "status", "protocol_fingerprint",
                 "freeze_ts"],
                rows, len(rows), None,
            )

        if s == SQL_GET_EVENT:
            event = self.events.get(params[0])
            rows = [tuple(event[c] for c in _EVENT_COLUMNS)] if event else []
            return (list(_EVENT_COLUMNS), rows, len(rows), None)

        if s == SQL_LIST_EVENTS_STATUS:
            self.event_selects += 1
            rows = [
                tuple(e[c] for c in _EVENT_COLUMNS)
                for e in sorted(
                    (e for e in self.events.values()
                     if e["status"] == params[0]),
                    key=lambda e: (e["signal_time"], e["event_key"]),
                )
            ]
            return (list(_EVENT_COLUMNS), rows, len(rows), None)

        if s == SQL_LIST_EVENTS_ALL:
            self.event_selects += 1
            rows = [
                tuple(e[c] for c in _EVENT_COLUMNS)
                for e in sorted(
                    self.events.values(),
                    key=lambda e: (e["signal_time"], e["event_key"]),
                )
            ]
            return (list(_EVENT_COLUMNS), rows, len(rows), None)

        if s == SQL_POSITIONS_STATUS:
            rows = [
                tuple(p[c] for c in _POSITION_COLUMNS)
                for p in sorted(
                    (p for p in self.positions.values()
                     if p["status"] == params[0]),
                    key=lambda p: p["entered_at"],
                )
            ]
            return (list(_POSITION_COLUMNS), rows, len(rows), None)

        if s == SQL_POSITIONS_ALL:
            rows = [
                tuple(p[c] for c in _POSITION_COLUMNS)
                for p in sorted(self.positions.values(),
                                key=lambda p: p["entered_at"])
            ]
            return (list(_POSITION_COLUMNS), rows, len(rows), None)

        if s == SQL_HAS_POSITION:
            return (["?"], [(1,)] if params[0] in self.positions else [],
                    1 if params[0] in self.positions else 0, None)

        if s == SQL_GET_POSITION:
            row = self.positions.get(params[0])
            rows = [tuple(row[c] for c in _POSITION_COLUMNS)] if row else []
            return (list(_POSITION_COLUMNS), rows, len(rows), None)

        if ("COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN net_pnl"
                in s):
            closed_net = sum(
                (p["net_pnl"] or 0.0) for p in self.positions.values()
                if p["status"] == POSITION_STATUS_CLOSED
            )
            open_costs = sum(
                (p["entry_fee"] or 0.0) + (p["entry_slippage"] or 0.0)
                + (p["funding_paid"] or 0.0)
                for p in self.positions.values()
                if p["status"] == POSITION_STATUS_OPEN
            )
            return (["closed_net", "open_costs"],
                    [(closed_net, open_costs)], 1, None)

        if "COUNT(*)" in s and "WHERE status = 'CLOSED'" in s:
            closed = [p for p in self.positions.values()
                      if p["status"] == POSITION_STATUS_CLOSED]
            net = sum((p["net_pnl"] or 0.0) for p in closed)
            wins = sum(1 for p in closed if (p["net_pnl"] or 0.0) > 0)
            losses = len(closed) - wins
            return (["closed", "wins", "losses", "net"],
                    [(len(closed), wins, losses, net)], 1, None)

        raise AssertionError(f"fake PostgreSQL received unhandled SQL: {s}")


class _FakeConnection:
    """One session: staged transaction, commit/rollback, scriptable death."""

    def __init__(self, server: _FakeServer, index: int) -> None:
        self.server = server
        self.index = index
        self.broken = False      # socket died mid-session
        self.closed = False      # closed by the store (old transport)
        self.staged: list = []

    def _ensure_alive(self) -> None:
        if self.closed or self.broken or self.server.down:
            raise self.server.connection_error()

    def cursor(self) -> "_FakeCursor":
        self._ensure_alive()
        return _FakeCursor(self)

    def commit(self) -> None:
        self._ensure_alive()
        server = self.server
        if server.fail_commit_with is not None:
            exc = server.fail_commit_with
            server.fail_commit_with = None
            # The socket dies DURING the commit: the transaction is lost
            # with the session — nothing was applied.
            self.broken = True
            raise exc
        staged = list(self.staged)
        for _, mutation in staged:
            mutation()
        self.staged.clear()
        server.commit_count += 1
        ack_targeted = (
            server.commit_ack_lost_on is None
            or any(tag == server.commit_ack_lost_on for tag, _ in staged)
        )
        if staged and ack_targeted and server.commit_ack_lost:
            # Ambiguous COMMIT: the server APPLIED the transaction but the
            # acknowledgement is lost with the dying socket.
            server.commit_ack_lost = False
            self.broken = True
            raise server.connection_error()

    def rollback(self) -> None:
        self.staged.clear()

    def close(self) -> None:
        self.closed = True
        self.staged.clear()


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self.conn = conn
        self.description: list | None = None
        self.rows: list[tuple] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        self.conn._ensure_alive()
        server = self.conn.server
        s = " ".join(sql.split())
        if server.fail_execute_with is not None and (
            server.fail_execute_match is None
            or server.fail_execute_match in s
        ):
            if server.fail_execute_skip > 0:
                server.fail_execute_skip -= 1
            elif server.fail_execute_times > 0:
                server.fail_execute_times -= 1
                exc = server.fail_execute_with
                if server.fail_execute_times == 0:
                    server.fail_execute_with = None
                    server.fail_execute_match = None
                raise exc
        columns, rows, rowcount, mutation = server.plan(
            s, tuple(params or ()),
        )
        if mutation is not None:
            self.conn.staged.append((_tag_of(s), mutation))
        self.description = [(c,) for c in columns] if columns else None
        self.rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple]:
        return list(self.rows)

    def close(self) -> None:
        pass


# ======================================================================
# Fixture-style builders
# ======================================================================

def _make_store(server: _FakeServer, **overrides) -> PgInverseShortStore:
    kwargs: dict = dict(
        connect_kwargs={"host": "fake", "port": 5432},
        connection_factory=server.connect,
        reconnect_backoff=0.0,
        reconnect_backoff_max=0.0,
        max_reconnect_attempts=3,
    )
    kwargs.update(overrides)
    return PgInverseShortStore(server.connect(), **kwargs)


def _contour(server: _FakeServer, *, status: str = "RUNNING",
             **store_overrides):
    server.activation = (
        EXPERIMENT_ID, status, protocol_fingerprint(), FREEZE_TS,
    )
    store = _make_store(server, **store_overrides)
    clock = _Clock(SIGNAL_AT)
    contour = InverseShortContour(
        _settings(), store, clock=clock, activation_enabled=True,
    )
    assert store.reconnect_count == 0   # healthy at construction
    return store, contour, clock


def _open(contour, store, clock, *, symbol: str = "BTCUSDT"):
    """Open one SHORT position through the REAL capture + entry path."""
    assert _capture(store, _candidate(symbol, 100.0, detected_at=clock()),
                    clock=clock) == CAPTURED
    cycle = contour.run_cycle({symbol: 100.0})
    assert cycle["entries"] == 1, cycle
    position = next(iter(contour.engine.open_positions.values()))
    return position


# ======================================================================
# 1. Connection-level vs. SQL-level error classification
# ======================================================================

class TestConnectionErrorClassification:
    def test_transport_errors_are_connection_errors(self):
        assert is_connection_error(
            pg_exceptions.InterfaceError("network error"),
        ) is True
        assert is_connection_error(ConnectionError("reset")) is True
        assert is_connection_error(OSError("broken pipe")) is True
        assert is_connection_error(EOFError()) is True

    def test_sql_and_constraint_errors_are_never_connection_errors(self):
        for cls in (
            pg_dbapi.DatabaseError, pg_dbapi.IntegrityError,
            pg_dbapi.ProgrammingError, pg_dbapi.DataError,
            pg_dbapi.InternalError, pg_dbapi.NotSupportedError,
            pg_dbapi.OperationalError,
        ):
            assert is_connection_error(
                cls("violates constraint"),
            ) is False, cls
        assert is_connection_error(ValueError("bad params")) is False

    def test_sql_error_propagates_without_any_reconnect_or_retry(self):
        server = _FakeServer()
        store = _make_store(server)
        server.fail_execute_with = pg_dbapi.IntegrityError(
            "duplicate key value violates unique constraint",
        )
        with pytest.raises(pg_dbapi.IntegrityError):
            store.load_activation()
        # No reconnect, no statement retry: the statement itself is at fault.
        assert store.reconnect_count == 0
        assert server.connect_attempts == 1


# ======================================================================
# 2-4. Reconnection: drop, restart, retry/backoff, exactly-once writes
# ======================================================================

class TestReconnection:
    def test_connection_drop_reconnects_and_retries_the_write_once(self):
        server = _FakeServer()
        store, contour, clock = _contour(server)
        position = _open(contour, store, clock)
        key = position.position_key

        # Network interruption: THIS session's socket dies (DB is fine).
        store._conn.broken = True
        before_commits = server.commit_count

        store.update_position(
            dataclasses.replace(position, funding_paid=1.5,
                                funding_periods_charged=1),
        )

        # A NEW transport was created and the idempotent statement was
        # retried on it — applied and committed EXACTLY once.
        assert store.reconnect_count == 1
        assert len(server.connections) == 2
        assert store._conn is server.connections[1]
        assert server.positions[key]["funding_paid"] == 1.5
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1
        assert server.commit_count == before_commits + 1

    def test_database_restart_reconnect_is_bounded_then_recovers(
        self, monkeypatch,
    ):
        server = _FakeServer()
        server.activation = (
            EXPERIMENT_ID, "RUNNING", protocol_fingerprint(), FREEZE_TS,
        )
        store = _make_store(
            server, max_reconnect_attempts=3,
            reconnect_backoff=0.25, reconnect_backoff_max=2.0,
        )
        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", sleeps.append)

        # Database restart window: sessions die, connects refused.
        server.restart_window()
        with pytest.raises(pg_exceptions.InterfaceError):
            store.load_activation()

        # LIMITED retries with exponential backoff — never an infinite loop.
        assert store.reconnect_count == 0
        assert server.connect_attempts == 1 + 3   # initial + 3 attempts
        assert sleeps == [0.25, 0.5, 1.0]

        # PostgreSQL comes back: the SAME store object reconnects.
        server.recover()
        activation = store.load_activation()
        assert activation is not None
        assert activation.status == "RUNNING"
        assert store.reconnect_count == 1
        assert server.connect_attempts == 1 + 3 + 1

    def test_commit_lost_to_a_drop_re_executes_the_whole_unit(self):
        server = _FakeServer()
        store, contour, clock = _contour(server)
        position = _open(contour, store, clock)
        key = position.position_key
        before_commits = server.commit_count

        # The socket dies DURING the commit: the transaction is lost with
        # the session — the store must NOT report success for it.
        server.fail_commit_with = pg_exceptions.InterfaceError(
            "network error",
        )
        store.update_position(
            dataclasses.replace(position, funding_paid=2.0,
                                funding_periods_charged=1),
        )

        # The unit (execute + commit) was retried on a NEW connection and
        # reached the committed state exactly once — no false durability,
        # no double application.
        assert store.reconnect_count == 1
        assert server.positions[key]["funding_paid"] == 2.0
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1
        assert server.commit_count == before_commits + 1

    def test_deliberate_close_never_silently_reconnects(self):
        server = _FakeServer()
        store = _make_store(server)
        store.close()
        with pytest.raises(pg_exceptions.InterfaceError):
            store.load_activation()
        assert store.reconnect_count == 0
        assert server.connect_attempts == 1


# ======================================================================
# 5-8. Engine state survives the outage (never recreated), funding and
#      protective exits recover exactly once, entries fail closed.
# ======================================================================

class TestEngineStateRecovery:
    def test_engine_state_survives_outage_dirty_funding_commits_once(self):
        server = _FakeServer()
        store, contour, clock = _contour(server)
        position = _open(contour, store, clock)
        key = position.position_key
        engine = contour.engine
        engine_identity = id(engine)

        # Database restart: sessions die, connects refused.
        server.restart_window()
        clock.advance(hours=8)              # one funding interval due
        cycle = contour.run_cycle(
            {position.symbol: 100.0}, {position.symbol: 0.01},
        )
        # (7) the cycle ran WITHOUT raising: exits were evaluated …
        assert cycle["exits"] == 0
        # (6) new entries fail closed while the store is unreachable.
        assert cycle["entries"] == 0
        # (3) engine state preserved in-process; the accrual is DIRTY
        # (not claimed durable) with its exact snapshot.
        assert id(contour.engine) == engine_identity
        assert key in engine._dirty_funding
        assert engine.durability_risk is False
        dirty = engine._dirty_funding[key]
        assert dirty.funding_paid == position.funding_paid
        assert dirty.funding_periods_charged == position.funding_periods_charged
        assert key in engine.uncommitted_deltas
        # … and the committed state knows NOTHING about it yet (no claim).
        assert server.positions[key]["funding_paid"] == 0.0

        # PostgreSQL recovers → the SAME engine retries the frozen snapshot
        # without a new interval and without re-accruing.
        server.recover()
        cycle2 = contour.run_cycle({position.symbol: 100.0})
        assert id(contour.engine) == engine_identity
        assert key not in engine._dirty_funding
        assert engine.uncommitted_deltas == {}
        assert server.positions[key]["funding_paid"] == dirty.funding_paid
        assert server.positions[key]["funding_periods"] == (
            dirty.funding_periods_charged
        )
        # (4)+(5) exactly ONE applied write — no double counting.
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1
        assert cycle2["entries"] == 0        # no pending event
        assert engine.accounting_consistent is True

    def test_protective_exit_survives_outage_and_recovers_facts_once(
        self, caplog,
    ):
        server = _FakeServer()
        store, contour, clock = _contour(server)
        position = _open(contour, store, clock)
        key = position.position_key
        engine = contour.engine
        engine_identity = id(engine)

        server.restart_window()
        clock.advance(hours=128)            # MAX_HOLD timeout
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle = contour.run_cycle({position.symbol: 100.0})

        # (7) the protective exit executed on market data alone.
        assert cycle["exits"] == 1
        assert cycle["exit_reasons"] == ["TIMEOUT"]
        # (3)+(8) retained through the EXISTING pending-closes mechanism
        # with an explicit durability risk — the store has NO commit, so
        # nothing is claimed durable.
        assert id(contour.engine) == engine_identity
        assert len(engine.pending_closes) == 1
        assert engine.durability_risk is True
        assert cycle["durability_risk"] is True
        assert server.positions[key]["status"] == POSITION_STATUS_OPEN
        assert key not in server.outbox
        # (6) entries fail closed during the outage.
        assert cycle["entries"] == 0
        exit_facts = engine.pending_closes[key]
        assert exit_facts.exit_reason == "TIMEOUT"

        # PostgreSQL recovers → byte-for-byte recovery of the SAME facts.
        server.recover()
        with caplog.at_level(logging.WARNING, logger=ENGINE_LOGGER):
            cycle2 = contour.run_cycle({position.symbol: 100.0})
        assert id(contour.engine) == engine_identity
        assert len(engine.pending_closes) == 0
        assert engine.durability_risk is False
        assert cycle2["durability_risk"] is False
        stored = server.positions[key]
        assert stored["status"] == POSITION_STATUS_CLOSED
        assert stored["exit_reason"] == "TIMEOUT"
        assert stored["closed_at"] == exit_facts.closed_at
        assert stored["net_pnl"] == exit_facts.net_pnl
        assert stored["exit_price"] == exit_facts.exit_price
        assert key not in server.outbox          # outbox replayed + cleared
        # Exactly-once terminal application (no double counting).
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1
        assert server.apply_log.count(("STAGE_CLOSE", key)) == 1
        assert server.apply_log.count(("CLEAR_CLOSE", key)) == 1
        closed_rows = [
            p for p in server.positions.values()
            if p["status"] == POSITION_STATUS_CLOSED
        ]
        assert len(closed_rows) == 1
        assert engine.closed_positions()[0].position_key == key

    def test_entries_require_connection_and_accounting_state_restored(
        self, caplog,
    ):
        server = _FakeServer()
        store, contour, clock = _contour(server)
        assert _capture(
            store, _candidate("ETHUSDT", 50.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURED
        engine = contour.engine

        server.restart_window()

        # Capture fails OPEN-but-honest: never a silent success.
        assert _capture(
            store, _candidate("SOLUSDT", 20.0, detected_at=clock()),
            clock=clock,
        ) == CAPTURE_ERROR

        # (6) connection down → the accounting state cannot be confirmed:
        # a reconciliation fails and entries are forbidden even though the
        # activation identity is still RUNNING in-process.
        assert engine.reconcile_balance() is False
        assert engine.accounting_consistent is False
        assert engine.open_entries(
            {"BTCUSDT": 100.0, "ETHUSDT": 50.0},
        ) == []
        cycle = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 50.0})
        assert cycle["entries"] == 0
        assert cycle["pending_events_unavailable"] is True

        # Connection AND accounting state restored → the entry resumes.
        server.recover()
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 50.0})
        assert cycle2["entries"] == 1
        assert engine.accounting_consistent is True
        assert engine.open_positions[next(iter(engine.open_positions))].symbol == "ETHUSDT"

    def test_exit_only_mode_survives_connection_loss_and_recovers(
        self, caplog,
    ):
        server = _FakeServer()
        store, contour, clock = _contour(server)
        position = _open(contour, store, clock)
        key = position.position_key
        engine = contour.engine
        engine_identity = id(engine)

        # Switch the identity to DRAINING (fingerprint + freeze preserved).
        server.set_status("DRAINING")
        cycle0 = contour.run_cycle({position.symbol: 100.0})
        assert cycle0["exit_only"] is True
        assert cycle0["entries"] == 0

        # Connection loss while EXIT_ONLY.
        server.restart_window()
        clock.advance(hours=128)
        cycle1 = contour.run_cycle({position.symbol: 100.0})
        # (7)+(9) protective exit still executed; entries stayed refused.
        assert cycle1["exits"] == 1
        assert cycle1["exit_reasons"] == ["TIMEOUT"]
        assert cycle1["entries"] == 0
        assert id(contour.engine) == engine_identity
        assert len(engine.pending_closes) == 1
        assert engine.durability_risk is True

        # Recovery: DRAINING identity restored, close committed exactly once.
        server.recover()
        event_selects_before = server.event_selects
        cycle2 = contour.run_cycle({position.symbol: 100.0})
        assert id(contour.engine) == engine_identity
        assert contour.exit_only is True
        assert cycle2["exit_only"] is True
        assert cycle2["entries"] == 0
        # (9) EXIT_ONLY never requests prospective entry events at all.
        assert cycle2["pending_events"] == 0
        assert server.event_selects == event_selects_before
        # Exit facts recovered byte-for-byte, applied exactly once.
        assert len(engine.pending_closes) == 0
        assert engine.durability_risk is False
        stored = server.positions[key]
        assert stored["status"] == POSITION_STATUS_CLOSED
        assert stored["exit_reason"] == "TIMEOUT"
        assert stored["closed_at"] == engine.closed_positions()[0].closed_at
        assert server.apply_log.count(("UPDATE_POSITION", key)) == 1
        assert server.apply_log.count(("STAGE_CLOSE", key)) == 1
        assert server.apply_log.count(("CLEAR_CLOSE", key)) == 1


# ======================================================================
# 10. Frozen protocol fingerprint (untouched by this round)
# ======================================================================

def test_frozen_protocol_fingerprint_unchanged():
    assert protocol_fingerprint() == FROZEN_FINGERPRINT
