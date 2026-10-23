"""Issue #185 — safe, snapshot-driven rollback of migration 075 (HTF Phase-2).

Why this exists
---------------
The original runbook claimed that ``DROP OWNED BY trad_bot_htf_p2_observer``
plus ``DROP OWNED BY trad_bot_htf_p2_admin`` would "restore the pre-075
privilege state".  That is wrong: ``DROP OWNED BY`` can only delete
privileges the dropped role holds — it can never RE-CREATE the grants that
075 revoked from ``PUBLIC`` and from the existing application roles, and it
never restores ``SECURITY DEFINER`` / ``search_path`` settings.  The only
way to restore the pre-075 state without guessing is to capture that state
before applying 075 and to drive the rollback from the capture.

This module is the verified mechanism:

    pre-075 snapshot  ->  drift/dependency checks (FAIL CLOSED)  ->
    single transaction restore  ->  exact re-capture & comparison

Guarantees
----------
* FAIL CLOSED on: missing snapshot, checksum mismatch (file or payload),
  object identity drift, unexpected role dependencies (ownership,
  memberships, grants outside the audited surface, dependencies in other
  databases), unexpected state changes not explained by the audited 075
  delta, missing grantees, insufficient applying role.
* The whole restore runs in ONE transaction owned by ``run_rollback``: the
  post-restore capture and verification run on that same uncommitted state,
  the single ``commit`` happens ONLY after every verification passed, and any
  failure (including the ``--fail-after``/``--fail-point`` test seams) ends in
  ``rollback`` — no partial state is ever committed.
* Roles created by 075 are dropped ONLY when the snapshot proves they did
  not exist before 075 and only when they carry no dependency anywhere.
  Roles that existed before 075 are NEVER dropped and never altered.
* Phase-2 observations and the frozen Point-B OOS stack are never touched:
  the planner emits only GRANT/REVOKE/ALTER FUNCTION/ALTER ... OWNER-free
  statements and DROP ROLE — never DML/DDL on tables.
* Emergency-stopping the observer (``observer_enabled = FALSE``) is a
  different, independent procedure and is NOT a rollback — see the runbook.

Exit codes (CLI): 0 = restored and verified, 2 = FAIL CLOSED (nothing was
committed), 3 = refused because there is nothing to roll back.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import htf_baseline_short_phase2_acl_snapshot_v1 as snapmod  # noqa: E402

OBSERVER_ROLE = "trad_bot_htf_p2_observer"
ADMIN_ROLE = "trad_bot_htf_p2_admin"
P2_ROLES = (OBSERVER_ROLE, ADMIN_ROLE)
APP_ROLES = ("trad_bot", "analytics_runner", "analytics_agent")

P2_RELATION_KEYS = frozenset(f"relation:{name}" for name in snapmod.PHASE2_RELATIONS)
P2_FUNCTION_KEYS = frozenset(f"function:{name}" for name in snapmod.PHASE2_FUNCTIONS)
P2_TABLE_KEYS = frozenset(
    key for key in P2_RELATION_KEYS if not key.endswith("_observation_id_seq")
)
P2_SEQUENCE_KEYS = P2_RELATION_KEYS - P2_TABLE_KEYS
INSERT_FN_KEY = "function:" + snapmod.PHASE2_FUNCTIONS[0]
ACTIVATE_FN_KEY = "function:" + snapmod.PHASE2_FUNCTIONS[1]

# exact role attributes migration 075 creates
_EXPECTED_ROLE_ATTRIBUTES = {
    OBSERVER_ROLE: {
        "superuser": False,
        "inherit": False,
        "createrole": False,
        "createdb": False,
        "can_login": True,
        "replication": False,
        "bypassrls": False,
        "connlimit": -1,
        "valid_until": None,
    },
    ADMIN_ROLE: {
        "superuser": False,
        "inherit": False,
        "createrole": False,
        "createdb": False,
        "can_login": False,
        "replication": False,
        "bypassrls": False,
        "connlimit": -1,
        "valid_until": None,
    },
}

_SEARCH_PATH_TARGET = "search_path=pg_catalog, research"

# privilege vocabulary accepted by GRANT/REVOKE per object class
_GRANT_KIND = {
    "database": "DATABASE",
    "schema": "SCHEMA",
    "relation": "TABLE",
    "sequence": "SEQUENCE",
    "function": "FUNCTION",
}


class RollbackError(RuntimeError):
    """Base class for rollback refusals."""

    code = "P2_RB_ERROR"


class FailClosed(RollbackError):
    """State cannot be rolled back safely — refuse without changing anything."""

    code = "P2_RB_FAIL_CLOSED"


class NothingToRollBack(RollbackError):
    """The database already matches the pre-075 snapshot."""

    code = "P2_RB_NOTHING_TO_ROLL_BACK"


def _fail(code: str, message: str) -> None:
    raise FailClosed(f"{code}: {message}")


# --------------------------------------------------------------------------- #
# 075 delta model (from the privilege delta audit)
# --------------------------------------------------------------------------- #
def _proconfig_change_explained(before: list, after: list) -> bool:
    """Only the pinned search_path may differ on the two 075 functions.

    075 runs ``ALTER FUNCTION ... SET search_path = pg_catalog, research``,
    which replaces any previous search_path entry and leaves all other
    proconfig entries untouched.  So the post-075 state is exactly
    ``{search_path=pg_catalog, research} ∪ (other entries unchanged)``.
    """
    before_set = {e for e in (before or [])}
    after_set = {e for e in (after or [])}
    others_before = {e for e in before_set if not e.startswith("search_path=")}
    others_after = {e for e in after_set if not e.startswith("search_path=")}
    if others_before != others_after:
        return False
    sp_after = {e for e in after_set if e.startswith("search_path=")}
    return sp_after == {_SEARCH_PATH_TARGET}


def _acl_change_explained(change: dict, before: dict, database_key: str, research_key: str) -> bool:
    """True only for ACL changes that migration 075 is known to produce."""
    obj = change["object"]
    action = change["action"]
    grantor, grantee, privilege, grantable = change["entry"]
    if action == "added":
        if obj == database_key:
            return grantee in P2_ROLES and privilege == "CONNECT" and not grantable
        if obj == research_key:
            return grantee in P2_ROLES and privilege == "USAGE" and not grantable
        if obj in P2_TABLE_KEYS:
            return grantee in P2_ROLES and privilege == "SELECT" and not grantable
        if obj == INSERT_FN_KEY:
            return (
                grantee == OBSERVER_ROLE and privilege == "EXECUTE" and not grantable
            )
        if obj == ACTIVATE_FN_KEY:
            return grantee == ADMIN_ROLE and privilege == "EXECUTE" and not grantable
        # PostgreSQL materialization quirk: the first GRANT/REVOKE on an
        # object whose stored ACL is NULL (default form) materializes the
        # owner's own privileges as explicit entries.  For sequences that
        # materialized set (rwU) is larger than the stored default (U).
        # Owner entries grant no capability the owner does not already hold
        # implicitly, the pre-state must be in default form, and the restore
        # normalizes everything back to the exact snapshot tuples.
        if obj in P2_RELATION_KEYS or obj in P2_FUNCTION_KEYS:
            before_obj = _object_from_state(before, obj)
            return (
                before_obj is not None
                and before_obj.get("acl_is_null") is True
                and grantee == before_obj["identity"]["owner"]
                and grantor == before_obj["identity"]["owner"]
                and not grantable
            )
        return False
    if action == "removed":
        if obj in P2_RELATION_KEYS or obj in P2_FUNCTION_KEYS:
            return grantee == "PUBLIC" or grantee in APP_ROLES
        return False
    return False


def _object_from_state(state: dict, key: str):
    return snapmod._objects_by_key(state).get(key)


def classify_changes(changes: list, before: dict, current: dict) -> tuple[list, list, list]:
    """Split state differences into (explained_by_075, derived, unexpected).

    ``derived`` holds differences that are computed state (per-role effective
    privileges and stored-ACL representation).  They can only change when an
    authoritative input (ACL entry, role, membership, default privileges)
    changes, and every such input is classified above; they are verified
    exactly at the end of the rollback and never decide the refusal.
    """
    database_key = current["database"]["key"]
    research_key = f"schema:{snapmod.AFFECTED_SCHEMA}"
    current_db_oid = current["database"]["identity"]["oid"]
    affected_oids = {("pg_database", current_db_oid)}
    affected_oids.add(("pg_namespace", _schema_oid(current)))
    for rel in current["relations"]:
        if rel["key"] in P2_RELATION_KEYS:
            affected_oids.add(("pg_class", rel["identity"]["oid"]))
    for fn in current["functions"]:
        if fn["key"] in P2_FUNCTION_KEYS:
            affected_oids.add(("pg_proc", fn["identity"]["oid"]))

    explained, derived, unexpected = [], [], []
    for change in changes:
        kind = change["kind"]
        if kind in ("acl_representation", "effective_privilege"):
            derived.append(change)
            continue
        ok = False
        if kind == "role" and change.get("action") == "added":
            name = change["role"]
            ok = (
                name in P2_ROLES
                and change.get("attributes") == _EXPECTED_ROLE_ATTRIBUTES.get(name)
            )
        elif kind == "acl_entry":
            ok = _acl_change_explained(change, before, database_key, research_key)
        elif kind == "function_config":
            obj = change["object"]
            if obj in (INSERT_FN_KEY, ACTIVATE_FN_KEY):
                if change["field"] == "security":
                    ok = change["before"] == "INVOKER" and change["after"] == "DEFINER"
                elif change["field"] == "proconfig":
                    ok = _proconfig_change_explained(change["before"], change["after"])
        elif kind == "shdepend":
            row = change["row"]
            role, class_name, objid, objsubid, deptype, dbid = row
            in_surface = (
                deptype == "a"
                and int(dbid) in (0, current_db_oid)
                and (class_name, int(objid)) in affected_oids
            )
            if change.get("action") == "added":
                # validated up-front by check_role_dependencies
                ok = role in P2_ROLES and in_surface
            else:
                # 075's REVOKEs drop the dependency rows of the revoked
                # grantees on the audited surface
                ok = (role in P2_ROLES or role in APP_ROLES) and in_surface
        elif kind == "membership":
            # check_role_dependencies already refused any membership 075 does
            # not explain; a removal can never be produced by 075
            ok = False
        (explained if ok else unexpected).append(change)
    return explained, derived, unexpected


def _schema_oid(state: dict) -> int:
    for schema in state["schemas"]:
        if schema["identity"]["name"] == snapmod.AFFECTED_SCHEMA:
            return schema["identity"]["oid"]
    _fail("P2_RB_RESEARCH_SCHEMA_MISSING", "schema research is not in the snapshot")


# --------------------------------------------------------------------------- #
# role dependency checks (cross-database / ownership / membership)
# --------------------------------------------------------------------------- #
def check_role_dependencies(
    conn, before: dict, current: dict, roles: list[str]
) -> None:
    """FAIL CLOSED on any Phase-2 role dependency the audited 075 delta cannot
    explain.

    A dependency is accepted only when it is either

      * present in the pre-075 snapshot (it predates 075; the rollback
        preserves it and the roles carrying it are never dropped), or
      * an ``a``-type ACL dependency on the audited Phase-2 surface inside the
        current database — exactly what 075 grants.

    Anything else — role ownership of objects, role memberships, privileges on
    objects outside the audited surface, dependencies in other databases — is
    ``P2_RB_ROLE_DEPENDENCY_UNEXPECTED`` and the rollback refuses before any
    SQL is planned or executed.
    """
    if not roles:
        return
    current_db_oid = current["database"]["identity"]["oid"]
    affected = {("pg_database", current_db_oid), ("pg_namespace", _schema_oid(current))}
    for rel in current["relations"]:
        if rel["key"] in P2_RELATION_KEYS:
            affected.add(("pg_class", rel["identity"]["oid"]))
    for fn in current["functions"]:
        if fn["key"] in P2_FUNCTION_KEYS:
            affected.add(("pg_proc", fn["identity"]["oid"]))
    snapshot_rows = {
        (
            row["role"], row["class"], row["objid"], row["objsubid"],
            row["deptype"], row["dbid"],
        )
        for row in before.get("shdepend", [])
        if row["role"] in roles
    }
    snapshot_memberships = {
        tuple(row) for row in before.get("memberships", [])
        if row[0] in roles or row[1] in roles
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_userbyid(s.refobjid), s.classid::regclass::text,
                   s.objid, s.objsubid, s.deptype, s.dbid
              FROM pg_shdepend s
             WHERE s.refclassid = 'pg_authid'::regclass
               AND pg_get_userbyid(s.refobjid) = ANY(%s)
            """,
            (roles,),
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT pg_get_userbyid(m.roleid), pg_get_userbyid(m.member), m.admin_option
              FROM pg_auth_members m
             WHERE pg_get_userbyid(m.roleid) = ANY(%s)
                OR pg_get_userbyid(m.member) = ANY(%s)
            """,
            (roles, roles),
        )
        memberships = cur.fetchall()
    for row in memberships:
        if tuple(row) not in snapshot_memberships:
            _fail(
                "P2_RB_ROLE_DEPENDENCY_UNEXPECTED",
                f"unexpected role membership involving a Phase-2 role: {tuple(row)!r}",
            )
    for role, class_name, objid, objsubid, deptype, dbid in rows:
        key = (role, class_name, int(objid), int(objsubid), deptype, int(dbid))
        if key in snapshot_rows:
            continue  # pre-075 dependency: preserved, never guessed
        if (
            deptype == "a"
            and (class_name, int(objid)) in affected
            and int(dbid) in (0, current_db_oid)
        ):
            continue  # exactly the 075 grants on the audited surface
        if int(dbid) not in (0, current_db_oid):
            _fail(
                "P2_RB_ROLE_DEPENDENCY_UNEXPECTED",
                f"{role} has a dependency in another database (dbid={dbid}); "
                "refusing to drop or touch it",
            )
        _fail(
            "P2_RB_ROLE_DEPENDENCY_UNEXPECTED",
            f"{role} is referenced with deptype={deptype} by "
            f"{class_name}/{objid} (objsubid={objsubid}) outside the audited "
            "Phase-2 surface",
        )


# --------------------------------------------------------------------------- #
# restore planning (SQL generation)
# --------------------------------------------------------------------------- #
def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _object_sql(kind: str, key: str) -> str:
    """SQL object reference for GRANT/REVOKE, schema-qualified where needed."""
    if kind in ("database", "schema"):
        return _quote_ident(key.split(":", 1)[1])
    qualified = key.split(":", 1)[1]
    schema, rest = qualified.split(".", 1)
    if kind == "function":
        name, args = rest.split("(", 1)
        return f"{_quote_ident(schema)}.{_quote_ident(name)}({args}"
    return f"{_quote_ident(schema)}.{_quote_ident(rest)}"


def _restore_acl_statements(entry: dict) -> list[str]:
    """Revoke everything current and re-grant the exact target ACL."""
    kind = entry["kind"]
    key = entry["key"]
    obj_sql = _object_sql(kind, key)
    keyword = _GRANT_KIND[kind]
    target = {tuple(e) for e in entry["target"]}
    current = {tuple(e) for e in entry["current"]}
    if target == current:
        return []
    grantees = sorted({e[1] for e in target | current})
    statements = []
    for grantee in grantees:
        grantee_sql = "PUBLIC" if grantee == "PUBLIC" else _quote_ident(grantee)
        statements.append(f"REVOKE ALL ON {keyword} {obj_sql} FROM {grantee_sql}")
    grouped: dict = {}
    for grantor, grantee, privilege, grantable in sorted(target):
        grouped.setdefault((grantor, grantee, bool(grantable)), []).append(privilege)
    for (grantor, grantee, grantable), privileges in sorted(grouped.items()):
        grantee_sql = "PUBLIC" if grantee == "PUBLIC" else _quote_ident(grantee)
        priv_sql = ", ".join(privileges)
        stmt = f"GRANT {priv_sql} ON {keyword} {obj_sql} TO {grantee_sql}"
        if grantable:
            stmt += " WITH GRANT OPTION"
        stmt += f" GRANTED BY {_quote_ident(grantor)}"
        statements.append(stmt)
    return statements


def _restore_function_config_statements(key: str, target_config: dict) -> list[str]:
    signature = key.split(":", 1)[1]
    schema, rest = signature.split(".", 1)
    name, args = rest.split("(", 1)
    fn_sql = f"{_quote_ident(schema)}.{_quote_ident(name)}({args}"
    statements = [
        f"ALTER FUNCTION {fn_sql} SECURITY {target_config['security']}",
        f"ALTER FUNCTION {fn_sql} RESET ALL",
    ]
    for entry in target_config.get("proconfig", []):
        setting, _, value = entry.partition("=")
        statements.append(f"ALTER FUNCTION {fn_sql} SET {setting} = {value}")
    return statements


def build_restore_plan(before: dict, current: dict) -> dict:
    """Compute every statement needed to return ``current`` to ``before``.

    Only the audited 075 surface is touched: the database ACL, the schema
    ``research`` ACL, the Phase-2 relations/functions ACL and the two
    function configurations, plus DROP ROLE for roles 075 created.
    """
    before_objs = snapmod._objects_by_key(before)
    current_objs = snapmod._objects_by_key(current)
    targets = [
        (before["database"]["key"], "database"),
        (f"schema:{snapmod.AFFECTED_SCHEMA}", "schema"),
    ]
    targets += [
        (key, "sequence" if key in P2_SEQUENCE_KEYS else "relation")
        for key in sorted(P2_RELATION_KEYS)
    ]
    targets += [(key, "function") for key in sorted(P2_FUNCTION_KEYS)]

    known_roles = set(before.get("roles", {})) | set(current.get("roles", {}))
    plan: list = []
    acl_restore_objects = []
    for key, kind in sorted(targets):
        target_entry = before_objs.get(key)
        current_entry = current_objs.get(key)
        if target_entry is None or current_entry is None:
            _fail(
                "P2_RB_OBJECT_IDENTITY_DRIFT",
                f"object missing from one of the states: {key}",
            )
        target_acl = target_entry.get("acl_effective", [])
        current_acl = current_entry.get("acl_effective", [])
        if target_acl == current_acl:
            continue
        for entry in target_acl + current_acl:
            for name in (entry[0], entry[1]):
                if name != "PUBLIC" and name not in known_roles:
                    _fail(
                        "P2_RB_GRANTEE_ROLE_MISSING",
                        f"ACL entry of {key} references missing role {name!r}",
                    )
        acl_restore_objects.append(
            {"key": key, "kind": kind, "target": target_acl, "current": current_acl}
        )
        plan.extend(
            {"kind": "sql", "sql": stmt, "reason": f"restore ACL of {key}"}
            for stmt in _restore_acl_statements(
                {"kind": kind, "key": key, "target": target_acl, "current": current_acl}
            )
        )
    for key in (INSERT_FN_KEY, ACTIVATE_FN_KEY):
        target_config = before_objs[key].get("config", {})
        current_config = current_objs[key].get("config", {})
        if target_config != current_config:
            plan.extend(
                {"kind": "sql", "sql": stmt, "reason": f"restore function config of {key}"}
                for stmt in _restore_function_config_statements(key, target_config)
            )
    roles_to_drop = sorted(
        (set(P2_ROLES) - set(before.get("roles", {}))) & set(current.get("roles", {}))
    )
    for role in roles_to_drop:
        plan.append(
            {
                "kind": "sql",
                "sql": f"DROP ROLE {_quote_ident(role)}",
                "reason": f"role {role} was created by 075 and is absent from the snapshot",
            }
        )
    return {
        "statements": plan,
        "acl_restore_objects": acl_restore_objects,
        "roles_to_drop": roles_to_drop,
    }


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
# TEST-ONLY fault-injection seams exercised by the atomicity integration
# tests (see tests/test_htf_baseline_short_phase2_rollback_hardening_v1.py).
_TEST_FAIL_POINTS = frozenset(
    {
        "verify_exception",
        "skip_acl_grants",
        "skip_function_config_restore",
        "before_commit",
    }
)


class _InjectedFailure(RuntimeError):
    """Raised by the ``fail_point``/``fail_after`` test seams."""


def _apply_fail_point(plan: dict, fail_point: str | None) -> dict:
    """TEST-ONLY: reshape the plan so one specific failure is deterministic.

    ``skip_acl_grants`` drops the re-grant half of the ACL restore (the
    REVOKE half still runs), producing a REAL post-restore ACL mismatch that
    the verification must refuse; ``skip_function_config_restore`` omits the
    function-configuration restore, producing a REAL function-config
    mismatch; ``verify_exception`` / ``before_commit`` are raised at the
    corresponding points of ``_restore_and_verify``.  Every seam must end
    with the transaction rolled back and the database exactly as it was
    before the attempt.
    """
    if fail_point is None:
        return plan
    if fail_point not in _TEST_FAIL_POINTS:
        raise ValueError(f"unknown fail_point {fail_point!r}")
    if fail_point == "skip_acl_grants":
        kept = [
            item
            for item in plan["statements"]
            if not (
                item["reason"].startswith("restore ACL")
                and item["sql"].startswith("GRANT ")
            )
        ]
    elif fail_point == "skip_function_config_restore":
        kept = [
            item
            for item in plan["statements"]
            if not item["reason"].startswith("restore function config")
        ]
    else:
        return plan
    return {**plan, "statements": kept}


def _execute_plan(conn, plan: dict, fail_after: int | None = None) -> list[str]:
    """Execute the plan's statements inside the CALLER'S transaction.

    This function NEVER commits and NEVER rolls back: the transaction
    lifecycle belongs exclusively to ``run_rollback``, so the post-restore
    verification runs on the same uncommitted state and the single final
    commit happens only after that verification passed.  ``fail_after`` is a
    test seam that raises before executing statement ``fail_after``
    (0-based), so the interruption path (T17) is exercised exactly like a
    real abort.
    """
    executed: list[str] = []
    for index, item in enumerate(plan["statements"]):
        if fail_after is not None and index == fail_after:
            raise _InjectedFailure(f"injected failure before statement {index}")
        with conn.cursor() as cur:
            cur.execute(item["sql"])
        executed.append(item["sql"])
    return executed


def _restore_and_verify(
    conn,
    meta: dict,
    before: dict,
    snapshot_path,
    *,
    dry_run: bool = False,
    fail_after: int | None = None,
    fail_point: str | None = None,
) -> dict:
    """Check, restore and VERIFY in ONE uncommitted transaction (no commit!).

    Everything below — drift/dependency checks, every restore statement, the
    post-restore state capture and the complete ACL/role/function
    verification — runs in the transaction owned by ``run_rollback``.  The
    returned report is produced only after every verification check passed;
    ``run_rollback`` performs the single ``conn.commit()`` after this
    function returns.  Any raise propagates to ``run_rollback``, which rolls
    the transaction back, so a failed attempt can never leave partial state.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        is_super = bool(cur.fetchone()[0])
    if not is_super:
        _fail(
            "P2_RB_APPLYING_ROLE_INSUFFICIENT",
            "the rollback must run as a superuser (same requirement as applying 075)",
        )

    current = snapmod.capture_state(conn, manage_transaction=False)
    if current["database"]["identity"]["name"] != before["database"]["identity"]["name"]:
        _fail(
            "P2_RB_DATABASE_MISMATCH",
            f"snapshot is for database {before['database']['identity']['name']!r}, "
            f"connected to {current['database']['identity']['name']!r}",
        )

    # dependency safety runs BEFORE anything is planned: any dependency the
    # audited 075 delta cannot explain is an immediate fail-closed refusal
    check_role_dependencies(
        conn,
        before,
        current,
        sorted(r for r in P2_ROLES if r in current.get("roles", {})),
    )

    changes = snapmod.diff_states(before, current)
    explained, derived, unexpected = classify_changes(changes, before, current)
    if unexpected:
        _fail(
            "P2_RB_CONFLICTING_STATE",
            "state differs from the snapshot in ways the audited 075 delta cannot "
            f"explain ({len(unexpected)} change(s)); refusing to guess: "
            + json.dumps(unexpected[:5], sort_keys=True),
        )
    substantive = [
        c for c in changes
        if c["kind"] not in ("acl_representation", "effective_privilege")
    ]
    if not substantive:
        raise NothingToRollBack(
            "P2_RB_NOTHING_TO_ROLL_BACK: the database already matches the "
            "pre-075 snapshot (075 absent or already rolled back)"
        )

    plan = build_restore_plan(before, current)
    plan = _apply_fail_point(plan, fail_point)

    report = {
        "verdict": "DRY_RUN" if dry_run else "RESTORE_PLANNED",
        "snapshot": str(snapshot_path),
        "snapshot_meta": meta,
        "changes_explained_by_075": explained,
        "changes_derived": derived,
        "statements": [item["sql"] for item in plan["statements"]],
        "statement_reasons": [item["reason"] for item in plan["statements"]],
        "roles_to_drop": plan["roles_to_drop"],
        "roles_preserved": sorted(set(before.get("roles", {})) & set(P2_ROLES)),
    }
    if dry_run:
        report["verdict"] = "DRY_RUN"
        return report

    executed = _execute_plan(conn, plan, fail_after=fail_after)
    report["executed_statements"] = executed

    if fail_point == "verify_exception":
        raise _InjectedFailure("injected failure during post-restore verification")

    # verification runs on the SAME still-uncommitted transaction: it sees
    # exactly the state that would become durable at the final commit
    after = snapmod.capture_state(conn, manage_transaction=False)
    verification = snapmod.compare_states(before, after)
    report["verification"] = verification
    report["full_state_equal"] = verification["equal"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(P2_ROLES),)
        )
        present = {row[0] for row in cur.fetchall()}
    report["roles_dropped_verified"] = sorted(set(plan["roles_to_drop"]) - present)
    report["roles_preserved_verified"] = sorted(set(report["roles_preserved"]) & present)

    if not verification["acl_equal"]:
        _fail(
            "P2_RB_VERIFICATION_FAILED",
            "the restored ACL does not exactly match the pre-075 snapshot: "
            + json.dumps(verification["acl_differences"][:5], sort_keys=True),
        )
    if not verification["effective_privileges_equal"]:
        _fail(
            "P2_RB_VERIFICATION_FAILED",
            "effective privileges after rollback differ from the pre-075 snapshot: "
            + json.dumps(verification["effective_privilege_differences"][:5], sort_keys=True),
        )
    if verification["other_differences"]:
        _fail(
            "P2_RB_VERIFICATION_FAILED",
            "role/membership/default-privilege state after rollback differs from "
            "the pre-075 snapshot: "
            + json.dumps(verification["other_differences"][:5], sort_keys=True),
        )
    if set(report["roles_dropped_verified"]) != set(plan["roles_to_drop"]):
        _fail(
            "P2_RB_VERIFICATION_FAILED",
            f"roles created by 075 are still present: {sorted(present & set(P2_ROLES))}",
        )
    if fail_point == "before_commit":
        raise _InjectedFailure("injected failure immediately before the final commit")
    # every verification passed; ``run_rollback`` now performs the single,
    # final commit of this transaction
    return report


