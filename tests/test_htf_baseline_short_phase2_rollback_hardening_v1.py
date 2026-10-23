"""Issue #185 — PostgreSQL integration matrix T01–T20 for migration 075
rollback hardening, on DISPOSABLE PostgreSQL databases only.

SAFETY: connects to the local test server (P2_TEST_DB_*), creates/drops only
``trad_bot_p2_it_*`` databases and test-owned roles; the production
``trad_bot`` database name is blocked.  Connection targets are fail-closed:
the server must be loopback (or explicitly confirmed isolated via
P2_TEST_DB_ISOLATION_ACK=yes) and every test database must match
``trad_bot_p2_it_*``.  The module skips when no local test server is
reachable.

Matrix (see docs/research/HTF_BASELINE_SHORT_PHASE2_MIGRATION_075_ROLLBACK_HARDENING_V1.md):

  T01 snapshot before 075                T11 existing roles preserved
  T02 apply 074 -> 075                   T12 Phase-2 data preserved
  T03 observer least privilege           T13 missing snapshot = FAIL CLOSED
  T04 admin activation-only access       T14 checksum mismatch = FAIL CLOSED
  T05 PUBLIC access revoked              T15 object identity drift = FAIL CLOSED
  T06 application roles isolated         T16 unexpected role dependency = FAIL CLOSED
  T07 rollback 075                       T17 interrupted rollback = no partial state
  T08 ACL before == ACL after rollback   T18 repeated rollback = safe refusal
  T09 SECURITY DEFINER restored          T19 unrelated research objects unchanged
  T10 search_path restored               T20 existing Point-B stack unchanged

Plus adversarial cases with pre-seeded non-standard PUBLIC / trad_bot /
custom-role grants.  The exact ACL comparison is a tuple-level comparison of
(grantor, grantee, privilege, grantable) over the effective ACL — never a
mere SELECT/INSERT availability check.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402

DB_HOST = os.environ.get("P2_TEST_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("P2_TEST_DB_PORT", "5432"))
DB_USER = os.environ.get("P2_TEST_DB_USER", "postgres")

_DB_PATTERN = re.compile(r"\Atrad_bot_p2_it_[a-z0-9_]+\Z")
_BLOCKED = frozenset({"trad_bot", "postgres", "template0", "template1", "srr", "test"})
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_074 = ROOT / "sql" / "migrations" / "074_htf_baseline_short_phase2_registration.sql"
MIGRATION_075 = ROOT / "sql" / "migrations" / "075_htf_baseline_short_phase2_privilege_hardening.sql"

sys.path.insert(0, str(ROOT / "tools" / "research"))
import htf_baseline_short_phase2_acl_snapshot_v1 as snap  # noqa: E402
import htf_baseline_short_phase2_rollback_075_v1 as rb  # noqa: E402

OBSERVER_ROLE = rb.OBSERVER_ROLE
ADMIN_ROLE = rb.ADMIN_ROLE
EXP = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"
P2_TABLES = (
    "research.htf_baseline_short_phase2_experiment",
    "research.htf_baseline_short_phase2_observation",
    "research.htf_baseline_short_phase2_freeze_registry",
)
P2_SEQ = "research.htf_baseline_short_phase2_observation_observation_id_seq"
INSERT_FN = "research.htf_baseline_short_phase2_insert_observation"
ACTIVATE_FN = "research.htf_baseline_short_phase2_activate"


def _assert_isolation_confirmed() -> None:
    if DB_HOST not in _LOOPBACK and os.environ.get("P2_TEST_DB_ISOLATION_ACK") != "yes":
        raise RuntimeError(
            "P2_TEST_DB_HOST is not loopback and isolation is not confirmed; "
            "refusing to run database-creating tests"
        )


def _assert_safe_database(name: str) -> None:
    if name in _BLOCKED or not _DB_PATTERN.match(name):
        raise RuntimeError(f"refusing unsafe test database name: {name}")


_assert_isolation_confirmed()

_server_up = True
try:
    _probe = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database="postgres", user=DB_USER, connect_timeout=4
    )
    _probe.close()
except Exception:
    _server_up = False

pytestmark = pytest.mark.skipif(
    not _server_up,
    reason=f"no isolated test PostgreSQL at {DB_HOST}:{DB_PORT}; matrix skipped",
)


# --------------------------------------------------------------------------- #
# connection / provisioning helpers
# --------------------------------------------------------------------------- #
def _admin_connect():
    _assert_isolation_confirmed()
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, database="postgres", user=DB_USER, connect_timeout=5
    )


@contextmanager
def _tracked_resources():
    """Track every disposable database/role a test creates and clean up."""
    created_databases: list[str] = []
    created_roles: list[str] = []
    try:
        yield {"databases": created_databases, "roles": created_roles}
    finally:
        admin = _admin_connect()
        admin.autocommit = True
        try:
            with admin.cursor() as cur:
                for dbname in created_databases:
                    cur.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (dbname,),
                    )
                    cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
                # test-owned roles: 075 roles plus any role this test created;
                # each statement is best-effort so a failing assertion in the
                # test never hides the cleanup
                for role in list(created_roles) + [ADMIN_ROLE, OBSERVER_ROLE]:
                    try:
                        cur.execute(f'DROP ROLE IF EXISTS "{role}"')
                    except Exception:
                        admin.rollback()
        finally:
            admin.close()


def _provision_db(resources, *, apply_074: bool = True):
    name = f"trad_bot_p2_it_rollback_{uuid.uuid4().hex[:8]}"
    _assert_safe_database(name)
    admin = _admin_connect()
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    resources["databases"].append(name)
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=name, user=DB_USER)
    conn.autocommit = False
    if apply_074:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_074.read_text(encoding="utf-8"))
        conn.commit()
    return conn, name


def _apply_075(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(MIGRATION_075.read_text(encoding="utf-8"))
    conn.commit()


def _ensure_role(resources, conn, role: str, ddl: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        exists = cur.fetchone() is not None
    if not exists:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        resources["roles"].append(role)


def _capture(conn) -> dict:
    return snap.capture_state(conn)


def _snapshot_file(tmp_path, state: dict, tag: str) -> Path:
    path = Path(tmp_path) / f"{tag}.json"
    snap.write_snapshot(path, snap.build_snapshot(state, {"database": tag}))
    return path


def _assert_exact_acl_equal(before: dict, after: dict) -> None:
    """Tuple-level ACL comparison — the proof required by issue #185."""
    report = snap.compare_states(before, after)
    assert report["acl_equal"], report["acl_differences"]
    assert report["effective_privileges_equal"], report["effective_privilege_differences"]
    before_objs = snap._objects_by_key(before)
    after_objs = snap._objects_by_key(after)
    assert set(before_objs) == set(after_objs)
    for key in before_objs:
        assert (
            before_objs[key].get("acl_effective") == after_objs[key].get("acl_effective")
        ), f"acl_effective mismatch for {key}"
    return report


