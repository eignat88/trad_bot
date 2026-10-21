"""Issue #176 Phase-2 mandatory test matrix (6): persistence — dedup,
concurrency, atomicity, immutable rows, strict DB-level freeze enforcement.

SAFETY: this suite never connects to any existing database except the
``postgres`` maintenance database of a LOCAL test server, and it only ever
creates/drops a disposable database whose name matches ``trad_bot_p2_it_*``.
The production database ``trad_bot`` is blocked by name. The whole module
skips when no local test server is reachable.

Test server selection (env): P2_TEST_DB_HOST (default 127.0.0.1),
P2_TEST_DB_PORT (default 5432), P2_TEST_DB_USER (default postgres).
"""
from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path

import pytest

pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402

from app.research.htf_baseline_short_phase2_persistence_v1 import (
    ExperimentState,
    Phase2ObservationWriter,
    P2ExperimentStateError,
    freeze_sources,
    insert_observation_raw,
    load_experiment_state,
)
from app.research.htf_baseline_short_phase2_freeze_v1 import Phase2FreezeBoundary

import os

DB_HOST = os.environ.get("P2_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("P2_TEST_DB_PORT", "5432"))
DB_USER = os.environ.get("P2_TEST_DB_USER", "postgres")

_DB_PATTERN = re.compile(r"\Atrad_bot_p2_it_[a-z0-9_]+\Z")
_BLOCKED = frozenset({"trad_bot", "postgres", "template0", "template1", "srr", "test"})

MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "sql"
    / "migrations"
    / "074_htf_baseline_short_phase2_registration.sql"
)

FREEZE = "2026-10-20T00:00:00Z"  # past: DB guard rejects far-future freezes
SIGNAL_TIME = "2026-10-21T00:00:00Z"
FIRST_TOUCH_TIME = "2026-10-20T23:00:00Z"
FINGERPRINT = "0" * 64
EXP = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"


def _validate_name(name: str) -> None:
    if not _DB_PATTERN.match(name) or name in _BLOCKED:
        raise AssertionError(f"refusing unsafe test database name: {name}")


def _admin_connect():
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database="postgres", user=DB_USER, connect_timeout=5)
    conn.autocommit = True
    return conn


def _server_available() -> bool:
    try:
        conn = _admin_connect()
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_available(),
    reason=f"no local test PostgreSQL at {DB_HOST}:{DB_PORT}; persistence IT skipped",
)


@pytest.fixture()
def db():
    name = f"trad_bot_p2_it_{uuid.uuid4().hex[:8]}"
    _validate_name(name)
    admin = _admin_connect()
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database=name, user=DB_USER, connect_timeout=5)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()
    with admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
        )
        cur.execute(f"DROP DATABASE IF EXISTS {name}")
    admin.close()


def _event(**overrides) -> dict:
    from app.research.htf_baseline_short_phase2_freeze_v1 import parse_ts_ms

    event = {
        "source_event_id": "ep1:FIRST_CONFIRM",
        "episode_id": "ep1",
        "level_id": "lvl1",
        "symbol": "BTCUSDT",
        "known_at_ms": parse_ts_ms(SIGNAL_TIME),
        "first_touch_open_ms": parse_ts_ms(FIRST_TOUCH_TIME),
        "level_price_at_touch": 110.5,
        "entry_reference_price": 109.9,
        "warmup_origin": False,
    }
    event.update(overrides)
    return event


def _activate(conn, freeze=FREEZE, fingerprint=FINGERPRINT):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            (EXP, freeze, fingerprint),
        )
    conn.commit()


def _enable_observer(conn):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE research.htf_baseline_short_phase2_experiment "
            "SET observer_enabled = TRUE WHERE experiment_id = %s",
            (EXP,),
        )
    conn.commit()


def _boundary():
    return Phase2FreezeBoundary.create(
        experiment_id=EXP,
        protocol_freeze_ts=FREEZE,
        registry_freeze_ts=FREEZE,
        database_freeze_ts=FREEZE,
        protocol_fingerprint=FINGERPRINT,
    )


