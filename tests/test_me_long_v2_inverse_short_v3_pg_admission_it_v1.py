"""ME_LONG_V2_INVERSE_SHORT_V3 — PostgreSQL admission IT (Issue #285).

Static SQL-contract tests run everywhere; the PostgreSQL integration tests
are gated on ``TEST_DB_HOST`` / ``TEST_DB_UNIX_SOCK`` (the repository-wide
integration convention) and skip cleanly without a test server.

Proven against real PostgreSQL (the in-memory suite cannot prove these):

* the FIXED admission lock order (advisory -> event -> activation) is
  deadlock-free and serializes concurrent admissions;
* two CONCURRENT admissions of one symbol (different event_keys) insert
  exactly ONE row — the loser is terminally disposed SYMBOL_REJECTED /
  SYMBOL_ALREADY_OPEN with reason + timestamp, in the same transaction;
* the V2+V3 OPEN cap (12) and the 12h per-symbol cooldown are judged at
  ``statement_timestamp()`` (DB time) — exactly 12h is admissible;
* V2 rows are READ-ONLY: identical before/after every V3 admission;
* fail-closed: an unreadable or still-admitting V2 activation blocks NEW
  entries and never disposes the event (the TTL sweep owns its fate);
* ambiguous COMMIT outcomes resolve to ADOPT of the committed row and are
  never classified as a refusal;
* migration 090 is idempotent (CREATE ... IF NOT EXISTS).
"""
from __future__ import annotations

import hashlib
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import connect_test_db

from app.config import Settings
from app.paper.me_long_v2_inverse_short_v3.activation import (
    ACTIVATION_FLAG,
    evaluate_activation,
)
from app.paper.me_long_v2_inverse_short_v3.capture import (
    CAPTURED,
    capture_with_status,
)
from app.paper.me_long_v2_inverse_short_v3.identity import position_key
from app.paper.me_long_v2_inverse_short_v3.models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_ENTERED,
    EVENT_STATUS_SYMBOL_REJECTED,
    EVENT_REASON_ENTRY_CAP,
    EVENT_REASON_SYMBOL_ALREADY_OPEN,
    EVENT_REASON_SYMBOL_COOLDOWN,
    ActivationRecord,
    InverseShortPosition,
)
from app.paper.me_long_v2_inverse_short_v3.protocol import (
    ADMISSION_LOCK_KEY,
    ADMISSION_LOCK_NAMESPACE,
    ENTRY_MAX_AGE_MINUTES,
    EXPERIMENT_ID,
    MAX_OPEN_POSITIONS_TOTAL,
    REFUSAL_ENTRY_CAP,
    REFUSAL_ENTRY_TTL,
    REFUSAL_SYMBOL_ALREADY_OPEN,
    REFUSAL_SYMBOL_COOLDOWN,
    SYMBOL_ENTRY_COOLDOWN_HOURS,
    protocol_fingerprint,
    short_geometry,
)
from app.paper.me_long_v2_inverse_short_v3.store import (
    ADMIT_ADOPT,
    ADMIT_DEFERRED,
    ADMIT_INSERTED,
    ADMIT_NOT_ADMISSIBLE,
    ADMIT_REFUSED_TERMINAL,
    ADMISSION_DECISION_SQL,
    ADMISSION_LOCK_SQL,
    CROSS_CONTOUR_STATE_SQL,
    INSERT_POSITION_SQL,
    MARK_EVENT_EXPIRED_SQL,
    V2_STATUS_FOR_ADMISSION_SQL,
    connect_pg_store,
)

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_087 = (
    ROOT / "sql" / "migrations" / "087_me_long_v2_inverse_short_v2.sql")
MIGRATION_090 = (
    ROOT / "sql" / "migrations" / "090_me_long_v2_inverse_short_v3.sql")

ACTIVATION_TABLE = "dds.me_long_v2_inverse_short_v3_activation"
EVENT_TABLE = "dds.me_long_v2_inverse_short_v3_event"
POSITION_TABLE = "dds.me_long_v2_inverse_short_v3_position"
CLOSE_OUTBOX_TABLE = "dds.me_long_v2_inverse_short_v3_close_outbox"
V2_ACTIVATION_TABLE = "dds.me_long_v2_inverse_short_v2_activation"
V2_POSITION_TABLE = "dds.me_long_v2_inverse_short_v2_position"
V2_EVENT_TABLE = "dds.me_long_v2_inverse_short_v2_event"

