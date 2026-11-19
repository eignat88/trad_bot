"""ME_LONG_V2_INVERSE_SHORT_OOS2_V2 — Issue #287 admission guards (REAL PostgreSQL).

``ME_INVERSE_SHORT_OOS2_ENTRY_GUARDS_AND_CAPACITY_V1`` against a live
PostgreSQL database (NEVER mocks): the serialized admission critical section
(``pg_advisory_xact_lock``), the OOS-2-family symbol / cooldown / capacity
guards judged at ``statement_timestamp()`` after every lock wait, terminal
symbol refusals, the pending->TTL ``ENTRY_CAP`` contract, ambiguous-COMMIT
adoption and the legacy OOS-2 v1 READ-ONLY coexistence contract.

Environment (see ``tests/conftest.py``):

    TEST_DB_HOST / TEST_DB_PORT / TEST_DB_NAME / TEST_DB_USER / TEST_DB_PASSWORD
    or TEST_DB_UNIX_SOCK
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_088 = (
    REPO_ROOT / "sql" / "migrations" / "088_me_long_v2_inverse_short_oos2_v1.sql"
)
MIGRATION_089 = (
    REPO_ROOT / "sql" / "migrations"
    / "089_me_long_v2_inverse_short_oos2_v1_funding_obligation.sql"
)
MIGRATION_091 = (
    REPO_ROOT / "sql" / "migrations" / "091_me_long_v2_inverse_short_oos2_v2.sql"
)

V2_TABLE = "dds.me_long_v2_inverse_short_oos2_v2_"
LEGACY_TABLE = "dds.me_long_v2_inverse_short_oos2_v1_position"
LEGACY_ACTIVATION_TABLE = "dds.me_long_v2_inverse_short_oos2_v1_activation"

HAS_TEST_DB = bool(
    os.getenv("TEST_DB_HOST") or os.getenv("TEST_DB_UNIX_SOCK")
    or os.getenv("TEST_DB_NAME")
)
if not HAS_TEST_DB:  # pragma: no cover - env gate
    pytest.skip(
        "TEST_DB_* not configured (real PostgreSQL required)",
        allow_module_level=True,
    )

import pg8000  # noqa: E402

from app.paper.me_long_v2_inverse_short_oos2_v2.identity import (  # noqa: E402
    position_key as build_position_key,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.models import (  # noqa: E402
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_CAPACITY_REJECTED,
    EVENT_STATUS_ENTERED,
    EVENT_STATUS_SYMBOL_REJECTED,
    InverseShortEvent,
    InverseShortPosition,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.protocol import (  # noqa: E402
    ENTRY_MAX_AGE_MINUTES,
    EXPERIMENT_ID,
    LEGACY_EXPERIMENT_ID,
    MAX_OPEN_POSITIONS,
    REFUSAL_ENTRY_CAP,
    REFUSAL_SYMBOL_ALREADY_OPEN,
    REFUSAL_SYMBOL_COOLDOWN,
    SOURCE_DIRECTION,
    SOURCE_SCANNER,
    TRADE_DIRECTION,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_oos2_v2.store import (  # noqa: E402
    ADMIT_ADOPT,
    ADMIT_DEFERRED,
    ADMIT_INSERTED,
    ADMIT_NOT_ADMISSIBLE,
    ADMIT_REFUSED_TERMINAL,
    connect_pg_store,
)

FINGERPRINT = protocol_fingerprint()
LEGACY_FINGERPRINT = (
    "1d1e7b4d87c6125183887c8f054a4edb1ff3f0711e70da1feeb9b7e249d7b7ec"
)
FREEZE_TS = datetime.now(timezone.utc) - timedelta(hours=6)


def _db_kwargs() -> dict:
    kwargs: dict = {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": os.getenv("TEST_DB_NAME", "trad_bot_migration_test"),
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", ""),
    }
    unix_sock = os.getenv("TEST_DB_UNIX_SOCK")
    if unix_sock:
        kwargs = {
            "unix_sock": unix_sock,
            "database": kwargs["database"],
            "user": kwargs["user"],
            "password": kwargs["password"],
        }
    return kwargs


def _raw_connect():
    return pg8000.connect(**_db_kwargs())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _Db:
    """Session fixture: schema present + per-test purge."""

    def __init__(self) -> None:
        self.conn = _raw_connect()
        self.conn.autocommit = True
        self._apply_schema()

    def cursor(self):
        return self.conn.cursor()

    def _apply_schema(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("CREATE SCHEMA IF NOT EXISTS dds")
        cursor.execute("SELECT to_regclass(%s)", (LEGACY_TABLE,))
        if cursor.fetchone()[0] is None:
            cursor.execute(MIGRATION_088.read_text(encoding="utf-8"))
        cursor.execute(
            "SELECT to_regclass(%s)",
            (f"{V2_TABLE}funding_obligation",),
        )
        # 089's legacy funding table is not needed for admission tests, but
        # the migration must stay applyable end-to-end in order.
        cursor.execute("SELECT to_regclass(%s)", (LEGACY_TABLE,))
        cursor.execute(MIGRATION_091.read_text(encoding="utf-8"))
        cursor.close()

    def purge(self) -> None:
        cursor = self.conn.cursor()
        # children first: position.event_key REFERENCES event (FK)
        for table in ("funding_obligation", "close_leg", "close_outbox",
                      "position", "event"):
            cursor.execute(f"DELETE FROM {V2_TABLE}{table}")
        # v2 activation: RUNNING + frozen fingerprint + freeze (test harness).
        cursor.execute(
            f"INSERT INTO {V2_TABLE}activation (experiment_id, status, "
            "protocol_fingerprint, freeze_ts) VALUES (%s, 'RUNNING', %s, %s) "
            "ON CONFLICT (experiment_id) DO UPDATE SET status='RUNNING', "
            "protocol_fingerprint=EXCLUDED.protocol_fingerprint, "
            "freeze_ts=EXCLUDED.freeze_ts, updated_at=now()",
            (EXPERIMENT_ID, FINGERPRINT, FREEZE_TS),
        )
        # legacy v1 activation: drained + frozen (cutover precondition),
        # untouched fingerprint/freeze semantics of migration 088.
        cursor.execute(
            f"INSERT INTO {LEGACY_ACTIVATION_TABLE} (experiment_id, status, "
            "protocol_fingerprint, freeze_ts) VALUES (%s, 'DRAINING', %s, %s) "
            "ON CONFLICT (experiment_id) DO UPDATE SET status='DRAINING', "
            "protocol_fingerprint=EXCLUDED.protocol_fingerprint, "
            "freeze_ts=EXCLUDED.freeze_ts, updated_at=now()",
            (LEGACY_EXPERIMENT_ID, LEGACY_FINGERPRINT, FREEZE_TS),
        )
        cursor.execute(
            f"DELETE FROM {LEGACY_TABLE} WHERE position_key LIKE %s",
            (f"{LEGACY_EXPERIMENT_ID}:it287-%",),
        )
        cursor.execute(
            "DELETE FROM dds.me_long_v2_inverse_short_oos2_v1_event "
            "WHERE event_key LIKE 'it287-legacy-event-%'",
        )
        cursor.close()


def _seed_legacy(
    db: _Db,
    suffix: str,
    *,
    symbol: str,
    entered_at_sql: str,
    closed: bool = False,
) -> None:
    """Insert a legacy OOS-2 v1 event + position row (peer-ledger fixture;
    the v2 admission only ever READS these)."""
    cursor = db.cursor()
    event_key = f"it287-legacy-event-{suffix}"
    cursor.execute(
        "INSERT INTO dds.me_long_v2_inverse_short_oos2_v1_event ("
        "event_key, strategy_id, protocol_version, protocol_hash, "
        "source_scanner, source_scanner_version, source_direction, "
        "source_setup_id, symbol, direction, entry_timeframe, signal_time, "
        "captured_at, activation_freeze_ts, signal_candle_open_time, "
        "capture_route, status, lineage) VALUES (%s, %s, '1.0.0', %s, "
        "'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2', '1.0.0', 'LONG', %s, %s, "
        "'SHORT', '5m', now() - interval '2 days', now() - interval '2 days', "
        "now() - interval '3 days', 1, 'PRE_FILTER_SOURCE', 'CAPTURED', '{}')",
        (event_key, LEGACY_EXPERIMENT_ID, LEGACY_FINGERPRINT,
         f"setup-{suffix}", symbol),
    )
    if closed:
        cursor.execute(
            f"INSERT INTO {LEGACY_TABLE} (position_key, event_key, symbol, "
            "direction, entered_at, deadline_at, entry_market_price, "
            "entry_price, entry_fee, entry_slippage, stop_price, target_price, "
            "tp2_price, tp3_price, position_size, remaining_size, risk_usdt, "
            "balance_before, status, exit_reason, closed_at) VALUES ("
            f"'{LEGACY_EXPERIMENT_ID}:it287-{suffix}', %s, %s, 'SHORT', "
            f"{entered_at_sql}, now() + interval '1 hour', 1, 1, 0, 0, 1.1, "
            "0.95, 0.93, 0.90, 1, 0, 0.1, 10000, 'CLOSED', 'TIMEOUT', now())",
            (event_key, symbol),
        )
    else:
        cursor.execute(
            f"INSERT INTO {LEGACY_TABLE} (position_key, event_key, symbol, "
            "direction, entered_at, deadline_at, entry_market_price, "
            "entry_price, entry_fee, entry_slippage, stop_price, target_price, "
            "tp2_price, tp3_price, position_size, remaining_size, risk_usdt, "
            "balance_before) VALUES ("
            f"'{LEGACY_EXPERIMENT_ID}:it287-{suffix}', %s, %s, 'SHORT', "
            f"{entered_at_sql}, now() + interval '1 hour', 1, 1, 0, 0, 1.1, "
            "0.95, 0.93, 0.90, 1, 1, 0.1, 10000)",
            (event_key, symbol),
        )
    cursor.close()


@pytest.fixture(scope="session")
def db() -> _Db:
    try:
        return _Db()
    except Exception as exc:  # pragma: no cover - env gate
        pytest.skip(f"real PostgreSQL unavailable: {type(exc).__name__}")


@pytest.fixture()
def clean_db(db: _Db):
    db.purge()
    yield db
    db.purge()


@pytest.fixture()
def store(clean_db: _Db):
    store = connect_pg_store(**_db_kwargs())
    yield store
    store.close()


def _event(
    key: str,
    *,
    symbol: str = "BTCUSDT",
    signal_time: datetime | None = None,
    freeze_ts: datetime | None = None,
) -> InverseShortEvent:
    signal_time = signal_time or _utc_now() - timedelta(minutes=1)
    candle_ms = int(signal_time.timestamp() // 300) * 300_000
    return InverseShortEvent(
        event_key=key,
        symbol=symbol,
        signal_time=signal_time,
        captured_at=signal_time,
        activation_freeze_ts=freeze_ts or FREEZE_TS,
        signal_candle_open_time=candle_ms,
        source_scanner=SOURCE_SCANNER,
        source_scanner_version="1.0.0",
        source_direction=SOURCE_DIRECTION,
        source_setup_id=f"setup-{key}",
        capture_route="PRE_FILTER_SOURCE",
    )


def _position(
    event: InverseShortEvent,
    *,
    entered_at: datetime | None = None,
    symbol: str | None = None,
    event_key: str | None = None,
) -> InverseShortPosition:
    entered_at = entered_at or _utc_now()
    event_key = event_key or event.event_key
    symbol = symbol or event.symbol
    return InverseShortPosition(
        position_key=build_position_key(event_key),
        event_key=event_key,
        symbol=symbol,
        direction=TRADE_DIRECTION,
        entered_at=entered_at,
        deadline_at=entered_at + timedelta(minutes=ENTRY_MAX_AGE_MINUTES),
        entry_market_price=100.0,
        entry_price=100.0,
        entry_fee=0.055,
        entry_slippage_cost=0.05,
        stop_price=110.0,
        target_price=95.0,
        tp2_price=93.0,
        tp3_price=90.0,
        position_size=1.0,
        remaining_size=1.0,
        risk_usdt=50.0,
        balance_before=10_000.0,
    )


def _capture(store, event: InverseShortEvent) -> None:
    assert store.save_event(event) == "CAPTURED", event.event_key


def _seed_v2_position(
    db: _Db,
    event_key: str,
    symbol: str,
    entered_at_sql: str,
    *,
    closed: bool = False,
) -> None:
    """Insert a v2 position row directly with a chosen ``entered_at``.

    The position trigger freezes entry facts on UPDATE (they are immutable by
    design), so historical-entry fixtures are built at INSERT time — exactly
    like rows a running contour committed hours ago.
    """
    cursor = db.cursor()
    if closed:
        cursor.execute(
            f"INSERT INTO {V2_TABLE}position (position_key, event_key, symbol, "
            "direction, entered_at, deadline_at, entry_market_price, "
            "entry_price, entry_fee, entry_slippage, stop_price, target_price, "
            "tp2_price, tp3_price, position_size, remaining_size, risk_usdt, "
            "balance_before, status, exit_reason, closed_at) VALUES ("
            "%s, %s, %s, 'SHORT', "
            f"{entered_at_sql}, {entered_at_sql} + interval '2 hours', "
            "100, 100, 0.055, 0.05, 110, 95, 93, 90, 1, 0, 50, 10000, "
            "'CLOSED', 'TIMEOUT', now())",
            (build_position_key(event_key), event_key, symbol),
        )
    else:
        cursor.execute(
            f"INSERT INTO {V2_TABLE}position (position_key, event_key, symbol, "
            "direction, entered_at, deadline_at, entry_market_price, "
            "entry_price, entry_fee, entry_slippage, stop_price, target_price, "
            "tp2_price, tp3_price, position_size, remaining_size, risk_usdt, "
            "balance_before) VALUES ("
            "%s, %s, %s, 'SHORT', "
            f"{entered_at_sql}, {entered_at_sql} + interval '2 hours', "
            "100, 100, 0.055, 0.05, 110, 95, 93, 90, 1, 1, 50, 10000)",
            (build_position_key(event_key), event_key, symbol),
        )
    cursor.close()


def _event_row(db: _Db, event_key: str) -> dict:
    cursor = db.cursor()
    cursor.execute(
        f"SELECT status, status_reason, status_at, position_key "
        f"FROM {V2_TABLE}event WHERE event_key = %s",
        (event_key,),
    )
    row = cursor.fetchone()
    cursor.close()
    return {
        "status": row[0], "reason": row[1], "status_at": row[2],
        "position_key": row[3],
    } if row else {}


def _count_positions(db: _Db, *, legacy: bool = False) -> int:
    cursor = db.cursor()
    table = LEGACY_TABLE if legacy else f"{V2_TABLE}position"
    cursor.execute(f"SELECT count(*) FROM {table} WHERE status = 'OPEN'")
    value = cursor.fetchone()[0]
    cursor.close()
    return value


# ---------------------------------------------------------------------------
# Serialized admission / concurrency
# ---------------------------------------------------------------------------
class TestSerializedAdmission:
    def test_concurrent_same_symbol_distinct_events_open_exactly_one(
        self, clean_db: _Db,
    ):
        """Two processes racing on the SAME symbol (distinct event keys):
        the advisory admission lock serializes them and the decision-time
        re-check refuses the loser TERMINALLY — exactly one row and one fee."""
        results = {}
        barrier = threading.Barrier(2)

        def _worker(name: str, key: str) -> None:
            store = connect_pg_store(**_db_kwargs())
            try:
                event = _event(key, symbol="BTCUSDT")
                _capture(store, event)
                barrier.wait()
                results[name] = store.admit_position(_position(event))
            finally:
                store.close()

        threads = [
            threading.Thread(target=_worker, args=("a", "it287-race-a")),
            threading.Thread(target=_worker, args=("b", "it287-race-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        outcomes = sorted(
            (r.outcome, r.reason) for r in results.values()
        )
        assert outcomes == [
            (ADMIT_INSERTED, None),
            (ADMIT_REFUSED_TERMINAL, REFUSAL_SYMBOL_ALREADY_OPEN),
        ], outcomes
        assert _count_positions(clean_db) == 1
        refused_key = (
            "it287-race-a" if results["a"].outcome != ADMIT_INSERTED
            else "it287-race-b"
        )
        row = _event_row(clean_db, refused_key)
        assert row["status"] == EVENT_STATUS_SYMBOL_REJECTED
        assert row["reason"] == REFUSAL_SYMBOL_ALREADY_OPEN
        assert row["status_at"] is not None
        assert row["position_key"] is None

    def test_cap_is_checked_under_the_same_lock(self, clean_db: _Db, store):
        """Concurrent distinct symbols at the cap boundary: exactly
        MAX_OPEN_POSITIONS rows, the rest deferred with ENTRY_CAP."""
        for i in range(MAX_OPEN_POSITIONS):
            event = _event(f"it287-cap-{i}", symbol=f"CAP{i:02d}")
            _capture(store, event)
            assert store.admit_position(_position(event)).outcome == ADMIT_INSERTED

        results = {}
        barrier = threading.Barrier(3)

        def _worker(name: str, symbol: str) -> None:
            local = connect_pg_store(**_db_kwargs())
            try:
                event = _event(f"it287-caprace-{name}", symbol=symbol)
                _capture(local, event)
                barrier.wait()
                results[name] = local.admit_position(_position(event))
            finally:
                local.close()

        threads = [
            threading.Thread(target=_worker, args=(name, f"ZZ{name.upper()}"))
            for name in ("x", "y", "z")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert _count_positions(clean_db) == MAX_OPEN_POSITIONS
        assert sorted(
            r.reason for r in results.values()
        ) == [REFUSAL_ENTRY_CAP] * 3
        # deferred events stay CAPTURED (pending -> TTL contract)
        for name in ("x", "y", "z"):
            assert _event_row(
                clean_db, f"it287-caprace-{name}",
            )["status"] == EVENT_STATUS_CAPTURED


# ---------------------------------------------------------------------------
# Guard 2: SINGLE_ACTIVE_SYMBOL (family)
# ---------------------------------------------------------------------------
class TestSingleActiveSymbolPg:
    def test_second_event_same_symbol_is_refused_terminal(
        self, clean_db: _Db, store,
    ):
        first = _event("it287-sym-1")
        _capture(store, first)
        assert store.admit_position(_position(first)).outcome == ADMIT_INSERTED
        second = _event("it287-sym-2", symbol=first.symbol)
        _capture(store, second)
        decision = store.admit_position(_position(second))
        assert decision.outcome == ADMIT_REFUSED_TERMINAL
        assert decision.reason == REFUSAL_SYMBOL_ALREADY_OPEN
        row = _event_row(clean_db, second.event_key)
        assert row["status"] == EVENT_STATUS_SYMBOL_REJECTED
        assert row["reason"] == REFUSAL_SYMBOL_ALREADY_OPEN
        assert row["status_at"] is not None
        assert _count_positions(clean_db) == 1

    def test_refused_event_is_never_replayed(self, clean_db: _Db, store):
        first = _event("it287-noreplay-1")
        _capture(store, first)
        assert store.admit_position(_position(first)).outcome == ADMIT_INSERTED
        second = _event("it287-noreplay-2")
        _capture(store, second)
        assert store.admit_position(_position(second)).outcome \
            == ADMIT_REFUSED_TERMINAL
        # replay: the terminal event is NOT re-admissible
        replay = store.admit_position(_position(second))
        assert replay.outcome == ADMIT_NOT_ADMISSIBLE
        assert replay.reason == "EVENT_NOT_CAPTURED"

    def test_partially_closed_position_still_blocks_its_symbol(
        self, clean_db: _Db, store,
    ):
        first = _event("it287-tp1-1")
        _capture(store, first)
        assert store.admit_position(_position(first)).outcome == ADMIT_INSERTED
        # simulate the TP1 partial leg: remaining shrinks, row stays OPEN
        cursor = clean_db.cursor()
        cursor.execute(
            f"UPDATE {V2_TABLE}position SET remaining_size = 0.5, "
            "tp1_executed = TRUE, realized_legs_net = 1.0 "
            "WHERE position_key = %s",
            (build_position_key(first.event_key),),
        )
        cursor.close()
        second = _event("it287-tp1-2", symbol=first.symbol)
        _capture(store, second)
        decision = store.admit_position(_position(second))
        assert decision.outcome == ADMIT_REFUSED_TERMINAL
        assert decision.reason == REFUSAL_SYMBOL_ALREADY_OPEN

    def test_legacy_duplicate_open_rows_block_new_family_duplicate(
        self, clean_db: _Db, store,
    ):
        _seed_legacy(
            clean_db, "dup-1", symbol="BTCUSDT",
            entered_at_sql="now() - interval '3 hours'",
        )
        _seed_legacy(
            clean_db, "dup-2", symbol="BTCUSDT",
            entered_at_sql="now() - interval '2 hours'",
        )
        event = _event("it287-dup-blocked", symbol="BTCUSDT")
        _capture(store, event)
        decision = store.admit_position(_position(event))
        assert decision.outcome == ADMIT_REFUSED_TERMINAL
        assert decision.reason == REFUSAL_SYMBOL_ALREADY_OPEN
        # legacy rows untouched (READ-ONLY)
        assert _count_positions(clean_db, legacy=True) == 2
        cursor = clean_db.cursor()
        cursor.execute(
            f"SELECT event_key FROM {LEGACY_TABLE} WHERE position_key LIKE %s "
            "ORDER BY event_key",
            (f"{LEGACY_EXPERIMENT_ID}:it287-%",),
        )
        assert [r[0] for r in cursor.fetchall()] == [
            "it287-legacy-event-dup-1", "it287-legacy-event-dup-2",
        ]
        cursor.close()


# ---------------------------------------------------------------------------
# Guard 3: SYMBOL_ENTRY_COOLDOWN_12H (DB-decision-time boundaries)
# ---------------------------------------------------------------------------
class TestCooldownPg:
    def test_under_12h_refused_just_over_12h_admitted(self, clean_db: _Db, store):
        # ACCEPTED entry committed 11h59m ago, position CLOSED (so the
        # SYMBOL guard cannot bind before the cooldown gate).  Entry facts
        # are immutable by design, so the historical row is built at INSERT
        # time exactly like a row committed hours ago.
        first = _event("it287-cd-1")
        _capture(store, first)
        _seed_v2_position(
            clean_db, first.event_key, first.symbol,
            "now() - interval '11 hours 59 minutes'", closed=True,
        )
        second = _event("it287-cd-2", symbol=first.symbol)
        _capture(store, second)
        decision = store.admit_position(_position(second))
        assert decision.outcome == ADMIT_REFUSED_TERMINAL
        assert decision.reason == REFUSAL_SYMBOL_COOLDOWN
        row = _event_row(clean_db, second.event_key)
        assert row["status"] == EVENT_STATUS_SYMBOL_REJECTED
        assert row["reason"] == REFUSAL_SYMBOL_COOLDOWN

        # boundary: the same ACCEPTED entry 12h00m01s ago is admissible
        # (inclusive boundary: `entered_at > statement_timestamp() - 12h`
        # is the ONLY refusal condition).
        cursor = clean_db.cursor()
        cursor.execute(
            f"DELETE FROM {V2_TABLE}position WHERE position_key = %s",
            (build_position_key(first.event_key),),
        )
        cursor.close()
        replacement = _event("it287-cd-1b", symbol=first.symbol)
        _capture(store, replacement)
        _seed_v2_position(
            clean_db, replacement.event_key, first.symbol,
            "now() - interval '12 hours 1 second'", closed=True,
        )
        third = _event("it287-cd-3", symbol=first.symbol)
        _capture(store, third)
        decision = store.admit_position(_position(third))
        assert decision.outcome == ADMIT_INSERTED, decision

    def test_exact_boundary_expression_is_inclusive(self, clean_db: _Db):
        """The frozen cooldown condition refuses ONLY strictly-inside-12h
        rows: a row at exactly ``statement_timestamp() - 12h`` is admissible."""
        cursor = clean_db.cursor()
        cursor.execute(
            "SELECT (now()::timestamptz > statement_timestamp() - interval "
            "'12 hours') AS strictly_inside, (statement_timestamp() - "
            "interval '12 hours' > statement_timestamp() - interval "
            "'12 hours') AS exact_boundary"
        )
        strictly_inside, exact_boundary = cursor.fetchone()
        cursor.close()
        assert strictly_inside is True   # now() lands slightly after statement start
        assert exact_boundary is False   # exactly 12h old -> NOT refused

    def test_refusals_never_extend_the_timer(self, clean_db: _Db, store):
        first = _event("it287-ext-1")
        _capture(store, first)
        _seed_v2_position(
            clean_db, first.event_key, first.symbol,
            "now() - interval '1 hour'", closed=True,
        )
        for i in range(3):
            event = _event(f"it287-ext-ref-{i}", symbol=first.symbol)
            _capture(store, event)
            decision = store.admit_position(_position(event))
            assert decision.outcome == ADMIT_REFUSED_TERMINAL
            assert decision.reason == REFUSAL_SYMBOL_COOLDOWN
        # the ACCEPTED entry's timer is unchanged by the refusals: exactly
        # one position row for the symbol, its entered_at untouched, and the
        # refusals wrote no position row / entered_at of their own.
        cursor = clean_db.cursor()
        cursor.execute(
            f"SELECT entered_at FROM {V2_TABLE}position WHERE symbol = %s",
            (first.symbol,),
        )
        rows = cursor.fetchall()
        cursor.close()
        assert len(rows) == 1
        assert abs(
            (rows[0][0] - (_utc_now() - timedelta(hours=1))).total_seconds()
        ) < 300, rows[0][0]

    def test_closed_position_keeps_cooling_its_symbol(self, clean_db: _Db, store):
        first = _event("it287-closed-1")
        _capture(store, first)
        _seed_v2_position(
            clean_db, first.event_key, first.symbol,
            "now() - interval '1 hour'", closed=True,
        )
        second = _event("it287-closed-2", symbol=first.symbol)
        _capture(store, second)
        decision = store.admit_position(_position(second))
        assert decision.outcome == ADMIT_REFUSED_TERMINAL
        assert decision.reason == REFUSAL_SYMBOL_COOLDOWN

    def test_legacy_v1_entered_at_cools_v2(self, clean_db: _Db, store):
        _seed_legacy(
            clean_db, "cool", symbol="BTCUSDT",
            entered_at_sql="now() - interval '1 hour'", closed=True,
        )
        event = _event("it287-cool-blocked", symbol="BTCUSDT")
        _capture(store, event)
        decision = store.admit_position(_position(event))
        assert decision.outcome == ADMIT_REFUSED_TERMINAL
        assert decision.reason == REFUSAL_SYMBOL_COOLDOWN
        # legacy row untouched
        assert _count_positions(clean_db, legacy=True) == 0  # CLOSED row


# ---------------------------------------------------------------------------
# Guard 1: MAX_OPEN_POSITIONS (family) + ENTRY_CAP contract
# ---------------------------------------------------------------------------
class TestCapacityPg:
    def test_twelve_admit_the_thirteenth_defers_then_disposes_at_ttl(
        self, clean_db: _Db, store,
    ):
        for i in range(MAX_OPEN_POSITIONS):
            event = _event(f"it287-cap12-{i}", symbol=f"SYM{i:02d}")
            _capture(store, event)
            assert store.admit_position(
                _position(event),
            ).outcome == ADMIT_INSERTED
        thirteenth = _event("it287-cap12-late", symbol="SYM99")
        _capture(store, thirteenth)
        decision = store.admit_position(_position(thirteenth))
        assert decision.outcome == ADMIT_DEFERRED
        assert decision.reason == REFUSAL_ENTRY_CAP
        assert _event_row(
            clean_db, thirteenth.event_key,
        )["status"] == EVENT_STATUS_CAPTURED

        # TTL at DB decision time: an event past the frozen TTL is never
        # admitted and the sweep classifies the binding cause as the cap.
        cursor = clean_db.cursor()
        cursor.execute(
            f"UPDATE {V2_TABLE}event SET signal_time = now() - interval "
            "'121 minutes' WHERE event_key = %s",
            (thirteenth.event_key,),
        )
        cursor.close()
        stale = store.admit_position(_position(thirteenth))
        assert stale.outcome == ADMIT_NOT_ADMISSIBLE
        assert stale.reason == "ENTRY_TTL"

    def test_tp1_partial_never_frees_the_slot(self, clean_db: _Db, store):
        for i in range(MAX_OPEN_POSITIONS):
            event = _event(f"it287-capp-{i}", symbol=f"PAR{i:02d}")
            _capture(store, event)
            assert store.admit_position(
                _position(event),
            ).outcome == ADMIT_INSERTED
        cursor = clean_db.cursor()
        cursor.execute(
            f"UPDATE {V2_TABLE}position SET remaining_size = 0.5, "
            "tp1_executed = TRUE WHERE symbol = 'PAR00'",
        )
        cursor.close()
        late = _event("it287-capp-late", symbol="PAR99")
        _capture(store, late)
        decision = store.admit_position(_position(late))
        assert decision.outcome == ADMIT_DEFERRED
        assert decision.reason == REFUSAL_ENTRY_CAP

    def test_close_frees_the_slot(self, clean_db: _Db, store):
        for i in range(MAX_OPEN_POSITIONS):
            event = _event(f"it287-capc-{i}", symbol=f"CLO{i:02d}")
            _capture(store, event)
            assert store.admit_position(
                _position(event),
            ).outcome == ADMIT_INSERTED
        cursor = clean_db.cursor()
        cursor.execute(
            f"UPDATE {V2_TABLE}position SET status = 'CLOSED', "
            "exit_reason = 'TIMEOUT', closed_at = now(), remaining_size = 0 "
            "WHERE symbol = 'CLO00'",
        )
        cursor.close()
        late = _event("it287-capc-late", symbol="CLO99")
        _capture(store, late)
        assert store.admit_position(_position(late)).outcome == ADMIT_INSERTED

    def test_cap_counts_the_family_only(self, clean_db: _Db, store):
        for i in range(5):
            _seed_legacy(
                clean_db, f"family-{i}", symbol=f"FAM{i:02d}",
                entered_at_sql="now() - interval '2 days'",
            )
        state = store.cross_contour_state()
        assert state.available
        assert state.legacy_open == 5
        for i in range(MAX_OPEN_POSITIONS - 5):
            event = _event(f"it287-family-{i}", symbol=f"FV{i:02d}")
            _capture(store, event)
            assert store.admit_position(
                _position(event),
            ).outcome == ADMIT_INSERTED
        state = store.cross_contour_state()
        assert state.open_total == MAX_OPEN_POSITIONS
        late = _event("it287-family-late", symbol="FV99")
        _capture(store, late)
        decision = store.admit_position(_position(late))
        assert decision.outcome == ADMIT_DEFERRED
        assert decision.reason == REFUSAL_ENTRY_CAP


# ---------------------------------------------------------------------------
# Fail-closed legacy state / adoption / TTL
# ---------------------------------------------------------------------------
class TestFailClosedAndAdoptionPg:
    def test_legacy_activation_missing_defers_fail_closed(
        self, clean_db: _Db, store,
    ):
        cursor = clean_db.cursor()
        cursor.execute(
            f"DELETE FROM {LEGACY_ACTIVATION_TABLE} WHERE experiment_id = %s",
            (LEGACY_EXPERIMENT_ID,),
        )
        cursor.close()
        event = _event("it287-nolegacy")
        _capture(store, event)
        decision = store.admit_position(_position(event))
        assert decision.outcome == ADMIT_DEFERRED
        assert decision.reason == "LEGACY_STATUS_UNAVAILABLE"
        assert not _count_positions(clean_db)

    def test_legacy_still_admitting_defers_fail_closed(
        self, clean_db: _Db, store,
    ):
        cursor = clean_db.cursor()
        cursor.execute(
            f"UPDATE {LEGACY_ACTIVATION_TABLE} SET status = 'RUNNING' "
            "WHERE experiment_id = %s",
            (LEGACY_EXPERIMENT_ID,),
        )
        cursor.close()
        event = _event("it287-legacy-running")
        _capture(store, event)
        decision = store.admit_position(_position(event))
        assert decision.outcome == ADMIT_DEFERRED
        assert decision.reason == "LEGACY_NOT_DRAINING"
        assert not _count_positions(clean_db)
        assert _event_row(
            clean_db, event.event_key,
        )["status"] == EVENT_STATUS_CAPTURED

    def test_ambiguous_commit_resolves_to_adoption_never_a_refusal(
        self, clean_db: _Db, store,
    ):
        event = _event("it287-adopt-1")
        _capture(store, event)
        assert store.admit_position(_position(event)).outcome == ADMIT_INSERTED
        # The retried unit (lost COMMIT acknowledgement) re-runs admission:
        # the durable row is read back and ADOPTED — no second INSERT, no
        # second fee, and NEVER a symbol/cooldown refusal.
        decision = store.admit_position(_position(event))
        assert decision.outcome == ADMIT_ADOPT
        assert _count_positions(clean_db) == 1
        assert _event_row(
            clean_db, event.event_key,
        )["status"] in (EVENT_STATUS_ENTERED, EVENT_STATUS_CAPTURED)

    def test_adoption_when_position_committed_but_event_still_captured(
        self, clean_db: _Db, store,
    ):
        """A lost mark_event_entered leaves CAPTURED + committed row: the
        row is brought under management by adoption, never re-inserted."""
        event = _event("it287-adopt-2")
        _capture(store, event)
        position = _position(event)
        cursor = clean_db.cursor()
        cursor.execute(
            f"UPDATE {V2_TABLE}event SET status = 'CAPTURED', "
            "position_key = NULL WHERE event_key = %s",
            (event.event_key,),
        )
        cursor.close()
        # simulate "the row committed but the ack was lost": insert the row
        # through the SAME guarded path, then reset the event to CAPTURED
        assert store.admit_position(position).outcome == ADMIT_INSERTED
        cursor = clean_db.cursor()
        cursor.execute(
            f"UPDATE {V2_TABLE}event SET status = 'CAPTURED', "
            "position_key = NULL, status_reason = NULL WHERE event_key = %s",
            (event.event_key,),
        )
        cursor.close()
        decision = store.admit_position(position)
        assert decision.outcome == ADMIT_ADOPT
        assert _count_positions(clean_db) == 1

    def test_pre_freeze_events_are_never_admitted(self, clean_db: _Db, store):
        # The event row's own freeze passes the schema CHECK
        # (signal_time > activation_freeze_ts) but the ACTIVATION freeze is
        # later: the decision-time freeze gate must refuse it.
        event = _event(
            "it287-prefreeze",
            signal_time=FREEZE_TS - timedelta(minutes=5),
            freeze_ts=FREEZE_TS - timedelta(minutes=10),
        )
        _capture(store, event)
        decision = store.admit_position(_position(event))
        assert decision.outcome == ADMIT_NOT_ADMISSIBLE
        assert decision.reason == "PRE_FREEZE"


# ---------------------------------------------------------------------------
# Schema / migration contract (real database)
# ---------------------------------------------------------------------------
class TestMigration091Pg:
    def test_migration_091_is_idempotent(self, clean_db: _Db):
        cursor = clean_db.cursor()
        cursor.execute(MIGRATION_091.read_text(encoding="utf-8"))
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'dds' AND "
            "indexname IN ('uq_me_inverse_short_oos2_v2_position_symbol_open', "
            "'idx_me_inverse_short_oos2_v2_position_symbol_entered')"
        )
        names = {row[0] for row in cursor.fetchall()}
        cursor.close()
        assert names == {
            "uq_me_inverse_short_oos2_v2_position_symbol_open",
            "idx_me_inverse_short_oos2_v2_position_symbol_entered",
        }

    def test_symbol_open_unique_index_is_the_schema_backstop(
        self, clean_db: _Db, store,
    ):
        cursor = clean_db.cursor()
        event_a = _event("it287-uniq-a")
        event_b = _event("it287-uniq-b", symbol=event_a.symbol)
        _capture(store, event_a)
        _capture(store, event_b)
        assert store.admit_position(_position(event_a)).outcome == ADMIT_INSERTED
        with pytest.raises(Exception) as excinfo:
            # bypassing the admission protocol must STILL be impossible:
            # the partial UNIQUE (symbol) WHERE OPEN index rejects it
            cursor.execute(
                f"INSERT INTO {V2_TABLE}position (position_key, event_key, "
                "symbol, direction, entered_at, deadline_at, "
                "entry_market_price, entry_price, entry_fee, "
                "entry_slippage, stop_price, target_price, tp2_price, "
                "tp3_price, position_size, remaining_size, risk_usdt, "
                "balance_before) VALUES (%s, %s, %s, 'SHORT', now(), "
                "now() + interval '2 hours', 1, 1, 0, 0, 1.1, 0.95, "
                "0.93, 0.90, 1, 1, 0.1, 10000)",
                (build_position_key(event_b.event_key),
                 event_b.event_key, event_b.symbol),
            )
        assert "uq_me_inverse_short_oos2_v2_position_symbol_open" \
            in str(excinfo.value)
        cursor.close()

    def test_event_check_constraint_enforces_five_states(
        self, clean_db: _Db, store,
    ):
        cursor = clean_db.cursor()
        event = _event("it287-check-1")
        _capture(store, event)
        with pytest.raises(Exception) as excinfo:
            cursor.execute(
                f"UPDATE {V2_TABLE}event SET status = 'BOGUS' "
                "WHERE event_key = %s",
                (event.event_key,),
            )
        assert "status" in str(excinfo.value).lower()
        cursor.close()