def test_registration_lands_pending_unfrozen_disabled(db):
    state = load_experiment_state(db, EXP)
    assert state.status == "PENDING"
    assert state.freeze_ts is None
    assert state.observer_enabled is False
    assert state.protocol_fingerprint is None
    assert not state.is_runnable


def test_insert_refused_while_not_frozen(db):
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(db, experiment_id=EXP, **_event_as_kwargs())
    assert "P2_FREEZE_NOT_SET" in str(excinfo.value)
    db.rollback()


def _event_as_kwargs():
    e = _event()
    return {
        "source_event_id": e["source_event_id"],
        "episode_id": e["episode_id"],
        "level_id": e["level_id"],
        "symbol": e["symbol"],
        "signal_time": SIGNAL_TIME,
        "first_touch_time": FIRST_TOUCH_TIME,
        "known_at": SIGNAL_TIME,
        "level_price": 110.5,
        "entry_reference_price": 109.9,
        "features": None,
    }


def test_activate_sets_freeze_and_fingerprint_once(db):
    _activate(db)
    state = load_experiment_state(db, EXP)
    assert state.status == "RUNNING"
    assert state.freeze_ts is not None
    assert state.protocol_fingerprint == FINGERPRINT
    # set-once semantics
    with pytest.raises(Exception) as excinfo:
        _activate(db, freeze="2026-10-19T00:00:00Z", fingerprint="1" * 64)
    assert "P2_ACTIVATION_REQUIRES_PENDING_STATUS" in str(excinfo.value)
    db.rollback()
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE research.htf_baseline_short_phase2_experiment "
                "SET freeze_ts = %s WHERE experiment_id = %s",
                ("2026-11-03T00:00:00Z", EXP),
            )
    assert "P2_FREEZE_TS_SET_ONCE" in str(excinfo.value)
    db.rollback()
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE research.htf_baseline_short_phase2_experiment "
                "SET protocol_fingerprint = %s WHERE experiment_id = %s",
                ("2" * 64, EXP),
            )
    assert "P2_PROTOCOL_FINGERPRINT_SET_ONCE" in str(excinfo.value)
    db.rollback()


def test_insert_requires_observer_enabled_emergency_stop(db):
    _activate(db)
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(db, experiment_id=EXP, **_event_as_kwargs())
    assert "P2_OBSERVER_NOT_ENABLED" in str(excinfo.value)
    db.rollback()
    _enable_observer(db)
    row_id = insert_observation_raw(db, experiment_id=EXP, **_event_as_kwargs())
    db.commit()
    assert row_id is not None


def test_dedup_on_experiment_and_source_event_id(db):
    _activate(db)
    _enable_observer(db)
    first = insert_observation_raw(db, experiment_id=EXP, **_event_as_kwargs())
    db.commit()
    second = insert_observation_raw(db, experiment_id=EXP, **_event_as_kwargs())
    db.commit()
    assert first is not None
    assert second is None  # idempotent duplicate suppression
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation "
            "WHERE experiment_id = %s",
            (EXP,),
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_strict_freeze_enforced_at_db_boundary(db):
    _activate(db)
    _enable_observer(db)
    # signal_time == freeze -> rejected (strict >)
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXP,
            **{**_event_as_kwargs(), "signal_time": FREEZE, "known_at": FREEZE},
        )
    assert "P2_SIGNAL_NOT_AFTER_FREEZE" in str(excinfo.value)
    db.rollback()
    # first_touch <= freeze -> rejected
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXP,
            **{**_event_as_kwargs(), "first_touch_time": FREEZE},
        )
    assert "P2_FIRST_TOUCH_NOT_AFTER_FREEZE" in str(excinfo.value)
    db.rollback()


def test_observation_rows_are_immutable(db):
    _activate(db)
    _enable_observer(db)
    insert_observation_raw(db, experiment_id=EXP, **_event_as_kwargs())
    db.commit()
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE research.htf_baseline_short_phase2_observation SET level_price = 0"
            )
    assert "P2_OBSERVATIONS_ARE_IMMUTABLE" in str(excinfo.value)
    db.rollback()
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute("DELETE FROM research.htf_baseline_short_phase2_observation")
    assert "P2_OBSERVATIONS_ARE_IMMUTABLE" in str(excinfo.value)
    db.rollback()