V2_EXPERIMENT_ID = "ME_LONG_V2_INVERSE_SHORT_V2"


# ---------------------------------------------------------------------------
# 1. Static SQL contract (runs without any database)
# ---------------------------------------------------------------------------
class TestAdmissionSqlContract:
    def test_admission_lock_is_one_deterministic_advisory_lock(self):
        assert ADMISSION_LOCK_SQL == "SELECT pg_advisory_xact_lock(%s)"
        expected = int(
            hashlib.sha256(
                ADMISSION_LOCK_NAMESPACE.encode("utf-8")).hexdigest()[:16],
            16,
        )
        assert ADMISSION_LOCK_KEY == expected
        assert ADMISSION_LOCK_NAMESPACE == "ME_LONG_V2_INVERSE_SHORT_V3_ADMISSION"

    def test_insert_matches_the_frozen_v2_guarded_insert(self):
        """No V2 regression (Issue #285 acceptance): the V3 guarded INSERT is
        the V2 one with identity renamed — same gates, same lock, same shape."""
        from app.paper.me_long_v2_inverse_short_v2 import store as v2store

        def _normalize(sql: str) -> str:
            return (
                sql.replace("me_long_v2_inverse_short_v2_", "me_long_v2_inverse_short_vX_")
                   .replace("me_long_v2_inverse_short_v3_", "me_long_v2_inverse_short_vX_")
                   .replace("ME_LONG_V2_INVERSE_SHORT_V2", "ME_LONG_V2_INVERSE_SHORT_VX")
                   .replace("ME_LONG_V2_INVERSE_SHORT_V3", "ME_LONG_V2_INVERSE_SHORT_VX")
            )

        assert _normalize(v2store.INSERT_POSITION_SQL) == _normalize(
            INSERT_POSITION_SQL)

    def test_decision_gate_order_matches_the_frozen_classification(self):
        order = [
            "'NOT_ADMISSIBLE'",
            f"'{REFUSAL_ENTRY_TTL}'",
            f"'{REFUSAL_SYMBOL_ALREADY_OPEN}'",
            f"'{REFUSAL_SYMBOL_COOLDOWN}'",
            f"'{REFUSAL_ENTRY_CAP}'",
            "'ADMISSIBLE'",
        ]
        positions = [ADMISSION_DECISION_SQL.index(token) for token in order]
        assert positions == sorted(positions), (
            "the ADMISSION_DECISION_SQL gate order must be: NOT_ADMISSIBLE -> "
            "ENTRY_TTL -> SYMBOL_ALREADY_OPEN -> SYMBOL_COOLDOWN_12H -> "
            "ENTRY_CAP -> ADMISSIBLE"
        )

    def test_cooldown_boundary_is_db_time_and_inclusive(self):
        import re as _re

        # Refuse strictly INSIDE the window (entered_at newer than now-12h);
        # equality with the boundary is admissible (the '>' is on the refusal
        # side), judged at statement_timestamp().
        assert _re.search(
            r"entered_at > statement_timestamp\(\)\s*- interval '12 hours'",
            ADMISSION_DECISION_SQL,
        ), ADMISSION_DECISION_SQL
        assert "statement_timestamp()" in ADMISSION_DECISION_SQL

    def test_symbol_guard_covers_both_contours(self):
        assert "p.symbol = ev.symbol AND p.status = 'OPEN'" in (
            ADMISSION_DECISION_SQL.replace("                      ", " "))
        assert V2_POSITION_TABLE in ADMISSION_DECISION_SQL
        assert "p2.symbol = ev.symbol AND p2.status = 'OPEN'" in (
            ADMISSION_DECISION_SQL)

    def test_cap_counts_both_contours(self):
        decision = ADMISSION_DECISION_SQL
        assert (
            f"(SELECT COUNT(*) FROM {POSITION_TABLE} WHERE status = 'OPEN')"
            in decision
        )
        assert (
            f"(SELECT COUNT(*) FROM {V2_POSITION_TABLE} WHERE status = 'OPEN')"
            in decision
        )

    def test_insert_is_guarded_and_v3_only(self):
        """The V2 guarded-insert shape is preserved byte-for-byte in
        structure: state gate with event+activation lock inside the INSERT."""
        assert INSERT_POSITION_SQL.strip().startswith("INSERT")
        assert POSITION_TABLE in INSERT_POSITION_SQL
        assert V2_POSITION_TABLE not in INSERT_POSITION_SQL
        assert "ON CONFLICT (position_key) DO NOTHING" in INSERT_POSITION_SQL
        assert "WHERE EXISTS (" in INSERT_POSITION_SQL
        assert "FOR UPDATE OF ev, act" in INSERT_POSITION_SQL

    def test_terminal_disposal_records_reason_and_timestamp(self):
        assert "status_reason = %s" in MARK_EVENT_EXPIRED_SQL
        assert "status_at = %s" in MARK_EVENT_EXPIRED_SQL
        assert "NOT EXISTS" in MARK_EVENT_EXPIRED_SQL

    def test_guard_values_are_the_frozen_issue285_values(self):
        assert MAX_OPEN_POSITIONS_TOTAL == 12
        assert SYMBOL_ENTRY_COOLDOWN_HOURS == 12
        assert ENTRY_MAX_AGE_MINUTES == 120
        assert ACTIVATION_FLAG == "ME_INVERSE_SHORT_V3_ENABLED"


