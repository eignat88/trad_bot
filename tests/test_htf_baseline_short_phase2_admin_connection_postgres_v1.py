"""Issue #205 Phase-2 ADMIN connection routing — PostgreSQL integration tests.

Covers the full ``mint -> claim -> verify`` cycle through the REAL admin
connection path (``app.research.htf_baseline_short_phase2_admin_connection_v1``)
plus the complete trust-boundary matrix, against a DISPOSABLE database on an
ISOLATED test cluster only.

SAFETY: like the other Phase-2 IT modules this suite connects only to the
local test server (``P2_TEST_DB_*``), creates/drops only disposable
``trad_bot_p2_it_*`` databases and ephemeral roles, and blocks production
database names. On top of that (issue #205 hard boundary) it REFUSES to run
against port 5432 at all: integration tests must target the isolated test
cluster (the task validation used 127.0.0.1:5544). The whole module skips when
no test server is reachable.

Positive evidence:
* full cycle via the production-documented ``postgres`` operator login channel
  (trust on loopback) AND via a dedicated SCRAM operator login (real password
  auth, pg8000 and psycopg2 driver paths);
* per-operation privilege matrix: mint/verify run in PostgreSQL READ-ONLY
  sessions and work WITHOUT the activation privilege; only the initial claim
  opens read-write and its single write is the sanctioned activation function;
  direct DML is denied to the admin role in BOTH modes.

Negative evidence (all fail-closed, nothing written, database untouched):
missing admin credentials (the production incident shape), observer
credentials supplied to the admin channel, wrong password, non-member login,
admin role converted to LOGIN, observer RBAC expansion, wrong cluster pin,
unreadable cluster pin, wrong database target, stale timestamp, fingerprint
mismatch, repeated mint/claim, concurrent claims, damaged/missing claim
receipt in recovery, observer attempting to bypass the freeze.

Invariants re-checked after every full cycle: ``observer_enabled`` stays FALSE,
zero observations, no enable-flag file is created, and a Point-B sentinel
table is byte-identical before/after.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "research"))

import app.research.htf_baseline_short_phase2_admin_connection_v1 as admin  # noqa: E402
import htf_baseline_short_phase2_freeze_record_v1 as tool  # noqa: E402

from app.research.htf_baseline_short_phase2_persistence_v1 import (  # noqa: E402
    load_experiment_state,
)
from app.research.htf_baseline_short_phase2_protocol_v1 import (  # noqa: E402
    EXPERIMENT_ID,
    protocol_fingerprint,
)
from app.research.htf_baseline_short_phase2_timestamp_authority_v1 import (  # noqa: E402
    build_freeze_record,
    freeze_record_hash,
    load_freeze_record,
)

DB_HOST = os.environ.get("P2_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("P2_TEST_DB_PORT", "5432"))
DB_USER = os.environ.get("P2_TEST_DB_USER", "postgres")

_DB_PATTERN = re.compile(r"\Atrad_bot_p2_it_[a-z0-9_]+\Z")
_BLOCKED = frozenset({"trad_bot", "postgres", "template0", "template1", "srr", "test"})
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

MIGRATION_074 = ROOT / "sql" / "migrations" / "074_htf_baseline_short_phase2_registration.sql"
MIGRATION_075 = ROOT / "sql" / "migrations" / "075_htf_baseline_short_phase2_privilege_hardening.sql"

OBSERVER_ROLE = "trad_bot_htf_p2_observer"
ADMIN_ROLE = "trad_bot_htf_p2_admin"
FP = protocol_fingerprint()
ACTIVATE_CALL = (
    "SELECT research.htf_baseline_short_phase2_activate(%s, %s, %s)"
)


def _assert_isolation_confirmed() -> None:
    if DB_HOST not in _LOOPBACK and os.environ.get("P2_TEST_DB_ISOLATION_ACK") != "yes":
        raise RuntimeError(
            "P2_TEST_DB_HOST is not loopback and isolation is not confirmed; "
            "refusing to run database-creating tests (issue #205 admin IT)"
        )


def _validate_name(name: str) -> None:
    if name in _BLOCKED or not _DB_PATTERN.match(name):
        raise AssertionError(f"refusing unsafe test database name: {name}")


_assert_isolation_confirmed()


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


pytestmark = [
    pytest.mark.skipif(
        DB_PORT == 5432,
        reason=(
            "issue #205 hard boundary: integration tests never run against the "
            "production port 5432 — run with P2_TEST_DB_PORT=<isolated cluster, e.g. 5544>"
        ),
    ),
    pytest.mark.skipif(
        not _server_available(),
        reason=f"no local test PostgreSQL at {DB_HOST}:{DB_PORT}; admin-connection IT skipped",
    ),
]


# --------------------------------------------------------------- provisioning
class Provisioned:
    def __init__(self, db_name, sid, oper_user, oper_password, observer_password,
                 admin_conn, db_conn):
        self.db_name = db_name
        self.sid = sid
        self.oper_user = oper_user
        self.oper_password = oper_password
        self.observer_password = observer_password
        self.admin_conn = admin_conn  # maintenance database ('postgres')
        self.db_conn = db_conn  # the disposable test database


def _drop_role(conn, role: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DROP OWNED BY {role}")
        cur.execute(f"DROP ROLE IF EXISTS {role}")


@pytest.fixture(scope="module", autouse=True)
def phase2_role_lifecycle():
    """Own the cluster-wide Phase-2 roles for the whole module.

    The roles are created by migration 075 inside the first disposable
    database (075 is deliberately idempotent: create-if-missing + strict
    attribute validation, so any drift — e.g. the admin role converted to
    LOGIN by an interrupted run — fails loudly instead of being reused).
    They are dropped best-effort at module teardown after every disposable
    database is gone.
    """
    yield
    try:
        conn = _admin_connect()
    except Exception:
        return
    for role in (ADMIN_ROLE, OBSERVER_ROLE):
        try:
            _drop_role(conn, role)
        except Exception as exc:
            warnings.warn(f"could not drop test role {role}: {exc}", stacklevel=2)
    conn.close()


@pytest.fixture()
def prov():
    """Disposable database (074+075 applied) + ephemeral operator login per test."""
    _assert_isolation_confirmed()
    admin_conn = _admin_connect()
    name = f"trad_bot_p2_it_{uuid.uuid4().hex[:8]}"
    _validate_name(name)
    with admin_conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database=name, user=DB_USER, connect_timeout=5
    )
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(MIGRATION_074.read_text(encoding="utf-8"))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(MIGRATION_075.read_text(encoding="utf-8"))
    conn.commit()
    oper_user = f"p2_it_oper_{uuid.uuid4().hex[:8]}"
    oper_password = secrets.token_hex(16)
    observer_password = secrets.token_hex(16)
    with admin_conn.cursor() as cur:
        cur.execute(f"CREATE ROLE {oper_user} LOGIN PASSWORD '{oper_password}'")
        cur.execute(f"GRANT {ADMIN_ROLE} TO {oper_user}")
        cur.execute(f"ALTER ROLE {OBSERVER_ROLE} PASSWORD '{observer_password}'")
        cur.execute("SELECT system_identifier FROM pg_control_system()")
        sid = cur.fetchone()[0]
    try:
        yield Provisioned(name, sid, oper_user, oper_password, observer_password,
                          admin_conn, conn)
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        try:
            with admin_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                    (name,),
                )
        except Exception:
            pass
        dropped = False
        try:
            with admin_conn.cursor() as cur:
                # restricted runtimes stall on DROP DATABASE for ~60s before the
                # blocked checkpoint signal errors out — never let teardown hang
                cur.execute("SET statement_timeout = 8000")
                cur.execute(f"DROP DATABASE IF EXISTS {name}")
            dropped = True
        except Exception:
            pass
        if not dropped:
            # Sandboxed Windows runtimes block the process signal DROP DATABASE
            # needs for its checkpoint ("could not signal for checkpoint:
            # Operation not permitted"). Fall back to scrubbing every Phase-2
            # privilege from the leftover disposable database so nothing can
            # depend on these test roles; the empty throwaway shell is harmless
            # and database names stay unique per test.
            try:
                leftover = psycopg2.connect(
                    host=DB_HOST, port=DB_PORT, database=name, user=DB_USER, connect_timeout=5
                )
                leftover.autocommit = True
                with leftover.cursor() as cur:
                    cur.execute(
                        f"DROP OWNED BY {OBSERVER_ROLE}, {ADMIN_ROLE}, {oper_user}"
                    )
                leftover.close()
            except Exception:
                pass
            warnings.warn(
                f"could not DROP DATABASE {name} in this runtime (checkpoint "
                "signaling blocked); privileges scrubbed instead",
                stacklevel=2,
            )
        try:
            _drop_role(admin_conn, oper_user)
        except Exception:
            warnings.warn(f"could not drop test role {oper_user}", stacklevel=2)
        admin_conn.close()


def _set_admin_env(monkeypatch, prov, *, user="postgres", password=None, name=None,
                   system_identifier="pin", port=None):
    """Configure the admin channel exactly like an operator would."""
    monkeypatch.setenv("HTF_P2_ADMIN_DB_HOST", DB_HOST)
    monkeypatch.setenv("HTF_P2_ADMIN_DB_PORT", str(DB_PORT if port is None else port))
    monkeypatch.setenv("HTF_P2_ADMIN_DB_NAME", name or prov.db_name)
    monkeypatch.setenv("HTF_P2_ADMIN_DB_USER", user)
    monkeypatch.setenv("HTF_P2_ADMIN_DB_PASSWORD", password if password is not None else "trust-or-scram")
    monkeypatch.delenv("HTF_P2_ADMIN_DB_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("HTF_P2_ADMIN_DB_SYSTEM_IDENTIFIER", raising=False)
    if system_identifier == "pin":
        monkeypatch.setenv("HTF_P2_ADMIN_DB_SYSTEM_IDENTIFIER", str(prov.sid))
    elif system_identifier:
        monkeypatch.setenv("HTF_P2_ADMIN_DB_SYSTEM_IDENTIFIER", str(system_identifier))


def _run(*argv) -> int:
    return tool.main([str(a) for a in argv])


def _mint_argv(record_path, **extra):
    argv = [
        "mint",
        "--operator-approval-ref", extra.get("approval_ref", "GO-IT-205"),
        "--issued-by", extra.get("issued_by", "it-operator"),
        "--record-id", str(uuid.uuid4()),
        "--out", str(record_path),
    ]
    if extra.get("freeze_ts"):
        argv += ["--freeze-ts", extra["freeze_ts"]]
    return argv


def _claim_argv(record_path, activation_path, repair=False):
    argv = ["claim", "--record", str(record_path), "--activation-out", str(activation_path)]
    if repair:
        argv.append("--repair-activation-json")
    return argv


def _verify_argv(record_path, activation_path):
    return ["verify", "--record", str(record_path), "--activation", str(activation_path)]


def _db_state(prov) -> dict:
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database=prov.db_name, user=DB_USER, connect_timeout=5
    )
    try:
        state = load_experiment_state(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM research.htf_baseline_short_phase2_freeze_registry"
            )
            registry_n = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM research.htf_baseline_short_phase2_observation"
            )
            observation_n = cur.fetchone()[0]
        return {
            "status": state.status,
            "freeze_ts": state.freeze_ts,
            "fingerprint": state.protocol_fingerprint,
            "observer_enabled": state.observer_enabled,
            "registry_n": registry_n,
            "observation_n": observation_n,
        }
    finally:
        conn.close()


def _full_cycle(prov, record_path, activation_path, capsys=None):
    assert _run(*_mint_argv(record_path)) == tool.EXIT_OK
    assert _run(*_claim_argv(record_path, activation_path)) == tool.EXIT_OK
    assert _run(*_verify_argv(record_path, activation_path)) == tool.EXIT_OK
    if capsys is not None:
        assert "THREE_AUTHORITY_AGREEMENT=OK" in capsys.readouterr().out


# ============================================================ positive: full cycle
def test_full_cycle_via_postgres_operator_login(prov, monkeypatch, tmp_path, capsys):
    """Production-documented channel: operator login -> SET ROLE admin."""
    _set_admin_env(monkeypatch, prov, user="postgres")
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _full_cycle(prov, record_path, activation_path, capsys)

    record = load_freeze_record(record_path)
    state = _db_state(prov)
    assert state["status"] == "RUNNING"
    assert state["observer_enabled"] is False  # never touched
    assert state["observation_n"] == 0  # no observer ran
    assert state["registry_n"] == 1
    assert state["fingerprint"] == record["protocol_fingerprint"]
    receipt = activation_path.with_name(activation_path.name + ".claim_receipt.json")
    assert receipt.exists()  # claim receipt sealed before commit
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    assert activation["freeze_ts"] == record["freeze_ts"]
    assert freeze_record_hash(record) == record["record_sha256"]


def test_full_cycle_via_dedicated_operator_login_scram(prov, monkeypatch, tmp_path):
    """A dedicated operator LOGIN (real SCRAM password auth) is sufficient."""
    _set_admin_env(monkeypatch, prov, user=prov.oper_user,
                   password=prov.oper_password, system_identifier=None)
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _full_cycle(prov, record_path, activation_path)
    state = _db_state(prov)
    assert state["status"] == "RUNNING"
    assert state["observer_enabled"] is False


def test_full_cycle_psycopg2_driver_path(prov, monkeypatch, tmp_path):
    """The libpq (psycopg2) fallback driver works identically."""
    _set_admin_env(monkeypatch, prov, user=prov.oper_user,
                   password=prov.oper_password, system_identifier=None)
    monkeypatch.setattr(
        tool,
        "connect_admin_db",
        lambda mode: admin.connect_admin_db(mode, driver="psycopg2"),
    )
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _full_cycle(prov, record_path, activation_path)
    assert _db_state(prov)["status"] == "RUNNING"


# ==================================================== per-operation privilege matrix
def test_mint_and_verify_sessions_are_db_read_only(prov, monkeypatch):
    """READ-ONLY mode: PostgreSQL itself forbids writes (issue #205)."""
    _set_admin_env(monkeypatch, prov, user="postgres")
    conn = admin.connect_admin_db(admin.READ_ONLY_MODE)
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_user")
        assert cur.fetchone()[0] == ADMIN_ROLE
        with pytest.raises(Exception) as excinfo:
            cur.execute(
                "UPDATE research.htf_baseline_short_phase2_experiment "
                "SET status = 'CANCELLED' WHERE experiment_id = %s",
                (EXPERIMENT_ID,),
            )
        assert "read-only" in str(excinfo.value).lower()
        cur.close()
    finally:
        admin.close_admin_db(conn)


def test_claim_session_denies_direct_dml(prov, monkeypatch):
    """ACTIVATE mode: even read-write, direct DML is denied to the admin role."""
    _set_admin_env(monkeypatch, prov, user="postgres")
    conn = admin.connect_admin_db(admin.ACTIVATE_MODE)
    try:
        cur = conn.cursor()
        for relation in admin.PHASE2_RELATIONS:
            with pytest.raises(Exception) as excinfo:
                cur.execute(f"INSERT INTO {relation} DEFAULT VALUES")
            assert "permission denied" in str(excinfo.value).lower()
            conn.rollback()  # clear the aborted-transaction state between attempts
        with pytest.raises(Exception) as excinfo:
            cur.execute(
                "UPDATE research.htf_baseline_short_phase2_experiment "
                "SET status = 'CANCELLED' WHERE experiment_id = %s",
                (EXPERIMENT_ID,),
            )
        assert "permission denied" in str(excinfo.value).lower()
        conn.rollback()
        # the sanctioned write path IS executable for the admin role
        cur.execute(
            "SELECT has_function_privilege(%s, 'EXECUTE')",
            (admin.ACTIVATE_FUNCTION_SIGNATURE,),
        )
        assert cur.fetchone()[0] is True
        cur.close()
    finally:
        admin.close_admin_db(conn)


def test_mint_verify_do_not_require_activation_privilege(prov, monkeypatch, tmp_path):
    """mint works WITHOUT EXECUTE on activate(); only claim needs it."""
    with prov.db_conn.cursor() as cur:
        cur.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "research.htf_baseline_short_phase2_activate(text,timestamp with time zone,text) "
            f"FROM {ADMIN_ROLE}"
        )
    prov.db_conn.commit()
    _set_admin_env(monkeypatch, prov, user="postgres")
    record_path = tmp_path / "freeze_record.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_OK  # mint needs no activation right
    assert record_path.exists()
    assert _run(*_claim_argv(record_path, tmp_path / "activation.json")) == tool.EXIT_REFUSED
    assert not (tmp_path / "activation.json").exists()
    assert _db_state(prov)["status"] == "PENDING"