# --------------------------------------------------------------------------- #
# T01 — snapshot before 075
# --------------------------------------------------------------------------- #
def test_t01_snapshot_before_075_captures_full_surface(tmp_path):
    with _tracked_resources() as res:
        conn, dbname = _provision_db(res)
        state = _capture(conn)
        again = _capture(conn)
        # determinism: identical state -> byte-identical payload
        assert snap.payload_sha256(state) == snap.payload_sha256(again)
        assert snap.canonical_bytes(state) == snap.canonical_bytes(again)

        # every required section is present
        assert state["database"]["key"] == f"database:{dbname}"
        assert state["database"]["acl_effective"]
        keys = snap._objects_by_key(state)
        for table in P2_TABLES:
            assert f"relation:{table}" in keys
        assert f"relation:{P2_SEQ}" in keys
        for fn in (
            snap.PHASE2_FUNCTIONS[0],
            snap.PHASE2_FUNCTIONS[1],
            snap.PHASE2_FUNCTIONS[2],
            snap.PHASE2_FUNCTIONS[3],
            snap.PHASE2_FUNCTIONS[4],
        ):
            assert f"function:{fn}" in keys, fn
        research = [s for s in state["schemas"] if s["identity"]["name"] == "research"]
        assert research and research[0]["affected"]
        assert "roles" in state and "memberships" in state
        assert "default_privileges" in state and "shdepend" in state
        assert "effective_privileges" in state
        # explicit vs effective distinction is recorded
        rel = keys[f"relation:{P2_TABLES[1]}"]
        assert rel["acl_is_null"] is True  # fresh 074 object keeps default ACL
        assert rel["acl_explicit"] == []
        assert any(e[1] == "postgres" for e in rel["acl_effective"])

        # function identity + configuration captured exactly
        fn = keys[f"function:{snap.PHASE2_FUNCTIONS[0]}"]
        assert fn["config"]["security"] == "INVOKER"
        assert fn["config"]["proconfig"] == []
        assert len(fn["identity"]["prosrc_md5"]) == 32

        # checksum + sidecar, and no secrets anywhere
        path = _snapshot_file(tmp_path, state, "t01")
        snap.verify_snapshot_file(path)
        text = path.read_text(encoding="utf-8")
        assert "SCRAM-SHA-256$" not in text
        assert "rolpassword" not in text
        snap.assert_no_secrets(state)

        # read-only proof: no mutating SQL statement anywhere in the tool
        source = (ROOT / "tools" / "research" / (
            "htf_baseline_short_phase2_acl_snapshot_v1.py")).read_text(encoding="utf-8")
        code_only = source.split('"""', 2)[-1]  # after the module docstring
        assert not re.search(
            r"(?im)^\s*(grant|revoke|drop|create|alter|insert\s+into|update\s|delete\s+from)\b",
            code_only,
        ), "snapshot tool must stay read-only"
        assert "default_transaction_read_only = on" in source


# --------------------------------------------------------------------------- #
# T02 — apply 074 -> 075 and post-apply state
# --------------------------------------------------------------------------- #
def test_t02_apply_074_then_075():
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        _apply_075(conn)
        post = _capture(conn)
        report = snap.compare_states(pre, post)
        # every difference must be explainable by the audited 075 delta
        explained, derived, unexpected = rb.classify_changes(report["changes"], pre, post)
        assert unexpected == [], unexpected
        assert explained, "075 must change something"
        # both roles exist with the contracted attributes
        assert set(post["roles"]) >= {OBSERVER_ROLE, ADMIN_ROLE}
        for role, attrs in rb._EXPECTED_ROLE_ATTRIBUTES.items():
            assert post["roles"][role] == attrs
        conn.close()


# --------------------------------------------------------------------------- #
# T03 / T04 / T05 / T06 — post-075 privilege matrix
# --------------------------------------------------------------------------- #
def _effective(state: dict, role: str, key: str) -> dict:
    return state["effective_privileges"].get(role, {}).get(key, {})


