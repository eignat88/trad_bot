"""Issue #176 Phase-2 mandatory test matrix (6): persistence — dedup,
concurrency, atomicity, immutable rows, strict DB-level freeze enforcement.

Remediation (Codex review CHANGES_REQUIRED) additionally covers:
CP1 backdated/reused freeze rejection, activation record binding, concurrent
activation; CP2 three independent freeze authorities in ``require_runnable``;
CP3 warmup/eligibility rejection at BOTH the SQL function and the raw-INSERT
trigger; CP6 mid-batch rollback, two-connection duplicate, pg8000 smoke.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402

from app.research.htf_baseline_short_phase2_persistence_v1 import (
    ExperimentState,
    Phase2ObservationWriter,
    P2ExperimentStateError,
    P2PersistenceError,
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

# Relative timestamps: the DB rejects backdated freezes, so activation always
# uses a freshly computed near-future freeze. Ordering invariants are kept by
# construction: freeze < first_touch < signal.
_NOW = datetime.now(timezone.utc)
FREEZE = (_NOW + timedelta(seconds=2)).isoformat()          # boundary-level tests
SIGNAL_TIME = (_NOW + timedelta(hours=1)).isoformat()
FIRST_TOUCH_TIME = (_NOW + timedelta(minutes=30)).isoformat()
PAST_FREEZE = (_NOW - timedelta(days=1)).isoformat()
FAR_FUTURE_FREEZE = (_NOW + timedelta(hours=6)).isoformat()
FINGERPRINT = "0" * 64
OTHER_FINGERPRINT = "1" * 64
EXP = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"


def _future_freeze() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()


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


def _activate(conn, freeze=None, fingerprint=FINGERPRINT):
    freeze = freeze or _future_freeze()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            (EXP, freeze, fingerprint),
        )
    conn.commit()
    return freeze


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


def _event_as_kwargs(**overrides):
    e = _event()
    kwargs = {
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
    kwargs.update(overrides)
    return kwargs


def test_activate_sets_freeze_and_fingerprint_once(db):
    freeze = _activate(db)
    state = load_experiment_state(db, EXP)
    assert state.status == "RUNNING"
    assert state.freeze_ts is not None
    assert state.protocol_fingerprint == FINGERPRINT
    # repeat activation does not mutate freeze (status gate + set-once)
    with pytest.raises(Exception) as excinfo:
        _activate(db)
    assert "P2_ACTIVATION_REQUIRES_PENDING_STATUS" in str(excinfo.value)
    db.rollback()
    state2 = load_experiment_state(db, EXP)
    assert state2.freeze_ts == state.freeze_ts
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE research.htf_baseline_short_phase2_experiment "
                "SET freeze_ts = %s WHERE experiment_id = %s",
                (FAR_FUTURE_FREEZE, EXP),
            )
    assert "P2_FREEZE_TS_SET_ONCE" in str(excinfo.value)
    db.rollback()
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE research.htf_baseline_short_phase2_experiment "
                "SET protocol_fingerprint = %s WHERE experiment_id = %s",
                (OTHER_FINGERPRINT, EXP),
            )
    assert "P2_PROTOCOL_FINGERPRINT_SET_ONCE" in str(excinfo.value)
    db.rollback()


# ---------------------------------------------------------------- CP1 freeze


def test_backdated_freeze_rejected(db):
    with pytest.raises(Exception) as excinfo:
        _activate(db, freeze=PAST_FREEZE)
    assert "P2_FREEZE_TS_IN_PAST" in str(excinfo.value)
    db.rollback()
    state = load_experiment_state(db, EXP)
    assert state.freeze_ts is None  # no retroactive activation


def test_far_future_freeze_rejected(db):
    with pytest.raises(Exception) as excinfo:
        _activate(db, freeze=FAR_FUTURE_FREEZE)
    assert "P2_FREEZE_TS_MUST_NOT_BE_FAR_FUTURE" in str(excinfo.value)
    db.rollback()


def test_reused_freeze_timestamp_rejected(db):
    # Simulate another experiment family having claimed a timestamp: record the
    # claim in the canonical registry (fixture action, not production code).
    claimed = _future_freeze()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO research.htf_baseline_short_phase2_freeze_registry "
            "(freeze_ts, experiment_id, protocol_fingerprint, note) "
            "VALUES (%s, %s, %s, %s)",
            (claimed, "OTHER_EXPERIMENT_FAMILY", OTHER_FINGERPRINT, "external claim"),
        )
    db.commit()
    with pytest.raises(Exception) as excinfo:
        _activate(db, freeze=claimed)
    assert "P2_FREEZE_TS_ALREADY_CLAIMED" in str(excinfo.value)
    db.rollback()
    state = load_experiment_state(db, EXP)
    assert state.freeze_ts is None


def test_activation_record_binds_fingerprint_and_freeze(db):
    freeze = _activate(db)
    with db.cursor() as cur:
        cur.execute(
            "SELECT freeze_ts, experiment_id, protocol_fingerprint FROM "
            "research.htf_baseline_short_phase2_freeze_registry WHERE freeze_ts = %s",
            (freeze,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[1] == EXP
    assert row[2] == FINGERPRINT
    # registry rows are append-only
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute("UPDATE research.htf_baseline_short_phase2_freeze_registry SET note = 'x'")
    assert "P2_FREEZE_REGISTRY_IS_IMMUTABLE" in str(excinfo.value)
    db.rollback()


def test_concurrent_activation_exactly_one_wins(db):
    results: list[str] = []
    errors: list[str] = []
    dbname = db.get_dsn_parameters()["dbname"]
    barrier = threading.Barrier(2)

    def worker(tag: str):
        try:
            conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database=dbname, user=DB_USER)
            barrier.wait(timeout=10)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
                    (EXP, _future_freeze(), FINGERPRINT),
                )
            conn.commit()
            results.append(tag)
            conn.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{tag}: {exc}")

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1, f"exactly one activation must win: {results} {errors}"
    assert errors, "the losing activation must fail cleanly"
    state = load_experiment_state(db, EXP)
    assert state.status == "RUNNING"
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_freeze_registry"
        )
        (count,) = cur.fetchone()
    assert count == 1


# ------------------------------------------------------- CP3 eligibility wall


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


def test_warmup_event_rejected_by_sql_function(db):
    _activate(db)
    _enable_observer(db)
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXP,
            **_event_as_kwargs(warmup_origin=True),
        )
    assert "P2_WARMUP_EVENT_REJECTED" in str(excinfo.value)
    db.rollback()


def test_resumed_pre_freeze_rejected_by_sql_function(db):
    _activate(db)
    _enable_observer(db)
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXP,
            **_event_as_kwargs(resumed_pre_freeze=True),
        )
    assert "P2_RESUMED_PRE_FREEZE_REJECTED" in str(excinfo.value)
    db.rollback()


def test_source_event_id_shape_enforced(db):
    _activate(db)
    _enable_observer(db)
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXP,
            **_event_as_kwargs(source_event_id="ep1:WHATEVER"),
        )
    assert "P2_SOURCE_EVENT_ID_SHAPE" in str(excinfo.value)
    db.rollback()


def test_raw_insert_cannot_bypass_eligibility(db):
    _activate(db)
    _enable_observer(db)
    # direct SQL caller: warmup flag in authoritative column -> rejected
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO research.htf_baseline_short_phase2_observation "
                "(experiment_id, source_event_id, episode_id, level_id, symbol, "
                " signal_time, first_touch_time, known_at, warmup_origin) "
                "VALUES (%s, 'epX:FIRST_CONFIRM', 'epX', 'lvlX', 'BTCUSDT', "
                " %s, %s, %s, TRUE)",
                (EXP, SIGNAL_TIME, FIRST_TOUCH_TIME, SIGNAL_TIME),
            )
    assert "P2_WARMUP_EVENT_REJECTED" in str(excinfo.value)
    db.rollback()
    # direct SQL caller: pre-freeze touch -> rejected by the INSERT trigger
    with pytest.raises(Exception) as excinfo:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO research.htf_baseline_short_phase2_observation "
                "(experiment_id, source_event_id, episode_id, level_id, symbol, "
                " signal_time, first_touch_time, known_at) "
                "VALUES (%s, 'epY:FIRST_CONFIRM', 'epY', 'lvlY', 'BTCUSDT', "
                " %s, %s, %s)",
                (EXP, SIGNAL_TIME, PAST_FREEZE, SIGNAL_TIME),
            )
    assert "P2_FIRST_TOUCH_NOT_AFTER_FREEZE" in str(excinfo.value)
    db.rollback()
    # spoofed features JSONB can never grant eligibility: the insert succeeds
    # only for a valid event and stores authoritative flags server-side
    row_id = insert_observation_raw(
        db, experiment_id=EXP,
        **_event_as_kwargs(features={"warmup_origin": True, "resumed_pre_freeze": True}),
    )
    db.commit()
    assert row_id is not None
    with db.cursor() as cur:
        cur.execute(
            "SELECT warmup_origin, resumed_pre_freeze, features FROM "
            "research.htf_baseline_short_phase2_observation WHERE observation_id = %s",
            (row_id,),
        )
        warmup, resumed, features = cur.fetchone()
    assert warmup is False and resumed is False
    assert features.get("warmup_origin") is False
    assert features["caller"].get("warmup_origin") is True  # kept as inert data


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
    freeze = _activate(db)
    _enable_observer(db)
    # signal_time == freeze -> rejected (strict >)
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXP,
            **_event_as_kwargs(signal_time=freeze, known_at=freeze),
        )
    assert "P2_SIGNAL_NOT_AFTER_FREEZE" in str(excinfo.value)
    db.rollback()
    # first_touch <= freeze -> rejected
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXP,
            **_event_as_kwargs(first_touch_time=freeze),
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


# ------------------------------------------------------ transactions / CP6


def test_writer_requires_boundary_never_bypasses_filtering(db):
    _activate(db)
    _enable_observer(db)
    writer = Phase2ObservationWriter(db)
    with pytest.raises(ValueError):
        writer.emit([_event()], None)


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


def test_mid_batch_failure_rolls_back_whole_batch(db):
    _activate(db)
    _enable_observer(db)
    writer = Phase2ObservationWriter(db)
    boundary = _boundary()
    good = _event(**{"source_event_id": "ep1:FIRST_CONFIRM"})
    # CHECK violation mid-batch: signal_time must be strictly after first_touch
    bad = _event(**{
        "source_event_id": "ep2:FIRST_CONFIRM",
        "episode_id": "ep2",
        "first_touch_open_ms": _event()["known_at_ms"] + 5000,
    })
    with pytest.raises(P2PersistenceError):
        writer.emit([good, bad], boundary)
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation"
        )
        (count,) = cur.fetchone()
    assert count == 0  # the good first event was rolled back with the batch
    # after rollback a clean retry of the same batch succeeds (idempotent path)
    result = writer.emit([good], boundary)
    assert result.inserted == 1


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


def test_two_connections_second_insert_is_deduplicated(db):
    _activate(db)
    _enable_observer(db)
    dbname = db.get_dsn_parameters()["dbname"]
    conn2 = psycopg2.connect(host=DB_HOST, port=DB_PORT, database=dbname, user=DB_USER)
    try:
        first = insert_observation_raw(db, experiment_id=EXP, **_event_as_kwargs())
        db.commit()
        second = insert_observation_raw(conn2, experiment_id=EXP, **_event_as_kwargs())
        conn2.commit()
        assert first is not None and second is None
    finally:
        conn2.close()


def test_pg_writer_checkpoint_restart_never_duplicates(db, tmp_path, monkeypatch):
    """CP6 end-to-end: real PG writer + runner restart after a simulated crash
    between DB commit and checkpoint persistence (at-least-once + idempotent)."""
    import app.research.htf_keylevel_point_b as point_b
    from app.models import Candle
    from app.research.htf_baseline_short_phase2_freeze_v1 import parse_ts_ms
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        EXIT_OK,
        FileCandleSource,
        Phase2ObserverRunner,
        RunnerConfig,
    )

    monkeypatch.setattr(
        point_b, "_find_reaction_end", lambda **kwargs: (len(kwargs["closed"]) - 1, 0)
    )
    BAR, HOUR = 300_000, 3_600_000
    freeze = _activate(db)
    _enable_observer(db)
    # synthetic series AFTER the freeze so events are prospective
    hour0 = (parse_ts_ms(freeze) + 2 * HOUR) // HOUR * HOUR
    highs = [100.0 + (i % 3) * 0.1 for i in range(32)]
    for i in (6, 12):
        highs[i] = 109.8
        highs[i - 1] = highs[i + 1] = 109.9
    highs[20] = 110.0  # 4/4 swing
    for i in (25, 26, 27, 28):
        highs[i] = 110.0  # contact bars
    candles = []
    for hi, high in enumerate(highs):
        for bi in range(12):
            ts = hour0 + hi * HOUR + bi * BAR
            candles.append(
                Candle(ts, 100.0, high, 99.0, 100.0 if high < 109.5 else 109.5, 1.0)
            )
    data_dir = tmp_path / "candles"
    data_dir.mkdir()
    with (data_dir / "BTCUSDT_5m.csv").open("w", encoding="utf-8", newline="") as h:
        h.write("timestamp,open,high,low,close,volume\n")
        for c in candles:
            h.write(f"{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume}\n")

    boundary = Phase2FreezeBoundary.create(
        experiment_id=EXP,
        protocol_freeze_ts=freeze,
        registry_freeze_ts=freeze,
        database_freeze_ts=freeze,
        protocol_fingerprint=FINGERPRINT,
    )
    config = RunnerConfig(
        symbols=("BTCUSDT",),
        checkpoint_path=str(tmp_path / "state.json"),
        warmup_bars=300,
    )
    writer = Phase2ObservationWriter(db)
    source = FileCandleSource(data_dir)

    first = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert first.run_once() == EXIT_OK

    def row_count() -> int:
        with db.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM research.htf_baseline_short_phase2_observation"
            )
            (count,) = cur.fetchone()
        return count

    rows_after_first = row_count()
    assert rows_after_first >= 1
    # crash AFTER DB commit BEFORE checkpoint persistence
    (tmp_path / "state.json").unlink()
    resumed = Phase2ObserverRunner(config, boundary=boundary, source=source, writer=writer)
    assert resumed.run_once() == EXIT_OK
    assert resumed.metrics.duplicate_suppressions >= 1  # replay detected
    assert row_count() == rows_after_first  # DB dedup absorbed the replay
    # nothing ineligible ever crossed the boundary
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation "
            "WHERE signal_time <= %s::timestamptz OR first_touch_time <= %s::timestamptz "
            "OR warmup_origin OR resumed_pre_freeze",
            (freeze, freeze),
        )
        (bad,) = cur.fetchone()
    assert bad == 0


def test_pg8000_driver_compatibility_smoke(db):
    pg8000 = pytest.importorskip("pg8000.dbapi")
    dbname = db.get_dsn_parameters()["dbname"]
    conn = pg8000.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, database=dbname, timeout=5)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            (EXP, _future_freeze(), FINGERPRINT),
        )
        cur.execute(
            "UPDATE research.htf_baseline_short_phase2_experiment "
            "SET observer_enabled = TRUE WHERE experiment_id = %s",
            (EXP,),
        )
        cur.close()
        conn.commit()
        row_id = insert_observation_raw(conn, experiment_id=EXP, **_event_as_kwargs())
        conn.commit()
        assert row_id is not None
        again = insert_observation_raw(conn, experiment_id=EXP, **_event_as_kwargs())
        conn.commit()
        assert again is None  # dedup works under pg8000 too
    finally:
        conn.close()


# ------------------------------------------------- registration / guards


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
    assert count == 3  # experiment, observation, freeze_registry


# --------------------------------------- CP2: three freeze authorities


def _activation_for(freeze: str) -> dict:
    return {
        "experiment_id": EXP,
        "freeze_ts": freeze,
        "protocol_fingerprint": FINGERPRINT,
    }


def test_require_runnable_three_sources_agree(db):
    from app.research.htf_baseline_short_phase2_freeze_v1 import parse_ts_ms
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        RunnerConfig,
        require_runnable,
    )

    config = RunnerConfig(protocol_fingerprint=FINGERPRINT)
    freeze = _activate(db)
    boundary = require_runnable(
        db, config,
        activation=_activation_for(freeze),
        protocol_freeze_ts=freeze,
    )
    assert boundary.freeze_ts_ms == parse_ts_ms(freeze)
    assert boundary.protocol_fingerprint == FINGERPRINT


def test_require_runnable_rejects_every_pairwise_mismatch(db):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        NotRunnableError,
        RunnerConfig,
        require_runnable,
    )

    config = RunnerConfig(protocol_fingerprint=FINGERPRINT)
    freeze = _activate(db)
    other = _future_freeze()
    cases = [
        # protocol != registry(database)
        {"activation": _activation_for(freeze), "protocol_freeze_ts": other},
        # registry(database) != runtime activation config
        {"activation": _activation_for(other), "protocol_freeze_ts": freeze},
        # protocol != runtime activation config
        {"activation": _activation_for(other), "protocol_freeze_ts": other},
    ]
    for case in cases:
        with pytest.raises(NotRunnableError):
            require_runnable(db, config, **case)


def test_require_runnable_rejects_missing_sources(db):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        NotRunnableError,
        RunnerConfig,
        require_runnable,
    )

    config = RunnerConfig(protocol_fingerprint=FINGERPRINT)
    freeze = _activate(db)
    # missing protocol authority (protocol not frozen)
    with pytest.raises(NotRunnableError):
        require_runnable(db, config, activation=_activation_for(freeze), protocol_freeze_ts=None)
    # missing runtime activation config
    with pytest.raises(NotRunnableError):
        require_runnable(db, config, activation=None, protocol_freeze_ts=freeze)
    # missing database freeze (PENDING)
    from app.research.htf_baseline_short_phase2_persistence_v1 import load_experiment_state

    state = load_experiment_state(db, EXP)
    assert state.freeze_ts is not None  # activated above; the un-activated case
    # is covered by the fail-closed default in test_registration_gate below


def test_require_runnable_rejects_outdated_activation_config(db):
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        NotRunnableError,
        RunnerConfig,
        require_runnable,
    )

    config = RunnerConfig(protocol_fingerprint=FINGERPRINT)
    freeze = _activate(db)
    # outdated runtime config: fingerprint of an older protocol version
    stale = {"experiment_id": EXP, "freeze_ts": freeze, "protocol_fingerprint": OTHER_FINGERPRINT}
    with pytest.raises(NotRunnableError):
        require_runnable(db, config, activation=stale, protocol_freeze_ts=freeze)
    # outdated runtime config: freeze of an older boundary
    stale_freeze = {"experiment_id": EXP, "freeze_ts": _future_freeze(), "protocol_fingerprint": FINGERPRINT}
    with pytest.raises(NotRunnableError):
        require_runnable(db, config, activation=stale_freeze, protocol_freeze_ts=freeze)


def test_registration_gate_fail_closed_then_admits_after_activation(db):
    from app.research.htf_baseline_short_phase2_freeze_v1 import parse_ts_ms
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        NotRunnableError,
        RunnerConfig,
        require_runnable,
    )

    config = RunnerConfig(protocol_fingerprint=FINGERPRINT)
    # PENDING -> refuse startup (fail-closed)
    with pytest.raises(NotRunnableError):
        require_runnable(db, config, activation=_activation_for(FREEZE), protocol_freeze_ts=FREEZE)
    freeze = _activate(db)
    # runtime fingerprint must match the registration exactly
    bad_config = RunnerConfig(protocol_fingerprint=OTHER_FINGERPRINT)
    with pytest.raises(NotRunnableError):
        require_runnable(db, bad_config, activation=_activation_for(freeze), protocol_freeze_ts=freeze)
    # matching fingerprint + all three sources -> boundary admitted
    boundary = require_runnable(
        db, config, activation=_activation_for(freeze), protocol_freeze_ts=freeze
    )
    assert boundary.freeze_ts_ms == parse_ts_ms(freeze)
    assert boundary.protocol_fingerprint == FINGERPRINT
