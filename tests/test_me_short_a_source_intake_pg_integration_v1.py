"""Real PostgreSQL integration for the shared READ ONLY transaction.

Issue #208 corrective — review P1 regression tests (items 2 and 3):

* ONE failed statement aborts the WHOLE PostgreSQL transaction. A failure of
  the OPTIONAL research intake read (e.g. before migration 080 is installed)
  must therefore never leave the SHARED read-only transaction aborted and
  break the MANDATORY ``dds.scanner_setup`` transport;
* the writer's CACHED connection must survive a failed ``activation_snapshot``
  (rollback) and capture successfully afterwards WITHOUT a scanner restart.

SAFETY
------
* OPT-IN ONLY: the whole module skips unless ``ME_SHORT_A_PG_INTEGRATION=1``
  is set (fail-closed; nothing ever runs against a database by accident).
* It only ever connects to the configured TEST server
  (``ME_SHORT_A_PG_IT_HOST/PORT/USER``, defaults 127.0.0.1:5432 postgres) and
  creates + drops its OWN scratch database. It never writes to any existing
  database and never touches production data, the frozen OOS cohort, Phase 1
  or the Issue #208 capture tables.
* The scratch database is created with
  ``default_transaction_read_only = on`` (mirroring the production runtime
  ROLE-level GUC, which survives ROLLBACK), so the reader session under test
  is read-only by construction.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

psycopg2 = pytest.importorskip("psycopg2")
pg8000_dbapi = pytest.importorskip("pg8000.dbapi")

import test_me_short_a_forward_outcome_runtime_v1 as base  # noqa: E402

from app.research import me_short_a_source_intake_v1 as writer  # noqa: E402
from app.research.me_short_a_forward_outcome_runtime_sources_v1 import (  # noqa: E402
    CompositeCandidateSource,
    DbScannerSetupCandidateSource,
    DbSourceIntakeCandidateSource,
    assert_read_only_session,
)

HOST = os.environ.get("ME_SHORT_A_PG_IT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ME_SHORT_A_PG_IT_PORT", "5432"))
USER = os.environ.get("ME_SHORT_A_PG_IT_USER", "postgres")
MIGRATION_080 = (
    Path(__file__).resolve().parents[1]
    / "sql" / "migrations" / "080_me_short_a_source_intake_v1.sql"
)
FREEZE = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def _server_ok() -> bool:
    if os.environ.get("ME_SHORT_A_PG_INTEGRATION") != "1":
        return False
    try:
        conn = psycopg2.connect(
            host=HOST, port=PORT, database="postgres", user=USER,
            connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_ok(),
    reason="ME_SHORT_A_PG_INTEGRATION != 1 or no test PostgreSQL at "
           f"{HOST}:{PORT}; shared-transaction IT skipped",
)


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def admin():
    """Admin connection to a fresh READ-ONLY-by-default scratch database."""
    name = f"me_short_a_it_{uuid4().hex[:10]}"
    root = psycopg2.connect(
        host=HOST, port=PORT, database="postgres", user=USER, connect_timeout=5)
    root.autocommit = True
    cur = root.cursor()
    cur.execute(f'CREATE DATABASE "{name}"')
    # Mirror the production runtime role: read-only is a SESSION-level
    # default, so it survives ROLLBACK (unlike an in-transaction SET).
    cur.execute(
        f'ALTER DATABASE "{name}" SET default_transaction_read_only = on')
    conn = psycopg2.connect(
        host=HOST, port=PORT, database=name, user=USER, connect_timeout=5)
    conn.autocommit = True
    yield conn
    conn.close()
    try:
        cur.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    except Exception:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s", (name,))
        cur.execute(f'DROP DATABASE "{name}"')
    cur.close()
    root.close()


def connect_reader(admin):
    """The runtime's shared READ ONLY connection (both transports use it)."""
    conn = pg8000_dbapi.connect(
        host=HOST, port=PORT, database=admin.info.dbname, user=USER, timeout=5)
    assert_read_only_session(conn)     # exactly the production pre-flight
    return conn


