"""Issue #197 Phase-2 PostgreSQL integration: timestamp authority + activation.

Covers the DB-side scenario matrix of the freeze record (timestamp authority
V1): mint/claim/verify end to end, stale/far-future/reused timestamp rejection,
concurrent activation serialization, interrupted-activation fail-closed +
documented repair, privilege denial and the strict pre-freeze boundary at the
database insert path.

SAFETY: this suite never connects to any existing database except the
``postgres`` maintenance database of a LOCAL TEST SERVER, and it only ever
creates/drops a disposable database whose name matches ``trad_bot_p2_it_*``.
The production database ``trad_bot`` is blocked by name and the production
server must never be selected: run this module ONLY against a dedicated
disposable test cluster (the Issue #197 validation used an isolated initdb
cluster on a non-production port). The whole module skips when no test server
is reachable.

Test server selection (env): P2_TEST_DB_HOST (default 127.0.0.1),
P2_TEST_DB_PORT (default 5432), P2_TEST_DB_USER (default postgres).
"""
from __future__ import annotations

import os
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "research"))
import htf_baseline_short_phase2_freeze_record_v1 as tool  # noqa: E402

from app.research.htf_baseline_short_phase2_freeze_v1 import parse_ts_ms  # noqa: E402
from app.research.htf_baseline_short_phase2_persistence_v1 import (  # noqa: E402
    insert_observation_raw,
)
from app.research.htf_baseline_short_phase2_protocol_v1 import (  # noqa: E402
    EXPERIMENT_ID,
    protocol_fingerprint,
)
from app.research.htf_baseline_short_phase2_timestamp_authority_v1 import (  # noqa: E402
    load_freeze_record,
)

DB_HOST = os.environ.get("P2_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("P2_TEST_DB_PORT", "5432"))
DB_USER = os.environ.get("P2_TEST_DB_USER", "postgres")

_DB_PATTERN = re.compile(r"\Atrad_bot_p2_it_[a-z0-9_]+\Z")
_BLOCKED = frozenset({"trad_bot", "postgres", "template0", "template1", "srr", "test"})

MIGRATION = (
    ROOT / "sql" / "migrations" / "074_htf_baseline_short_phase2_registration.sql"
)

FP = protocol_fingerprint()  # the pinned FROZEN fingerprint of the running code


def _validate_name(name: str) -> None:
    if not _DB_PATTERN.match(name) or name in _BLOCKED:
        raise AssertionError(f"refusing unsafe test database name: {name}")


def _admin_connect():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database="postgres", user=DB_USER, connect_timeout=5
    )
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
    reason=f"no local test PostgreSQL at {DB_HOST}:{DB_PORT}; freeze-record IT skipped",
)


@pytest.fixture()
def db():
    name = f"trad_bot_p2_it_{uuid.uuid4().hex[:8]}"
    _validate_name(name)
    admin = _admin_connect()
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database=name, user=DB_USER, connect_timeout=5
    )
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


def _fresh_conn(db):
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=db.get_dsn_parameters()["dbname"],
        user=DB_USER,
        connect_timeout=5,
    )


@pytest.fixture()
def tool_db(db, monkeypatch):
    """Point the timestamp-authority tool at the disposable database."""
    conns = []

    def fake_connect():
        conn = _fresh_conn(db)
        conn.autocommit = False
        conns.append(conn)
        return conn

    monkeypatch.setattr(tool, "_connect", fake_connect)
    yield db
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass


def _namespace(**kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


def _mint(record_path: Path, *, freeze_ts: str | None = None, **extra) -> int:
    args = _namespace(
        experiment_id=EXPERIMENT_ID,
        operator_approval_ref=extra.get("operator_approval_ref", "GO-IT-001"),
        issued_by=extra.get("issued_by", "it-operator"),
        record_id=extra.get("record_id", str(uuid.uuid4())),
        freeze_ts=freeze_ts,
        note=None,
        dry_run=False,
        out=str(record_path),
    )
    try:
        return tool.cmd_mint(args)
    except Exception:  # noqa: BLE001 - a refusal is a refusal
        return tool.EXIT_REFUSED


def _claim(record_path: Path, activation_path: Path, **extra) -> int:
    args = _namespace(
        record=str(record_path),
        activation_out=str(activation_path),
        dry_run=False,
        repair_activation_json=bool(extra.get("repair_activation_json", False)),
    )
    try:
        return tool.cmd_claim(args)
    except Exception:  # noqa: BLE001 - a refusal/crash is a refusal
        return tool.EXIT_REFUSED


# =============================================================== mint / claim E2E
def test_mint_claim_verify_end_to_end(tool_db, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    assert _mint(record_path) == tool.EXIT_OK
    record = load_freeze_record(record_path)
    assert record["protocol_fingerprint"] == FP
    assert record["freeze_ts"].endswith("Z")

    assert _claim(record_path, activation_path) == tool.EXIT_OK
    state_row = tool_db.cursor()
    state_row.execute(
        "SELECT status, freeze_ts, protocol_fingerprint, observer_enabled "
        "FROM research.htf_baseline_short_phase2_experiment WHERE experiment_id = %s",
        (EXPERIMENT_ID,),
    )
    status, freeze_ts, fingerprint, observer_enabled = state_row.fetchone()
    state_row.close()
    assert status == "RUNNING"
    assert fingerprint == FP
    assert observer_enabled is False  # issue #197 never enables the observer
    assert parse_ts_ms(freeze_ts) == parse_ts_ms(record["freeze_ts"])

    with tool_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM research.htf_baseline_short_phase2_freeze_registry")
        (registry_n,) = cur.fetchone()
    assert registry_n == 1

    # all three authorities agree -> the observer startup gate admits
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        RunnerConfig,
        require_runnable,
    )

    config = RunnerConfig(protocol_fingerprint=FP)
    boundary = require_runnable(
        tool_db,
        config,
        activation={
            "experiment_id": EXPERIMENT_ID,
            "freeze_ts": record["freeze_ts"],
            "protocol_fingerprint": FP,
        },
        freeze_record=record,
        require_pinned_fingerprint=True,
    )
    assert boundary.freeze_ts_ms == parse_ts_ms(record["freeze_ts"])


def test_verify_reports_agreement(tool_db, tmp_path, capsys):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _mint(record_path)
    _claim(record_path, activation_path)
    args = _namespace(record=str(record_path), activation=str(activation_path))
    assert tool.cmd_verify(args) == tool.EXIT_OK
    out = capsys.readouterr().out
    assert "THREE_AUTHORITY_AGREEMENT=OK" in out


# ============================================================ timestamp rejection
def test_mint_rejects_forbidden_legacy_timestamp(tool_db, tmp_path):
    # the Point-B freeze boundary can never be re-used by Phase-2
    assert (
        _mint(tmp_path / "r.json", freeze_ts="2026-10-06T14:00:00Z") == tool.EXIT_REFUSED
    )


@pytest.mark.parametrize(
    "delta",
    [
        timedelta(minutes=-5),   # too old for the 60s-past window
        timedelta(minutes=-2),
        timedelta(minutes=10),   # beyond the 5-minute future window
        timedelta(hours=2),
    ],
)
def test_mint_rejects_out_of_window_timestamps(tool_db, tmp_path, delta):
    stale = (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _mint(tmp_path / "r.json", freeze_ts=stale) == tool.EXIT_REFUSED


@pytest.mark.parametrize("delta", [timedelta(seconds=-30), timedelta(minutes=2)])
def test_mint_accepts_timestamps_inside_the_acceptance_window(tool_db, tmp_path, delta):
    fresh = (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _mint(tmp_path / "r.json", freeze_ts=fresh) == tool.EXIT_OK


def test_db_activation_rejects_out_of_window_timestamps(db):
    for delta, code in (
        (timedelta(minutes=-5), "P2_FREEZE_TS_IN_PAST"),
        (timedelta(minutes=10), "P2_FREEZE_TS_MUST_NOT_BE_FAR_FUTURE"),
    ):
        ts = (datetime.now(timezone.utc) + delta).isoformat()
        with pytest.raises(Exception) as excinfo:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
                    (EXPERIMENT_ID, ts, FP),
                )
        assert code in str(excinfo.value)
        db.rollback()  # leave failed tx state before the next expected error


def test_freeze_timestamp_reuse_is_structurally_rejected(tool_db, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _mint(record_path)
    _claim(record_path, activation_path)
    record = load_freeze_record(record_path)

    # (a) direct registry reuse of the claimed timestamp is impossible
    with pytest.raises(Exception) as excinfo:
        with tool_db.cursor() as cur:
            cur.execute(
                "INSERT INTO research.htf_baseline_short_phase2_freeze_registry "
                "(freeze_ts, experiment_id, protocol_fingerprint) VALUES (%s, %s, %s)",
                (record["freeze_ts"], "OTHER_EXPERIMENT", FP),
            )
    assert "duplicate key" in str(excinfo.value).lower() or "unique" in str(excinfo.value).lower()
    tool_db.rollback()

    # (b) a second claim with ANY timestamp is refused by the tool: the freeze
    # is claimed once; recovery never re-mints a timestamp
    second = tmp_path / "freeze_record_2.json"
    assert _mint(second) == tool.EXIT_OK
    assert _claim(second, tmp_path / "activation_2.json") == tool.EXIT_REFUSED

    # (c) raw activation with a fresh timestamp also fails at the DB guard
    fresh = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    with pytest.raises(Exception) as excinfo:
        with tool_db.cursor() as cur:
            cur.execute(
                "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
                (EXPERIMENT_ID, fresh, FP),
            )
    assert "P2_ACTIVATION_REQUIRES_PENDING_STATUS" in str(excinfo.value)
    tool_db.rollback()


# ========================================================== concurrent activation
def test_concurrent_activation_is_serialized_single_winner(db):
    freeze_a = (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    freeze_b = (datetime.now(timezone.utc) + timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}
    lock = threading.Lock()

    def attempt(label: str, freeze_ts: str) -> None:
        conn = _fresh_conn(db)
        conn.autocommit = False
        try:
            barrier.wait(timeout=15)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
                    (EXPERIMENT_ID, freeze_ts, FP),
                )
                cur.fetchone()
            conn.commit()
            outcome = "OK"
        except Exception as exc:  # noqa: BLE001 - record the losing attempt
            conn.rollback()
            outcome = str(exc)
        finally:
            conn.close()
        with lock:
            results[label] = outcome

    threads = [
        threading.Thread(target=attempt, args=("a", freeze_a)),
        threading.Thread(target=attempt, args=("b", freeze_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    outcomes = sorted(results.values())
    assert len(outcomes) == 2
    winners = [value for value in results.values() if value == "OK"]
    losers = [value for value in results.values() if value != "OK"]
    assert len(winners) == 1, results
    assert len(losers) == 1
    assert (
        "P2_ACTIVATION_REQUIRES_PENDING_STATUS" in losers[0]
        or "P2_FREEZE_TS_ALREADY_CLAIMED" in losers[0]
        or "P2_FREEZE_TS_SET_ONCE" in losers[0]
    ), losers[0]
    with db.cursor() as cur:
        cur.execute(
            "SELECT freeze_ts FROM research.htf_baseline_short_phase2_experiment "
            "WHERE experiment_id = %s",
            (EXPERIMENT_ID,),
        )
        (claimed,) = cur.fetchone()
    assert parse_ts_ms(claimed) in (parse_ts_ms(freeze_a), parse_ts_ms(freeze_b))
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM research.htf_baseline_short_phase2_freeze_registry")
        (registry_n,) = cur.fetchone()
    assert registry_n == 1  # exactly one claim recorded


# ==================================================== interrupted activation
def test_interrupted_activation_fails_closed_then_documented_repair(
    tool_db, tmp_path, monkeypatch
):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _mint(record_path)

    real_write = tool._atomic_write_json
    calls = {"n": 0}

    def crash_once(path, payload, *, overwrite=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated crash before activation JSON write")
        return real_write(path, payload, overwrite=overwrite)

    monkeypatch.setattr(tool, "_atomic_write_json", crash_once)
    assert _claim(record_path, activation_path) == tool.EXIT_REFUSED

    # the DB claim DID commit, but no activation JSON exists -> the observer
    # startup gate must refuse to run (fail closed, zero observations)
    assert not activation_path.exists()
    record = load_freeze_record(record_path)
    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        NotRunnableError,
        RunnerConfig,
        require_runnable,
    )

    config = RunnerConfig(protocol_fingerprint=FP)
    with pytest.raises(NotRunnableError):
        require_runnable(tool_db, config, activation=None, freeze_record=record)

    # a naive retry must NOT mint a new timestamp: the tool refuses
    assert _claim(record_path, tmp_path / "activation_other.json") == tool.EXIT_REFUSED

    # documented recovery: re-derive the activation JSON from the SAME record
    monkeypatch.setattr(tool, "_atomic_write_json", real_write)
    assert (
        _claim(record_path, activation_path, repair_activation_json=True) == tool.EXIT_OK
    )
    assert activation_path.exists()
    boundary = require_runnable(
        tool_db,
        config,
        activation={
            "experiment_id": EXPERIMENT_ID,
            "freeze_ts": record["freeze_ts"],
            "protocol_fingerprint": FP,
        },
        freeze_record=record,
        require_pinned_fingerprint=True,
    )
    assert boundary.freeze_ts_ms == parse_ts_ms(record["freeze_ts"])


# ============================================================== privilege denial
def test_role_without_activation_privilege_is_denied(db):
    low_role = f"trad_bot_p2_it_low_{uuid.uuid4().hex[:8]}"
    with db.cursor() as cur:
        cur.execute(f"CREATE ROLE {low_role} NOLOGIN")
        cur.execute(
            "REVOKE ALL ON FUNCTION research.htf_baseline_short_phase2_activate("
            "text, timestamp with time zone, text) FROM PUBLIC"
        )
    db.commit()
    ts = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    try:
        with db.cursor() as cur:
            cur.execute(f"SET LOCAL ROLE {low_role}")
            with pytest.raises(Exception) as excinfo:
                cur.execute(
                    "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
                    (EXPERIMENT_ID, ts, FP),
                )
            assert "permission denied" in str(excinfo.value).lower()
    finally:
        db.rollback()
        with db.cursor() as cur:
            cur.execute(f"DROP ROLE IF EXISTS {low_role}")
        db.commit()
    # no partial activation happened
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, freeze_ts FROM research.htf_baseline_short_phase2_experiment "
            "WHERE experiment_id = %s",
            (EXPERIMENT_ID,),
        )
        status, freeze_ts = cur.fetchone()
    assert status == "PENDING" and freeze_ts is None


# ================================================= strict boundary at the DB edge
def test_db_strict_boundary_rejects_pre_freeze_and_warmup_events(db):
    freeze = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            (EXPERIMENT_ID, freeze, FP),
        )
        cur.fetchone()
        cur.execute(
            "UPDATE research.htf_baseline_short_phase2_experiment "
            "SET observer_enabled = TRUE WHERE experiment_id = %s",
            (EXPERIMENT_ID,),
        )
    db.commit()

    def event(**overrides) -> dict:
        # migration 074: CHECK (known_at = signal_time) — exact equality, so a
        # single timestamp value must back both columns.
        signal_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        base = {
            "source_event_id": "ep1:FIRST_CONFIRM",
            "episode_id": "ep1",
            "level_id": "lv1",
            "symbol": "BTCUSDT",
            "signal_time": signal_time,
            "first_touch_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "known_at": signal_time,
            "level_price": 100.0,
            "entry_reference_price": 99.0,
            "warmup_origin": False,
            "resumed_pre_freeze": False,
            "features": {},
        }
        base.update(overrides)
        return base

    # pre-freeze first touch with post-freeze confirmation -> rejected
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db,
            experiment_id=EXPERIMENT_ID,
            **event(
                first_touch_time=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            ),
        )
    assert "P2_FIRST_TOUCH_NOT_AFTER_FREEZE" in str(excinfo.value)
    db.rollback()

    # warmup-origin episode -> rejected even with post-freeze timestamps
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(db, experiment_id=EXPERIMENT_ID, **event(warmup_origin=True))
    assert "P2_WARMUP_EVENT_REJECTED" in str(excinfo.value)
    db.rollback()

    # resumed pre-freeze episode -> rejected
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXPERIMENT_ID, **event(resumed_pre_freeze=True)
        )
    assert "P2_RESUMED_PRE_FREEZE_REJECTED" in str(excinfo.value)
    db.rollback()

    # a fully post-freeze event is accepted exactly once
    row_id = insert_observation_raw(db, experiment_id=EXPERIMENT_ID, **event())
    assert row_id is not None
    again = insert_observation_raw(db, experiment_id=EXPERIMENT_ID, **event())
    assert again is None  # duplicate absorbed, never an error
    db.commit()


# ==================================================== startup gate disagreement
def test_startup_gate_refuses_any_authority_drift_after_claim(tool_db, tmp_path):
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _mint(record_path)
    _claim(record_path, activation_path)
    record = load_freeze_record(record_path)

    from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
        NotRunnableError,
        RunnerConfig,
        require_runnable,
    )

    config = RunnerConfig(protocol_fingerprint=FP)
    good = {
        "experiment_id": EXPERIMENT_ID,
        "freeze_ts": record["freeze_ts"],
        "protocol_fingerprint": FP,
    }
    drifted = dict(good, freeze_ts="2030-01-01T00:00:00Z")
    with pytest.raises(NotRunnableError):
        require_runnable(tool_db, config, activation=drifted, freeze_record=record)
    with pytest.raises(NotRunnableError):
        require_runnable(
            tool_db,
            config,
            activation=dict(good, protocol_fingerprint="0" * 64),
            freeze_record=record,
        )
    with pytest.raises(NotRunnableError):
        require_runnable(tool_db, config, activation=good, freeze_record=None)
    # and the happy path still holds
    boundary = require_runnable(tool_db, config, activation=good, freeze_record=record)
    assert boundary.protocol_fingerprint == FP