# ============================================================ negative: credentials
def test_missing_admin_credentials_incident_shape(prov, monkeypatch, tmp_path):
    """The production incident: ONLY observer env present -> every op REFUSED."""
    for key in ("HOST", "PORT", "NAME", "USER", "PASSWORD", "PASSWORD_FILE", "SYSTEM_IDENTIFIER"):
        monkeypatch.delenv(f"HTF_P2_ADMIN_DB_{key}", raising=False)
    monkeypatch.setenv("HTF_P2_DB_HOST", DB_HOST)
    monkeypatch.setenv("HTF_P2_DB_PORT", str(DB_PORT))
    monkeypatch.setenv("HTF_P2_DB_NAME", prov.db_name)
    monkeypatch.setenv("HTF_P2_DB_USER", OBSERVER_ROLE)
    monkeypatch.setenv("HTF_P2_DB_PASSWORD", prov.observer_password)
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    for argv in (
        _mint_argv(record_path),
        _claim_argv(record_path, activation_path),
        _verify_argv(record_path, activation_path),
    ):
        assert _run(*argv) == tool.EXIT_REFUSED
    assert not record_path.exists() and not activation_path.exists()
    assert _db_state(prov)["status"] == "PENDING"


def test_observer_credentials_refused_by_admin_channel(prov, monkeypatch, tmp_path):
    _set_admin_env(monkeypatch, prov, user=OBSERVER_ROLE,
                   password=prov.observer_password, system_identifier=None)
    record_path = tmp_path / "freeze_record.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED
    assert not record_path.exists()
    assert _db_state(prov)["status"] == "PENDING"