def test_t03_observer_least_privilege():
    with _tracked_resources() as res:
        conn, dbname = _provision_db(res)
        _apply_075(conn)
        state = _capture(conn)
        for table in P2_TABLES:
            eff = _effective(state, OBSERVER_ROLE, f"relation:{table}")
            assert eff.get("SELECT") is True
            for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                assert eff.get(priv) is False, (table, priv)
        eff = _effective(state, OBSERVER_ROLE, f"function:{snap.PHASE2_FUNCTIONS[0]}")
        assert eff.get("EXECUTE") is True
        eff = _effective(state, OBSERVER_ROLE, f"function:{snap.PHASE2_FUNCTIONS[1]}")
        assert eff.get("EXECUTE") is False
        eff = _effective(state, OBSERVER_ROLE, f"relation:{P2_SEQ}")
        assert eff.get("USAGE") is False and eff.get("SELECT") is False
        eff = _effective(state, OBSERVER_ROLE, "schema:research")
        assert eff.get("USAGE") is True and eff.get("CREATE") is False
        eff = _effective(state, OBSERVER_ROLE, f"database:{dbname}")
        assert eff.get("CONNECT") is True and eff.get("CREATE") is False
        conn.close()


def test_t04_admin_activation_only_access():
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        _apply_075(conn)
        state = _capture(conn)
        eff = _effective(state, ADMIN_ROLE, f"function:{snap.PHASE2_FUNCTIONS[1]}")
        assert eff.get("EXECUTE") is True
        eff = _effective(state, ADMIN_ROLE, f"function:{snap.PHASE2_FUNCTIONS[0]}")
        assert eff.get("EXECUTE") is False
        for table in P2_TABLES:
            eff = _effective(state, ADMIN_ROLE, f"relation:{table}")
            assert eff.get("SELECT") is True
            assert eff.get("INSERT") is False and eff.get("DELETE") is False
        # and the activation path is unavailable to the runtime observer role
        eff = _effective(state, OBSERVER_ROLE, f"function:{snap.PHASE2_FUNCTIONS[1]}")
        assert eff.get("EXECUTE") is False
        conn.close()


def test_t05_public_access_revoked_on_phase2_surface():
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        _apply_075(conn)
        state = _capture(conn)
        keys = snap._objects_by_key(state)
        for key in [f"relation:{t}" for t in P2_TABLES] + [
            f"function:{f}" for f in snap.PHASE2_FUNCTIONS
        ]:
            public = [e for e in keys[key]["acl_effective"] if e[1] == "PUBLIC"]
            assert public == [], (key, public)
        # sequence: PUBLIC must hold nothing on the Phase-2 sequence either
        public = [
            e for e in keys[f"relation:{P2_SEQ}"]["acl_effective"] if e[1] == "PUBLIC"
        ]
        assert public == [], public
        conn.close()


def test_t06_existing_application_roles_isolated():
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        for role in ("trad_bot", "analytics_runner", "analytics_agent"):
            _ensure_role(res, conn, role, f'CREATE ROLE "{role}" NOLOGIN')
        # pre-seeded non-standard grants that 075 must revoke
        with conn.cursor() as cur:
            cur.execute(
                f"GRANT SELECT, INSERT, UPDATE ON {P2_TABLES[1]} TO trad_bot "
                f"WITH GRANT OPTION"
            )
            cur.execute(f"GRANT ALL ON {P2_TABLES[0]} TO analytics_runner")
            cur.execute(f"GRANT SELECT ON {P2_TABLES[2]} TO analytics_agent")
            cur.execute(f"GRANT USAGE, SELECT, UPDATE ON {P2_SEQ} TO trad_bot")
            cur.execute(f"GRANT EXECUTE ON FUNCTION {INSERT_FN} TO trad_bot")
            cur.execute(f"GRANT EXECUTE ON FUNCTION {ACTIVATE_FN} TO analytics_runner")
        conn.commit()
        _apply_075(conn)
        state = _capture(conn)
        for role in ("trad_bot", "analytics_runner", "analytics_agent"):
            for table in P2_TABLES:
                eff = _effective(state, role, f"relation:{table}")
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert eff.get(priv) is False, (role, table, priv)
            for fn in (snap.PHASE2_FUNCTIONS[0], snap.PHASE2_FUNCTIONS[1]):
                assert _effective(state, role, f"function:{fn}").get("EXECUTE") is False
        assert _effective(state, "trad_bot", f"relation:{P2_SEQ}").get("USAGE") is False
        # unrelated research objects are not touched by 075 either
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE research.p2hardening_t06_decoy(x int)")
            cur.execute("GRANT SELECT ON research.p2hardening_t06_decoy TO trad_bot")
        conn.commit()
        keys = snap._objects_by_key(state)
        assert "relation:research.p2hardening_t06_decoy" not in keys
        conn.close()


