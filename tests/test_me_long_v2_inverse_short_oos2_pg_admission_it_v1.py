"""ME_LONG_V2_INVERSE_SHORT_OOS2_V1 — P1-2 entry-TTL admission (real PostgreSQL).

The final admission decision (``save_position``) must be enforced BY
PostgreSQL, not by Python alone.  This suite proves, against a real
PostgreSQL server with migration 088 applied, that:

* ``INSERT_POSITION_SQL`` is a CONDITIONAL insert that atomically requires
  the frozen entry TTL at database time (``statement_timestamp()``),
  consistent fingerprint/activation identity, strict post-freeze signal
  time, ``CAPTURED`` status and no existing position row — with
  ``FOR UPDATE`` row locks on the event AND activation rows;
* the TTL boundary is exactly the frozen 120 minutes: 119m59.999s admits,
  exactly 120m and 120m+ε refuse (deterministic, because
  ``statement_timestamp()`` is constant within one statement / one
  simple-query message);
* a TTL that elapses BETWEEN the Python check and the DB INSERT is
  refused by the DB gate (capacity free notwithstanding), and the event
  remains available for the terminal sweep (``EXPIRED``/``ENTRY_TTL``);
* concurrent expire-vs-INSERT and RUNNING→DRAINING-vs-INSERT resolve to
  EXACTLY ONE valid outcome (row-level locking + conditional insert);
* an ambiguous COMMIT (committed row, lost acknowledgement) is adopted
  without a second INSERT, without double-booked fees, and survives a
  restart with the original ``entered_at``/``deadline_at``;
* the sweep never disposes of an event whose position already exists.

The static SQL-contract tests at the top need no database.  Every
PostgreSQL test is gated on ``TEST_DB_HOST`` / ``TEST_DB_UNIX_SOCK`` and
refuses to run against a database named like production (``trad_bot``)
unless ``TEST_DB_ALLOW_PRODUCTION=1`` — this suite never writes to the
production database.
"""
from __future__ import annotations

import dataclasses
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import connect_test_db