def test_wrong_password_fails_closed(prov, monkeypatch, tmp_path):
    """A wrong password is refused at authentication (SCRAM clusters).

    On a trust-auth test cluster the server accepts any password, so this
    refusal cannot materialize there and the scenario is skipped with that
    exact reason (it runs on the SCRAM-configured test cluster).
    """
    try:
        probe = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, database=prov.db_name,
            user=prov.oper_user, password="wrong-" + prov.oper_password,
            connect_timeout=5,
        )
        probe.close()
    except Exception:
        pass  # password authentication is enforced -> the scenario is testable
    else:
        pytest.skip(
            "test cluster accepts any password (trust auth); the wrong-password "
            "refusal is not exercisable here"
        )
    _set_admin_env(monkeypatch, prov, user=prov.oper_user,
                   password="wrong-" + prov.oper_password, system_identifier=None)
    record_path = tmp_path / "freeze_record.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED
    assert not record_path.exists()


def test_non_member_login_refused(prov, monkeypatch, tmp_path):
    """A login that may not SET ROLE to the admin role is refused."""
    stranger = f"p2_it_stranger_{uuid.uuid4().hex[:8]}"
    stranger_password = secrets.token_hex(16)
    with prov.admin_conn.cursor() as cur:
        cur.execute(f"CREATE ROLE {stranger} LOGIN PASSWORD '{stranger_password}'")
    try:
        _set_admin_env(monkeypatch, prov, user=stranger,
                       password=stranger_password, system_identifier=None)
        record_path = tmp_path / "freeze_record.json"
        assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED
        assert not record_path.exists()
    finally:
        _drop_role(prov.admin_conn, stranger)


