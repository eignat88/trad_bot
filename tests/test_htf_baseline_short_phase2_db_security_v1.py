"""Issue #176 Phase-2 DB privilege hardening (CP4): integration tests for
migration 074 -> 075 on DISPOSABLE PostgreSQL databases only.

SAFETY: connects to the local test server (P2_TEST_DB_*), creates/drops only
``trad_bot_p2_it_*`` databases and ephemeral roles; production ``trad_bot`` is
blocked by name. The module skips when no local test server is reachable.

Covers: least-privilege observer role, SECURITY DEFINER insert wall, denied
direct DML, admin-only activation, freeze-registry isolation, unchanged grants
on pre-existing research objects, concurrency, eligibility rejection, pg8000
driver path (SCRAM-capable) and transaction rollback.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402

from app.research.htf_baseline_short_phase2_persistence_v1 import (
    insert_observation_raw,
)

DB_HOST = os.environ.get("P2_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("P2_TEST_DB_PORT", "5432"))
DB_USER = os.environ.get("P2_TEST_DB_USER", "postgres")

_DB_PATTERN = re.compile(r"\Atrad_bot_p2_it_[a-z0-9_]+\Z")
_BLOCKED = frozenset({"trad_bot", "postgres", "template0", "template1", "srr", "test"})

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_074 = ROOT / "sql" / "migrations" / "074_htf_baseline_short_phase2_registration.sql"
MIGRATION_075 = ROOT / "sql" / "migrations" / "075_htf_baseline_short_phase2_privilege_hardening.sql"

EXP = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"
FINGERPRINT = "0" * 64
OBSERVER_ROLE = "trad_bot_htf_p2_observer"
ADMIN_ROLE = "trad_bot_htf_p2_admin"


def _future_freeze() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()


def _signal_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _first_touch_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()


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
    reason=f"no local test PostgreSQL at {DB_HOST}:{DB_PORT}; security IT skipped",
)


@pytest.fixture()
def db():
    """Disposable database with 074 applied; roles tracked for teardown."""
    name = f"trad_bot_p2_it_{uuid.uuid4().hex[:8]}"
    if not _DB_PATTERN.match(name) or name in _BLOCKED:
        raise AssertionError(f"refusing unsafe test database name: {name}")
    admin = _admin_connect()
    preexisting = _existing_roles(admin)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database=name, user=DB_USER, connect_timeout=5)
    with conn.cursor() as cur:
        cur.execute(MIGRATION_074.read_text(encoding="utf-8"))
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()
    with admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
        )
        cur.execute(f"DROP DATABASE IF EXISTS {name}")
    # drop roles the migration created during this test (never pre-existing ones)
    for role in (OBSERVER_ROLE, ADMIN_ROLE):
        if role not in preexisting:
            with admin.cursor() as cur:
                try:
                    cur.execute(f"DROP ROLE IF EXISTS {role}")
                except Exception:  # pragma: no cover - role may own grants elsewhere
                    pass
    admin.close()


def _existing_roles(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles")
        return {row[0] for row in cur.fetchall()}


def apply_075(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(MIGRATION_075.read_text(encoding="utf-8"))
    conn.commit()


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


def _event_kwargs(suffix: str = "ep1"):
    signal = _signal_time()  # known_at must equal signal_time (table CHECK)
    return {
        "source_event_id": f"{suffix}:FIRST_CONFIRM",
        "episode_id": suffix,
        "level_id": "lvl1",
        "symbol": "BTCUSDT",
        "signal_time": signal,
        "first_touch_time": _first_touch_time(),
        "known_at": signal,
        "level_price": 110.5,
        "entry_reference_price": 109.9,
        "features": None,
    }


def _denied(conn_sql: str, conn, *params):
    """Run SQL expected to fail; return the raised exception."""
    with pytest.raises(Exception) as excinfo:
        with conn.cursor() as cur:
            cur.execute(conn_sql, params)
    conn.rollback()
    return excinfo.value


def _assert_insufficient_privilege(exc: Exception) -> None:
    """Privilege refusal must be SQLSTATE 42501 (locale independent)."""
    code = getattr(exc, "pgcode", None)
    text = str(exc)
    assert code == "42501" or "permission denied" in text or "нет доступа" in text, (
        f"expected insufficient privilege, got: {text[:160]}"
    )


# ------------------------------------------------------------------- roles


def test_075_creates_least_privilege_roles(db):
    apply_075(db)
    with db.cursor() as cur:
        cur.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
            "WHERE rolname = %s",
            (OBSERVER_ROLE,),
        )
        obs = cur.fetchone()
        cur.execute(
            "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = %s",
            (ADMIN_ROLE,),
        )
        adm = cur.fetchone()
    assert obs == (True, False, False, False)  # LOGIN, no admin attributes
    assert adm == (False, False)  # NOLOGIN admin procedure holder


def test_075_scoped_to_phase2_surface_static():
    """Migration 075 must never touch non-Phase-2 objects or grant broadly."""
    sql = MIGRATION_075.read_text(encoding="utf-8")
    upper = sql.upper()
    assert "GRANT ALL" not in upper
    assert "ALTER TABLE" not in upper  # no table surgery in a privilege migration
    assert "SECURITY DEFINER" in upper
    assert "SEARCH_PATH = PG_CATALOG, RESEARCH" in upper
    low = sql.lower()
    for forbidden in ("prospective", "srr_", "dds.", "analytics.", "mart.", "config.", "market."):
        assert forbidden not in low, f"075 must not mention {forbidden}"
    # every named schema object is a Phase-2 object
    named = set(re.findall(r"research\.([a-z0-9_]+)", low))
    assert named, "expected explicit research object names"
    for obj in named:
        assert obj.startswith("htf_baseline_short_phase2"), f"out-of-scope object: {obj}"


def test_075_requires_074_fail_closed(db):
    with db.cursor() as cur:
        cur.execute("DROP TABLE research.htf_baseline_short_phase2_freeze_registry")
    db.commit()
    with pytest.raises(Exception) as excinfo:
        apply_075(db)
    assert "P2_MIGRATION_074_REQUIRED" in str(excinfo.value)
    db.rollback()


# ----------------------------------------------- observer write wall (CP1)


def test_observer_insert_via_function_allowed(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    row_id = insert_observation_raw(db, experiment_id=EXP, **_event_kwargs())
    db.commit()
    assert row_id is not None
    with db.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation"
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_observer_direct_insert_update_delete_denied(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied(
        "INSERT INTO research.htf_baseline_short_phase2_observation "
        "(experiment_id, source_event_id, episode_id, level_id, symbol, "
        " signal_time, first_touch_time, known_at) "
        "VALUES (%s, 'e:FIRST_CONFIRM', 'e', 'l', 'BTCUSDT', now(), now(), now())",
        db, EXP,
    )
    _assert_insufficient_privilege(err)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied("UPDATE research.htf_baseline_short_phase2_experiment SET observer_enabled = TRUE", db)
    _assert_insufficient_privilege(err)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied("DELETE FROM research.htf_baseline_short_phase2_observation", db)
    _assert_insufficient_privilege(err)


def test_observer_cannot_activate(db):
    apply_075(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied(
        "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
        db, EXP, _future_freeze(), FINGERPRINT,
    )
    _assert_insufficient_privilege(err)


def test_activation_is_admin_only(db):
    apply_075(db)
    # unprivileged role cannot even execute the activation function
    unauth = f"trad_bot_p2_it_unauth_{uuid.uuid4().hex[:6]}"
    admin = _admin_connect()
    with admin.cursor() as cur:
        cur.execute(f"CREATE ROLE {unauth} NOLOGIN")
    try:
        with db.cursor() as cur:
            cur.execute("GRANT USAGE ON SCHEMA research TO " + unauth)
            cur.execute(f"SET ROLE {unauth}")
        err = _denied(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            db, EXP, _future_freeze(), FINGERPRINT,
        )
        _assert_insufficient_privilege(err)
        with db.cursor() as cur:
            cur.execute(f"SET ROLE {unauth}")
        err = _denied(
            "SELECT research.htf_baseline_short_phase2_insert_observation("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            db, EXP, "e:FIRST_CONFIRM", "e", "l", "BTCUSDT",
            _signal_time(), _first_touch_time(), _signal_time(), 1.0, 1.0,
            False, False, "{}",
        )
        _assert_insufficient_privilege(err)
    finally:
        with db.cursor() as cur:
            cur.execute("RESET ROLE")
            cur.execute(f"DROP OWNED BY {unauth}")
        db.rollback()
        admin = _admin_connect()
        with admin.cursor() as cur:
            cur.execute(f"DROP ROLE IF EXISTS {unauth}")
        admin.close()
    # the administrative procedure itself works for the admin role
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {ADMIN_ROLE}")
        cur.execute(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            (EXP, _future_freeze(), FINGERPRINT),
        )
    db.commit()
    with db.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT status, freeze_ts FROM research.htf_baseline_short_phase2_experiment "
            "WHERE experiment_id = %s",
            (EXP,),
        )
        status, freeze = cur.fetchone()
    assert status == "RUNNING" and freeze is not None


def test_application_role_revoked_on_p2_surface(db):
    admin = _admin_connect()
    roles = _existing_roles(admin)
    created: list[str] = []
    if "trad_bot" not in roles:
        with admin.cursor() as cur:
            cur.execute("CREATE ROLE trad_bot NOLOGIN")
        created.append("trad_bot")
    admin.close()
    try:
        apply_075(db)
        with db.cursor() as cur:
            cur.execute("SET ROLE trad_bot")
        err = _denied(
            "INSERT INTO research.htf_baseline_short_phase2_observation "
            "(experiment_id, source_event_id, episode_id, level_id, symbol, "
            " signal_time, first_touch_time, known_at) "
            "VALUES (%s, 'e:FIRST_CONFIRM', 'e', 'l', 'BTCUSDT', now(), now(), now())",
            db, EXP,
        )
        _assert_insufficient_privilege(err)
        with db.cursor() as cur:
            cur.execute("SET ROLE trad_bot")
        err = _denied(
            "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)",
            db, EXP, _future_freeze(), FINGERPRINT,
        )
        _assert_insufficient_privilege(err)
    finally:
        with db.cursor() as cur:
            cur.execute("RESET ROLE")
        db.rollback()
        if created:
            admin = _admin_connect()
            for role in created:
                with admin.cursor() as cur:
                    cur.execute(f"DROP ROLE IF EXISTS {role}")
            admin.close()


def test_freeze_registry_isolated_from_direct_writes(db):
    apply_075(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied(
        "INSERT INTO research.htf_baseline_short_phase2_freeze_registry "
        "(freeze_ts, experiment_id, protocol_fingerprint) VALUES (%s, %s, %s)",
        db, _future_freeze(), EXP, FINGERPRINT,
    )
    _assert_insufficient_privilege(err)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    err = _denied("UPDATE research.htf_baseline_short_phase2_freeze_registry SET note = 'x'", db)
    _assert_insufficient_privilege(err)


# ------------------------------------------- grants on pre-existing objects


def test_existing_research_grants_unchanged_by_075(db):
    admin = _admin_connect()
    try:
        unauth = f"trad_bot_p2_it_reader_{uuid.uuid4().hex[:6]}"
        with admin.cursor() as cur:
            cur.execute(f"CREATE ROLE {unauth} NOLOGIN")
    finally:
        admin.close()
    try:
        # decoy pre-existing research object with its own grants
        with db.cursor() as cur:
            cur.execute("CREATE TABLE research.p2hardening_decoy (id INT PRIMARY KEY)")
            cur.execute(f"GRANT SELECT ON research.p2hardening_decoy TO {unauth}")
        db.commit()

        def snapshot() -> dict:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT table_name, grantee, privilege_type FROM "
                    "information_schema.table_privileges WHERE table_schema = 'research'"
                )
                rows = cur.fetchall()
            return {(r[0], r[1], r[2]) for r in rows}

        before = snapshot()
        apply_075(db)
        after = snapshot()
        decoy_before = {s for s in before if s[0] == "p2hardening_decoy"}
        decoy_after = {s for s in after if s[0] == "p2hardening_decoy"}
        assert decoy_before == decoy_after  # untouched
        # Phase-2 tables: direct DML must be gone for everyone but the owner
        p2_inserts = {
            s for s in after
            if s[0].startswith("htf_baseline_short_phase2") and s[2] == "INSERT"
        }
        assert all(s[1] in ("postgres",) for s in p2_inserts)
    finally:
        db.rollback()
        with db.cursor() as cur:
            cur.execute(f"DROP OWNED BY {unauth}")
        db.commit()
        admin = _admin_connect()
        with admin.cursor() as cur:
            cur.execute(f"DROP ROLE IF EXISTS {unauth}")
        admin.close()


# ------------------------------------------------- eligibility and runtime


def test_warmup_and_prefreeze_rejected_for_observer(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(db, experiment_id=EXP, warmup_origin=True, **_event_kwargs())
    assert "P2_WARMUP_EVENT_REJECTED" in str(excinfo.value)
    db.rollback()
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    with pytest.raises(Exception) as excinfo:
        insert_observation_raw(
            db, experiment_id=EXP,
            **dict(_event_kwargs(), first_touch_time="2000-01-01T00:00:00+00:00"),
        )
    assert "P2_FIRST_TOUCH_NOT_AFTER_FREEZE" in str(excinfo.value)
    db.rollback()
    with db.cursor() as cur:
        cur.execute("RESET ROLE")


def test_concurrent_observer_inserts_single_row(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    dbname = db.get_dsn_parameters()["dbname"]
    errors: list[str] = []
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        try:
            conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, database=dbname, user=OBSERVER_ROLE)
            row = insert_observation_raw(conn, experiment_id=EXP, **_event_kwargs())
            conn.commit()
            conn.close()
            with lock:
                results.append(row)
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len([r for r in results if r is not None]) == 1
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation"
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_transaction_rollback_leaves_no_rows(db):
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    with db.cursor() as cur:
        cur.execute(f"SET ROLE {OBSERVER_ROLE}")
    insert_observation_raw(db, experiment_id=EXP, **_event_kwargs())
    db.rollback()  # aborted transaction: nothing persists
    with db.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            "SELECT count(*) FROM research.htf_baseline_short_phase2_observation"
        )
        (count,) = cur.fetchone()
    assert count == 0


def test_pg8000_scram_capable_driver_path_as_observer(db):
    pg8000 = pytest.importorskip("pg8000.dbapi")
    apply_075(db)
    _activate(db)
    _enable_observer(db)
    dbname = db.get_dsn_parameters()["dbname"]
    # pg8000 speaks SCRAM-SHA-256 natively; the local trust server ignores the
    # password, so this proves the driver path and credential plumbing. A true
    # SCRAM handshake needs a scram-secured server (staging verification).
    conn = pg8000.connect(
        host=DB_HOST, port=DB_PORT, user=OBSERVER_ROLE, database=dbname,
        password="probe-not-logged", timeout=5,
    )
    try:
        row_id = insert_observation_raw(conn, experiment_id=EXP, **_event_kwargs())
        conn.commit()
        assert row_id is not None
    finally:
        conn.close()