from app.config import Settings
from app.paper.me_long_v2_inverse_short_oos2_v1.activation import ACTIVATION_FLAG
from app.paper.me_long_v2_inverse_short_oos2_v1.capture import capture_with_status
from app.paper.me_long_v2_inverse_short_oos2_v1.identity import (
    event_key as build_event_key,
    position_key as build_position_key,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.models import (
    EVENT_STATUS_CAPTURED,
    EVENT_STATUS_ENTERED,
    EVENT_STATUS_EXPIRED,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.protocol import (
    ENTRY_MAX_AGE_MINUTES,
    EXPERIMENT_ID,
    ROUTE_PRE_FILTER_SOURCE,
    protocol_fingerprint,
)
from app.paper.me_long_v2_inverse_short_oos2_v1.runner import InverseShortContour
from app.paper.me_long_v2_inverse_short_oos2_v1.store import (
    ACTIVATION_TABLE,
    CLOSE_OUTBOX_TABLE,
    EVENT_TABLE,
    FUNDING_OBLIGATION_TABLE,
    MARK_EVENT_EXPIRED_LOCK_SQL,
    MARK_EVENT_EXPIRED_SQL,
    POSITION_TABLE,
    INSERT_POSITION_SQL,
    InMemoryInverseShortStore,
    _POSITION_COLUMNS,
    connect_pg_store,
)
from app.scanners.models import SetupCandidate, SetupState

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_087 = REPO_ROOT / "sql" / "migrations" / "088_me_long_v2_inverse_short_oos2_v1.sql"
# Round 6 (P2-funding): the additive funding-obligation table.  Applied
# right after 088 by every OOS-2 PostgreSQL fixture (tests only — the
# production database never receives either migration in this task).
MIGRATION_089 = (
    REPO_ROOT / "sql" / "migrations"
    / "089_me_long_v2_inverse_short_oos2_v1_funding_obligation.sql"
)


def ensure_funding_obligation_table(conn) -> None:
    """Apply migration 089 when the obligation table is absent.

    Raises on failure: 089 is a plain additive CREATE TABLE, so an error
    here is a real defect that must fail the suite loudly (the
    obligation-dependent tests cannot run without it), never a silent skip.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT to_regclass(%s)", (FUNDING_OBLIGATION_TABLE,))
    if cursor.fetchone()[0]:
        return
    cursor.execute(MIGRATION_089.read_text(encoding="utf-8"))
    conn.commit()
    cursor.execute("SELECT to_regclass(%s)", (FUNDING_OBLIGATION_TABLE,))
    if not cursor.fetchone()[0]:
        raise RuntimeError("migration 089 did not create the obligation table")

PINNED_V2_FINGERPRINT = (
    "1d1e7b4d87c6125183887c8f054a4edb1ff3f0711e70da1feeb9b7e249d7b7ec"
)

# Real-PostgreSQL gate: opt-in via TEST_DB_* (same convention as the other
# integration suites in this repository).
_PG_ONLY = pytest.mark.skipif(
    not (os.getenv("TEST_DB_HOST") or os.getenv("TEST_DB_UNIX_SOCK")),
    reason="TEST_DB_HOST / TEST_DB_UNIX_SOCK not set (real-PostgreSQL suite)",
)

TTL_SECONDS = int(ENTRY_MAX_AGE_MINUTES) * 60


# ---------------------------------------------------------------------------
# Static SQL contract (no database required)
# ---------------------------------------------------------------------------
class TestAdmissionGateSqlContract:
    def test_insert_position_sql_is_a_guarded_conditional_insert(self):
        # NOT a bare VALUES insert: every row must pass the admission gate.
        assert "VALUES (" not in INSERT_POSITION_SQL
        assert "WHERE EXISTS (" in INSERT_POSITION_SQL
        # Row-level locks: admission serializes vs sweep and drain.
        assert "FOR UPDATE OF ev, act" in INSERT_POSITION_SQL
        # Clock: DB statement time, never Python time.
        assert "statement_timestamp()" in INSERT_POSITION_SQL
        assert "now()" not in INSERT_POSITION_SQL.split("WHERE EXISTS")[1]
        assert "clock_timestamp()" not in INSERT_POSITION_SQL
        # Gates: status, identity, activation, freeze, idempotency.
        assert "ev.status = %s" in INSERT_POSITION_SQL          # CAPTURED
        assert "act.protocol_fingerprint = %s" in INSERT_POSITION_SQL
        assert "ev.protocol_hash = act.protocol_fingerprint" in INSERT_POSITION_SQL
        assert "act.status = 'RUNNING'" in INSERT_POSITION_SQL
        assert "act.freeze_ts IS NOT NULL" in INSERT_POSITION_SQL
        assert "ev.signal_time > act.freeze_ts" in INSERT_POSITION_SQL
        assert "ON CONFLICT (position_key) DO NOTHING" in INSERT_POSITION_SQL

    def test_ttl_interval_is_derived_from_the_frozen_spec(self):
        assert f"interval '{ENTRY_MAX_AGE_MINUTES} minutes'" in INSERT_POSITION_SQL
        # The production interval literal is the boundary used by the
        # boundary tests below (extracted, never re-typed).
        match = re.search(r"interval '([^']+)'", INSERT_POSITION_SQL)
        assert match is not None
        assert match.group(1) == f"{ENTRY_MAX_AGE_MINUTES} minutes"
        assert ENTRY_MAX_AGE_MINUTES == 120  # frozen spec

    def test_admission_params_are_position_columns_plus_four_gates(self):
        assert INSERT_POSITION_SQL.count("%s") == len(_POSITION_COLUMNS) + 4

    def test_sweep_takes_the_row_lock_and_refuses_existing_positions(self):
        # The sweep locks the event row FIRST (its own statement)...
        assert "FOR UPDATE" in MARK_EVENT_EXPIRED_LOCK_SQL
        assert "SELECT status FROM" in MARK_EVENT_EXPIRED_LOCK_SQL
        assert MARK_EVENT_EXPIRED_LOCK_SQL.count("FOR UPDATE") == 1
        # ...then refuses to dispose of an event that already has a
        # position (no EXPIRED-with-position state).
        assert "NOT EXISTS (" in MARK_EVENT_EXPIRED_SQL
        assert "pos.event_key = ev.event_key" in MARK_EVENT_EXPIRED_SQL
        assert "ev.status = %s" in MARK_EVENT_EXPIRED_SQL

    def test_fingerprint_still_pinned(self):
        assert protocol_fingerprint() == PINNED_V2_FINGERPRINT


class TestTestSignalIdentityUniqueness:
    def test_candle_steps_yield_distinct_deterministic_event_keys(self):
        """Uniqueness contract for test signal identities.

        Two scenarios (NORMAL vs RECOVERED) need two independent signals:
        ``candle_steps`` must produce DIFFERENT ``event_key`` values and
        DIFFERENT production dedup identities, while identical inputs
        always reproduce the SAME key (dedup/redelivery contract) and a
        re-delivery inside the same candle keeps hashing identically.
        Production key/dedup logic is never modified.
        """
        from app.paper.me_long_v2_inverse_short_oos2_v1.identity import (
            source_identity,
        )

        now = datetime(2026, 1, 5, 12, 0, 30, tzinfo=timezone.utc)
        base = _candidate(detected_at=now)
        shifted = _candidate(detected_at=now, candle_steps=1)

        base_key = build_event_key(base)
        shifted_key = build_event_key(shifted)
        # uniqueness: the two scenario identities never collide...
        assert base_key != shifted_key
        assert source_identity(base) != source_identity(shifted)
        # ...determinism: identical inputs reproduce the identical key...
        assert build_event_key(_candidate(detected_at=now)) == base_key
        assert (
            build_event_key(_candidate(detected_at=now, candle_steps=1))
            == shifted_key
        )
        # ...and a redelivery of the SAME candle (even at a later
        # detected_at inside that candle) still hashes to the SAME key —
        # the production dedup contract is untouched by candle_steps.
        assert build_event_key(
            _candidate(detected_at=now + timedelta(minutes=2)),
        ) == base_key


# ---------------------------------------------------------------------------
# Fixtures / helpers (PostgreSQL)
# ---------------------------------------------------------------------------
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


def _candidate(
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    *,
    detected_at: datetime,
    candle_steps: int = 0,
) -> SetupCandidate:
    # ``candle_steps`` deterministically shifts the signal-candle open time
    # BACK by whole 5-minute grid steps.  The candle open participates in
    # BOTH the production ``event_key`` payload and the production dedup
    # identity (``source_identity``), so two test signals with different
    # steps are two genuinely distinct, reproducible signal identities —
    # without touching production key/dedup logic.  ``detected_at`` (the
    # signal_time, admission TTL/freeze gates) stays exactly as fresh as
    # any other capture in this file.
    candle_ms = (
        int(detected_at.timestamp() // 300) * 300_000 - candle_steps * 300_000
    )
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


def _contour(store, clock=None) -> InverseShortContour:
    return InverseShortContour(
        _settings(), store, clock=clock, activation_enabled=True,
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
            f"{database!r} (production name guard)",
        )


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


_original_activation: tuple | None = None


def _cleanup(conn) -> None:
    cursor = conn.cursor()
    # obligation rows first (no FK, but they reference positions logically),
    # then outbox -> position -> event.
    for table in (FUNDING_OBLIGATION_TABLE, CLOSE_OUTBOX_TABLE,
                  POSITION_TABLE, EVENT_TABLE):
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
    if not cursor.fetchone()[0]:
        try:
            cursor.execute(MIGRATION_087.read_text(encoding="utf-8"))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.close()
            pytest.skip(f"migration 088 not applicable: {exc}")
        cursor.execute("SELECT to_regclass(%s)", (EVENT_TABLE,))
        if not cursor.fetchone()[0]:
            conn.close()
            pytest.skip("V2 tables missing after migration 088 attempt")

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
    """Activation = RUNNING with the pinned fingerprint + a fresh freeze."""
    freeze = datetime.now(timezone.utc) - timedelta(hours=3)
    _write_activation(pg_conn, "RUNNING", protocol_fingerprint(), freeze)
    return freeze


def _capture_event(
    store, *, age: timedelta, symbol: str = "BTCUSDT",
    candle_steps: int = 0, signal_at: datetime | None = None,
):
    """Capture ONE prospective source event; returns its key.

    ``candle_steps`` gives a second capture a DISTINCT deterministic
    signal identity (see ``_candidate``) — dedup itself stays fully
    active: a genuine duplicate still fails the CAPTURED assertion.

    ``signal_at`` pins ``signal_time`` to an explicit instant (typically
    the TEST ENGINE's clock): admission ages a signal against the
    engine clock, so a capture taken after ``clock.advance()`` must carry
    a signal_time on that same timeline or the frozen 120-minute TTL
    would reject it as stale.
    """
    if signal_at is None:
        signal_at = datetime.now(timezone.utc) - age
    candidate = _candidate(
        symbol, detected_at=signal_at, candle_steps=candle_steps,
    )
    status = capture_with_status(
        store, candidate,
        capture_route=ROUTE_PRE_FILTER_SOURCE,
        env={ACTIVATION_FLAG: "1"},
    )
    assert status == "CAPTURED", status
    return build_event_key(candidate)


def _admission_params(store, position) -> tuple:
    """Exactly what ``PgInverseShortStore.save_position`` sends."""
    return store._position_params(position) + (
        EXPERIMENT_ID,
        position.event_key,
        EVENT_STATUS_CAPTURED,
        store._expected_fingerprint,
    )


def _build_position(contour, store, event_key_value: str, price: float = 100.0):
    event = store.get_event(event_key_value)
    assert event is not None
    position = contour.engine._build_position(
        event, build_position_key(event_key_value), price,
    )
    assert position is not None
    return position


def _count(conn, table: str, where: str = "1=1") -> int:
    cursor = conn.cursor()
    cursor.execute(f"SELECT count(*) FROM {table} WHERE {where}")  # noqa: S608
    value = cursor.fetchone()[0]
    conn.rollback()
    return int(value)


def _event_row(conn, key: str) -> dict:
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT status, status_reason, status_at, position_key "
        f"FROM {EVENT_TABLE} WHERE event_key = %s",
        (key,),
    )
    row = cursor.fetchone()
    conn.rollback()
    assert row is not None, "event row missing"
    return {
        "status": row[0], "status_reason": row[1],
        "status_at": row[2], "position_key": row[3],
    }


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return "'" + value.isoformat() + "'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


def _literalize(sql: str, params: tuple) -> str:
    """Render the production statement with literal values (no bind params).

    Needed only by the exact-boundary tests: they must run the event-age
    UPDATE and the production INSERT inside ONE simple-query message so
    both share the same ``statement_timestamp()``.
    """
    parts = sql.split("%s")
    assert len(parts) == len(params) + 1, (
        f"placeholder mismatch: {len(parts) - 1} vs {len(params)}",
    )
    out = parts[0]
    for value, tail in zip(params, parts[1:]):
        out += _sql_literal(value) + tail
    return out


# ---------------------------------------------------------------------------
# TTL boundary semantics (real PostgreSQL clock)
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestTtlBoundaryOnPostgres:
    def test_statement_timestamp_is_constant_within_a_statement(self, pg_conn):
        cursor = pg_conn.cursor()
        cursor.execute(
            "SELECT statement_timestamp() = statement_timestamp() AS stable",
        )
        assert cursor.fetchone()[0] is True
        pg_conn.rollback()

    def test_one_simple_query_message_shares_statement_timestamp(self, pg_conn):
        # Harness precondition for the exact-boundary e2e tests below: all
        # statements of ONE simple-query message share statement_timestamp().
        cursor = pg_conn.cursor()
        cursor.execute(
            "CREATE TEMP TABLE _stmt_ts_probe(x timestamptz); "
            "INSERT INTO _stmt_ts_probe VALUES (statement_timestamp()); "
            "SELECT statement_timestamp() - x AS delta FROM _stmt_ts_probe",
        )
        delta = cursor.fetchone()[0]
        pg_conn.rollback()
        assert delta == timedelta(0), (
            "statement_timestamp() is per-statement on this server; the "
            "exact-boundary harness would no longer be deterministic",
        )

    def test_boundary_predicate_is_exactly_the_frozen_ttl(self, pg_conn):
        """119m59.999s admits; exactly 120m and 120m+ε refuse.

        Evaluated in ONE statement (constant statement_timestamp) with the
        production interval literal extracted from INSERT_POSITION_SQL, so
        the boundary proven here is the boundary production enforces.
        """
        import re
        production_interval = re.search(
            r"interval '([^']+)'", INSERT_POSITION_SQL,
        ).group(1)
        under = TTL_SECONDS - 0.001   # 119 m 59.999 s
        exact = float(TTL_SECONDS)    # exactly 120 minutes
        over = TTL_SECONDS + 0.000001  # 120 minutes + 1 µs
        cursor = pg_conn.cursor()
        cursor.execute(
            "SELECT "
            f"(statement_timestamp() - interval '{under} seconds')"
            f" > statement_timestamp() - interval '{production_interval}' "
            "AS under_admits, "
            f"(statement_timestamp() - interval '{exact} seconds')"
            f" > statement_timestamp() - interval '{production_interval}' "
            "AS exact_refuses, "
            f"(statement_timestamp() - interval '{over} seconds')"
            f" > statement_timestamp() - interval '{production_interval}' "
            "AS over_refuses",
        )
        under_admits, exact_refuses, over_refuses = cursor.fetchone()
        pg_conn.rollback()
        assert under_admits is True, "119m59.999s must ADMIT"
        assert exact_refuses is False, "exactly 120m must REFUSE"
        assert over_refuses is False, "120m+ε must REFUSE"

    @pytest.mark.parametrize(
        ("age_seconds", "admitted", "symbol"),
        [
            (TTL_SECONDS - 0.001, True, "BTCUSDT"),     # 119 m 59.999 s -> admit
            (float(TTL_SECONDS), False, "ETHUSDT"),     # exactly 120 min -> refuse
            (TTL_SECONDS + 0.000001, False, "SOLUSDT"),  # 120 min + 1 µs -> refuse
        ],
        ids=["119m59.999s", "exactly-120m", "120m-plus-epsilon"],
    )
    def test_e2e_exact_boundary_through_the_production_insert(
        self, pg_conn, running, age_seconds, admitted, symbol,
    ):
        """The real INSERT_POSITION_SQL at the exact TTL boundary.

        The event age is set and the admission runs inside ONE
        simple-query message, so both sides of the comparison use the
        SAME ``statement_timestamp()`` — the outcome is deterministic
        regardless of test-harness latency.
        """
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(
                store, age=timedelta(seconds=age_seconds / 3), symbol=symbol,
            )
            position = _build_position(contour, store, key)
            cursor = pg_conn.cursor()
            script = (
                f"UPDATE {EVENT_TABLE} SET signal_time = statement_timestamp() "
                f"- interval '{age_seconds!r} seconds' "
                f"WHERE event_key = '{key}';\n"
                + _literalize(INSERT_POSITION_SQL, _admission_params(store, position))
            )
            cursor.execute(script)  # ONE message -> shared statement_timestamp
            pg_conn.commit()

            # The age UPDATE really took effect (harness sanity):
            cursor.execute(
                f"SELECT statement_timestamp() - signal_time "
                f"FROM {EVENT_TABLE} WHERE event_key = %s",
                (key,),
            )
            measured = cursor.fetchone()[0]
            pg_conn.rollback()
            assert timedelta(seconds=age_seconds) <= measured <= (
                timedelta(seconds=age_seconds) + timedelta(seconds=30)
            ), measured

            rows = _count(pg_conn, POSITION_TABLE)
            row = _event_row(pg_conn, key)
            if admitted:
                assert rows == 1, "119m59.999s must be ADMITTED"
                assert row["status"] == EVENT_STATUS_CAPTURED  # not yet ENTERED
            else:
                assert rows == 0, "past the TTL nothing may be admitted"
                assert row["status"] == EVENT_STATUS_CAPTURED
        finally:
            store.close()

    def test_e2e_fresh_signal_admitted_with_real_roundtrip(
        self, pg_conn, running,
    ):
        """A just-captured signal (age = TTL - 90 s) is admitted end-to-end."""
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(
                store, age=timedelta(seconds=TTL_SECONDS - 90),
                symbol="BTCUSDT",
            )
            position = _build_position(contour, store, key)
            assert store.save_position(position) is True
            assert _count(pg_conn, POSITION_TABLE) == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# The DB gate outranks Python: TTL between the Python check and the INSERT
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestDatabaseClockAuthority:
    def test_ttl_elapsed_between_python_check_and_db_insert_is_refused(
        self, pg_conn, running,
    ):
        """Python says admissible, PostgreSQL says stale -> refused.

        The engine clock is a fake standing at signal + 119 min (the
        Python age check passes and the capacity slot is FREE), while the
        stored signal_time is 121 min old at the database — the DB gate
        must refuse.  The event then stays available for the terminal
        sweep and receives EXPIRED / ENTRY_TTL with a decision timestamp.
        """
        store = _connect_store()
        try:
            key = _capture_event(
                store,
                age=timedelta(minutes=ENTRY_MAX_AGE_MINUTES + 1),  # 121 min
                symbol="BTCUSDT",
            )
            # Anchor the fake engine clock to the STORED signal_time so the
            # Python-side ages below are exact (no harness latency drift):
            signal = store.get_event(key).signal_time
            clock = _Clock(signal + timedelta(minutes=ENTRY_MAX_AGE_MINUTES - 1))
            contour = _contour(store, clock)

            cycle = contour.run_cycle({"BTCUSDT": 100.0})
            # Capacity is FREE — the refusal may only come from the gate:
            assert len(contour.engine.open_positions) == 0
            assert _count(pg_conn, POSITION_TABLE) == 0
            assert cycle["entries"] == 0
            row = _event_row(pg_conn, key)
            assert row["status"] == EVENT_STATUS_CAPTURED, (
                "the refused event must stay available for the sweep",
            )

            # Cross the TTL in the engine clock -> terminal disposition.
            clock.now = signal + timedelta(minutes=ENTRY_MAX_AGE_MINUTES)
            contour.run_cycle({"BTCUSDT": 100.0})
            row = _event_row(pg_conn, key)
            assert row["status"] == EVENT_STATUS_EXPIRED
            assert row["status_reason"] == "ENTRY_TTL"
            assert row["status_at"] is not None
            assert _count(pg_conn, POSITION_TABLE) == 0
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Identity gates: fingerprint / activation / status / freeze
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestAdmissionIdentityGates:
    def _refused(self, pg_conn, store, key, position,
                 expected_status=EVENT_STATUS_CAPTURED):
        assert store.save_position(position) is False
        assert _count(pg_conn, POSITION_TABLE) == 0
        assert _event_row(pg_conn, key)["status"] == expected_status

    def test_refuses_when_activation_is_draining(self, pg_conn, running):
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)
            _write_activation(
                pg_conn, "DRAINING", protocol_fingerprint(),
                datetime.now(timezone.utc) - timedelta(hours=3),
            )
            self._refused(pg_conn, store, key, position)
        finally:
            store.close()

    def test_refuses_on_activation_fingerprint_mismatch(self, pg_conn, running):
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)
            _write_activation(
                pg_conn, "RUNNING", "0" * 64,
                datetime.now(timezone.utc) - timedelta(hours=3),
            )
            self._refused(pg_conn, store, key, position)
        finally:
            store.close()

    def test_refuses_on_event_protocol_hash_mismatch(self, pg_conn, running):
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)
            cursor = pg_conn.cursor()
            cursor.execute(
                f"UPDATE {EVENT_TABLE} SET protocol_hash = %s "
                f"WHERE event_key = %s",
                ("0" * 64, key),
            )
            assert cursor.rowcount == 1
            pg_conn.commit()
            self._refused(pg_conn, store, key, position)
        finally:
            store.close()

    def test_refuses_pre_freeze_event(self, pg_conn, running):
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=10))
            position = _build_position(contour, store, key)
            # Freeze moved past the signal AFTER capture (re-freeze):
            _write_activation(
                pg_conn, "RUNNING", protocol_fingerprint(),
                datetime.now(timezone.utc),
            )
            self._refused(pg_conn, store, key, position)
        finally:
            store.close()

    def test_refuses_event_that_is_no_longer_captured(self, pg_conn, running):
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)
            store.mark_event_entered(key, position.position_key)
            self._refused(
                pg_conn, store, key, position,
                expected_status=EVENT_STATUS_ENTERED,
            )
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Concurrent expire (TTL disposition) vs INSERT (admission)
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestConcurrentExpireVsInsert:
    def test_admission_holds_the_lock_and_disposal_refuses(
        self, pg_conn, running,
    ):
        """INSERT commits first -> the sweep must NOT dispose the event.

        Deterministic interleaving: the admission statement holds the
        event-row lock (uncommitted) while the sweep blocks on the same
        row; once the admission commits, the sweep's fresh snapshot sees
        the position and refuses (no EXPIRED-with-position).
        """
        store = _connect_store()
        sweeper = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)

            cursor = pg_conn.cursor()
            cursor.execute(INSERT_POSITION_SQL, _admission_params(store, position))
            # Admission now holds the event + activation row locks.

            disposed: list = []

            def _sweep() -> None:
                disposed.append(
                    sweeper.mark_event_expired(
                        key, EVENT_STATUS_EXPIRED,
                        reason="ENTRY_TTL",
                        decided_at=datetime.now(timezone.utc),
                    ),
                )

            thread = threading.Thread(target=_sweep)
            thread.start()
            thread.join(timeout=0.5)
            assert thread.is_alive(), (
                "the sweep must block on the event-row lock held by the "
                "in-flight admission",
            )

            pg_conn.commit()  # release the locks
            thread.join(timeout=30)
            assert not thread.is_alive()
            assert disposed == [False], (
                "the sweep must refuse once the position is committed",
            )
            row = _event_row(pg_conn, key)
            assert row["status"] == EVENT_STATUS_CAPTURED
            assert _count(pg_conn, POSITION_TABLE) == 1
        finally:
            store.close()
            sweeper.close()

    def test_disposal_holds_the_lock_and_admission_refuses(
        self, pg_conn, running,
    ):
        """Sweep commits first -> the INSERT must insert nothing.

        Deterministic interleaving: the sweep locks + disposes the event
        while the admission blocks; after the sweep commits, the gate
        re-checks the row (EvalPlanQual) and sees EXPIRED.
        """
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)

            cursor = pg_conn.cursor()
            cursor.execute(
                MARK_EVENT_EXPIRED_LOCK_SQL, (key, EVENT_STATUS_CAPTURED),
            )
            # The sweep now holds the event-row lock (not yet disposed).

            result: list = []

            def _admit() -> None:
                result.append(store.save_position(position))

            thread = threading.Thread(target=_admit)
            thread.start()
            thread.join(timeout=0.5)
            assert thread.is_alive(), (
                "the admission must block on the event-row lock held by "
                "the sweep",
            )

            cursor.execute(
                MARK_EVENT_EXPIRED_SQL,
                (EVENT_STATUS_EXPIRED, "ENTRY_TTL",
                 datetime.now(timezone.utc), key, EVENT_STATUS_CAPTURED),
            )
            assert cursor.rowcount == 1
            pg_conn.commit()

            thread.join(timeout=30)
            assert not thread.is_alive()
            assert result == [False]
            row = _event_row(pg_conn, key)
            assert row["status"] == EVENT_STATUS_EXPIRED
            assert row["status_reason"] == "ENTRY_TTL"
            assert _count(pg_conn, POSITION_TABLE) == 0
        finally:
            store.close()

    def test_parallel_disposal_and_admission_leave_one_valid_outcome(
        self, pg_conn, running,
    ):
        """Six real races: never EXPIRED-with-position, never a duplicate."""
        symbols = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT",
            "XRPUSDT", "DOGEUSDT", "ADAUSDT",
        ]
        store_a = _connect_store()
        store_b = _connect_store()
        contour = _contour(store_a)
        try:
            pairs = []
            for symbol in symbols:
                key = _capture_event(store_a, age=timedelta(minutes=1),
                                     symbol=symbol)
                pairs.append((key, _build_position(contour, store_a, key)))

            for key, position in pairs:
                outcome: dict = {}

                def _admit() -> None:
                    try:
                        outcome["inserted"] = store_a.save_position(position)
                    except Exception as exc:  # noqa: BLE001
                        outcome["admit_error"] = exc

                def _expire() -> None:
                    try:
                        outcome["disposed"] = store_b.mark_event_expired(
                            key, EVENT_STATUS_EXPIRED,
                            reason="ENTRY_TTL",
                            decided_at=datetime.now(timezone.utc),
                        )
                    except Exception as exc:  # noqa: BLE001
                        outcome["expire_error"] = exc

                t1 = threading.Thread(target=_admit)
                t2 = threading.Thread(target=_expire)
                t1.start(); t2.start()
                t1.join(timeout=30); t2.join(timeout=30)
                assert not t1.is_alive() and not t2.is_alive(), outcome
                assert "admit_error" not in outcome, outcome
                assert "expire_error" not in outcome, outcome

                inserted = outcome["inserted"]
                disposed = outcome["disposed"]
                row = _event_row(pg_conn, key)
                positions = _count(
                    pg_conn, POSITION_TABLE, f"event_key = '{key}'",
                )
                # EXACTLY ONE valid outcome is possible, whichever order
                # the two transactions won the row lock:
                if inserted:
                    assert disposed is False, outcome
                    assert positions == 1
                    assert row["status"] != EVENT_STATUS_EXPIRED
                else:
                    assert disposed is True, outcome
                    assert positions == 0
                    assert row["status"] == EVENT_STATUS_EXPIRED
                    assert row["status_reason"] == "ENTRY_TTL"
                assert not (
                    row["status"] == EVENT_STATUS_EXPIRED and positions == 1
                ), "EXPIRED-with-position is forbidden"
        finally:
            store_a.close()
            store_b.close()


# ---------------------------------------------------------------------------
# Concurrent RUNNING -> DRAINING vs INSERT
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestConcurrentDrainVsInsert:
    def test_no_admission_after_draining_in_force(self, pg_conn, running):
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)
            _write_activation(
                pg_conn, "DRAINING", protocol_fingerprint(),
                datetime.now(timezone.utc) - timedelta(hours=3),
            )
            assert store.save_position(position) is False
            assert _count(pg_conn, POSITION_TABLE) == 0
        finally:
            store.close()

    def test_inflight_admission_serializes_with_running_to_draining(
        self, pg_conn, running,
    ):
        """An in-flight admission finishes BEFORE DRAINING takes effect;
        every later admission is refused."""
        store = _connect_store()
        later = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)
            key2 = _capture_event(
                store, age=timedelta(minutes=1), symbol="ETHUSDT",
            )
            position2 = _build_position(contour, store, key2)

            cursor = pg_conn.cursor()
            cursor.execute(INSERT_POSITION_SQL, _admission_params(store, position))
            # The admission holds the ACTIVATION row lock (gate join).

            drained: list = []

            def _drain() -> None:
                drain_conn = connect_test_db()
                try:
                    drain_cursor = drain_conn.cursor()
                    drain_cursor.execute(
                        f"UPDATE {ACTIVATION_TABLE} SET status = 'DRAINING', "
                        f"updated_at = now() WHERE experiment_id = %s "
                        f"AND status = 'RUNNING'",
                        (EXPERIMENT_ID,),
                    )
                    drained.append(drain_cursor.rowcount)
                    drain_conn.commit()
                finally:
                    drain_conn.close()

            thread = threading.Thread(target=_drain)
            thread.start()
            thread.join(timeout=0.5)
            assert thread.is_alive(), (
                "the DRAINING transition must block on the activation-row "
                "lock held by the in-flight admission",
            )

            pg_conn.commit()  # admission commits -> DRAINING may proceed
            thread.join(timeout=30)
            assert not thread.is_alive()
            assert drained == [1]

            # Created while RUNNING (before DRAINING was in force):
            assert _count(pg_conn, POSITION_TABLE) == 1
            assert _event_row(pg_conn, key)["status"] == EVENT_STATUS_CAPTURED
            # After DRAINING is in force no further admission is possible:
            assert later.save_position(position2) is False
            assert _count(pg_conn, POSITION_TABLE) == 1
        finally:
            store.close()
            later.close()


# ---------------------------------------------------------------------------
# Ambiguous COMMIT: adoption + restart recovery
# ---------------------------------------------------------------------------
@_PG_ONLY
class TestLostAckAndRestart:
    def test_committed_but_unacked_insert_is_adopted_without_duplicate(
        self, pg_conn, running,
    ):
        """Commit durable, acknowledgement lost -> adopted, no 2nd INSERT.

        The row is inserted out-of-band (as the lost attempt would have
        committed it) while the event is still CAPTURED; the next cycle
        must ADOPT it verbatim — one row, original entry facts, entry
        costs folded exactly once.
        """
        symbol = "BTCUSDT"
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1), symbol=symbol)
            position = _build_position(contour, store, key)

            # The "previous attempt": committed INSERT, no acknowledgement.
            cursor = pg_conn.cursor()
            cursor.execute(INSERT_POSITION_SQL, _admission_params(store, position))
            assert cursor.rowcount == 1
            pg_conn.commit()

            cycle = contour.run_cycle({symbol: 100.0})
            assert _count(pg_conn, POSITION_TABLE) == 1, "no second INSERT"
            assert position.position_key in contour.engine.open_positions, (
                "the committed row must be adopted",
            )
            row = _event_row(pg_conn, key)
            assert row["status"] == EVENT_STATUS_ENTERED
            assert row["position_key"] == position.position_key

            stored = store.get_position(position.position_key)
            assert stored.entered_at == position.entered_at  # verbatim
            assert stored.deadline_at == position.deadline_at  # verbatim

            # Entry costs folded EXACTLY once (no double fee):
            expected = 10_000.0 - (position.entry_fee + position.entry_slippage_cost)
            assert contour.engine.balance == pytest.approx(expected, abs=1e-9)

            # A second cycle never re-opens or re-books it:
            contour.run_cycle({symbol: 100.0})
            assert _count(pg_conn, POSITION_TABLE) == 1
            assert contour.engine.balance == pytest.approx(expected, abs=1e-9)
            assert cycle is not None
        finally:
            store.close()

    def test_second_save_of_the_same_position_is_idempotent(
        self, pg_conn, running,
    ):
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)
            assert store.save_position(position) is True
            # Lost-ack retry of the SAME committed INSERT:
            assert store.save_position(position) is False
            assert _count(pg_conn, POSITION_TABLE) == 1
        finally:
            store.close()

    def test_restart_recovers_the_committed_position(self, pg_conn, running):
        symbol = "BTCUSDT"
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1), symbol=symbol)
            cycle = contour.run_cycle({symbol: 100.0})
            assert cycle["entries"] == 1
            stored_before = store.list_positions()[0]
        finally:
            store.close()

        # RESTART: brand-new store connection + brand-new contour process.
        store2 = _connect_store()
        contour2 = _contour(store2)
        try:
            recovered = store2.load_open_positions()
            assert len(recovered) == 1
            assert recovered[0].entered_at == stored_before.entered_at
            assert recovered[0].deadline_at == stored_before.deadline_at

            cycle2 = contour2.run_cycle({symbol: 100.0})
            assert cycle2["entries"] == 0, "no re-entry after restart"
            assert len(contour2.engine.open_positions) == 1
            assert _count(pg_conn, POSITION_TABLE) == 1
        finally:
            store2.close()

    def test_sweep_never_disposes_an_event_that_has_a_position(
        self, pg_conn, running,
    ):
        store = _connect_store()
        contour = _contour(store)
        try:
            key = _capture_event(store, age=timedelta(minutes=1))
            position = _build_position(contour, store, key)
            assert store.save_position(position) is True
            # Race window: position committed, event not yet ENTERED.
            assert store.mark_event_expired(
                key, EVENT_STATUS_EXPIRED, reason="ENTRY_TTL",
                decided_at=datetime.now(timezone.utc),
            ) is False
            assert _event_row(pg_conn, key)["status"] == EVENT_STATUS_CAPTURED
            assert _count(pg_conn, POSITION_TABLE) == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# In-memory parity (no database): same no-EXPIRED-with-position rule
# ---------------------------------------------------------------------------
class TestInMemorySweepParity:
    def test_inmemory_store_never_disposes_an_event_with_a_position(self):
        from app.paper.me_long_v2_inverse_short_oos2_v1.models import ActivationRecord

        activation = ActivationRecord(
            experiment_id=EXPERIMENT_ID,
            status="RUNNING",
            protocol_fingerprint=protocol_fingerprint(),
            freeze_ts=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        store = InMemoryInverseShortStore(activation)
        contour = _contour(store)
        key = _capture_event(store, age=timedelta(minutes=1))
        position = _build_position(contour, store, key)
        assert store.save_position(position) is True
        assert store.mark_event_expired(
            key, EVENT_STATUS_EXPIRED, reason="ENTRY_TTL",
            decided_at=datetime.now(timezone.utc),
        ) is False
        assert store.get_event(key).status == EVENT_STATUS_CAPTURED
        # Without a position the disposal still works:
        key2 = _capture_event(store, age=timedelta(minutes=1),
                              symbol="ETHUSDT")
        assert store.mark_event_expired(
            key2, EVENT_STATUS_EXPIRED, reason="ENTRY_TTL",
            decided_at=datetime.now(timezone.utc),
        ) is True
        assert store.get_event(key2).status == EVENT_STATUS_EXPIRED
