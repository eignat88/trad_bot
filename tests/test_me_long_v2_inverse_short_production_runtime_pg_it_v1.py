"""Real-PostgreSQL integration for the Issue #268 production runtime.

SEPARATE TEST DATABASE ONLY — the suite:

* refuses to run against a database named like production (guard below);
* runs only when TEST_DB_HOST / TEST_DB_UNIX_SOCK is configured
  (the repository-wide convention for real-PG tests);
* applies migrations 085/086 to the TEST database when they are missing
  (idempotent files, executed through the test connection itself — no
  psql binary needed) and skips honestly when they cannot be applied;
* snapshots the activation row and RESTORES it in ``finally`` — a test
  never leaves a RUNNING activation behind;
* deletes every row it created (event / position / close outbox).

Covered end-to-end on the real store: prospective capture (incl. duplicate
delivery), one runtime entry, cycle-level idempotency, restart recovery
without re-entry, a TP exit with durable close persistence, and the
disabled (flag OFF) runtime writing nothing.
"""
from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.paper.me_long_v2_inverse_short_v1 import (
    capture_with_status,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_v1.market_data import (
    STATUS_OK,
    MarketDataSnapshot,
)
from app.paper.me_long_v2_inverse_short_v1.protocol import (
    EXPERIMENT_ID,
    ROUTE_PRE_FILTER_SOURCE,
)
from app.paper.me_long_v2_inverse_short_v1.production_runtime import (
    EXIT_OK,
    ProductionRuntime,
)
from app.paper.me_long_v2_inverse_short_v1.store import connect_pg_store
from app.scanners.models import SetupCandidate, SetupState
from conftest import connect_test_db

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TEST_DB_HOST") or os.environ.get("TEST_DB_UNIX_SOCK")),
    reason="no test PostgreSQL configured (set TEST_DB_HOST / TEST_DB_UNIX_SOCK)",
)

FREEZE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIGNAL_AT = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    "085_me_long_v2_inverse_short_v1.sql",
    "086_me_long_v2_inverse_short_v1_close_outbox.sql",
)

POSITION_TABLE = "dds.me_long_v2_inverse_short_v1_position"
EVENT_TABLE = "dds.me_long_v2_inverse_short_v1_event"
OUTBOX_TABLE = "dds.me_long_v2_inverse_short_v1_close_outbox"
ACTIVATION_TABLE = "dds.me_long_v2_inverse_short_v1_activation"


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + timedelta(**kwargs)


def _settings() -> Settings:
    return dataclasses.replace(
        Settings(),
        initial_balance=10_000.0,
        risk_per_trade=0.005,
        max_symbol_exposure=0.20,
        max_open_positions=3,
        taker_fee=0.00055,
        slippage_percent=0.0005,
        paper_funding_interval_hours=8,
        setup_ttl_multiplier=2.0,
    )


def _candidate(symbol: str = "BTCUSDT") -> SetupCandidate:
    candle_ms = int(SIGNAL_AT.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        scanner_version="1.0.0",
        symbol=symbol,
        direction="LONG",
        entry_timeframe="5m",
        detected_at=SIGNAL_AT,
        setup_started_at=SIGNAL_AT,
        signal_candle_open_time=candle_ms,
        reference_price=100.0,
        entry_zone_low=99.8,
        entry_zone_high=101.0,
        invalidation_price=97.5,
        target_1=103.0,
        score=45.0,
        market_regime="RANGE",
        state=SetupState.SETUP_READY,
        features={"exhaustion_magnitude": 0.01},
    )


class FakeProvider:
    """Scripted market data (same duck-type as the unit suite)."""

    def __init__(self, prices, *, price_status=None) -> None:
        self.prices = dict(prices)
        self.price_status = dict(price_status or {})
        self.calls: list[dict] = []

    def snapshot(self, *, price_symbols, bar_symbols=(), funding_symbols=()):
        price_symbols, bar_symbols, funding_symbols = (
            set(price_symbols), set(bar_symbols), set(funding_symbols),
        )
        self.calls.append({"price_symbols": set(price_symbols)})
        statuses = {
            symbol: {"price": "MISSING", "bar": "MISSING", "funding": "MISSING"}
            for symbol in (price_symbols | bar_symbols | funding_symbols)
        }
        prices: dict[str, float] = {}
        ages: dict[str, int] = {}
        for symbol in price_symbols:
            status = self.price_status.get(
                symbol, STATUS_OK if symbol in self.prices else "MISSING",
            )
            statuses[symbol]["price"] = status
            if status == STATUS_OK and symbol in self.prices:
                prices[symbol] = float(self.prices[symbol])
                ages[symbol] = 0
        return MarketDataSnapshot(
            observed_at=datetime.now(timezone.utc),
            prices=prices,
            bars={},
            funding_rates_percent={},
            price_age_ms=ages,
            statuses=statuses,
            api_errors=0,
        )


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _guard_not_production(conn) -> None:
    cursor = conn.cursor()
    cursor.execute("SELECT current_database()")
    database = cursor.fetchone()[0]
    forbidden = os.environ.get("PRODUCTION_DB_NAME", "trad_bot")
    if database == forbidden and os.environ.get("TEST_DB_ALLOW_PRODUCTION") != "1":
        conn.rollback()
        pytest.skip(
            f"refusing to run integration writes against database "
            f"{database!r} (production name guard)",
        )
    conn.rollback()