def test_admin_role_converted_to_login_refused(prov, monkeypatch, tmp_path):
    """The NOLOGIN invariant of trad_bot_htf_p2_admin is re-verified."""
    with prov.admin_conn.cursor() as cur:
        cur.execute(f"ALTER ROLE {ADMIN_ROLE} LOGIN")
    try:
        _set_admin_env(monkeypatch, prov, user="postgres")
        record_path = tmp_path / "freeze_record.json"
        assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED
        assert not record_path.exists()
    finally:
        with prov.admin_conn.cursor() as cur:
            cur.execute(f"ALTER ROLE {ADMIN_ROLE} NOLOGIN")


def test_observer_rbac_expansion_refused(prov, monkeypatch, tmp_path):
    """Observer EXECUTE on activate() = RBAC expansion -> everything REFUSED."""
    with prov.db_conn.cursor() as cur:
        cur.execute(
            "GRANT EXECUTE ON FUNCTION "
            "research.htf_baseline_short_phase2_activate(text,timestamp with time zone,text) "
            f"TO {OBSERVER_ROLE}"
        )
    prov.db_conn.commit()
    _set_admin_env(monkeypatch, prov, user="postgres")
    record_path = tmp_path / "freeze_record.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED
    assert not record_path.exists()
    assert _db_state(prov)["status"] == "PENDING"