def test_writer_batch_is_atomic_and_boundary_gated(db):
    _activate(db)
    _enable_observer(db)
    writer = Phase2ObservationWriter(db)
    boundary = _boundary()
    events = [
        _event(**{"source_event_id": "ep1:FIRST_CONFIRM"}),
        _event(**{"source_event_id": "ep2:FIRST_CONFIRM", "episode_id": "ep2"}),
        _event(**{"source_event_id": "ep0:FIRST_CONFIRM", "episode_id": "ep0",
                  "known_at_ms": 1_750_000_000_000, "first_touch_open_ms": 1_749_999_700_000}),  # pre-freeze
    ]
    result = writer.emit(events, boundary)
    assert result.inserted == 2
    assert result.rejected == 1
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation "
            "WHERE experiment_id = %s",
            (EXP,),
        )
        (count,) = cur.fetchone()
    assert count == 2


def test_concurrent_same_event_exactly_one_row(db):
    _activate(db)
    _enable_observer(db)
    errors: list[Exception] = []
    results: list[object] = []

    def worker():
        try:
            conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database=db.get_dsn_parameters()["dbname"], user=DB_USER)
            results.append(insert_observation_raw(conn, experiment_id=EXP, **_event_as_kwargs()))
            conn.commit()
            conn.close()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    inserted = [r for r in results if r is not None]
    deduped = [r for r in results if r is None]
    assert len(inserted) == 1
    assert len(deduped) == 3
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation "
            "WHERE experiment_id = %s",
            (EXP,),
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_registration_guard_blocks_activation_leak_on_insert(db):
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO research.htf_baseline_short_phase2_experiment "
                "(experiment_id, direction, status, freeze_ts, observer_enabled) "
                "VALUES (%s, 'SHORT', 'RUNNING', %s, TRUE)",
                ("trad_bot_p2_it_leak", FREEZE),
            )
    assert "P2_REGISTRATION_MUST_START_PENDING_UNFROZEN_DISABLED" in str(excinfo.value)
    db.rollback()


def test_cancellation_is_terminal(db):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE research.htf_baseline_short_phase2_experiment "
            "SET status = 'CANCELLED' WHERE experiment_id = %s",
            (EXP,),
        )
    db.commit()
    with pytest.raises(Exception) as excinfo:
        _activate(db)
    assert "P2_ACTIVATION_REQUIRES_PENDING_STATUS" in str(excinfo.value)
    db.rollback()
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE research.htf_baseline_short_phase2_experiment "
                "SET status = 'PENDING' WHERE experiment_id = %s",
                (EXP,),
            )
    assert "P2_TERMINAL_STATUS_IMMUTABLE" in str(excinfo.value)
    db.rollback()


def test_point_b_tables_untouched_by_migration(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('research.prospective_experiment'), "
            "to_regclass('research.prospective_observation')"
        )
        (pe, po) = cur.fetchone()
    # migration 074 must not create or modify the Point-B stack
    assert pe is None and po is None
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'research' AND table_name LIKE 'htf_baseline_short_phase2%'"
        )
        (count,) = cur.fetchone()
    assert count == 2  # exactly the two new Phase-2 tables


def test_registration_gate_fail_closed_then_admits_after_activation(db):
    from app.research.htf_baseline_short_phase2_freeze_v1 import parse_ts_ms
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        NotRunnableError,
        RunnerConfig,
        require_runnable,
    )

    config = RunnerConfig(protocol_fingerprint=FINGERPRINT)
    # PENDING + fingerprint mismatch -> refuse startup (fail-closed)
    with pytest.raises(NotRunnableError):
        require_runnable(db, config)
    _activate(db)
    # runtime fingerprint must match the registration exactly
    bad_config = RunnerConfig(protocol_fingerprint="1" * 64)
    with pytest.raises(NotRunnableError):
        require_runnable(db, bad_config)
    # matching fingerprint + freeze -> boundary admitted
    boundary = require_runnable(db, config)
    assert boundary.freeze_ts_ms == parse_ts_ms(FREEZE)
    assert boundary.protocol_fingerprint == FINGERPRINT
