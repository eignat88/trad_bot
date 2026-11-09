"""Close-durability review fixes for ME_LONG_V2_INVERSE_SHORT_V1 (PR #267).

Codex P1 findings addressed by this suite (no PostgreSQL required — the PG
store runs against a driver-faithful fake connection whose cursor RAISES on
any access after close(), exactly like a real driver):

P1-1  closed-cursor lifecycle
    * reproduces the original read-after-close failure mode;
    * proves every PgInverseShortStore read/write materializes
      description/rows/rowcount BEFORE cursor.close();
    * proves cursors are never leaked and failures roll back.

P1-2  INSERT_POSITION_SQL syntax
    * the VALUES (...) group must contain exactly one placeholder per
      column, validated by an INSERT-grammar parser (real PostgreSQL is
      unavailable locally; a PREPARE-based integration check runs when
      TEST_DB_HOST / TEST_DB_UNIX_SOCK is configured).

P1-3  durable terminal close persistence
    * six mandatory fault-injection scenarios: retried close UPDATE,
      unchanged PnL on retry, restart recovery of exit facts, duplicate
      signal while the close is pending, no re-accrued funding, and zero
      impact on the production Paper engine;
    * plus: pending closes are never re-evaluated by STOP/TP/TIMEOUT, a
      lingering outbox row replays idempotently, outbox table absence
      (migration 086 not applied) degrades gracefully, and migration 086
      is the next free number.

Known baseline failure (reproduced on origin/main, NOT touched here):
tests/test_paper_runner.py::test_paper_disable_uses_exact_canonical_identifier
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.paper.engine import PaperTradingEngine
from app.paper.me_long_v2_inverse_short_v1 import (
    STRATEGY_ID,
    ActivationRecord,
    InMemoryInverseShortStore,
    InverseShortContour,
    capture_with_status,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_v1.capture import (
    CAPTURE_DUPLICATE,
    CAPTURED,
)
from app.paper.me_long_v2_inverse_short_v1.models import (
    InverseShortEvent,
    InverseShortPosition,
    POSITION_STATUS_CLOSED,
    POSITION_STATUS_OPEN,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID,
    MAX_HOLD_MINUTES,
    ROUTE_PRE_FILTER_SOURCE,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
)
from app.paper.me_long_v2_inverse_short_v1.store import (
    DELETE_CLOSE_OUTBOX_SQL,
    EVENT_TABLE,
    INSERT_CLOSE_OUTBOX_SQL,
    INSERT_EVENT_SQL,
    INSERT_POSITION_SQL,
    LIST_CLOSE_OUTBOX_SQL,
    MARK_EVENT_ENTERED_SQL,
    POSITION_TABLE,
    SAVE_CAPTURED,
    SAVE_DUPLICATE,
    SAVE_NOT_ACTIVE,
    UPDATE_POSITION_SQL,
    PgInverseShortStore,
    _EVENT_COLUMNS,
    _POSITION_COLUMNS,
)
from app.scanners.models import SetupCandidate, SetupState

REPO_ROOT = Path(__file__).resolve().parents[1]

PINNED_PROTOCOL_FINGERPRINT = (
    "4f97850478d65360a8a9500633d98cde5d8cb476c7a755fe05731c3748ab163c"
)
FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)


# ======================================================================
# Shared helpers (mirror of the helpers in test_me_long_v2_inverse_short_v1)
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


def _activation() -> ActivationRecord:
    return ActivationRecord(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        protocol_fingerprint=protocol_fingerprint(),
        freeze_ts=FREEZE_TS,
    )


def _candidate(
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    *,
    detected_at: datetime = SIGNAL_AT,
    candle_ms: int | None = None,
) -> SetupCandidate:
    if candle_ms is None:
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


def _me_short_candidate(symbol: str = "BTCUSDT") -> SetupCandidate:
    return SetupCandidate(
        scanner_name="MOMENTUM_EXHAUSTION",
        symbol=symbol,
        direction="SHORT",
        score=38.0,
        entry_zone_low=99.0,
        entry_zone_high=101.0,
        invalidation_price=110.0,
        target_1=85.0,
    )


def _capture(store, candidate, *, clock=None) -> str:
    return capture_with_status(
        store, candidate, capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"}, clock=clock,
    )


class _FlakyCloseStore(InMemoryInverseShortStore):
    """In-memory store with INJECTED faults on the terminal-close path.

    ``fail_close_updates`` — the next N ``update_position`` calls with a
    CLOSED position raise (``_apply_funding`` updates keep their own
    path: an OPEN position is never failed, so funding persistence tests
    stay deterministic).
    ``fail_stage`` — the next N ``stage_close`` calls raise.
    """

    def __init__(
        self,
        activation: ActivationRecord | None = None,
        *,
        initial_balance: float = 10_000.0,
        fail_close_updates: int = 0,
        fail_stage: int = 0,
    ) -> None:
        super().__init__(activation, initial_balance=initial_balance)
        self.fail_close_updates = int(fail_close_updates)
        self.fail_stage = int(fail_stage)
        self.close_update_attempts = 0
        self.stage_attempts = 0

    def stage_close(self, position: InverseShortPosition) -> bool:
        self.stage_attempts += 1
        if self.fail_stage > 0:
            self.fail_stage -= 1
            raise RuntimeError("INJECTED_STAGE_FAILURE")
        return super().stage_close(position)

    def update_position(self, position: InverseShortPosition) -> None:
        if position.status == POSITION_STATUS_CLOSED:
            self.close_update_attempts += 1
            if self.fail_close_updates > 0:
                self.fail_close_updates -= 1
                raise RuntimeError("INJECTED_CLOSE_UPDATE_FAILURE")
        return super().update_position(position)


def _flaky(fail_close_updates: int = 0, fail_stage: int = 0) -> _FlakyCloseStore:
    return _FlakyCloseStore(
        _activation(), initial_balance=_settings().initial_balance,
        fail_close_updates=fail_close_updates, fail_stage=fail_stage,
    )


def _open_position(
    store: InMemoryInverseShortStore,
    *,
    clock: _Clock | None = None,
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    settings: Settings | None = None,
):
    """Capture one source event and open its SHORT position at `price`."""
    settings = settings or _settings()
    clock = clock or _Clock(SIGNAL_AT)
    contour = InverseShortContour(
        settings, store, clock=clock, activation_enabled=True,
    )
    assert _capture(store, _candidate(symbol, price, detected_at=clock()),
                    clock=clock) == CAPTURED
    cycle = contour.run_cycle({symbol: price})
    assert cycle["entries"] == 1, cycle
    position = next(iter(contour.engine.open_positions.values()))
    return contour, clock, position


# ======================================================================
# P1-1 — closed-cursor regression tests (driver-faithful fake)
# ======================================================================

class _MissingRelationError(Exception):
    """Stand-in for PostgreSQL 42P01 (undefined table)."""

    def __init__(self, message: str, sqlstate: str = "42P01") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakeCursor:
    """DBAPI cursor that raises on EVERY attribute access after close().

    This reproduces the original P1-1 defect: the pre-fix code returned the
    cursor from ``_execute`` AFTER ``cursor.close()`` in ``finally``, so
    ``fetchone``/``fetchall``/``description`` hit a closed cursor.
    """

    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._closed = False
        self._rowcount = 0
        self._columns: list[str] = []
        self._rows: list[tuple] = []
        self.closed = False
        self.accesses_after_close = 0
        self.executed: list[tuple[str, tuple]] = []

    def _guard(self, attr: str) -> None:
        if self._closed:
            self.accesses_after_close += 1
            raise RuntimeError(f"cursor is closed: {attr}")

    @property
    def description(self):
        self._guard("description")
        if not self._columns:
            return None
        return [(name,) for name in self._columns]

    @property
    def rowcount(self):
        self._guard("rowcount")
        return self._rowcount

    def execute(self, sql: str, params=None) -> None:
        self._guard("execute")
        params = tuple(params or ())
        self.executed.append((sql, params))
        self._conn.statements.append((sql, params))
        failure = self._conn.fail_on(sql)
        if failure is not None:
            raise failure
        self._rowcount, self._columns, self._rows = self._conn._route(sql, params)

    def fetchone(self):
        self._guard("fetchone")
        return self._rows[0] if self._rows else None

    def fetchall(self):
        self._guard("fetchall")
        return list(self._rows)

    def close(self) -> None:
        self._closed = True
        self.closed = True


class _FakeConn:
    def __init__(self, route=None) -> None:
        self._route = route or (lambda sql, params: (1, [], []))
        self.cursors: list[_FakeCursor] = []
        self.statements: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0
        self.close_calls = 0
        self.fail_on = lambda sql: None

    def cursor(self) -> _FakeCursor:
        cursor = _FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.close_calls += 1


def _pg_route(
    *,
    activation_row=None,
    event_rows=(),
    position_rows=(),
    outbox_rows=(),
    outbox_error: Exception | None = None,
    insert_rowcount: int = 1,
    update_rowcount: int = 1,
    delete_rowcount: int = 1,
    balance_row=None,
    summary_row=None,
    has_position: bool = True,
):
    def route(sql: str, params: tuple):
        flat = " ".join(sql.split())
        if "close_outbox" in flat and outbox_error is not None:
            raise outbox_error
        if flat.startswith(f"INSERT INTO {EVENT_TABLE}"):
            return (insert_rowcount, [], [])
        if flat.startswith(f"INSERT INTO {POSITION_TABLE}"):
            return (insert_rowcount, [], [])
        if flat.startswith("INSERT INTO dds.me_long_v2_inverse_short_v1_close_outbox"):
            return (insert_rowcount, [], [])
        if flat.startswith(f"UPDATE {EVENT_TABLE}"):
            return (update_rowcount, [], [])
        if flat.startswith(f"UPDATE {POSITION_TABLE} SET"):
            return (update_rowcount, [], [])
        if flat.startswith("DELETE FROM dds.me_long_v2_inverse_short_v1_close_outbox"):
            return (delete_rowcount, [], [])
        if "FROM dds.me_long_v2_inverse_short_v1_activation" in flat:
            rows = [activation_row] if activation_row else []
            return (
                len(rows),
                ["experiment_id", "status", "protocol_fingerprint", "freeze_ts"],
                rows,
            )
        if "SELECT position_key, snapshot" in flat:
            rows = [
                (row.get("position_key"), row.get("snapshot"))
                for row in outbox_rows
            ]
            return (len(rows), ["position_key", "snapshot"], rows)
        if f"SELECT 1 FROM {POSITION_TABLE}" in flat:
            key = params[0] if params else None
            found = any(
                row.get("position_key") == key for row in position_rows
            )
            return (1 if found else 0, ["probe"], [(1,)] if found else [])
        if f"SELECT * FROM {POSITION_TABLE}" in flat:
            columns = list(_POSITION_COLUMNS)
            rows = [[row.get(c) for c in columns] for row in position_rows]
            return (len(rows), columns, rows)
        if "WHEN status = 'CLOSED' THEN net_pnl" in flat:
            row = balance_row if balance_row is not None else (0.0, 0.0)
            return (1, ["closed_net", "open_costs"], [row])
        if "COUNT(*)" in flat and f"FROM {POSITION_TABLE}" in flat:
            row = summary_row if summary_row is not None else (0, 0, 0, 0.0)
            return (1, ["closed", "wins", "losses", "net"], [row])
        if f"SELECT * FROM {EVENT_TABLE}" in flat:
            columns = list(_EVENT_COLUMNS)
            rows_source = event_rows
            if "WHERE event_key = %s" in flat:
                rows_source = [
                    row for row in event_rows
                    if row.get("event_key") == (params[0] if params else None)
                ]
            rows = [[row.get(c) for c in columns] for row in rows_source]
            return (len(rows), columns, rows)
        raise AssertionError(f"unrouted SQL in fake store: {flat[:160]}")

    return route


def _pg_store(route=None, **kwargs) -> tuple[PgInverseShortStore, _FakeConn]:
    conn = _FakeConn(route)
    store = PgInverseShortStore(conn, **kwargs)
    return store, conn


def _assert_cursor_hygiene(conn: _FakeConn) -> None:
    assert conn.cursors, "no cursors were created"
    for cursor in conn.cursors:
        assert cursor.closed is True, "cursor leaked (never closed)"
        assert cursor.accesses_after_close == 0, (
            "READ AFTER CLOSE — the closed-cursor defect (P1-1) is back"
        )


def _captured_event() -> InverseShortEvent:
    store = InMemoryInverseShortStore(_activation())
    assert _capture(store, _candidate(), clock=_Clock(SIGNAL_AT)) == CAPTURED
    return store.list_events()[0]


def _real_position() -> InverseShortPosition:
    store = InMemoryInverseShortStore(_activation())
    _contour, _clock, position = _open_position(store)
    return position


def _real_terminal_position() -> InverseShortPosition:
    """A genuinely closed position (real engine close, in-memory store)."""
    store = InMemoryInverseShortStore(_activation())
    contour, clock, _position = _open_position(store)
    clock.advance(minutes=MAX_HOLD_MINUTES)
    cycle = contour.run_cycle({"BTCUSDT": 100.0})
    assert cycle["exits"] == 1, cycle
    closed = store.list_positions(status=POSITION_STATUS_CLOSED)
    assert len(closed) == 1
    return closed[0]


class TestP11ClosedCursorRegression:
    def test_strict_cursor_reproduces_closed_cursor_error(self):
        # Faithful reproduction of the ORIGINAL defect: the pre-fix
        # ``_execute`` closed the cursor in ``finally`` and returned it, so
        # the caller's ``fetchone``/``description`` raised.
        conn = _FakeConn(lambda sql, params: (1, ["probe"], [(1,)]))
        cursor = conn.cursor()
        cursor.execute("SELECT 1", ())
        cursor.close()
        with pytest.raises(RuntimeError, match="cursor is closed"):
            cursor.fetchone()
        with pytest.raises(RuntimeError, match="cursor is closed"):
            cursor.fetchall()
        with pytest.raises(RuntimeError, match="cursor is closed"):
            _ = cursor.description

    def test_all_read_paths_materialize_before_close(self):
        event = _captured_event()
        position = _real_position()
        terminal = _real_terminal_position()
        route = _pg_route(
            activation_row=(EXPERIMENT_ID, "RUNNING", protocol_fingerprint(), FREEZE_TS),
            event_rows=[event.to_dict()],
            position_rows=[position.to_dict()],
            outbox_rows=[{
                "position_key": terminal.position_key,
                "snapshot": json.dumps(terminal.to_dict()),
            }],
            balance_row=(-1.5, 2.5),
            summary_row=(1, 0, 1, -1.5),
        )
        store, conn = _pg_store(route)

        activation = store.load_activation()
        assert activation is not None and activation.is_running
        assert store.get_event(event.event_key) is not None
        assert store.get_event("missing") is None
        assert store.list_events()
        assert store.list_events(status="CAPTURED")
        assert store.load_open_positions()
        assert store.has_position(position.position_key) is True
        assert store.has_position("ME_LONG_V2_INVERSE_SHORT_V1:none") is False
        assert store.list_positions()
        assert store.list_positions(status=POSITION_STATUS_OPEN)
        components = store.balance_components()
        assert components["balance"] == pytest.approx(10_000.0 - 1.5 - 2.5)
        summary = store.net_pnl_summary()
        assert summary["closed_trades"] == 1.0 and summary["net_pnl"] == -1.5
        pending = store.list_pending_closes()
        assert [p.position_key for p in pending] == [terminal.position_key]

        _assert_cursor_hygiene(conn)
        # Every read committed its transaction promptly (no idle txn).
        assert conn.commits >= 12
        assert conn.rollbacks == 0

    def test_all_write_paths_materialize_before_close_and_commit(self):
        event = _captured_event()
        position = _real_position()
        terminal = _real_terminal_position()
        route = _pg_route(
            activation_row=(EXPERIMENT_ID, "RUNNING", protocol_fingerprint(), FREEZE_TS),
            insert_rowcount=1,
            update_rowcount=1,
        )
        store, conn = _pg_store(route)

        assert store.save_event(event) == SAVE_CAPTURED
        assert store.save_position(position) is True
        store.update_position(position)          # funding-style update
        store.update_position(terminal)          # close update
        store.mark_event_entered(event.event_key, position.position_key)
        assert store.stage_close(terminal) is True
        assert store.clear_pending_close(terminal.position_key) is None

        _assert_cursor_hygiene(conn)
        # load_activation (inside save_event) + save_event + save_position +
        # 2 updates + mark_event_entered + stage_close + clear_pending_close
        assert conn.commits == 8
        assert conn.rollbacks == 0

    def test_failed_read_rolls_back_and_closes_cursor(self):
        conn = _FakeConn(_pg_route(
            activation_row=(EXPERIMENT_ID, "RUNNING", protocol_fingerprint(), FREEZE_TS),
        ))
        conn.fail_on = lambda sql: (
            RuntimeError("connection reset mid-read")
            if "FROM dds.me_long_v2_inverse_short_v1_activation" in " ".join(sql.split())
            else None
        )
        store = PgInverseShortStore(conn)

        with pytest.raises(RuntimeError, match="connection reset"):
            store.load_activation()
        _assert_cursor_hygiene(conn)
        assert conn.rollbacks == 1
        assert conn.commits == 0

    def test_failed_write_rolls_back_and_closes_cursor(self):
        conn = _FakeConn(_pg_route(insert_rowcount=1))
        conn.fail_on = lambda sql: (
            RuntimeError("deadlock detected")
            if sql.strip().startswith(f"INSERT INTO {POSITION_TABLE}") else None
        )
        store = PgInverseShortStore(conn)

        with pytest.raises(RuntimeError, match="deadlock"):
            store.save_position(_real_position())
        _assert_cursor_hygiene(conn)
        assert conn.rollbacks == 1
        assert conn.commits == 0

    def test_update_position_fails_loudly_when_no_row_matched(self):
        # A "successful" UPDATE that matched nothing must not be silently
        # treated as a persisted close (would lose the exit record).
        store, conn = _pg_store(_pg_route(update_rowcount=0))
        with pytest.raises(RuntimeError, match="POSITION_UPDATE_MATCHED_NO_ROWS"):
            store.update_position(_real_terminal_position())
        # The empty write was committed (nothing to keep open) and the
        # cursor is closed — no leak, no hanging transaction.
        _assert_cursor_hygiene(conn)
        assert conn.commits == 1

    def test_save_event_separates_guard_failure_from_idempotent_replay(self):
        event = _captured_event()
        activation = (
            EXPERIMENT_ID, "RUNNING", protocol_fingerprint(), FREEZE_TS
        )
        # INSERT suppressed by the WHERE EXISTS guard + row absent -> NOT_ACTIVE
        store, _ = _pg_store(_pg_route(activation_row=activation, insert_rowcount=0))
        assert store.save_event(event) == SAVE_NOT_ACTIVE
        # INSERT suppressed + row present -> honest DUPLICATE
        store, _ = _pg_store(_pg_route(
            activation_row=activation, insert_rowcount=0,
            event_rows=[event.to_dict()],
        ))
        assert store.save_event(event) == SAVE_DUPLICATE
        # Happy path.
        store, _ = _pg_store(_pg_route(activation_row=activation, insert_rowcount=1))
        assert store.save_event(event) == SAVE_CAPTURED


# ======================================================================
# P1-2 — SQL structure: placeholders inside VALUES (integration-style
# syntax validation; real PostgreSQL PREPARE runs when TEST_DB_* is set)
# ======================================================================

def _paren_balanced(sql: str) -> bool:
    depth = 0
    for char in sql:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _values_group(sql: str) -> str:
    match = re.search(r"\bVALUES\s*\(", sql)
    assert match is not None, f"statement has no VALUES ( ... ) group: {sql[:120]}"
    depth = 0
    for index in range(match.end() - 1, len(sql)):
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
            if depth == 0:
                return sql[match.end():index]
    raise AssertionError("unbalanced VALUES group")


# INSERT grammar:  INSERT INTO <table> (<columns>) VALUES (<values>) [ON CONFLICT ...]
_INSERT_VALUES_GRAMMAR = re.compile(
    r"^\s*INSERT\s+INTO\s+(?P<table>[A-Za-z_][\w.]*)\s*"
    r"\(\s*(?P<cols>[^()]+?)\s*\)\s*"
    r"VALUES\s*\(\s*(?P<values>[^()]+?)\s*\)\s*"
    r"(?P<tail>ON\s+CONFLICT\s*\([^()]+\)\s*DO\s+(?:NOTHING|UPDATE\b.*))?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _assert_insert_values_grammar(sql: str, *, table: str, expected: int) -> None:
    match = _INSERT_VALUES_GRAMMAR.match(sql)
    assert match is not None, (
        "statement does not parse as INSERT INTO t (cols) VALUES ( ... ) "
        f"[ON CONFLICT ...] — syntax is invalid: {sql[:200]!r}"
    )
    assert match.group("table") == table
    columns = [c.strip() for c in match.group("cols").split(",")]
    assert len(columns) == expected, "column list arity mismatch"
    assert match.group("values").count("%s") == expected, (
        "placeholders must sit INSIDE VALUES ( ... ) — one per column"
    )
    assert "%s" not in sql[:match.start("values")], (
        "placeholder outside VALUES ( ... )"
    )
    assert _paren_balanced(sql)


class TestP12InsertSqlSyntax:
    def test_insert_position_sql_placeholders_are_inside_values(self):
        _assert_insert_values_grammar(
            INSERT_POSITION_SQL,
            table=f"{POSITION_TABLE}",
            expected=len(_POSITION_COLUMNS),
        )
        # No stray placeholder anywhere else in the statement.
        values = _values_group(INSERT_POSITION_SQL)
        without_values = INSERT_POSITION_SQL.replace(f"VALUES ({values})", "")
        assert "%s" not in without_values

    def test_insert_position_sql_column_list_matches_model_order(self):
        columns = re.search(
            r"INSERT\s+INTO\s+\S+\s*\(([^)]*)\)", INSERT_POSITION_SQL,
        ).group(1)
        parsed = [c.strip() for c in columns.split(",")]
        assert parsed == list(_POSITION_COLUMNS)
        assert _values_group(INSERT_POSITION_SQL).count("%s") == len(parsed)

    def test_insert_close_outbox_sql_placeholders_are_inside_values(self):
        _assert_insert_values_grammar(
            INSERT_CLOSE_OUTBOX_SQL,
            table="dds.me_long_v2_inverse_short_v1_close_outbox",
            expected=5,
        )
        # snapshot is bound as an explicit jsonb cast.
        assert "%s::jsonb" in _values_group(INSERT_CLOSE_OUTBOX_SQL)

    def test_insert_event_sql_keeps_select_where_exists_guard(self):
        flat = " ".join(INSERT_EVENT_SQL.split())
        assert flat.startswith(f"INSERT INTO {EVENT_TABLE} (")
        assert ") SELECT " in flat, "guard projection lost"
        assert "WHERE EXISTS (" in flat, "activation guard lost"
        assert "status = 'RUNNING'" in flat
        assert "protocol_fingerprint = %s" in flat
        assert "freeze_ts IS NOT NULL" in flat
        assert "ON CONFLICT (event_key) DO NOTHING" in flat
        assert flat.count("%s") == len(_EVENT_COLUMNS) + 2
        assert _paren_balanced(INSERT_EVENT_SQL)

    def test_update_position_sql_is_paren_safe_and_immutable(self):
        assert _paren_balanced(UPDATE_POSITION_SQL)
        assert UPDATE_POSITION_SQL.count("%s") == 16  # 15 SET + the key
        set_clause = UPDATE_POSITION_SQL.split("SET", 1)[1].split("WHERE", 1)[0]
        for column in (
            "entered_at", "deadline_at", "entry_market_price", "entry_price",
            "entry_fee", "entry_slippage", "stop_price", "target_price",
            "position_size", "risk_usdt", "balance_before", "event_key",
            "symbol", "direction",
        ):
            assert column not in set_clause, (
                f"immutable/entry column {column} must never be updated"
            )
        assert "position_key = %s" in UPDATE_POSITION_SQL.split("WHERE", 1)[1]

    def test_every_contour_sql_constant_is_paren_balanced(self):
        statements = {
            "INSERT_EVENT_SQL": INSERT_EVENT_SQL,
            "INSERT_POSITION_SQL": INSERT_POSITION_SQL,
            "UPDATE_POSITION_SQL": UPDATE_POSITION_SQL,
            "MARK_EVENT_ENTERED_SQL": MARK_EVENT_ENTERED_SQL,
            "INSERT_CLOSE_OUTBOX_SQL": INSERT_CLOSE_OUTBOX_SQL,
            "LIST_CLOSE_OUTBOX_SQL": LIST_CLOSE_OUTBOX_SQL,
            "DELETE_CLOSE_OUTBOX_SQL": DELETE_CLOSE_OUTBOX_SQL,
        }
        for name, sql in statements.items():
            assert _paren_balanced(sql), f"{name} has unbalanced parentheses"

    def test_save_and_update_bind_one_parameter_per_column(self):
        position = _real_position()
        store, conn = _pg_store(_pg_route(insert_rowcount=1, update_rowcount=1))
        assert store.save_position(position) is True
        sql, params = conn.statements[-1]
        assert sql == INSERT_POSITION_SQL
        assert len(params) == len(_POSITION_COLUMNS)
        expected = tuple(position.to_dict().get(c) for c in _POSITION_COLUMNS)
        assert params == expected

        store.update_position(position)
        sql, params = conn.statements[-1]
        assert sql == UPDATE_POSITION_SQL
        assert len(params) == UPDATE_POSITION_SQL.count("%s")
        assert params[-1] == position.position_key

    # -- real PostgreSQL check (runs only where TEST_DB_* is configured) --
    @staticmethod
    def _pg_test_configured() -> bool:
        return bool(os.environ.get("TEST_DB_HOST") or os.environ.get("TEST_DB_UNIX_SOCK"))

    @pytest.mark.skipif(
        not _pg_test_configured(),
        reason="no test PostgreSQL configured (set TEST_DB_HOST / TEST_DB_UNIX_SOCK)",
    )
    def test_contour_statements_prepare_on_real_postgres(self):
        """PREPARE parses each statement with PostgreSQL's own grammar.

        Missing contour tables (42P01/42704/42703) still prove the syntax
        is valid — only genuine syntax errors (42601/42602/42712) fail.
        """
        import pg8000

        def to_dollar(sql: str) -> str:
            counter = {"n": 0}

            def sub(_match):
                counter["n"] += 1
                return f"${counter['n']}"

            return re.sub(r"%s", sub, sql)

        conn = pg8000.dbapi.connect(
            host=os.environ.get("TEST_DB_HOST", "localhost"),
            port=int(os.environ.get("TEST_DB_PORT", "5432")),
            user=os.environ.get("TEST_DB_USER", "postgres"),
            password=os.environ.get("TEST_DB_PASSWORD", ""),
            database=os.environ.get("TEST_DB_NAME", "trad_bot_migration_test"),
        )
        cursor = conn.cursor()
        benign = {None, "42P01", "42704", "42703"}
        statements = [
            INSERT_POSITION_SQL, UPDATE_POSITION_SQL, INSERT_EVENT_SQL,
            MARK_EVENT_ENTERED_SQL, INSERT_CLOSE_OUTBOX_SQL,
            LIST_CLOSE_OUTBOX_SQL, DELETE_CLOSE_OUTBOX_SQL,
        ]
        try:
            for index, sql in enumerate(statements):
                cursor.execute(f"PREPARE me_inverse_short_syntax_{index} AS {to_dollar(sql)}")
                cursor.execute(f"DEALLOCATE me_inverse_short_syntax_{index}")
        except Exception as exc:  # pragma: no cover - env-dependent
            import pg8000.exceptions

            if not isinstance(exc, pg8000.exceptions.DatabaseError):
                raise
            state = getattr(exc, "sqlstate", None)
            assert state in benign, (
                f"PostgreSQL rejected statement #{index} with sqlstate={state}: {exc}"
            )
        finally:
            try:
                cursor.close()
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass


# ======================================================================
# Migration 086 — number safety + shape
# ======================================================================

class TestMigration086CloseOutbox:
    @staticmethod
    def _migration_numbers() -> list[int]:
        numbers = []
        for path in sorted((REPO_ROOT / "sql" / "migrations").glob("*.sql")):
            match = re.match(r"(\d+)_", path.name)
            if match:
                numbers.append(int(match.group(1)))
        return numbers

    def test_migration_086_is_next_free_and_unique(self):
        numbers = self._migration_numbers()
        assert numbers.count(86) == 1, "migration number 086 must be unique"
        assert numbers.index(86) == numbers.index(85) + 1, (
            "086 must directly follow 085 on this branch"
        )
        # origin/main highest migration is 083 (verified via git ls-tree) —
        # 084/085/086 all belong to this PR and cannot collide with main.
        assert numbers.count(83) == 1

    def test_migration_086_shape_is_bootstrap_safe(self):
        path = REPO_ROOT / "sql" / "migrations" / "086_me_long_v2_inverse_short_v1_close_outbox.sql"
        text = path.read_text(encoding="utf-8")
        # Statements only — comments legitimately mention other tables.
        body = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("--")
        )
        assert text.count("$$") == 0, "no function bodies (splitter-safe)"
        assert "BEGIN;" in text and "COMMIT;" in text
        assert (
            "CREATE TABLE IF NOT EXISTS "
            "dds.me_long_v2_inverse_short_v1_close_outbox" in " ".join(body.split())
        )
        # Additive only: never touches existing tables or the frozen
        # protocol (the fingerprint is activated through the activation row,
        # never hardcoded into migration SQL).
        for forbidden in (
            "ALTER TABLE dds.me_long_v2_inverse_short_v1_position",
            "DROP TABLE",
            "dds.paper_trade",
            PINNED_PROTOCOL_FINGERPRINT,
        ):
            assert forbidden not in body
            # The fingerprint is frozen protocol — never in migration SQL,
            # comments included.
            if forbidden == PINNED_PROTOCOL_FINGERPRINT:
                assert forbidden not in text

    def test_schema_bootstrap_mirrors_the_outbox_table(self):
        schema = (REPO_ROOT / "app" / "db" / "schema.sql").read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS dds.me_long_v2_inverse_short_v1_close_outbox" in schema
        assert schema.count("$$") == 0
        assert "086_me_long_v2_inverse_short_v1_close_outbox.sql" in schema


# ======================================================================
# PG outbox semantics (fake connection)
# ======================================================================

class TestPgCloseOutbox:
    def test_stage_close_persists_exact_terminal_snapshot(self):
        terminal = _real_terminal_position()
        store, conn = _pg_store(_pg_route(insert_rowcount=1))

        assert store.stage_close(terminal) is True
        sql, params = conn.statements[-1]
        assert "close_outbox" in sql
        assert len(params) == 5
        position_key, symbol, exit_reason, closed_at, snapshot = params
        assert position_key == terminal.position_key
        assert symbol == terminal.symbol
        assert exit_reason == terminal.exit_reason
        assert closed_at == terminal.closed_at
        parsed = json.loads(snapshot)
        assert parsed["position_key"] == terminal.position_key
        assert parsed["exit_reason"] == terminal.exit_reason
        assert parsed["net_pnl"] == pytest.approx(terminal.net_pnl)
        assert parsed["entered_at"] == terminal.entered_at.isoformat()
        assert parsed["deadline_at"] == terminal.deadline_at.isoformat()
        _assert_cursor_hygiene(conn)
        assert conn.commits == 1

    def test_stage_close_refuses_non_terminal_position(self):
        store, conn = _pg_store(_pg_route(insert_rowcount=1))
        with pytest.raises(ValueError, match="STAGE_CLOSE_NOT_TERMINAL"):
            store.stage_close(_real_position())  # status is OPEN
        assert conn.commits == 0

    def test_list_pending_closes_parses_json_string_and_object(self):
        terminal = _real_terminal_position()
        snapshot = terminal.to_dict()
        route = _pg_route(outbox_rows=[
            {"position_key": terminal.position_key, "snapshot": json.dumps(snapshot)},
            {"position_key": terminal.position_key, "snapshot": dict(snapshot)},
        ])
        store, _conn = _pg_store(route)
        pending = store.list_pending_closes()
        assert len(pending) == 2
        for position in pending:
            assert position.position_key == terminal.position_key
            assert position.exit_reason == terminal.exit_reason
            assert position.closed_at == terminal.closed_at
            assert position.net_pnl == pytest.approx(terminal.net_pnl)

    def test_list_pending_closes_skips_unreadable_row(self):
        route = _pg_route(outbox_rows=[
            {"position_key": "ME_LONG_V2_INVERSE_SHORT_V1:x", "snapshot": None},
        ])
        store, _conn = _pg_store(route)
        assert store.list_pending_closes() == []

    def test_clear_pending_close_deletes_by_key(self):
        terminal = _real_terminal_position()
        store, conn = _pg_store(_pg_route(delete_rowcount=1))
        store.clear_pending_close(terminal.position_key)
        sql, params = conn.statements[-1]
        assert sql == DELETE_CLOSE_OUTBOX_SQL
        assert params == (terminal.position_key,)
        assert conn.commits == 1

    def test_missing_outbox_table_degrades_gracefully(self):
        # Migration-safe rollout: no 086 on the database yet (42P01) must
        # NOT break the close path — durability degrades, nothing raises.
        error = _MissingRelationError(
            'relation "dds.me_long_v2_inverse_short_v1_close_outbox" does not exist'
        )
        route = _pg_route(outbox_error=error, insert_rowcount=1)
        store, conn = _pg_store(route)
        terminal = _real_terminal_position()

        assert store.stage_close(terminal) is False      # degraded, no raise
        assert store.list_pending_closes() == []         # resume finds nothing
        store.clear_pending_close(terminal.position_key)  # no-op, no raise
        _assert_cursor_hygiene(conn)


# ======================================================================
# P1-3 — the six mandatory fault-injection scenarios
# ======================================================================

class TestP13FaultInjection:
    # -- (1) first close UPDATE fails, retry succeeds --------------------
    def test_first_close_update_failure_is_retried_until_persisted(self):
        store = _flaky(fail_close_updates=1)
        settings = _settings()
        contour, clock, _position = _open_position(store, settings=settings)

        clock.advance(minutes=MAX_HOLD_MINUTES)
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["exits"] == 1
        assert cycle["exit_reasons"] == ["TIMEOUT"]
        assert cycle["pending_closes"] == 1
        assert store.close_update_attempts == 1
        # Terminal snapshot is staged durably, the row is still OPEN.
        pending = store.list_pending_closes()
        assert len(pending) == 1 and pending[0].exit_reason == "TIMEOUT"
        assert any(p.status == POSITION_STATUS_OPEN for p in store.list_positions())
        assert contour.engine.pending_closes

        # The very next cycle retries with the same snapshot and succeeds.
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle2["pending_closes"] == 0
        assert store.close_update_attempts == 2
        assert store.list_pending_closes() == []
        closed = store.list_positions(status=POSITION_STATUS_CLOSED)
        assert len(closed) == 1 and closed[0].exit_reason == "TIMEOUT"
        assert contour.engine.open_positions == {}

    # -- (2) retry does not change PnL -----------------------------------
    def test_retry_replays_identical_pnl(self):
        store = _flaky(fail_close_updates=1)
        settings = _settings()
        contour, clock, _position = _open_position(store, settings=settings)

        clock.advance(minutes=MAX_HOLD_MINUTES)
        assert contour.run_cycle({"BTCUSDT": 100.0})["pending_closes"] == 1
        facts = store.list_pending_closes()[0]
        balance_at_failure = contour.engine.balance

        contour.run_cycle({"BTCUSDT": 100.0})  # retry succeeds

        closed = store.list_positions(status=POSITION_STATUS_CLOSED)
        assert len(closed) == 1
        replayed = closed[0]
        assert replayed.net_pnl == facts.net_pnl          # exact, not approx
        assert replayed.gross_pnl == facts.gross_pnl
        assert replayed.exit_price == facts.exit_price
        assert replayed.exit_reason == facts.exit_reason
        assert replayed.closed_at == facts.closed_at
        assert replayed.funding_paid == facts.funding_paid
        assert replayed.duration_sec == facts.duration_sec
        # Flush never re-books the realized PnL into memory...
        assert contour.engine.balance == balance_at_failure
        # ...and the ledger books it exactly once.
        summary = store.net_pnl_summary()
        assert summary["closed_trades"] == 1.0
        assert summary["net_pnl"] == pytest.approx(facts.net_pnl)
        assert contour.engine.balance == pytest.approx(
            store.balance_components()["balance"]
        )

    # -- (3) restart after failure preserves original exit facts ---------
    def test_restart_after_failure_preserves_original_exit_facts(self):
        store = _flaky(fail_close_updates=1)
        settings = _settings()
        contour, clock, position = _open_position(store, settings=settings)

        clock.advance(minutes=MAX_HOLD_MINUTES)
        assert contour.run_cycle({"BTCUSDT": 100.0})["pending_closes"] == 1
        facts = store.list_pending_closes()[0]
        entered_at, deadline_at = position.entered_at, position.deadline_at

        # ---- RESTART: brand-new contour over the SAME store, before any
        # successful retry, at a DIFFERENT market price.
        store.fail_close_updates = 0
        contour2 = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        engine2 = contour2.engine
        # Recovery resumed the pending close and kept it out of the open
        # algorithm (the row is still OPEN in the store).
        assert facts.position_key in engine2.pending_closes
        assert engine2.open_positions == {}
        assert engine2.balance == pytest.approx(
            store.balance_components()["balance"]
        )
        assert store.immutability_repairs == 0

        clock.advance(hours=1)
        cycle = contour2.run_cycle({"BTCUSDT": 137.0})
        assert cycle["exits"] == 0                      # not recomputed
        assert cycle["pending_closes"] == 0             # flush persisted it

        closed = store.list_positions(status=POSITION_STATUS_CLOSED)
        assert len(closed) == 1
        final = closed[0]
        # Original exit facts verbatim — never re-derived at 137.0.
        assert final.exit_reason == facts.exit_reason
        assert final.closed_at == facts.closed_at
        assert final.exit_price == facts.exit_price
        assert final.net_pnl == facts.net_pnl
        assert final.gross_pnl == facts.gross_pnl
        assert final.funding_paid == facts.funding_paid
        recomputed_fill = 137.0 * (1 + float(settings.slippage_percent))
        assert final.exit_price != pytest.approx(recomputed_fill)
        # Immutable entry facts untouched across the failed close + restart.
        assert final.entered_at == entered_at == facts.entered_at
        assert final.deadline_at == deadline_at == facts.deadline_at
        assert store.immutability_repairs == 0
        # Realized PnL restored into memory exactly once after the flush.
        assert engine2.balance == pytest.approx(
            settings.initial_balance + final.net_pnl
        )
        assert engine2.pending_closes == {}

    def test_pending_close_is_never_returned_to_stop_tp_timeout(self):
        store = _flaky(fail_close_updates=10_000)
        contour, clock, _position = _open_position(store)

        clock.advance(minutes=MAX_HOLD_MINUTES)
        assert contour.run_cycle({"BTCUSDT": 100.0})["pending_closes"] == 1
        facts = store.list_pending_closes()[0]
        stages_before = store.stage_attempts
        attempts_before = store.close_update_attempts

        # A price that would immediately trigger STOP_LOSS if the position
        # were still in the exit algorithm.
        clock.advance(hours=1)
        cycle = contour.run_cycle(
            {"BTCUSDT": 111.0},
            bars={"BTCUSDT": {"high": 112.0, "low": 105.0}},
        )
        assert cycle["exits"] == 0                     # no second close
        assert cycle["pending_closes"] == 1
        assert store.stage_attempts == stages_before   # _close never re-ran
        assert store.close_update_attempts == attempts_before + 1  # flush only
        pending = store.list_pending_closes()[0]
        assert pending.exit_reason == facts.exit_reason
        assert pending.exit_price == facts.exit_price
        assert pending.closed_at == facts.closed_at
        assert pending.net_pnl == facts.net_pnl
        assert any(p.status == POSITION_STATUS_OPEN for p in store.list_positions())
        assert contour.engine.open_positions == {}

    # -- (4) duplicate signal does not create a duplicate position -------
    def test_duplicate_signal_cannot_create_position_while_close_pending(self):
        store = _flaky(fail_close_updates=10_000)
        contour, clock, _position = _open_position(store)
        clock.advance(minutes=MAX_HOLD_MINUTES)
        assert contour.run_cycle({"BTCUSDT": 100.0})["pending_closes"] == 1

        # (a) redelivery of the SAME source event — idempotent.
        assert _capture(store, _candidate(), clock=clock) == CAPTURE_DUPLICATE
        assert contour.run_cycle({"BTCUSDT": 100.0})["entries"] == 0

        # (b) a brand-new source event on the SAME symbol is refused while
        # the previous close is unfinished.
        new_event = _candidate(
            "BTCUSDT", 100.0, detected_at=clock() + timedelta(minutes=5),
        )
        assert _capture(store, new_event, clock=clock) == CAPTURED
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["entries"] == 0
        assert cycle["pending_closes"] == 1
        btc_rows = [p for p in store.list_positions() if p.symbol == "BTCUSDT"]
        assert len(btc_rows) == 1

        # (c) other symbols remain free to trade.
        other = _candidate("ETHUSDT", 100.0, detected_at=clock())
        assert _capture(store, other, clock=clock) == CAPTURED
        cycle2 = contour.run_cycle({"BTCUSDT": 100.0, "ETHUSDT": 100.0})
        assert cycle2["entries"] == 1
        btc_rows = [p for p in store.list_positions() if p.symbol == "BTCUSDT"]
        assert len(btc_rows) == 1, "duplicate BTCUSDT position was created"

    # -- (5) funding is not re-accrued -----------------------------------
    def test_funding_is_not_reaccrued_across_flush_and_restart(self):
        store = _flaky(fail_close_updates=1)
        settings = _settings()
        contour, clock, _position = _open_position(store, settings=settings)

        clock.advance(minutes=MAX_HOLD_MINUTES)  # 15 whole 8h periods
        cycle = contour.run_cycle(
            {"BTCUSDT": 100.0}, {"BTCUSDT": 0.01},
        )
        assert cycle["exits"] == 1 and cycle["pending_closes"] == 1
        facts = store.list_pending_closes()[0]
        assert facts.funding_periods_charged == 15
        assert facts.to_dict()["funding_periods"] == 15
        assert facts.funding_paid != 0.0
        funded_open = [p for p in store.list_positions()
                       if p.status == POSITION_STATUS_OPEN]
        assert len(funded_open) == 1
        assert funded_open[0].funding_paid == facts.funding_paid

        # ---- RESTART before the retry; 8 more hours pass in the meantime.
        store.fail_close_updates = 0
        clock.advance(hours=8)
        contour2 = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        cycle2 = contour2.run_cycle(
            {"BTCUSDT": 100.0}, {"BTCUSDT": 0.01},
        )
        assert cycle2["pending_closes"] == 0

        closed = store.list_positions(status=POSITION_STATUS_CLOSED)
        assert len(closed) == 1
        final = closed[0]
        assert final.funding_periods_charged == facts.funding_periods_charged  # not 16
        assert final.to_dict()["funding_periods"] == 15
        assert final.funding_paid == facts.funding_paid         # charged once
        assert final.net_pnl == facts.net_pnl
        assert store.net_pnl_summary()["closed_trades"] == 1.0
        assert store.net_pnl_summary()["net_pnl"] == pytest.approx(final.net_pnl)
        assert contour2.engine.balance == pytest.approx(
            settings.initial_balance + final.net_pnl
        )
        assert store.immutability_repairs == 0

    # -- (6) existing production Paper is untouched ----------------------
    def test_pending_close_never_touches_production_paper(self):
        from app.paper.engine import PaperTradeRecord  # noqa: F401  (engine dep)

        settings = _settings()
        clock = _Clock(SIGNAL_AT)
        repo = _FakePaperRepo()
        main = PaperTradingEngine(settings, repo, clock=clock)
        main_opened = main.check_entries([_me_short_candidate()], {"BTCUSDT": 100.0})
        assert len(main_opened) == 1
        main_trade = main.open_trades["BTCUSDT"]
        main_balance = main.balance
        main_saved = list(repo.saved)
        main_gate_modes = list(repo.safety_gate_modes)

        # The contour opens its own SHORT on the SAME symbol and its close
        # persistence fails repeatedly.
        store = _flaky(fail_close_updates=10_000)
        contour, contour_clock, _position = _open_position(
            store, clock=clock, settings=settings,
        )
        contour_clock.advance(minutes=MAX_HOLD_MINUTES)
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["exits"] == 1 and cycle["pending_closes"] == 1

        # Several cycles with the close stuck — contour retries only.
        for _ in range(3):
            contour_clock.advance(hours=1)
            retry = contour.run_cycle({"BTCUSDT": 100.0})
            assert retry["exits"] == 0

        # Production Paper is byte-for-byte untouched.
        assert set(main.open_trades) == {"BTCUSDT"}
        assert main.open_trades["BTCUSDT"] is main_trade
        assert main_trade.status == "OPEN"
        assert main.balance == main_balance
        assert repo.closed == []
        assert repo.saved == main_saved
        # The gate mode was set during production setup only — the contour's
        # stuck close never triggers or changes a Paper safety gate.
        assert repo.safety_gate_modes == main_gate_modes
        assert not hasattr(main, "open_positions")
        assert not any(
            isinstance(key, str) and key.startswith(STRATEGY_ID)
            for key in main.open_trades
        )
        # The contour's pending close stays isolated in its own store.
        assert len(contour.engine.pending_closes) == 1
        assert contour.engine.open_positions == {}


# ======================================================================
# P1-3 — extra durability scenarios (stage failure, lingering outbox)
# ======================================================================

class TestP13DurabilityEdgeCases:
    def test_stage_failure_still_retains_terminal_snapshot(self):
        store = _flaky(fail_close_updates=1, fail_stage=1)
        contour, clock, _position = _open_position(store)

        clock.advance(minutes=MAX_HOLD_MINUTES)
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        # Outbox staging AND the status update both failed — no exception
        # escaped, the terminal snapshot is retained in-process.
        assert cycle["exits"] == 1
        assert cycle["pending_closes"] == 1
        assert store.stage_attempts == 1
        assert store.close_update_attempts == 1
        facts = dict(contour.engine.pending_closes)
        assert len(facts) == 1

        # Retry succeeds; the retained snapshot replays unchanged.
        contour.run_cycle({"BTCUSDT": 100.0})
        closed = store.list_positions(status=POSITION_STATUS_CLOSED)
        assert len(closed) == 1
        retained = next(iter(facts.values()))
        assert closed[0].net_pnl == retained.net_pnl
        assert closed[0].exit_price == retained.exit_price
        assert contour.engine.pending_closes == {}

    def test_lingering_outbox_row_replays_idempotently_after_restart(self):
        # Clear failure after a SUCCESSFUL close: the row is CLOSED, the
        # outbox row survived.  A restart must replay without double-booking.
        settings = _settings()
        store = InMemoryInverseShortStore(
            _activation(), initial_balance=settings.initial_balance,
        )
        contour, clock, _position = _open_position(store, settings=settings)
        clock.advance(minutes=MAX_HOLD_MINUTES)
        assert contour.run_cycle({"BTCUSDT": 100.0})["exits"] == 1
        closed_before = store.list_positions(status=POSITION_STATUS_CLOSED)[0]
        balance_before = contour.engine.balance
        # Simulate a failed clear: stage the SAME terminal snapshot again.
        assert store.stage_close(closed_before) is True
        assert len(store.list_pending_closes()) == 1

        contour2 = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        cycle = contour2.run_cycle({"BTCUSDT": 100.0})
        assert cycle["exits"] == 0
        assert cycle["pending_closes"] == 0
        assert store.list_pending_closes() == []
        closed = store.list_positions(status=POSITION_STATUS_CLOSED)
        assert len(closed) == 1, "idempotent replay duplicated the position"
        assert closed[0].net_pnl == closed_before.net_pnl
        assert contour2.engine.balance == pytest.approx(balance_before)
        assert store.net_pnl_summary()["closed_trades"] == 1.0

    def test_recovery_refuses_invalid_pending_snapshot(self):
        settings = _settings()
        store = InMemoryInverseShortStore(
            _activation(), initial_balance=settings.initial_balance,
        )
        contour, clock, _position = _open_position(store, settings=settings)
        clock.advance(minutes=MAX_HOLD_MINUTES)
        assert contour.run_cycle({"BTCUSDT": 100.0})["exits"] == 1
        closed = store.list_positions(status=POSITION_STATUS_CLOSED)[0]
        # Corrupt the staged snapshot: wrong direction (field assignment on a
        # copy — models are non-frozen, no constructor validation involved).
        corrupt = dataclasses.replace(closed)
        corrupt.direction = "LONG"
        assert store.stage_close(corrupt) is True
        # The position row itself stays a valid OPEN row (exit facts reset).
        row = dataclasses.replace(closed)
        row.status = POSITION_STATUS_OPEN
        row.exit_reason = None
        row.closed_at = None
        row.exit_price = None
        store._positions[closed.position_key] = row

        contour2 = InverseShortContour(
            settings, store, clock=clock, activation_enabled=True,
        )
        # The invalid pending snapshot is refused at recovery (kept out of
        # pending_closes); the still-valid OPEN row is recovered normally.
        assert contour2.engine.pending_closes == {}
        assert list(contour2.engine.open_positions) == [closed.position_key]
        assert contour2.engine.snapshot()["pending_closes"] == 0

    def test_snapshot_and_cycle_stats_expose_pending_closes(self):
        store = _flaky(fail_close_updates=10_000)
        contour, clock, _position = _open_position(store)
        clock.advance(minutes=MAX_HOLD_MINUTES)
        cycle = contour.run_cycle({"BTCUSDT": 100.0})
        assert cycle["pending_closes"] == 1
        assert contour.engine.snapshot()["pending_closes"] == 1
        contour.run_cycle({"BTCUSDT": 100.0})
        assert contour.engine.snapshot()["pending_closes"] == 1


# ======================================================================
# Production Paper fake (copied minimal surface from the contour tests)
# ======================================================================

class _FakePaperRepo:
    def __init__(self, open_rows=None, account_snapshot=None):
        self.open_rows = list(open_rows or [])
        self.saved = []
        self.closed = []
        self.safety_gate_modes = []
        self.account_snapshot = account_snapshot
        self.risk_state = {"daily_loss_usdt": 0.0, "consecutive_losses": 0}
        self.safety_gate_state = {
            "is_blocked": False, "reason": None, "blocked_since": None,
        }

    def get_open_paper_trades(self):
        return list(self.open_rows)

    def get_paper_risk_state(self):
        return self.risk_state

    def get_paper_safety_gate_state(self):
        return self.safety_gate_state

    def set_paper_safety_gate_mode(self, mode):
        self.safety_gate_modes.append(mode)

    def insert_paper_safety_event(self, event):
        pass

    def block_paper_safety_gate(self, reason, blocked_since):
        self.safety_gate_state = {
            "is_blocked": True, "reason": reason, "blocked_since": blocked_since,
        }

    def get_latest_paper_account_snapshot(self):
        return self.account_snapshot

    def get_paper_trade_by_setup(self, setup_id):
        for index, trade in enumerate(self.saved, start=1):
            if trade.setup_id == setup_id:
                return index
        return None

    def save_paper_trade(self, trade):
        self.saved.append(trade)
        return len(self.saved)

    def save_dca_state(self, *args):
        pass

    def close_paper_trade(self, **kwargs):
        self.closed.append(kwargs)

    def update_paper_trade_funding(self, *args):
        pass