# --------------------------------------------------------------------------- #
# T07 / T08 / T09 / T10 — rollback restores exactly
# --------------------------------------------------------------------------- #
def test_t07_rollback_075_completes(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t07_pre")
        _apply_075(conn)
        report = rb.run_rollback(conn, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        assert report["executed_statements"]
        assert report["roles_dropped_verified"] == [ADMIN_ROLE, OBSERVER_ROLE]
        conn.close()


def test_t08_exact_acl_before_equals_after_rollback(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t08_pre")
        _apply_075(conn)
        rb.run_rollback(conn, snap_path)
        post = _capture(conn)
        report = _assert_exact_acl_equal(pre, post)
        # the comparison is tuple-level, not a SELECT/INSERT smoke check
        before_objs = snap._objects_by_key(pre)
        for key, entry in before_objs.items():
            assert entry.get("acl_effective") == snap._objects_by_key(post)[key][
                "acl_effective"
            ]
        # representation-only notes never hide an ACL difference
        assert report["acl_differences"] == []
        conn.close()


def test_t09_security_definer_restored(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t09_pre")
        assert pre["functions"][0]["config"]["security"] == "INVOKER"
        _apply_075(conn)
        during = _capture(conn)
        keys = snap._objects_by_key(during)
        for fn in (snap.PHASE2_FUNCTIONS[0], snap.PHASE2_FUNCTIONS[1]):
            assert keys[f"function:{fn}"]["config"]["security"] == "DEFINER"
        rb.run_rollback(conn, snap_path)
        post = _capture(conn)
        keys = snap._objects_by_key(post)
        for fn in (snap.PHASE2_FUNCTIONS[0], snap.PHASE2_FUNCTIONS[1]):
            assert keys[f"function:{fn}"]["config"]["security"] == "INVOKER"
        conn.close()


def test_t10_search_path_restored(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t10_pre")
        _apply_075(conn)
        during = snap._objects_by_key(_capture(conn))
        for fn in (snap.PHASE2_FUNCTIONS[0], snap.PHASE2_FUNCTIONS[1]):
            assert during[f"function:{fn}"]["config"]["proconfig"] == [
                "search_path=pg_catalog, research"
            ]
        rb.run_rollback(conn, snap_path)
        post = snap._objects_by_key(_capture(conn))
        pre_keys = snap._objects_by_key(pre)
        # exact: same proconfig (and security) as before 075 for every
        # Phase-2 function — including the three guard functions
        for fn in snap.PHASE2_FUNCTIONS:
            assert (
                post[f"function:{fn}"]["config"]
                == pre_keys[f"function:{fn}"]["config"]
            )
            assert post[f"function:{fn}"]["config"]["proconfig"] == []
        conn.close()


# --------------------------------------------------------------------------- #
# T11 / T12 — roles preserved, data preserved
# --------------------------------------------------------------------------- #
def test_t11_existing_roles_preserved(tmp_path):
    with _tracked_resources() as res:
        conn, dbname = _provision_db(res)
        # the two 075 roles PRE-EXIST 075 with valid attributes: they must
        # survive the rollback untouched (075 only validated them)
        _ensure_role(res, conn, OBSERVER_ROLE, f'CREATE ROLE "{OBSERVER_ROLE}" LOGIN NOINHERIT')
        _ensure_role(res, conn, ADMIN_ROLE, f'CREATE ROLE "{ADMIN_ROLE}" NOLOGIN NOINHERIT')
        _ensure_role(res, conn, "trad_bot", 'CREATE ROLE "trad_bot" NOLOGIN')
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t11_pre")
        _apply_075(conn)
        report = rb.run_rollback(conn, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        assert report["roles_to_drop"] == []
        assert report["roles_preserved"] == [ADMIN_ROLE, OBSERVER_ROLE]
        post = _capture(conn)
        for role in (OBSERVER_ROLE, ADMIN_ROLE, "trad_bot", "postgres"):
            assert role in post["roles"]
            assert post["roles"][role] == pre["roles"][role]
        conn.close()


def test_t12_phase2_data_preserved(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)  # the pre-075 snapshot is taken BEFORE 075
        snap_path = _snapshot_file(tmp_path, pre, "t12_pre")
        _apply_075(conn)
        # write Phase-2 data through the 075 SECURITY DEFINER wall, exactly as
        # the runtime would: admin activates, observer inserts
        with conn.cursor() as cur:
            cur.execute("SELECT (now() + interval '2 minutes')::text")
            freeze = cur.fetchone()[0]
            cur.execute(f"SET ROLE \"{ADMIN_ROLE}\"")
            cur.execute(f"SELECT {ACTIVATE_FN}(%s, %s, %s)", (EXP, freeze, "0" * 64))
            cur.execute("RESET ROLE")
            cur.execute(
                "UPDATE research.htf_baseline_short_phase2_experiment "
                "SET observer_enabled = TRUE WHERE experiment_id = %s",
                (EXP,),
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT (%s::timestamptz + interval '1 minute')::text, "
                "(%s::timestamptz + interval '2 minutes')::text",
                (freeze, freeze),
            )
            first_touch, signal = cur.fetchone()
            # contract: source_event_id == episode_id || ':FIRST_CONFIRM'
            # and signal_time > first_touch_time > freeze_ts
            cur.execute(f"SET ROLE \"{OBSERVER_ROLE}\"")
            cur.execute(
                f"SELECT {INSERT_FN}(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (EXP, "rollbackt12:FIRST_CONFIRM", "rollbackt12", "lvl1", "BTCUSDT",
                 signal, first_touch, signal, 100.0, 101.0, False, False, "{}"),
            )
            cur.execute("RESET ROLE")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), md5(string_agg(source_event_id, ',' ORDER BY source_event_id)) "
                f"FROM {P2_TABLES[1]}"
            )
            before_rows = cur.fetchone()
        assert before_rows[0] == 1, "the observation must have been written"
        conn.commit()  # close the read transaction; run_rollback owns the next one
        report = rb.run_rollback(conn, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), md5(string_agg(source_event_id, ',' ORDER BY source_event_id)) "
                f"FROM {P2_TABLES[1]}"
            )
            after_rows = cur.fetchone()
        assert before_rows == after_rows, "Phase-2 observations changed by rollback"
        # the frozen experiment row is intact as well
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT status, freeze_ts FROM {P2_TABLES[0]} WHERE experiment_id = %s",
                (EXP,),
            )
            status, freeze_ts = cur.fetchone()
        assert status == "RUNNING" and freeze_ts is not None
        conn.close()


# --------------------------------------------------------------------------- #
# T13 / T14 / T15 / T16 — FAIL CLOSED paths
# --------------------------------------------------------------------------- #
def _assert_state_unchanged(conn, before_state: dict) -> None:
    after = _capture(conn)
    assert snap.payload_sha256(after) == snap.payload_sha256(before_state)


def test_t13_missing_snapshot_fail_closed(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        _apply_075(conn)
        before = _capture(conn)
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, None)
        assert "P2_RB_SNAPSHOT_REQUIRED" in str(exc.value)
        missing = Path(tmp_path) / "does_not_exist.json"
        with pytest.raises(snap.SnapshotIntegrityError) as exc:
            rb.run_rollback(conn, missing)
        assert "SNAPSHOT_FILE_MISSING" in str(exc.value)
        _assert_state_unchanged(conn, before)
        conn.close()


def test_t14_snapshot_checksum_mismatch_fail_closed(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t14_pre")
        _apply_075(conn)
        before = _capture(conn)

        # (a) any edit of the snapshot file breaks the sidecar checksum
        payload = json.loads(snap_path.read_text(encoding="utf-8"))
        payload["state"]["roles"]["totally_new_role"] = {"superuser": True}
        snap_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with pytest.raises(snap.SnapshotIntegrityError) as exc:
            rb.run_rollback(conn, snap_path)
        assert "SNAPSHOT_FILE_CHECKSUM_MISMATCH" in str(exc.value)

        # (b) payload tamper with a refreshed sidecar still fails the payload hash
        snap.write_snapshot(snap_path, payload)
        with pytest.raises(snap.SnapshotIntegrityError) as exc:
            rb.run_rollback(conn, snap_path)
        assert "SNAPSHOT_PAYLOAD_CHECKSUM_MISMATCH" in str(exc.value)

        # (c) a missing sidecar is also a refusal
        snap.sidecar_path(snap_path).unlink()
        with pytest.raises(snap.SnapshotIntegrityError) as exc:
            rb.run_rollback(conn, snap_path)
        assert "SNAPSHOT_CHECKSUM_FILE_MISSING" in str(exc.value)

        _assert_state_unchanged(conn, before)
        conn.close()


def test_t15_object_identity_drift_fail_closed(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t15_pre")
        _apply_075(conn)
        # drift: same signature, different body -> prosrc hash differs
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE OR REPLACE FUNCTION {ACTIVATE_FN}("
                "_experiment_id text, _freeze_ts timestamp with time zone, _fingerprint text) "
                "RETURNS void LANGUAGE plpgsql AS $fn$ BEGIN RAISE NOTICE 'drift'; END $fn$"
            )
        conn.commit()
        before = _capture(conn)
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_CONFLICTING_STATE" in str(exc.value)
        assert "prosrc_md5" in str(exc.value)
        _assert_state_unchanged(conn, before)
        conn.close()


def test_t16_unexpected_role_dependency_fail_closed(tmp_path):
    with _tracked_resources() as res:
        conn, dbname = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t16_pre")
        _apply_075(conn)

        # (a) privileges on an object outside the audited surface
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE research.p2hardening_t16_decoy(x int)")
            cur.execute("GRANT SELECT ON research.p2hardening_t16_decoy TO \"" + OBSERVER_ROLE + "\"")
        conn.commit()
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_ROLE_DEPENDENCY_UNEXPECTED" in str(exc.value)
        with conn.cursor() as cur:
            cur.execute(f"REVOKE ALL ON research.p2hardening_t16_decoy FROM \"{OBSERVER_ROLE}\"")
            cur.execute("DROP TABLE research.p2hardening_t16_decoy")
        conn.commit()

        # (b) dependency in ANOTHER database
        other = dbname + "_other"
        _assert_safe_database(other)
        admin = _admin_connect()
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{other}"')
            cur.execute(f'GRANT CONNECT ON DATABASE "{other}" TO "{OBSERVER_ROLE}"')
        admin.close()
        res["databases"].append(other)
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_ROLE_DEPENDENCY_UNEXPECTED" in str(exc.value)
        admin = _admin_connect()
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'REVOKE CONNECT ON DATABASE "{other}" FROM "{OBSERVER_ROLE}"')
        admin.close()

        # (c) unexpected role membership
        _ensure_role(res, conn, "trad_bot_p2_it_t16_member", 'CREATE ROLE "trad_bot_p2_it_t16_member" NOLOGIN')
        with conn.cursor() as cur:
            cur.execute(f'GRANT "{OBSERVER_ROLE}" TO "trad_bot_p2_it_t16_member"')
        conn.commit()
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_ROLE_DEPENDENCY_UNEXPECTED" in str(exc.value)
        with conn.cursor() as cur:
            cur.execute(f'REVOKE "{OBSERVER_ROLE}" FROM "trad_bot_p2_it_t16_member"')
            cur.execute('DROP ROLE "trad_bot_p2_it_t16_member"')
        conn.commit()

        # (d) object OWNED by the 075 role
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE research.p2hardening_t16_owned(x int)")
            cur.execute(f"ALTER TABLE research.p2hardening_t16_owned OWNER TO \"{OBSERVER_ROLE}\"")
        conn.commit()
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_ROLE_DEPENDENCY_UNEXPECTED" in str(exc.value)
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE research.p2hardening_t16_owned OWNER TO \"{DB_USER}\"")
            cur.execute("DROP TABLE research.p2hardening_t16_owned")
        conn.commit()

        # with every dependency removed the rollback succeeds again
        report = rb.run_rollback(conn, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        conn.close()


# --------------------------------------------------------------------------- #
# T17 / T18 — atomicity and idempotency
# --------------------------------------------------------------------------- #
def test_t17_interrupted_rollback_leaves_no_partial_changes(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t17_pre")
        _apply_075(conn)
        before = _capture(conn)
        plan = rb.build_restore_plan(pre, before)
        assert len(plan["statements"]) > 3
        for cut in (1, len(plan["statements"]) // 2, len(plan["statements"]) - 1):
            with pytest.raises(RuntimeError):
                rb.run_rollback(conn, snap_path, fail_after=cut)
            _assert_state_unchanged(conn, before)
        # and the full rollback still works afterwards
        report = rb.run_rollback(conn, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        conn.close()


def test_t18_repeated_rollback_safe_refusal(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t18_pre")
        _apply_075(conn)
        report = rb.run_rollback(conn, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        after_first = _capture(conn)
        with pytest.raises(rb.NothingToRollBack) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_NOTHING_TO_ROLL_BACK" in str(exc.value)
        _assert_state_unchanged(conn, after_first)
        conn.close()


# --------------------------------------------------------------------------- #
# T19 / T20 — unrelated research objects and the Point-B stack stay untouched
# --------------------------------------------------------------------------- #
def test_t19_unrelated_research_objects_unchanged(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE research.p2hardening_t19_decoy(x int)")
            cur.execute("INSERT INTO research.p2hardening_t19_decoy VALUES (1), (2)")
            cur.execute("GRANT SELECT ON research.p2hardening_t19_decoy TO PUBLIC")
            cur.execute(
                "CREATE FUNCTION research.p2hardening_t19_decoy_fn() RETURNS int "
                "LANGUAGE sql AS $$ SELECT 1 $$"
            )
            cur.execute("REVOKE ALL ON FUNCTION research.p2hardening_t19_decoy_fn() FROM PUBLIC")
        conn.commit()
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t19_pre")
        _apply_075(conn)
        rb.run_rollback(conn, snap_path)
        post = _capture(conn)
        pre_keys = snap._objects_by_key(pre)
        post_keys = snap._objects_by_key(post)
        for key in (
            "relation:research.p2hardening_t19_decoy",
            "function:research.p2hardening_t19_decoy_fn()",
        ):
            assert pre_keys[key] == post_keys[key], key
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM research.p2hardening_t19_decoy")
            assert cur.fetchone()[0] == 2
        conn.close()


def test_t20_point_b_stack_unchanged_and_structurally_untouchable(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        # emulate the frozen Point-B stack objects with decoys of the same
        # naming surface; the rollback must never name or touch them
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE research.prospective_p2hardening_decoy(x int)")
            cur.execute("INSERT INTO research.prospective_p2hardening_decoy VALUES (42)")
            cur.execute("GRANT SELECT ON research.prospective_p2hardening_decoy TO PUBLIC")
        conn.commit()
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "t20_pre")
        _apply_075(conn)
        report = rb.run_rollback(conn, snap_path)
        post = _capture(conn)
        key = "relation:research.prospective_p2hardening_decoy"
        assert snap._objects_by_key(pre)[key] == snap._objects_by_key(post)[key]
        assert "prospective" not in json.dumps(report["statements"])
        with conn.cursor() as cur:
            cur.execute("SELECT x FROM research.prospective_p2hardening_decoy")
            assert cur.fetchone()[0] == 42
        # structural proof: the rollback tool cannot even name Point-B objects
        source = (ROOT / "tools" / "research" / (
            "htf_baseline_short_phase2_rollback_075_v1.py")).read_text(encoding="utf-8")
        assert "prospective" not in source
        code_only = source.split('"""', 2)[-1]  # code, not the explanatory docstrings
        assert "DROP OWNED BY" not in code_only
        conn.close()


# --------------------------------------------------------------------------- #
# adversarial — non-standard grants must round-trip exactly
# --------------------------------------------------------------------------- #
def test_adv_nonstandard_public_and_app_grants_roundtrip(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        for role in ("trad_bot", "analytics_runner"):
            _ensure_role(res, conn, role, f'CREATE ROLE "{role}" NOLOGIN')
        with conn.cursor() as cur:
            cur.execute(f"GRANT SELECT, INSERT ON {P2_TABLES[1]} TO PUBLIC")
            cur.execute(f"GRANT UPDATE ON {P2_TABLES[1]} TO trad_bot WITH GRANT OPTION")
            cur.execute(f"GRANT TRUNCATE ON {P2_TABLES[0]} TO analytics_runner")
            cur.execute(f"GRANT SELECT ON {P2_TABLES[2]} TO trad_bot")
            cur.execute(f"GRANT USAGE ON {P2_SEQ} TO PUBLIC")
            cur.execute(f"GRANT EXECUTE ON FUNCTION {INSERT_FN} TO PUBLIC")
            cur.execute(f"GRANT EXECUTE ON FUNCTION {ACTIVATE_FN} TO trad_bot WITH GRANT OPTION")
            cur.execute(f"GRANT ALL ON {P2_TABLES[2]} TO trad_bot")
        conn.commit()
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "adv_pre")
        _apply_075(conn)
        mid = snap._objects_by_key(_capture(conn))
        for key in [f"relation:{t}" for t in P2_TABLES]:
            grantees = {e[1] for e in mid[key]["acl_effective"]}
            assert "PUBLIC" not in grantees and "trad_bot" not in grantees
        rb.run_rollback(conn, snap_path)
        post = _capture(conn)
        report = _assert_exact_acl_equal(pre, post)
        # grantable flags and grantors are part of the proof, not just access
        pre_keys = snap._objects_by_key(pre)
        post_keys = snap._objects_by_key(post)
        entry = [
            e for e in pre_keys[f"relation:{P2_TABLES[1]}"]["acl_effective"]
            if e[1] == "trad_bot" and e[2] == "UPDATE"
        ]
        assert entry and entry[0][3] is True  # WITH GRANT OPTION restored
        assert entry[0] in post_keys[f"relation:{P2_TABLES[1]}"]["acl_effective"]
        assert report["representation_only"] is not None
        conn.close()


def test_adv_custom_role_grants_outside_075_delta_survive(tmp_path):
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        custom = "trad_bot_p2_it_adv_custom"
        _ensure_role(res, conn, custom, f'CREATE ROLE "{custom}" NOLOGIN')
        with conn.cursor() as cur:
            cur.execute(f"GRANT SELECT ON {P2_TABLES[1]} TO \"{custom}\"")
        conn.commit()
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "adv_custom_pre")
        _apply_075(conn)
        report = rb.run_rollback(conn, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        post = snap._objects_by_key(_capture(conn))
        entry = [
            e for e in post[f"relation:{P2_TABLES[1]}"]["acl_effective"]
            if e[1] == custom
        ]
        assert entry == [["postgres", custom, "SELECT", False]]
        conn.close()


def test_adv_preseeded_p2_roles_with_login_are_preserved(tmp_path):
    """075 found both roles already present -> rollback must not drop them."""
    with _tracked_resources() as res:
        conn, _ = _provision_db(res)
        _ensure_role(res, conn, OBSERVER_ROLE, f'CREATE ROLE "{OBSERVER_ROLE}" LOGIN NOINHERIT')
        _ensure_role(res, conn, ADMIN_ROLE, f'CREATE ROLE "{ADMIN_ROLE}" NOLOGIN NOINHERIT')
        with conn.cursor() as cur:
            cur.execute(f"ALTER ROLE \"{OBSERVER_ROLE}\" CONNECTION LIMIT 7")
        conn.commit()
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "adv_roles_pre")
        _apply_075(conn)
        report = rb.run_rollback(conn, snap_path)
        assert report["roles_to_drop"] == []
        post = _capture(conn)
        assert post["roles"][OBSERVER_ROLE] == pre["roles"][OBSERVER_ROLE]
        assert post["roles"][OBSERVER_ROLE]["connlimit"] == 7
        conn.close()


def test_adv_superuser_requirement_fails_closed(tmp_path):
    """A non-superuser may not run the rollback at all (fail closed)."""
    with _tracked_resources() as res:
        conn, dbname = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, "adv_super_pre")
        _apply_075(conn)
        unpriv = "trad_bot_p2_it_adv_nosuper"
        _ensure_role(res, conn, unpriv, f'CREATE ROLE "{unpriv}" LOGIN NOINHERIT')
        with conn.cursor() as cur:
            cur.execute(f"GRANT CONNECT ON DATABASE \"{dbname}\" TO \"{unpriv}\"")
            cur.execute(f"GRANT USAGE ON SCHEMA research TO \"{unpriv}\"")
            cur.execute(f"SET ROLE \"{unpriv}\"")
        conn.commit()
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_APPLYING_ROLE_INSUFFICIENT" in str(exc.value)
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("RESET ROLE")
        conn.commit()
        conn.close()


# --------------------------------------------------------------------------- #
# Atomicity — the PR #191 review fix: run_rollback owns the transaction and
# commits ONLY after every post-restore verification passed; every failure
# (SQL execution, post-verification, ACL mismatch, function-config mismatch,
# interruption before commit) must leave ZERO committed changes.
# --------------------------------------------------------------------------- #
class _ConnectionSpy:
    """Delegating wrapper counting commit()/rollback() calls on a connection."""

    def __init__(self, conn):
        self._conn = conn
        self.commit_calls = 0
        self.rollback_calls = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        self.commit_calls += 1
        return self._conn.commit()

    def rollback(self):
        self.rollback_calls += 1
        return self._conn.rollback()

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)


def _assert_075_state_intact(conn, state_075: dict) -> None:
    """Nothing may be committed by a failed rollback: the FULL state — ACL
    tuples, effective privileges, roles, memberships, function config — must
    hash-identically to the committed 075 state."""
    assert snap.payload_sha256(_capture(conn)) == snap.payload_sha256(state_075)


def _assert_both_p2_roles_present(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(rb.P2_ROLES),)
        )
        present = {row[0] for row in cur.fetchall()}
    assert present == set(rb.P2_ROLES)


def _atomic_setup(tmp_path, tag: str):
    """Provision 074 -> snapshot -> 075 and return the committed 075 state."""
    resources = _tracked_resources()
    res = resources.__enter__()
    try:
        conn, _ = _provision_db(res)
        pre = _capture(conn)
        snap_path = _snapshot_file(tmp_path, pre, f"{tag}_pre")
        _apply_075(conn)
        state_075 = _capture(conn)
    except BaseException:
        resources.__exit__(None, None, None)
        raise
    return resources, conn, pre, snap_path, state_075


def test_atomic_sql_execution_failure_commits_nothing(tmp_path):
    """Failure DURING SQL execution -> zero commits, no partial restore."""
    resources, conn, pre, snap_path, state_075 = _atomic_setup(tmp_path, "atomic_sql")
    try:
        plan = rb.build_restore_plan(pre, state_075)
        assert len(plan["statements"]) > 3
        cut = len(plan["statements"]) // 2
        spy = _ConnectionSpy(conn)
        with pytest.raises(rb._InjectedFailure):
            rb.run_rollback(spy, snap_path, fail_after=cut)
        assert spy.commit_calls == 0
        assert spy.rollback_calls >= 1
        _assert_075_state_intact(conn, state_075)
        _assert_both_p2_roles_present(conn)
    finally:
        conn.close()
        resources.__exit__(None, None, None)


def test_atomic_post_verification_failure_commits_nothing(tmp_path):
    """Failure DURING post-restore verification -> zero commits."""
    resources, conn, pre, snap_path, state_075 = _atomic_setup(tmp_path, "atomic_verify")
    try:
        spy = _ConnectionSpy(conn)
        with pytest.raises(rb._InjectedFailure) as exc:
            rb.run_rollback(spy, snap_path, fail_point="verify_exception")
        assert "post-restore verification" in str(exc.value)
        assert spy.commit_calls == 0
        assert spy.rollback_calls >= 1
        _assert_075_state_intact(conn, state_075)
        _assert_both_p2_roles_present(conn)
    finally:
        conn.close()
        resources.__exit__(None, None, None)


def test_atomic_acl_mismatch_after_restoration_rolls_back(tmp_path):
    """A restore whose re-grant half is missing must be REFUSED by the
    post-restore verification and rolled back — the revoked privileges and
    the roles the partial plan dropped are fully restored afterwards."""
    resources, conn, pre, snap_path, state_075 = _atomic_setup(tmp_path, "atomic_acl")
    try:
        spy = _ConnectionSpy(conn)
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(spy, snap_path, fail_point="skip_acl_grants")
        assert "P2_RB_VERIFICATION_FAILED" in str(exc.value)
        assert spy.commit_calls == 0
        assert spy.rollback_calls >= 1
        _assert_075_state_intact(conn, state_075)
        _assert_both_p2_roles_present(conn)
    finally:
        conn.close()
        resources.__exit__(None, None, None)


def test_atomic_function_config_mismatch_rolls_back(tmp_path):
    """A restore that misses the function-config statements must be REFUSED
    (proconfig/prosecdef still differ) and rolled back."""
    resources, conn, pre, snap_path, state_075 = _atomic_setup(tmp_path, "atomic_cfg")
    try:
        spy = _ConnectionSpy(conn)
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(spy, snap_path, fail_point="skip_function_config_restore")
        assert "P2_RB_VERIFICATION_FAILED" in str(exc.value)
        assert spy.commit_calls == 0
        assert spy.rollback_calls >= 1
        _assert_075_state_intact(conn, state_075)
        _assert_both_p2_roles_present(conn)
    finally:
        conn.close()
        resources.__exit__(None, None, None)


def test_atomic_success_commits_exactly_once_and_verifies(tmp_path):
    """Success -> exactly ONE final commit and the verified restored state."""
    resources, conn, pre, snap_path, state_075 = _atomic_setup(tmp_path, "atomic_ok")
    try:
        spy = _ConnectionSpy(conn)
        report = rb.run_rollback(spy, snap_path)
        assert report["verdict"] == "RESTORED_AND_VERIFIED"
        assert report["full_state_equal"] is True
        assert spy.commit_calls == 1
        assert spy.rollback_calls == 0
        _assert_exact_acl_equal(pre, _capture(conn))
    finally:
        conn.close()
        resources.__exit__(None, None, None)


def test_atomic_failure_before_commit_leaves_no_partial_state(tmp_path):
    """Failure AFTER the full restore+verification but BEFORE the commit must
    change nothing: neither privileges nor the roles the plan dropped."""
    resources, conn, pre, snap_path, state_075 = _atomic_setup(tmp_path, "atomic_precommit")
    try:
        spy = _ConnectionSpy(conn)
        with pytest.raises(rb._InjectedFailure) as exc:
            rb.run_rollback(spy, snap_path, fail_point="before_commit")
        assert "before the final commit" in str(exc.value)
        assert spy.commit_calls == 0
        assert spy.rollback_calls >= 1
        _assert_075_state_intact(conn, state_075)
        _assert_both_p2_roles_present(conn)
    finally:
        conn.close()
        resources.__exit__(None, None, None)


def test_atomic_autocommit_connection_is_refused(tmp_path):
    """autocommit would bypass the transaction boundary -> fail closed."""
    resources, conn, pre, snap_path, state_075 = _atomic_setup(tmp_path, "atomic_ac")
    try:
        auto = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=conn.get_dsn_parameters()["dbname"],
            user=DB_USER,
        )
        auto.autocommit = True
        try:
            with pytest.raises(rb.FailClosed) as exc:
                rb.run_rollback(auto, snap_path)
            assert "P2_RB_CONNECTION_AUTOCOMMIT" in str(exc.value)
        finally:
            auto.close()
        _assert_075_state_intact(conn, state_075)
    finally:
        conn.close()
        resources.__exit__(None, None, None)


def test_atomic_foreign_open_transaction_is_refused(tmp_path):
    """A connection already inside a transaction the tool does not own must be
    refused (its uncommitted work would otherwise be destroyed)."""
    resources, conn, pre, snap_path, state_075 = _atomic_setup(tmp_path, "atomic_tx")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")  # leave an open transaction behind
        with pytest.raises(rb.FailClosed) as exc:
            rb.run_rollback(conn, snap_path)
        assert "P2_RB_TRANSACTION_NOT_IDLE" in str(exc.value)
        conn.rollback()
        _assert_075_state_intact(conn, state_075)
    finally:
        conn.close()
        resources.__exit__(None, None, None)