# ================================================== negative: wrong target / cluster
def test_wrong_cluster_pin_refused(prov, monkeypatch, tmp_path):
    _set_admin_env(monkeypatch, prov, user="postgres", system_identifier="1" * 8)
    record_path = tmp_path / "freeze_record.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED
    assert not record_path.exists()


def test_cluster_pin_mismatch_on_operator_login_refused(prov, monkeypatch, tmp_path):
    """The cluster pin is verified on the dedicated-login channel too."""
    _set_admin_env(monkeypatch, prov, user=prov.oper_user,
                   password=prov.oper_password, system_identifier="1" * 8)
    record_path = tmp_path / "freeze_record.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED
    assert not record_path.exists()


def test_wrong_database_target_fails_closed(prov, monkeypatch, tmp_path):
    """A database without the Phase-2 surface is refused by the channel itself.

    Pointing ``HTF_P2_ADMIN_DB_NAME`` at the maintenance database (or any
    database without the frozen Phase-2 relations/functions) can never touch
    the real registration: the RBAC invariants cannot be verified there, so the
    admin connection refuses before anything is written.
    """
    _set_admin_env(monkeypatch, prov, user="postgres", name="postgres")
    record_path = tmp_path / "freeze_record.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED
    assert not record_path.exists()
    assert _db_state(prov)["status"] == "PENDING"  # the real registration untouched