def _table_exists(conn, table: str) -> bool:
    cursor = conn.cursor()
    cursor.execute("SELECT to_regclass(%s)", (table,))
    row = cursor.fetchone()
    conn.rollback()
    return bool(row and row[0])


def _apply_migrations(conn) -> None:
    """Apply the contour migrations through the TEST connection itself.

    No ``psql`` binary is required (it is often absent on dev machines and
    containers): migrations 085/086 are plain SQL without psql
    meta-commands, so each file runs as one transaction on the isolated
    test database.  Any failure is raised and the fixture skips honestly.
    """
    for name in MIGRATIONS:
        path = REPO_ROOT / "sql" / "migrations" / name
        sql = path.read_text(encoding="utf-8")
        cursor = conn.cursor()
        cursor.execute(sql)
        conn.commit()


@pytest.fixture
def pg_conn():
    try:
        conn = connect_test_db()
    except Exception as exc:  # noqa: BLE001 - no local server
        pytest.skip(f"test PostgreSQL unreachable: {exc}")
    _guard_not_production(conn)

    cursor = conn.cursor()
    cursor.execute("CREATE SCHEMA IF NOT EXISTS dds")
    conn.commit()

    if not _table_exists(conn, POSITION_TABLE):
        try:
            _apply_migrations(conn)
        except Exception as exc:  # noqa: BLE001 - psql missing/broken
            conn.close()
            pytest.skip(f"contour tables missing; migrations not applied: {exc}")
        if not _table_exists(conn, POSITION_TABLE):
            conn.close()
            pytest.skip("contour tables still missing after migration attempt")

    yield conn
    try:
        _cleanup(conn)
        _restore_activation(conn, _original_activation)
    finally:
        conn.close()


_original_activation: tuple | None = None


def _cleanup(conn) -> None:
    cursor = conn.cursor()
    for table in (OUTBOX_TABLE, POSITION_TABLE, EVENT_TABLE):
        cursor.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
    conn.commit()


def _read_activation(conn) -> tuple:
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT status, protocol_fingerprint, freeze_ts "
        f"FROM {ACTIVATION_TABLE} WHERE experiment_id = %s",
        (EXPERIMENT_ID,),
    )
    row = cursor.fetchone()
    conn.rollback()
    if row is None:
        return ("MISSING", None, None)
    return (row[0], row[1], row[2])


def _write_activation(conn, status: str, fingerprint: str | None,
                      freeze_ts: datetime | None) -> None:
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE {ACTIVATION_TABLE} SET status = %s, protocol_fingerprint = %s, "
        f"freeze_ts = %s, updated_at = now() WHERE experiment_id = %s",
        (status, fingerprint, freeze_ts, EXPERIMENT_ID),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise AssertionError("activation row missing in the TEST database")
    conn.commit()


def _restore_activation(conn, original) -> None:
    """Put the TEST activation row back exactly as it was (teardown)."""
    if original is None:
        return
    status, fingerprint, freeze_ts = original
    try:
        if status == "MISSING":
            cursor = conn.cursor()
            cursor.execute(
                f"DELETE FROM {ACTIVATION_TABLE} WHERE experiment_id = %s",
                (EXPERIMENT_ID,),
            )
            conn.commit()
            return
        _write_activation(conn, status, fingerprint, freeze_ts)
    except Exception:  # noqa: BLE001 - teardown must not mask test results
        conn.rollback()


@pytest.fixture
def activation_guard(pg_conn):
    """Remember + restore the TEST activation row around every test."""
    global _original_activation
    _original_activation = _read_activation(pg_conn)
    yield pg_conn


def _connect_store():
    return connect_pg_store(
        host=os.environ.get("TEST_DB_HOST", "localhost"),
        port=int(os.environ.get("TEST_DB_PORT", "5432")),
        database=os.environ.get("TEST_DB_NAME", "trad_bot_migration_test"),
        user=os.environ.get("TEST_DB_USER", "postgres"),
        password=os.environ.get("TEST_DB_PASSWORD", ""),
        initial_balance=10_000.0,
        unix_sock=os.environ.get("TEST_DB_UNIX_SOCK") or None,
    )