# ---------------------------------------------------------------------------
# 2. PostgreSQL integration (gated)
# ---------------------------------------------------------------------------
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
    import dataclasses
    return dataclasses.replace(Settings(), **defaults)


def _candidate(symbol: str, *, detected_at: datetime, price: float = 100.0):
    from app.scanners.models import SetupCandidate, SetupState

    candle_ms = int(detected_at.timestamp() // 300) * 300_000
    return SetupCandidate(
        scanner_name="MOMENTUM_EXHAUSTION_REVERSE_LONG_V2",
        scanner_version="1.0.0",
        symbol=symbol,
        direction="LONG",
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
        features={
            "exhaustion_magnitude": 0.01,
            "rsi_14": 70.0,
            "rsi_delta_3": 2.0,
            "entry_confirmation_rsi_rising": True,
        },
    )


def _identity():
    identity, _reason = evaluate_activation(
        ActivationRecord(
            experiment_id=EXPERIMENT_ID,
            status="RUNNING",
            protocol_fingerprint=protocol_fingerprint(),
            freeze_ts=datetime.now(timezone.utc) - timedelta(hours=3),
        ),
        enabled=True,
    )
    assert identity is not None
    return identity


def _build_position(event, *, entered_at: datetime | None = None) -> InverseShortPosition:
    entered = entered_at or datetime.now(timezone.utc)
    stop, target = short_geometry(100.0)
    return InverseShortPosition(
        position_key=position_key(event.event_key),
        event_key=event.event_key,
        symbol=event.symbol,
        direction="SHORT",
        entered_at=entered,
        deadline_at=entered + timedelta(minutes=7200),
        entry_market_price=100.0,
        entry_price=100.0,
        entry_fee=0.055,
        entry_slippage_cost=0.05,
        stop_price=stop,
        target_price=target,
        position_size=0.5,
        risk_usdt=5.0,
        balance_before=10_000.0,
    )


def _capture(store, symbol: str, *, detected_at: datetime | None = None):
    from app.paper.me_long_v2_inverse_short_v3.capture import (
        event_from_candidate,
    )

    now = detected_at or datetime.now(timezone.utc)
    candidate = _candidate(symbol, detected_at=now)
    status = capture_with_status(
        store, candidate,
        env={"ME_INVERSE_SHORT_V3_ENABLED": "1"},
    )
    assert status == CAPTURED, status
    from app.paper.me_long_v2_inverse_short_v3.protocol import ROUTE_PRE_FILTER_SOURCE
    return event_from_candidate(
        candidate,
        capture_route=ROUTE_PRE_FILTER_SOURCE,
        activation=_identity(),
        captured_at=datetime.now(timezone.utc),
    )


def _connect_store():
    return connect_pg_store(
        host=os.environ.get("TEST_DB_HOST", "localhost"),
        port=int(os.environ.get("TEST_DB_PORT", "5432")),
        database=os.environ.get("TEST_DB_NAME", "trad_bot_migration_test"),
        user=os.environ.get("TEST_DB_USER", "postgres"),
        password=os.environ.get("TEST_DB_PASSWORD", ""),
        unix_sock=os.environ.get("TEST_DB_UNIX_SOCK") or None,
        initial_balance=10_000.0,
    )


def _guard_not_production(conn) -> None:
    cursor = conn.cursor()
    cursor.execute("SELECT current_database()")
    database = cursor.fetchone()[0]
    conn.rollback()
    forbidden = os.environ.get("PRODUCTION_DB_NAME", "trad_bot")
    if database == forbidden and os.environ.get("TEST_DB_ALLOW_PRODUCTION") != "1":
        pytest.skip(
            f"refusing to run integration writes against database "
            f"{database!r} (production name guard)"
        )


def _write_activation(conn, table, experiment_id, status, fingerprint, freeze_ts):
    cursor = conn.cursor()
    # UPSERT (not UPDATE): the row may be absent — a prior test may have
    # deleted it to simulate an unreadable peer ledger, and every test must
    # be order-independent on a shared test database.
    cursor.execute(
        f"INSERT INTO {table} (experiment_id, status, protocol_fingerprint, "
        f"freeze_ts) VALUES (%s, %s, %s, %s) "
        f"ON CONFLICT (experiment_id) DO UPDATE SET status = EXCLUDED.status, "
        f"protocol_fingerprint = EXCLUDED.protocol_fingerprint, "
        f"freeze_ts = EXCLUDED.freeze_ts, updated_at = now()",
        (experiment_id, status, fingerprint, freeze_ts),
    )
    assert cursor.rowcount == 1, f"activation row missing: {experiment_id}"
    conn.commit()


def _read_activation(conn, table, experiment_id):
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT status, protocol_fingerprint, freeze_ts FROM {table} "
        f"WHERE experiment_id = %s",
        (experiment_id,),
    )
    row = cursor.fetchone()
    conn.rollback()
    return row  # None or (status, fingerprint, freeze)