def connect_writer(admin):
    """The scanner's cached writer connection (read-write session)."""
    conn = pg8000_dbapi.connect(
        host=HOST, port=PORT, database=admin.info.dbname, user=USER, timeout=5)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET default_transaction_read_only = off")
    cur.close()
    conn.autocommit = False
    return conn


def create_dds_minimal(admin):
    cur = admin.cursor()
    cur.execute("CREATE SCHEMA dds")
    cur.execute(
        "CREATE TABLE dds.instrument ("
        " instrument_id bigint PRIMARY KEY, symbol text NOT NULL)")
    cur.execute(
        "CREATE TABLE dds.scanner_setup ("
        " setup_id text PRIMARY KEY, instrument_id bigint NOT NULL,"
        " detected_at timestamptz NOT NULL, reference_price numeric NOT NULL,"
        " invalidation_price numeric NOT NULL, target_1 numeric NOT NULL,"
        " entry_zone_low numeric, entry_zone_high numeric,"
        " signal_candle_open_time timestamptz, features text,"
        " market_regime text, status text, scanner_name text,"
        " direction text)")
    cur.execute(
        "INSERT INTO dds.instrument (instrument_id, symbol) "
        "VALUES (1, 'BTCUSDT')")
    cur.execute(
        "INSERT INTO dds.scanner_setup VALUES ("
        " 'dds-1', 1, %s, 342.10, 342.78, 338.0, 341.9, 342.2,"
        " NULL, '{\"trigger_5m_close\": 341.0}', 'TREND_START',"
        " 'READY_TO_TRADE', 'MOMENTUM_EXHAUSTION', 'SHORT')",
        (datetime(2026, 10, 27, 13, 0, 0, tzinfo=timezone.utc),))
    cur.close()


def activate_intake(admin, freeze_ts):
    """The runbook activation transition, with a past freeze so test events
    satisfy both ``signal_time > intake_freeze_ts`` and
    ``captured_at >= signal_time``."""
    cur = admin.cursor()
    cur.execute(
        "UPDATE research.me_short_a_source_intake_activation_v1 "
        "SET status = 'RUNNING', intake_freeze_ts = %s "
        "WHERE experiment_id = 'ME_SHORT_A_PROSPECTIVE_SOURCE_INTAKE_V1' "
        "AND status = 'READY_TO_START'", (freeze_ts,))
    cur.close()


def build_composite(conn):
    return CompositeCandidateSource((
        DbScannerSetupCandidateSource(conn, base.InMemoryCandleSource([])),
        DbSourceIntakeCandidateSource(conn, base.InMemoryCandleSource([])),
    ))


# ── P1-2: the optional intake read must never break dds ─────────────


def test_missing_intake_schema_never_aborts_the_shared_dds_stream(admin):
    """Before migration 080 the intake reads fail with a real SQL error. The
    shared READ ONLY transaction must be left usable: the dds transport is
    served regardless and later fetches keep working on the SAME connection
    (no reconnect, no scanner restart)."""
    create_dds_minimal(admin)
    conn = connect_reader(admin)
    try:
        sources = build_composite(conn)

        # Research schema does not exist yet — the intake read raises, must
        # fail soft AND un-abort the shared transaction for dds.
        page = sources.fetch_page(None, freeze_ts=FREEZE)
        assert [c.symbol for c in page.candidates] == ["BTCUSDT"]

        # The transaction is still usable: the next fetch runs fine.
        again = sources.fetch_page(page.next_cursor, freeze_ts=FREEZE)
        assert again.rows_read == 0

        # Migration 080 arrives mid-session: the SAME connection keeps
        # serving both transports (intake: fail-closed empty until RUNNING).
        admin.execute(MIGRATION_080.read_text(encoding="utf-8"))
        activate_intake(admin, datetime.now(timezone.utc) - timedelta(minutes=5))
        later = sources.fetch_page(again.next_cursor, freeze_ts=FREEZE)
        assert later.rows_read == 0          # no rows yet, but no abort state
    finally:
        conn.close()