def test_nonexistent_database_fails_closed(prov, monkeypatch, tmp_path):
    _set_admin_env(monkeypatch, prov, user="postgres", name="trad_bot_p2_it_missing_db")
    record_path = tmp_path / "freeze_record.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED
    assert not record_path.exists()


# ================================================ negative: freeze boundary / repeats
def test_stale_freeze_ts_refused(prov, monkeypatch, tmp_path):
    _set_admin_env(monkeypatch, prov, user="postgres")
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    record_path = tmp_path / "freeze_record.json"
    assert _run(*_mint_argv(record_path, freeze_ts=stale)) == tool.EXIT_REFUSED
    assert not record_path.exists()
    assert _db_state(prov)["status"] == "PENDING"


def test_fingerprint_mismatch_refused(prov, monkeypatch, tmp_path):
    _set_admin_env(monkeypatch, prov, user="postgres")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bad = build_freeze_record(
        experiment_id=EXPERIMENT_ID,
        freeze_ts=now,
        protocol_fingerprint="ab" * 32,
        claimed_at=now,
        operator_approval_ref="GO-IT-205",
        issued_by="it-operator",
        record_id=str(uuid.uuid4()),
    )
    record_path = tmp_path / "freeze_record.json"
    record_path.write_text(json.dumps(bad, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert _run(*_claim_argv(record_path, tmp_path / "activation.json")) == tool.EXIT_REFUSED
    assert _db_state(prov)["status"] == "PENDING"


def test_repeated_mint_and_claim_refused(prov, monkeypatch, tmp_path):
    _set_admin_env(monkeypatch, prov, user="postgres")
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_OK
    before = record_path.read_text(encoding="utf-8")
    assert _run(*_mint_argv(record_path)) == tool.EXIT_REFUSED  # no overwrite
    assert record_path.read_text(encoding="utf-8") == before
    assert _run(*_claim_argv(record_path, activation_path)) == tool.EXIT_OK
    # a SECOND record (even freshly minted) can never claim the boundary again
    second = tmp_path / "freeze_record_2.json"
    assert _run(*_mint_argv(second)) == tool.EXIT_OK  # mint itself is harmless
    assert _run(*_claim_argv(second, tmp_path / "activation_2.json")) == tool.EXIT_REFUSED
    assert not (tmp_path / "activation_2.json").exists()
    assert _db_state(prov)["registry_n"] == 1


def test_concurrent_claims_exactly_one_winner(prov, monkeypatch, tmp_path):
    """Two racing claims through real admin connections: one wins, one fails closed."""
    _set_admin_env(monkeypatch, prov, user="postgres")
    records = []
    for i in range(2):
        record_path = tmp_path / f"freeze_record_{i}.json"
        assert _run(*_mint_argv(record_path)) == tool.EXIT_OK
        records.append(record_path)
    results = []
    barrier = threading.Barrier(2)

    def worker(record_path, index):
        barrier.wait()
        results.append(
            _run(*_claim_argv(record_path, tmp_path / f"activation_{index}.json"))
        )

    threads = [
        threading.Thread(target=worker, args=(records[i], i)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert sorted(results) == [tool.EXIT_OK, tool.EXIT_REFUSED]
    state = _db_state(prov)
    assert state["registry_n"] == 1
    assert state["status"] == "RUNNING"


# ================================================== recovery / partial-failure paths
def test_partial_commit_repair_roundtrip(prov, monkeypatch, tmp_path):
    """Crash AFTER the DB commit but BEFORE the activation JSON -> repair."""
    _set_admin_env(monkeypatch, prov, user="postgres")
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    assert _run(*_mint_argv(record_path)) == tool.EXIT_OK

    real_write = tool._atomic_write_json
    calls = {"n": 0}

    def crash_once(path, payload, overwrite=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated crash after DB commit")
        return real_write(path, payload, overwrite=overwrite)

    monkeypatch.setattr(tool, "_atomic_write_json", crash_once)
    assert _run(*_claim_argv(record_path, activation_path)) == tool.EXIT_REFUSED
    assert not activation_path.exists()
    state = _db_state(prov)
    assert state["status"] == "RUNNING"  # DB commit survived
    receipt = activation_path.with_name(activation_path.name + ".claim_receipt.json")
    assert receipt.exists()  # identity sealed before commit (FIX-4)

    monkeypatch.setattr(tool, "_atomic_write_json", real_write)
    assert _run(*_claim_argv(record_path, activation_path, repair=True)) == tool.EXIT_OK
    assert activation_path.exists()
    assert _run(*_verify_argv(record_path, activation_path)) == tool.EXIT_OK


def test_recovery_missing_or_damaged_receipt_refused(prov, monkeypatch, tmp_path):
    """Repair without a pristine claim receipt always fails closed."""
    _set_admin_env(monkeypatch, prov, user="postgres")
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _full_cycle(prov, record_path, activation_path)
    receipt = activation_path.with_name(activation_path.name + ".claim_receipt.json")
    original_receipt = receipt.read_bytes()
    activation_path.unlink()

    receipt.unlink()  # missing receipt -> identity cannot be established
    assert _run(*_claim_argv(record_path, activation_path, repair=True)) == tool.EXIT_REFUSED
    assert not activation_path.exists()

    receipt.write_bytes(original_receipt)
    damaged = json.loads(original_receipt.decode("utf-8"))
    damaged["record_sha256"] = "0" * 64
    receipt.write_text(json.dumps(damaged, sort_keys=True) + "\n", encoding="utf-8")
    assert _run(*_claim_argv(record_path, activation_path, repair=True)) == tool.EXIT_REFUSED
    assert not activation_path.exists()


def test_verify_refuses_tampered_activation(prov, monkeypatch, tmp_path):
    _set_admin_env(monkeypatch, prov, user="postgres")
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _full_cycle(prov, record_path, activation_path)
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    activation["freeze_ts"] = "2030-01-01T00:00:00Z"
    activation_path.write_text(json.dumps(activation, sort_keys=True), encoding="utf-8")
    assert _run(*_verify_argv(record_path, activation_path)) == tool.EXIT_REFUSED


# ============================================================ observer isolation
def test_observer_cannot_bypass_freeze(prov, monkeypatch):
    """The observer login can neither SET ROLE to admin nor execute activate()."""
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database=prov.db_name,
        user=OBSERVER_ROLE, password=prov.observer_password, connect_timeout=5,
    )
    try:
        with conn.cursor() as cur:
            with pytest.raises(Exception) as excinfo:
                cur.execute(f"SET ROLE {ADMIN_ROLE}")
            assert "role" in str(excinfo.value).lower()
            conn.rollback()
            with pytest.raises(Exception) as excinfo:
                cur.execute(ACTIVATE_CALL, (EXPERIMENT_ID, "2030-01-01T00:00:00Z", FP))
            assert "permission denied" in str(excinfo.value).lower()
        conn.rollback()
    finally:
        conn.close()
    assert _db_state(prov)["status"] == "PENDING"


def test_invariants_no_side_effects_and_point_b_sentinel(prov, monkeypatch, tmp_path):
    """Full cycle leaves observer disabled, zero observations and Point-B intact."""
    with prov.db_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE research.prospective_point_b_sentinel AS "
            "SELECT 'HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE' AS experiment_id, "
            "'2026-10-06T14:00:00Z'::timestamptz AS freeze_ts, 42 AS metric"
        )
    prov.db_conn.commit()
    _set_admin_env(monkeypatch, prov, user="postgres")
    record_path = tmp_path / "freeze_record.json"
    activation_path = tmp_path / "activation.json"
    _full_cycle(prov, record_path, activation_path)
    state = _db_state(prov)
    assert state["observer_enabled"] is False  # observer NEVER enabled
    assert state["observation_n"] == 0  # no observation, ever
    with prov.db_conn.cursor() as cur:
        cur.execute("SELECT experiment_id, freeze_ts, metric FROM research.prospective_point_b_sentinel")
        sentinel = cur.fetchone()
    assert sentinel[0] == "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"
    assert sentinel[1].strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-10-06T14:00:00Z"
    assert sentinel[2] == 42
    # no enable flag / gate file is ever created by the freeze tool
    flags = [p.name for p in tmp_path.rglob("*") if "observer_enabled" in p.name or "flag" in p.name]
    assert flags == []
    produced = sorted(p.name for p in tmp_path.iterdir())
    assert produced == sorted(
        [
            "freeze_record.json",
            "activation.json",
            "activation.json.claim_receipt.json",
        ]
    )


@pytest.mark.parametrize(
    "mode",
    [admin.READ_ONLY_MODE, admin.ACTIVATE_MODE],
)
def test_admin_role_survives_commit_and_rollback(prov, monkeypatch, mode):
    """A transaction boundary must never restore the privileged login."""
    _set_admin_env(monkeypatch, prov, user="postgres")
    conn = admin.connect_admin_db(mode)

    try:
        def check_context():
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT current_user, session_user, "
                    "current_setting('transaction_read_only')"
                )
                current_user, session_user, read_only = cur.fetchone()
            finally:
                cur.close()

            assert current_user == ADMIN_ROLE
            assert session_user == "postgres"
            assert read_only == (
                "on" if mode == admin.READ_ONLY_MODE else "off"
            )

        check_context()

        conn.rollback()
        check_context()

        conn.commit()
        check_context()

        with pytest.raises(Exception) as excinfo:
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE research.htf_baseline_short_phase2_experiment "
                    "SET status = 'CANCELLED'"
                )
            finally:
                cur.close()

        error = str(excinfo.value).lower()

        if mode == admin.READ_ONLY_MODE:
            assert "read-only" in error
        else:
            assert "permission denied" in error

        conn.rollback()
        check_context()

    finally:
        admin.close_admin_db(conn)