def _seed_v2_activation(conn, status: str = "DRAINING") -> None:
    _write_activation(
        conn, V2_ACTIVATION_TABLE, V2_EXPERIMENT_ID, status,
        "0" * 64, datetime.now(timezone.utc) - timedelta(hours=3),
    )


def _seed_v2_position(conn, symbol: str, entered_at: datetime) -> str:
    """Seed ONE V2 source event + its OPEN position row (test seed)."""
    cursor = conn.cursor()
    key = f"ME_LONG_V2_INVERSE_SHORT_V2:seed-{symbol}-{entered_at.isoformat()}"
    event_key = f"seed-event-{symbol}-{entered_at.isoformat()}"
    cursor.execute(
        f"""
        INSERT INTO {V2_EVENT_TABLE} (
            event_key, protocol_version, protocol_hash, source_scanner,
            source_direction, symbol, signal_time, captured_at,
            activation_freeze_ts, signal_candle_open_time, capture_route,
            status
        ) VALUES (%s, '2', %s, 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V2',
                  'LONG', %s, %s, %s, %s, 300000, 'PRE_FILTER_SOURCE',
                  'CAPTURED')
        ON CONFLICT (event_key) DO NOTHING
        """,
        (
            event_key, "0" * 64, symbol,
            entered_at - timedelta(hours=1), entered_at - timedelta(hours=1),
            entered_at - timedelta(hours=2),
        ),
    )
    cursor.execute(
        f"""
        INSERT INTO {V2_POSITION_TABLE} (
            position_key, event_key, symbol, direction, entry_market_price,
            entry_price, stop_price, target_price, position_size,
            balance_before, entered_at, deadline_at, status
        ) VALUES (%s, %s, %s, 'SHORT', 100.0, 100.0, 110.0, 90.0, 0.5,
                  10000.0, %s, %s, 'OPEN')
        ON CONFLICT (position_key) DO NOTHING
        """,
        (key, event_key, symbol, entered_at, entered_at + timedelta(minutes=7200)),
    )
    conn.commit()
    return key