def _assert_transaction_boundary(conn) -> None:
    """Fail closed unless the connection cannot bypass our transaction.

    Autocommit would turn every statement into its own transaction (no atomic
    restore possible); a connection already inside a transaction this tool
    does not own would make the final rollback destroy the caller's
    uncommitted work.  Both settings are refused before anything runs.
    """
    if getattr(conn, "autocommit", False):
        _fail(
            "P2_RB_CONNECTION_AUTOCOMMIT",
            "the connection must run with autocommit disabled so the restore and "
            "its verification share exactly one transaction",
        )
    status = None
    info = getattr(conn, "info", None)
    if info is not None:
        status = getattr(info, "transaction_status", None)
    if status is None:
        getter = getattr(conn, "get_transaction_status", None)
        if getter is not None:
            status = getter()
    if status is not None:
        try:
            idle = int(status) == 0  # 0 = TRANSACTION_STATUS_IDLE
        except (TypeError, ValueError):
            idle = False  # fail closed: an uninterpretable status is not IDLE
        if not idle:
            _fail(
                "P2_RB_TRANSACTION_NOT_IDLE",
                "the connection is already inside a transaction the rollback does "
                "not own; commit or roll that transaction back first",
            )


def run_rollback(
    conn,
    snapshot_path,
    *,
    dry_run: bool = False,
    fail_after: int | None = None,
    fail_point: str | None = None,
) -> dict:
    """Snapshot-driven rollback of migration 075.  Fail closed, atomic.

    Transaction contract (PR #191 review fix)
    -----------------------------------------
    ``run_rollback`` OWNS the complete transaction lifecycle:

    * the drift/dependency checks, every restore statement, the post-restore
      state capture and the full ACL/role/function verification all run in
      ONE transaction;
    * the ONLY ``conn.commit()`` of the whole module sits here, strictly
      AFTER every post-restore verification passed;
    * ANY other exit — fail-closed refusal, ``NothingToRollBack``, injected
      test failure, unexpected exception — ends in ``conn.rollback()`` and
      leaves the database byte-for-byte unchanged.

    The connection must run with ``autocommit`` disabled and must not already
    be inside a foreign transaction (checked up front, fail closed), so no
    driver setting can bypass the transaction boundary.

    Returns a report dict; raises ``FailClosed``/``NothingToRollBack``
    without any committed change when the rollback cannot be proven safe.
    """
    if snapshot_path is None:
        _fail("P2_RB_SNAPSHOT_REQUIRED", "a verified pre-075 snapshot is mandatory")
    _assert_transaction_boundary(conn)
    meta, before = snapmod.load_snapshot(snapshot_path)

    try:
        report = _restore_and_verify(
            conn,
            meta,
            before,
            snapshot_path,
            dry_run=dry_run,
            fail_after=fail_after,
            fail_point=fail_point,
        )
        if report["verdict"] == "DRY_RUN":
            conn.rollback()  # read-only probe: close our transaction, keep nothing
            return report
        conn.commit()
        report["verdict"] = "RESTORED_AND_VERIFIED"
        return report
    except BaseException:
        conn.rollback()
        raise


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _connect(args):
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover
        raise FailClosed("P2_RB_DRIVER_MISSING: psycopg2 is required") from exc
    conn = psycopg2.connect(
        host=args.host,
        port=args.port,
        dbname=args.dbname,
        user=args.user,
        connect_timeout=args.connect_timeout,
    )
    conn.autocommit = False
    return conn


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="htf_baseline_short_phase2_rollback_075_v1")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--dbname", required=True)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fail-after", type=int, default=None,
                        help="test seam: abort before statement N (0-based)")
    parser.add_argument(
        "--fail-point",
        default=None,
        choices=sorted(_TEST_FAIL_POINTS),
        help="test seam: inject a deterministic failure (see _TEST_FAIL_POINTS)",
    )
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)

    conn = _connect(args)
    try:
        report = run_rollback(
            conn,
            args.snapshot,
            dry_run=args.dry_run,
            fail_after=args.fail_after,
            fail_point=args.fail_point,
        )
    except NothingToRollBack as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    except snapmod.SnapshotError as exc:
        print(f"FAIL_CLOSED: {exc}", file=sys.stderr)
        return 2
    except FailClosed as exc:
        print(f"FAIL_CLOSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # interrupted / injected failure
        print(f"ABORTED_NO_CHANGES: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    text = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