def _count(conn, table: str, where: str = "1=1") -> int:
    cursor = conn.cursor()
    cursor.execute(f"SELECT count(*) FROM {table} WHERE {where}")  # noqa: S608
    value = cursor.fetchone()[0]
    conn.rollback()
    return int(value)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestRuntimeOnRealPostgres:
    def test_disabled_runtime_writes_nothing(self, activation_guard,
                                             monkeypatch):
        conn = activation_guard
        _write_activation(conn, "RUNNING", protocol_fingerprint(), FREEZE_TS)
        monkeypatch.delenv(ACTIVATION_FLAG, raising=False)

        clock = _Clock(SIGNAL_AT)
        store = _connect_store()
        try:
            # The event was captured earlier while the flag was ON.
            assert capture_with_status(
                store, _candidate(), capture_route=ROUTE_PRE_FILTER_SOURCE,
                env={ACTIVATION_FLAG: "1"}, clock=clock,
            ) == "CAPTURED"

            provider = FakeProvider(prices={"BTCUSDT": 100.0})
            runtime = ProductionRuntime(
                _settings(), store, provider, clock=clock, interval_s=0.01,
            )
            assert runtime.run(once=True) == EXIT_OK
            assert runtime.last_telemetry["entries"] == 0
            assert runtime.last_telemetry["activation_mode"] == "NONE"
            assert _count(conn, POSITION_TABLE) == 0
            assert _count(conn, EVENT_TABLE, "status = 'CAPTURED'") == 1
            assert _count(conn, OUTBOX_TABLE) == 0
        finally:
            store.close()

    def test_full_cycle_restart_and_exit_on_real_postgres(
        self, activation_guard, monkeypatch,
    ):
        conn = activation_guard
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        _write_activation(conn, "RUNNING", protocol_fingerprint(), FREEZE_TS)

        clock = _Clock(SIGNAL_AT)
        store = _connect_store()
        try:
            # Prospective capture + idempotent duplicate delivery.
            assert capture_with_status(
                store, _candidate(), capture_route=ROUTE_PRE_FILTER_SOURCE,
                env={ACTIVATION_FLAG: "1"}, clock=clock,
            ) == "CAPTURED"
            assert capture_with_status(
                store, _candidate(), capture_route=ROUTE_PRE_FILTER_SOURCE,
                env={ACTIVATION_FLAG: "1"}, clock=clock,
            ) == "DUPLICATE"

            provider = FakeProvider(prices={"BTCUSDT": 100.0})
            runtime = ProductionRuntime(
                _settings(), store, provider, clock=clock, interval_s=0.01,
            )

            # 1) First cycle: exactly ONE committed position row.
            assert runtime.run(once=True) == EXIT_OK
            assert runtime.last_telemetry["entries"] == 1
            assert _count(conn, POSITION_TABLE, "status = 'OPEN'") == 1
            assert _count(conn, EVENT_TABLE, "status = 'ENTERED'") == 1

            # 2) Second cycle: no duplicate entry through the same process.
            assert runtime.run(once=True) == EXIT_OK
            assert runtime.last_telemetry["entries"] == 0
            assert _count(conn, POSITION_TABLE) == 1

            # The production Paper tables are NEVER written by the runtime.
            if _table_exists(conn, "dds.paper_trade"):
                before = _count(conn, "dds.paper_trade")
                assert runtime.run(once=True) == EXIT_OK
                assert _count(conn, "dds.paper_trade") == before
        finally:
            store.close()

        # 3) RESTART on a fresh connection: recovery, still no re-entry.
        store2 = _connect_store()
        try:
            runtime2 = ProductionRuntime(
                _settings(), store2, provider, clock=clock, interval_s=0.01,
            )
            assert runtime2.run(once=True) == EXIT_OK
            assert runtime2.last_telemetry["entries"] == 0
            assert runtime2.last_telemetry["open_positions"] == 1
            assert _count(conn, POSITION_TABLE, "status = 'OPEN'") == 1

            # 4) TP exit: durable terminal facts, outbox replayed/cleared.
            provider.prices["BTCUSDT"] = 89.0
            assert runtime2.run(once=True) == EXIT_OK
            assert runtime2.last_telemetry["exit_reasons"] == ["TAKE_PROFIT_1"]
            assert _count(conn, POSITION_TABLE, "status = 'CLOSED'") == 1
            assert _count(conn, POSITION_TABLE, "status = 'OPEN'") == 0
            assert _count(conn, OUTBOX_TABLE) == 0
            assert runtime2.last_telemetry["durability_risk"] is False
        finally:
            store2.close()

    def test_duplicate_event_cannot_open_a_second_row(self, activation_guard,
                                                      monkeypatch):
        conn = activation_guard
        monkeypatch.setenv(ACTIVATION_FLAG, "1")
        _write_activation(conn, "RUNNING", protocol_fingerprint(), FREEZE_TS)

        clock = _Clock(SIGNAL_AT)
        store = _connect_store()
        try:
            assert capture_with_status(
                store, _candidate(), capture_route=ROUTE_PRE_FILTER_SOURCE,
                env={ACTIVATION_FLAG: "1"}, clock=clock,
            ) == "CAPTURED"
            provider = FakeProvider(prices={"BTCUSDT": 100.0})
            runtime = ProductionRuntime(
                _settings(), store, provider, clock=clock, interval_s=0.01,
            )
            assert runtime.run(once=True) == EXIT_OK
            # Same source event redelivered by a later scan.
            assert capture_with_status(
                store, _candidate(), capture_route=ROUTE_PRE_FILTER_SOURCE,
                env={ACTIVATION_FLAG: "1"}, clock=clock,
            ) == "DUPLICATE"
            assert runtime.run(once=True) == EXIT_OK
            assert _count(conn, POSITION_TABLE) == 1
            assert _count(conn, EVENT_TABLE) == 1
        finally:
            store.close()