def _cleanup(conn) -> None:
    cursor = conn.cursor()
    for table in (CLOSE_OUTBOX_TABLE, POSITION_TABLE, EVENT_TABLE):
        cursor.execute(f"DELETE FROM {table}")
    cursor.execute(
        f"DELETE FROM {V2_POSITION_TABLE} WHERE position_key LIKE "
        f"'ME_LONG_V2_INVERSE_SHORT_V2:seed-%'"
    )
    cursor.execute(
        f"DELETE FROM {V2_EVENT_TABLE} WHERE event_key LIKE 'seed-event-%'"
    )
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
    # V2 tables first (cross-contour subqueries read them), then 090.
    cursor.execute("SELECT to_regclass(%s)", (V2_EVENT_TABLE,))
    if not cursor.fetchone()[0]:
        try:
            cursor.execute(MIGRATION_087.read_text(encoding="utf-8"))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.close()
            pytest.skip(f"migration 087 not applicable: {exc}")
    cursor.execute("SELECT to_regclass(%s)", (EVENT_TABLE,))
    if not cursor.fetchone()[0]:
        try:
            cursor.execute(MIGRATION_090.read_text(encoding="utf-8"))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.close()
            pytest.skip(f"migration 090 not applicable: {exc}")

    original_v3 = _read_activation(conn, ACTIVATION_TABLE, EXPERIMENT_ID)
    original_v2 = _read_activation(conn, V2_ACTIVATION_TABLE, V2_EXPERIMENT_ID)
    yield conn
    try:
        _cleanup(conn)
        for table, experiment_id, original in (
            (ACTIVATION_TABLE, EXPERIMENT_ID, original_v3),
            (V2_ACTIVATION_TABLE, V2_EXPERIMENT_ID, original_v2),
        ):
            if original is not None:
                _write_activation(conn, table, experiment_id, *original)
    finally:
        conn.close()


@pytest.fixture
def running(pg_conn):
    """V3 activation = RUNNING (pinned fingerprint, fresh freeze);
    V2 activation = DRAINING (the safe-drain precondition)."""
    now = datetime.now(timezone.utc)
    _write_activation(
        pg_conn, ACTIVATION_TABLE, EXPERIMENT_ID, "RUNNING",
        protocol_fingerprint(), now - timedelta(hours=3),
    )
    _seed_v2_activation(pg_conn, "DRAINING")
    return now


@pytest.fixture
def store(running):
    store = _connect_store()
    yield store
    store.close()