# ── P1-3: the cached writer connection survives activation errors ────


def test_writer_recovers_after_pre_migration_failure_without_restart(admin):
    """A capture attempted BEFORE migration 080 is installed fails open and
    rolls back the CACHED writer connection; the very next capture on the
    SAME connection succeeds once the migration is applied — no scanner
    restart — and the row round-trips through the read-only intake source
    with the frozen event identity intact."""
    create_dds_minimal(admin)
    conn = connect_writer(admin)
    try:
        symbol = "TSLAUSDT"
        ref, stop, target = Decimal("342.10"), Decimal("342.78"), Decimal("338.0")
        signal_time = datetime.now(timezone.utc) - timedelta(minutes=1)

        # 1) Before migration 080: the activation read raises UndefinedTable.
        status = writer.capture_with_status(
            conn,
            scanner_name="MOMENTUM_EXHAUSTION", direction="SHORT",
            symbol=symbol, signal_time=signal_time,
            reference_price=ref, invalidation_price=stop, target_1=target,
            capture_route="REGIME_FILTER_REJECTED",
            blocked_stage="regime_filter", blocked_reason="SHORT in TREND_UP",
            market_regime="TREND_UP",
            signal_candle_open_time=(
                signal_time - timedelta(minutes=5)).timestamp() * 1000,
        )
        assert status == "ERROR"

        # 2) Migration 080 + activation; the SAME cached connection captures.
        admin.execute(MIGRATION_080.read_text(encoding="utf-8"))
        activate_intake(admin, datetime.now(timezone.utc) - timedelta(minutes=5))
        status = writer.capture_with_status(
            conn,
            scanner_name="MOMENTUM_EXHAUSTION", direction="SHORT",
            symbol=symbol, signal_time=signal_time,
            reference_price=ref, invalidation_price=stop, target_1=target,
            capture_route="REGIME_FILTER_REJECTED",
            blocked_stage="regime_filter", blocked_reason="SHORT in TREND_UP",
            market_regime="TREND_UP",
            signal_candle_open_time=(
                signal_time - timedelta(minutes=5)).timestamp() * 1000,
        )
        assert status == "CAPTURED"

        # 3) Idempotent replay on the same connection.
        status = writer.capture_with_status(
            conn,
            scanner_name="MOMENTUM_EXHAUSTION", direction="SHORT",
            symbol=symbol, signal_time=signal_time,
            reference_price=ref, invalidation_price=stop, target_1=target,
            capture_route="REGIME_FILTER_REJECTED",
            blocked_stage="regime_filter", blocked_reason="SHORT in TREND_UP",
            market_regime="TREND_UP",
            signal_candle_open_time=(
                signal_time - timedelta(minutes=5)).timestamp() * 1000,
        )
        assert status == "DUPLICATE"

        # 4) The row is visible through the READ ONLY intake source with the
        #    frozen identity intact (round-trip through real PostgreSQL).
        reader = connect_reader(admin)
        try:
            sources = CompositeCandidateSource((
                DbSourceIntakeCandidateSource(
                    reader, base.InMemoryCandleSource([])),
                DbScannerSetupCandidateSource(
                    reader, base.InMemoryCandleSource([])),
            ))
            page = sources.fetch_page(None, freeze_ts=FREEZE)
            candidates = [c for c in page.candidates if c.symbol == symbol]
            assert len(candidates) == 1
            expected_key = writer.compute_event_key(
                symbol, signal_time, ref, stop, target)
            from app.research.me_short_a_forward_outcome_runtime_v1 import (
                event_key_for)
            assert event_key_for(candidates[0]) == expected_key
        finally:
            reader.close()
    finally:
        conn.close()