class TestPgAdmission:
    def test_migration_090_is_idempotent(self, pg_conn):
        cursor = pg_conn.cursor()
        cursor.execute(MIGRATION_090.read_text(encoding="utf-8"))
        pg_conn.commit()  # CREATE ... IF NOT EXISTS -> clean re-apply

    def test_unique_open_symbol_index_enforced_by_postgres(self, pg_conn, store):
        first = _capture(store, "IDXUSDT", detected_at=datetime.now(timezone.utc)
                         - timedelta(minutes=30))
        assert store.admit_position(_build_position(first)).outcome == ADMIT_INSERTED
        # A DIRECT second OPEN row for the same symbol is impossible even
        # bypassing admit_position entirely (different event_key, own key):
        second = _capture(store, "IDXUSDT", detected_at=datetime.now(timezone.utc)
                          - timedelta(minutes=25))
        position = _build_position(second)
        cursor = pg_conn.cursor()
        with pytest.raises(Exception) as excinfo:
            cursor.execute(
                f"""
                INSERT INTO {POSITION_TABLE} (
                    position_key, event_key, symbol, direction,
                    entry_market_price, entry_price, stop_price, target_price,
                    position_size, balance_before, entered_at, deadline_at,
                    status
                ) VALUES (%s, %s, %s, 'SHORT', 100.0, 100.0, 110.0, 90.0,
                          0.5, 10000.0, %s, %s, 'OPEN')
                """,
                (
                    position.position_key + ":dup", second.event_key,
                    "IDXUSDT", position.entered_at, position.deadline_at,
                ),
            )
        pg_conn.rollback()
        assert "uq_me_inverse_short_v3_position_symbol_open" in str(
            excinfo.value), excinfo.value

    def test_concurrent_same_symbol_admissions_insert_exactly_one(self, pg_conn, running):
        now = datetime.now(timezone.utc)
        events = [
            _capture(_connect_store(), "RACEUSDT", detected_at=now - timedelta(minutes=30)),
            _capture(_connect_store(), "RACEUSDT", detected_at=now - timedelta(minutes=25)),
        ]
        # Distinct event_keys (different signal candles).
        assert events[0].event_key != events[1].event_key

        results = []
        lock = threading.Lock()

        def _admit(event):
            thread_store = _connect_store()
            try:
                decision = thread_store.admit_position(_build_position(event))
                with lock:
                    results.append((event, decision))
            finally:
                thread_store.close()

        threads = [threading.Thread(target=_admit, args=(e,)) for e in events]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        outcomes = sorted(d.outcome for _e, d in results)
        assert outcomes == sorted([ADMIT_INSERTED, ADMIT_REFUSED_TERMINAL])
        # Exactly one row, exactly one fee entry.
        cursor = pg_conn.cursor()
        cursor.execute(
            f"SELECT COUNT(*) FROM {POSITION_TABLE} WHERE symbol = 'RACEUSDT'")
        assert cursor.fetchone()[0] == 1
        # The loser event is TERMINALLY disposed with its reason + timestamp.
        cursor.execute(
            f"SELECT status, status_reason, status_at FROM {EVENT_TABLE} "
            f"WHERE event_key = %s",
            (next(e.event_key for e, d in results
                  if d.outcome == ADMIT_REFUSED_TERMINAL),),
        )
        status, reason, status_at = cursor.fetchone()
        pg_conn.rollback()
        assert status == EVENT_STATUS_SYMBOL_REJECTED
        assert reason == EVENT_REASON_SYMBOL_ALREADY_OPEN
        assert status_at is not None

    def test_cap_counts_v2_rows_and_v2_stays_untouched(self, pg_conn, store, running):
        now = datetime.now(timezone.utc)
        v2_keys = [
            _seed_v2_position(pg_conn, f"CAP{i}USDT", now - timedelta(hours=20))
            for i in range(5)
        ]
        admitted = 0
        for i in range(8):
            # Distinct symbols from the V2 seeds: these events test the CAP
            # gate (not the symbol guard) over 5 OPEN V2 + 7 OPEN V3 rows.
            event = _capture(store, f"VCAP{i}USDT")
            decision = store.admit_position(_build_position(event))
            if decision.outcome == ADMIT_INSERTED:
                admitted += 1
            else:
                assert decision.outcome == ADMIT_DEFERRED
                assert decision.reason == REFUSAL_ENTRY_CAP
        assert admitted == 7  # 5 V2 + 7 V3 = 12

        cursor = pg_conn.cursor()
        cursor.execute(
            f"SELECT position_key, status, entered_at FROM {V2_POSITION_TABLE} "
            f"WHERE position_key LIKE 'ME_LONG_V2_INVERSE_SHORT_V2:seed-%'")
        rows = cursor.fetchall()
        pg_conn.rollback()
        assert sorted(r[0] for r in rows) == sorted(v2_keys)
        assert all(r[1] == "OPEN" for r in rows)

    def test_cooldown_boundary_judged_at_db_time(self, pg_conn, store, running):
        now = datetime.now(timezone.utc)
        # The seed entry of COOLUSDT: 11h59m old, already CLOSED — the
        # cooldown runs from the accepted ENTRY, never from the close.
        seed_event = _capture(
            store, "COOLUSDT", detected_at=now - timedelta(minutes=35))
        cursor = pg_conn.cursor()
        cursor.execute(
            f"""
            INSERT INTO {POSITION_TABLE} (
                position_key, event_key, symbol, direction, entry_market_price,
                entry_price, stop_price, target_price, position_size,
                balance_before, entered_at, deadline_at, status, closed_at,
                exit_reason
            ) VALUES (%s, %s, 'COOLUSDT', 'SHORT', 100.0, 100.0, 110.0, 90.0,
                      0.5, 10000.0,
                      now() - interval '11 hours 59 minutes',
                      now() - interval '5 hours', 'CLOSED',
                      now() - interval '5 hours', 'TIMEOUT')
            """,
            ("ME_LONG_V2_INVERSE_SHORT_V3:seed-cool-1", seed_event.event_key),
        )
        pg_conn.commit()

        event = _capture(
            store, "COOLUSDT", detected_at=now - timedelta(minutes=30))
        decision = store.admit_position(_build_position(event))
        assert decision.outcome == ADMIT_REFUSED_TERMINAL
        assert decision.reason == REFUSAL_SYMBOL_COOLDOWN

        # The blocked event is terminal with its reason recorded …
        cursor.execute(
            f"SELECT status, status_reason, status_at FROM {EVENT_TABLE} "
            f"WHERE event_key = %s", (event.event_key,))
        status, reason, status_at = cursor.fetchone()
        pg_conn.rollback()
        assert status == EVENT_STATUS_SYMBOL_REJECTED
        assert reason == EVENT_REASON_SYMBOL_COOLDOWN
        assert status_at is not None

        # … and does NOT extend the timer: at exactly 12h the boundary is
        # admissible (>=), judged at statement_timestamp().  (The seed row's
        # entered_at is immutable by the migration-090 guard trigger, so the
        # boundary case is re-seeded instead of updated.)
        cursor.execute(
            f"DELETE FROM {POSITION_TABLE} WHERE position_key = %s",
            ("ME_LONG_V2_INVERSE_SHORT_V3:seed-cool-1",),
        )
        cursor.execute(
            f"""
            INSERT INTO {POSITION_TABLE} (
                position_key, event_key, symbol, direction, entry_market_price,
                entry_price, stop_price, target_price, position_size,
                balance_before, entered_at, deadline_at, status, closed_at,
                exit_reason
            ) VALUES (%s, %s, 'COOLUSDT', 'SHORT', 100.0, 100.0, 110.0, 90.0,
                      0.5, 10000.0,
                      now() - interval '12 hours',
                      now() - interval '2 hours', 'CLOSED',
                      now() - interval '2 hours', 'TIMEOUT')
            """,
            ("ME_LONG_V2_INVERSE_SHORT_V3:seed-cool-2", seed_event.event_key),
        )
        pg_conn.commit()
        fresh = _capture(
            store, "COOLUSDT", detected_at=now - timedelta(minutes=25))
        decision = store.admit_position(_build_position(fresh))
        assert decision.outcome == ADMIT_INSERTED

    def test_v2_unreadable_or_running_fails_closed_for_entries(self, pg_conn, store, running):
        now = datetime.now(timezone.utc)
        # V2 still admitting -> V3 must not admit.
        _seed_v2_activation(pg_conn, "RUNNING")
        event = _capture(
            store, "FDBCUSDT", detected_at=now - timedelta(minutes=30))
        decision = store.admit_position(_build_position(event))
        assert decision.outcome in (ADMIT_DEFERRED, ADMIT_NOT_ADMISSIBLE)
        # Fail closed, not terminal: the event keeps aging under the frozen
        # TTL sweep contract.
        cursor = pg_conn.cursor()
        cursor.execute(
            f"SELECT status FROM {EVENT_TABLE} WHERE event_key = %s",
            (event.event_key,),
        )
        assert cursor.fetchone()[0] == EVENT_STATUS_CAPTURED
        pg_conn.rollback()

        # V2 activation row unreadable (missing) -> fail closed as well.
        cursor.execute(
            f"DELETE FROM {V2_ACTIVATION_TABLE} WHERE experiment_id = %s",
            (V2_EXPERIMENT_ID,),
        )
        pg_conn.commit()
        try:
            fresh = _capture(
                store, "FDBCUSDT", detected_at=now - timedelta(minutes=25))
            decision = store.admit_position(_build_position(fresh))
            assert decision.outcome in (ADMIT_DEFERRED, ADMIT_NOT_ADMISSIBLE)
        finally:
            # Restore the peer row so the suite stays order-independent.
            _seed_v2_activation(pg_conn, "DRAINING")

    def test_ambiguous_commit_resolves_to_adoption(self, pg_conn, store, running):
        event = _capture(store, "ADOPTUSDT")
        position = _build_position(event)
        first = store.admit_position(position)
        second = store.admit_position(position)
        assert first.outcome == ADMIT_INSERTED
        assert second.outcome == ADMIT_ADOPT

    def test_cross_contour_state_counts_both_contours(self, pg_conn, store, running):
        _seed_v2_position(
            pg_conn, "STATEUSDT",
            datetime.now(timezone.utc) - timedelta(hours=20),
        )
        event = _capture(store, "OPENUSDT")
        assert store.admit_position(_build_position(event)).outcome == ADMIT_INSERTED
        state = store.cross_contour_state()
        assert state.available is True
        assert state.v3_open == 1
        assert state.v2_open == 1
        assert state.v2_status == "DRAINING"
        assert state.open_total == 2
        assert state.v2_no_new_admissions is True

    def test_p1_adopt_resolves_even_at_full_capacity(self, pg_conn, store, running):
        """Codex P1 (PG): a committed row resolves to ADOPT_EXISTING even
        when every one of the 12 slots is taken — a committed position is
        never a guard refusal and never re-books fees."""
        now = datetime.now(timezone.utc)
        positions = []
        for i in range(12):
            event = _capture(
                store, f"ADOPT{i}USDT",
                detected_at=now - timedelta(minutes=30 - i))
            position = _build_position(event)
            decision = store.admit_position(position)
            assert decision.outcome == ADMIT_INSERTED
            positions.append(position)
        cursor = pg_conn.cursor()
        cursor.execute(
            f"SELECT COUNT(*) FROM {POSITION_TABLE} WHERE status = 'OPEN'")
        assert cursor.fetchone()[0] == 12
        pg_conn.rollback()

        # The first admission stands for a lost COMMIT acknowledgement:
        # re-admitting its position must resolve to ADOPT even though the
        # book is FULL (never REFUSED_*, never DEFERRED).
        decision = store.admit_position(positions[0])
        assert decision.outcome == ADMIT_ADOPT
        cursor.execute(
            f"SELECT COUNT(*) FROM {POSITION_TABLE} WHERE status = 'OPEN'")
        assert cursor.fetchone()[0] == 12
        pg_conn.rollback()

    def test_p2_terminal_symbol_guards_run_at_full_capacity(self, pg_conn, store, running):
        """Codex P2 (PG): SYMBOL_ALREADY_OPEN / SYMBOL_COOLDOWN_12H dispose
        pending events TERMINALLY and atomically even at the 12 cap; only
        guard-eligible signals may wait for a free slot (DEFERRED) until TTL."""
        now = datetime.now(timezone.utc)
        for i in range(5):
            _seed_v2_position(
                pg_conn, f"FULL{i}USDT", now - timedelta(hours=13))
        for i in range(7):
            event = _capture(
                store, f"FULLV{i}USDT",
                detected_at=now - timedelta(minutes=30 - i))
            assert store.admit_position(
                _build_position(event)).outcome == ADMIT_INSERTED
        assert store.cross_contour_state().open_total == 12  # FULL

        # Symbol-already-open refusal at capacity: TERMINAL, atomic, with
        # reason + timestamp — even though the V2 row is 13h old.
        open_event = _capture(
            store, "FULL0USDT", detected_at=now - timedelta(minutes=20))
        decision = store.admit_position(_build_position(open_event))
        assert decision.outcome == ADMIT_REFUSED_TERMINAL
        assert decision.reason == REFUSAL_SYMBOL_ALREADY_OPEN
        cursor = pg_conn.cursor()
        cursor.execute(
            f"SELECT status, status_reason, status_at FROM {EVENT_TABLE} "
            f"WHERE event_key = %s", (open_event.event_key,))
        status, reason, status_at = cursor.fetchone()
        assert status == EVENT_STATUS_SYMBOL_REJECTED
        assert reason == EVENT_REASON_SYMBOL_ALREADY_OPEN
        assert status_at is not None

        # Cooldown refusal at capacity: a CLOSED V2 row entered 2h ago —
        # the slot is free but the 12h timer runs; still TERMINAL at cap.
        _seed_v2_position(
            pg_conn, "FULLCOOLUSDT", now - timedelta(hours=2))
        cursor.execute(
            f"UPDATE {V2_POSITION_TABLE} SET status = 'CLOSED', "
            f"closed_at = now(), exit_reason = 'TIMEOUT' "
            f"WHERE symbol = 'FULLCOOLUSDT'")
        pg_conn.commit()
        cool_event = _capture(
            store, "FULLCOOLUSDT", detected_at=now - timedelta(minutes=18))
        decision = store.admit_position(_build_position(cool_event))
        assert decision.outcome == ADMIT_REFUSED_TERMINAL
        assert decision.reason == REFUSAL_SYMBOL_COOLDOWN
        cursor.execute(
            f"SELECT status, status_reason FROM {EVENT_TABLE} "
            f"WHERE event_key = %s", (cool_event.event_key,))
        status, reason = cursor.fetchone()
        pg_conn.rollback()
        assert status == EVENT_STATUS_SYMBOL_REJECTED
        assert reason == EVENT_REASON_SYMBOL_COOLDOWN

        # Only the guard-ELIGIBLE signal waits for a free slot (DEFERRED,
        # stays CAPTURED) — never terminally disposed at capacity.
        clean_event = _capture(
            store, "FULLCLEANUSDT", detected_at=now - timedelta(minutes=16))
        decision = store.admit_position(_build_position(clean_event))
        assert decision.outcome == ADMIT_DEFERRED
        assert decision.reason == REFUSAL_ENTRY_CAP
        cursor.execute(
            f"SELECT status FROM {EVENT_TABLE} WHERE event_key = %s",
            (clean_event.event_key,))
        assert cursor.fetchone()[0] == EVENT_STATUS_CAPTURED
        pg_conn.rollback()
