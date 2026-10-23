"""Issue #185 — READ-ONLY ACL / privilege snapshot tool (HTF Phase-2).

Purpose
-------
Capture a complete, deterministic, checksummed snapshot of the privilege
state that migration ``075_htf_baseline_short_phase2_privilege_hardening``
touches, BEFORE 075 is applied, so that a later rollback can restore the
pre-075 state exactly instead of guessing which grants 075 revoked.

This is the replacement for the incorrect ``DROP OWNED BY`` recovery claim:
``DROP OWNED BY`` deletes role-owned objects and revokes the role's own
grants — it can NEVER re-create privileges that 075 revoked from other
roles.  Only a captured pre-075 snapshot can do that.

Safety contract
---------------
* the tool is strictly READ-ONLY: the session runs with
  ``default_transaction_read_only = on`` and only ``SELECT``/``SHOW``
  statements are ever issued;
* no password, token, key or any ``pg_authid`` secret is read or emitted —
  role rows come from the ``pg_roles`` view which has no verifier column;
  a structural redaction self-check refuses to write a suspicious payload;
* it never writes to the database and never changes roles/grants.

Snapshot contents (state section)
---------------------------------
1. ACL of the current database (CONNECT/CREATE/TEMP surface);
2. ACL of every schema, with ``research`` marked as the affected surface;
3. ACL + identity (oid, owner, kind, column layout) of every relation in
   schema ``research`` — the Phase-2 tables/sequence plus the control set of
   unrelated research objects;
4. ACL + identity + configuration (``prosecdef``, ``proconfig``, ``prosrc``
   hash, argument types) of every function in schema ``research``;
5. PUBLIC privileges — recorded both as explicit ACL entries (``grantee =
   PUBLIC``) and as effective privileges;
6. existing application role grants (explicit AND effective per role);
7. role attributes and role memberships (password-free ``pg_roles``);
8. ``SECURITY DEFINER`` / ``SECURITY INVOKER`` and ``proconfig`` (incl.
   ``search_path``) of every captured function;
9. object ownership;
10. default privileges (``pg_default_acl``) that apply to the affected
    surface;
11. object identifiers (oid), exact function signatures and privilege
    types;
12. ``pg_shdepend`` rows of the captured roles — cross-database dependency
    proof for the rollback fail-closed checks.

Explicit ACL vs effective privileges
------------------------------------
* ``acl_is_null`` / ``acl_explicit`` describe the STORED ACL (what
  ``GRANT``/``REVOKE`` wrote);
* ``acl_effective`` is ``aclexplode(COALESCE(acl, acldefault(...)))`` — the
  exact set of (grantor, grantee, privilege, grantable) tuples that governs
  access, including the defaults PostgreSQL applies when the stored ACL is
  NULL;
* ``effective_privileges`` is the per-role view through
  ``has_*_privilege()``, which additionally resolves role inheritance.

A comparison of ``has_*_privilege()`` alone is NOT sufficient to prove an
exact ACL restoration (it hides grantors, grantable flags and defaults);
``compare_states()`` therefore compares the exact ``acl_effective`` tuples
first and uses the effective per-role view as an independent second proof.

Representation note (documented, unavoidable)
---------------------------------------------
PostgreSQL has no SQL statement that turns a materialized ACL array back
into NULL.  When the pre-075 stored ACL was NULL (default), the rollback
re-materializes the default entries explicitly, so ``acl_effective`` is
EXACTLY equal while ``acl_is_null`` may differ (NULL vs explicit default
entries).  ``compare_states()`` reports that difference separately as
``representation_only`` and never as an ACL difference.

Determinism
-----------
The ``state`` payload is canonical JSON (sorted keys, sorted lists,
``ensure_ascii``), so identical database states produce byte-identical
payloads.  ``payload_sha256`` covers exactly that canonical payload.  The
sidecar ``<snapshot>.sha256`` covers the whole snapshot file, so any edit —
payload, checksum or metadata — is detectable (T14).

Usage
-----
    python tools/research/htf_baseline_short_phase2_acl_snapshot_v1.py \
        snapshot --dbname trad_bot --output snap.json [--host H --port P --user U]
    python tools/research/htf_baseline_short_phase2_acl_snapshot_v1.py \
        verify --snapshot snap.json
    python tools/research/htf_baseline_short_phase2_acl_snapshot_v1.py \
        compare --before snap_a.json --after snap_b.json [--output diff.json]

Exit codes: 0 = ok/equal, 1 = different (compare), 2 = integrity or usage
error.  Nothing in this module mutates database state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "htf_p2_acl_snapshot_v1"
TOOL_NAME = "htf_baseline_short_phase2_acl_snapshot_v1"
TOOL_VERSION = "1.0.0"

AFFECTED_SCHEMA = "research"

# The exact Phase-2 surface created by migration 074 and re-granted/revoked
# by migration 075 (see the privilege delta audit, issue #185).
PHASE2_RELATIONS = (
    "research.htf_baseline_short_phase2_experiment",
    "research.htf_baseline_short_phase2_observation",
    "research.htf_baseline_short_phase2_freeze_registry",
    "research.htf_baseline_short_phase2_observation_observation_id_seq",
)
PHASE2_FUNCTIONS = (
    "research.htf_baseline_short_phase2_insert_observation(text,text,text,text,text,"
    "timestamp with time zone,timestamp with time zone,timestamp with time zone,"
    "numeric,numeric,boolean,boolean,jsonb)",
    "research.htf_baseline_short_phase2_activate(text,timestamp with time zone,text)",
    "research.htf_baseline_short_phase2_experiment_guard()",
    "research.htf_baseline_short_phase2_observation_guard()",
    "research.htf_baseline_short_phase2_freeze_registry_guard()",
)

# privilege vocabularies used for the per-role effective-privilege view
RELATION_PRIVILEGES = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER",
    "MAINTAIN",
)
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")
FUNCTION_PRIVILEGES = ("EXECUTE",)
SCHEMA_PRIVILEGES = ("USAGE", "CREATE")
DATABASE_PRIVILEGES = ("CONNECT", "CREATE", "TEMP")

_SECRET_KEY_RE = re.compile(r"(password|passwd|secret|token|api[_-]?key|verifier)", re.I)
_SECRET_VALUE_RE = re.compile(r"SCRAM-SHA-256\$|BEGIN [A-Z ]*PRIVATE KEY")

# roles that are never interesting and would bloat/denormalise the snapshot
_SYSTEM_ROLE_RE = re.compile(r"\Apg_")


class SnapshotError(RuntimeError):
    """Base error: the snapshot cannot be trusted or produced."""


class SnapshotIntegrityError(SnapshotError):
    """Checksum / tamper detection failure — fail closed."""


class SnapshotRedactionError(SnapshotError):
    """A secret-looking value reached the payload — refuse to write it."""


# --------------------------------------------------------------------------- #
# canonical serialization
# --------------------------------------------------------------------------- #
def canonical_bytes(payload: dict) -> bytes:
    """Deterministic byte form of the state payload."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# redaction self-check (no secrets ever leave the database)
# --------------------------------------------------------------------------- #
def assert_no_secrets(obj, _path: str = "") -> None:
    """Refuse payloads whose keys or values look like credentials.

    The capture SQL never reads ``pg_authid``; this is defence in depth so a
    future edit cannot leak verifiers/passwords into a committed snapshot.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{_path}.{key}" if _path else str(key)
            if _SECRET_KEY_RE.search(str(key)):
                raise SnapshotRedactionError(f"secret-like key in snapshot: {here}")
            assert_no_secrets(value, here)
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            assert_no_secrets(value, f"{_path}[{index}]")
    elif isinstance(obj, str):
        if _SECRET_VALUE_RE.search(obj):
            raise SnapshotRedactionError(f"secret-like value in snapshot at {_path}")


# --------------------------------------------------------------------------- #
# state capture (READ-ONLY)
# --------------------------------------------------------------------------- #
def _rows(cursor, sql: str, params=()) -> list[tuple]:
    cursor.execute(sql, params)
    return list(cursor.fetchall())


def _sorted_entries(rows) -> list[list]:
    return sorted([list(r) for r in rows])


def _capture_database(cursor) -> dict:
    row = _rows(
        cursor,
        """
        SELECT d.oid, d.datname, pg_get_userbyid(d.datdba),
               d.datacl IS NULL
          FROM pg_database d WHERE d.datname = current_database()
        """,
    )[0]
    explicit = _sorted_entries(
        _rows(
            cursor,
            """
            SELECT pg_get_userbyid(a.grantor),
                   CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
                   a.privilege_type, a.is_grantable
              FROM pg_database d, aclexplode(d.datacl) a
             WHERE d.datname = current_database()
            """,
        )
    )
    effective = _sorted_entries(
        _rows(
            cursor,
            """
            SELECT pg_get_userbyid(a.grantor),
                   CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
                   a.privilege_type, a.is_grantable
              FROM pg_database d,
                   aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) a
             WHERE d.datname = current_database()
            """,
        )
    )
    return {
        "key": f"database:{row[1]}",
        "kind": "database",
        "identity": {"oid": int(row[0]), "name": row[1], "owner": row[2]},
        "acl_is_null": bool(row[3]),
        "acl_explicit": explicit,
        "acl_effective": effective,
    }


def _capture_schemas(cursor) -> list[dict]:
    rows = _rows(
        cursor,
        """
        SELECT n.oid, n.nspname, pg_get_userbyid(n.nspowner), n.nspacl IS NULL
          FROM pg_namespace n
         ORDER BY n.nspname
        """,
    )
    explicit: dict = {}
    for r in _rows(
        cursor,
        """
        SELECT n.nspname,
               pg_get_userbyid(a.grantor),
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type, a.is_grantable
          FROM pg_namespace n, aclexplode(n.nspacl) a
         ORDER BY 1, 2, 3, 4
        """,
    ):
        explicit.setdefault(r[0], []).append(list(r[1:]))
    effective: dict = {}
    for r in _rows(
        cursor,
        """
        SELECT n.nspname,
               pg_get_userbyid(a.grantor),
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type, a.is_grantable
          FROM pg_namespace n,
               aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) a
         ORDER BY 1, 2, 3, 4
        """,
    ):
        effective.setdefault(r[0], []).append(list(r[1:]))
    out = []
    for oid, name, owner, is_null in rows:
        out.append(
            {
                "key": f"schema:{name}",
                "kind": "schema",
                "identity": {"oid": int(oid), "name": name, "owner": owner},
                "affected": name == AFFECTED_SCHEMA,
                "acl_is_null": bool(is_null),
                "acl_explicit": sorted(explicit.get(name, [])),
                "acl_effective": sorted(effective.get(name, [])),
            }
        )
    return out


def _capture_relations(cursor) -> list[dict]:
    rows = _rows(
        cursor,
        """
        SELECT c.oid, n.nspname, c.relname, c.relkind,
               pg_get_userbyid(c.relowner), c.relacl IS NULL
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s
           AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
         ORDER BY n.nspname, c.relname
        """,
        (AFFECTED_SCHEMA,),
    )
    explicit = {}
    for r in _rows(
        cursor,
        """
        SELECT c.oid, pg_get_userbyid(a.grantor),
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type, a.is_grantable
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace,
               aclexplode(c.relacl) a
         WHERE n.nspname = %s
         ORDER BY 1, 2, 3, 4
        """,
        (AFFECTED_SCHEMA,),
    ):
        explicit.setdefault(int(r[0]), []).append(list(r[1:]))
    effective = {}
    for r in _rows(
        cursor,
        """
        SELECT c.oid, pg_get_userbyid(a.grantor),
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type, a.is_grantable
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace,
               aclexplode(COALESCE(c.relacl, acldefault(CASE WHEN c.relkind = 'S' THEN 'S'::"char" ELSE 'r'::"char" END, c.relowner))) a
         WHERE n.nspname = %s
         ORDER BY 1, 2, 3, 4
        """,
        (AFFECTED_SCHEMA,),
    ):
        effective.setdefault(int(r[0]), []).append(list(r[1:]))
    columns = {}
    for r in _rows(
        cursor,
        """
        SELECT a.attrelid, a.attnum, a.attname, a.atttypid, a.atttypmod,
               a.attnotnull, a.attidentity, a.attgenerated
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attrelid, a.attnum
        """,
        (AFFECTED_SCHEMA,),
    ):
        columns.setdefault(int(r[0]), []).append(list(r[1:]))
    out = []
    for oid, schema, name, relkind, owner, is_null in rows:
        full = f"{schema}.{name}"
        out.append(
            {
                "key": f"relation:{full}",
                "kind": "sequence" if relkind == "S" else "relation",
                "relkind": relkind,
                "identity": {
                    "oid": int(oid),
                    "schema": schema,
                    "name": name,
                    "owner": owner,
                    "columns": columns.get(int(oid), []),
                },
                "affected": full in PHASE2_RELATIONS,
                "acl_is_null": bool(is_null),
                "acl_explicit": sorted(explicit.get(int(oid), [])),
                "acl_effective": sorted(effective.get(int(oid), [])),
            }
        )
    return out


def _capture_functions(cursor) -> list[dict]:
    rows = _rows(
        cursor,
        """
        SELECT p.oid, n.nspname, p.proname,
               pg_get_function_identity_arguments(p.oid),
               COALESCE((SELECT string_agg(format_type(u.t, NULL), ',' ORDER BY u.ord)
                           FROM unnest(p.proargtypes) WITH ORDINALITY AS u(t, ord)), ''),
               pg_get_userbyid(p.proowner), p.prokind, p.provolatile,
               p.prosecdef, p.proconfig, md5(p.prosrc), p.proargtypes::text,
               p.prorettype, p.proacl IS NULL
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = %s
         ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
        """,
        (AFFECTED_SCHEMA,),
    )
    explicit = {}
    for r in _rows(
        cursor,
        """
        SELECT p.oid, pg_get_userbyid(a.grantor),
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type, a.is_grantable
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace,
               aclexplode(p.proacl) a
         WHERE n.nspname = %s
         ORDER BY 1, 2, 3, 4
        """,
        (AFFECTED_SCHEMA,),
    ):
        explicit.setdefault(int(r[0]), []).append(list(r[1:]))
    effective = {}
    for r in _rows(
        cursor,
        """
        SELECT p.oid, pg_get_userbyid(a.grantor),
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type, a.is_grantable
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace,
               aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a
         WHERE n.nspname = %s
         ORDER BY 1, 2, 3, 4
        """,
        (AFFECTED_SCHEMA,),
    ):
        effective.setdefault(int(r[0]), []).append(list(r[1:]))
    out = []
    for (
        oid, schema, name, identity_args, argtypes_text, owner, prokind, provolatile,
        prosecdef, proconfig, prosrc_md5, proargtypes, prorettype, is_null,
    ) in rows:
        # key form matches PHASE2_FUNCTIONS: types only (no parameter names)
        signature = f"{schema}.{name}({argtypes_text})"
        out.append(
            {
                "key": f"function:{signature}",
                "kind": "function",
                "identity": {
                    "oid": int(oid),
                    "schema": schema,
                    "name": name,
                    "identity_arguments": identity_args,
                    "signature": signature,
                    "owner": owner,
                    "prokind": prokind,
                    "provolatile": provolatile,
                    "proargtypes": proargtypes,
                    "prorettype": int(prorettype),
                    "prosrc_md5": prosrc_md5,
                },
                "config": {
                    "security": "DEFINER" if prosecdef else "INVOKER",
                    "proconfig": sorted(proconfig) if proconfig else [],
                },
                "affected": signature in PHASE2_FUNCTIONS,
                "acl_is_null": bool(is_null),
                "acl_explicit": sorted(explicit.get(int(oid), [])),
                "acl_effective": sorted(effective.get(int(oid), [])),
            }
        )
    return out


def _capture_roles(cursor) -> dict:
    rows = _rows(
        cursor,
        """
        SELECT r.rolname, r.rolsuper, r.rolinherit, r.rolcreaterole,
               r.rolcreatedb, r.rolcanlogin, r.rolreplication, r.rolbypassrls,
               r.rolconnlimit, r.rolvaliduntil::text
          FROM pg_roles r
         WHERE r.rolname !~ '^pg_'
         ORDER BY r.rolname
        """,
    )
    roles = {}
    for (
        name, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
        rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil,
    ) in rows:
        roles[name] = {
            "superuser": bool(rolsuper),
            "inherit": bool(rolinherit),
            "createrole": bool(rolcreaterole),
            "createdb": bool(rolcreatedb),
            "can_login": bool(rolcanlogin),
            "replication": bool(rolreplication),
            "bypassrls": bool(rolbypassrls),
            "connlimit": int(rolconnlimit),
            "valid_until": rolvaliduntil,
        }
    return roles


def _capture_memberships(cursor) -> list[list]:
    return sorted(
        [list(r) for r in _rows(
            cursor,
            """
            SELECT pg_get_userbyid(m.roleid), pg_get_userbyid(m.member), m.admin_option
              FROM pg_auth_members m
             ORDER BY 1, 2
            """,
        )]
    )


def _capture_default_privileges(cursor) -> list[dict]:
    rows = _rows(
        cursor,
        """
        SELECT d.oid, pg_get_userbyid(d.defaclrole),
               COALESCE(n.nspname, ''), d.defaclobjtype, d.defaclacl IS NULL
          FROM pg_default_acl d
          LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
         ORDER BY 2, 3, 4
        """,
    )
    explicit = {}
    for r in _rows(
        cursor,
        """
        SELECT d.oid, pg_get_userbyid(a.grantor),
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type, a.is_grantable
          FROM pg_default_acl d, aclexplode(d.defaclacl) a
         ORDER BY 1, 2, 3, 4
        """,
    ):
        explicit.setdefault(int(r[0]), []).append(list(r[1:]))
    out = []
    for oid, grantee_role, nspname, objtype, is_null in rows:
        out.append(
            {
                "identity": {
                    "oid": int(oid),
                    "grantee_role": grantee_role,
                    "schema": nspname,
                    "object_type": objtype,
                },
                "acl_is_null": bool(is_null),
                "acl_explicit": sorted(explicit.get(int(oid), [])),
            }
        )
    return out


def _capture_shdepend(cursor, role_names: list[str]) -> list[dict]:
    rows = _rows(
        cursor,
        """
        SELECT pg_get_userbyid(s.refobjid) AS role_name,
               s.classid::regclass::text AS class_name,
               s.objid, s.objsubid, s.deptype, s.dbid
          FROM pg_shdepend s
         WHERE s.refclassid = 'pg_authid'::regclass
           AND pg_get_userbyid(s.refobjid) = ANY(%s)
         ORDER BY 1, 2, 3, 4, 6
        """,
        (role_names,),
    )
    return [
        {
            "role": r[0],
            "class": r[1],
            "objid": int(r[2]),
            "objsubid": int(r[3]),
            "deptype": r[4],
            "dbid": int(r[5]),
        }
        for r in rows
    ]


def _capture_effective_privileges(cursor, state: dict, role_names: list[str]) -> dict:
    """Per (role, object, privilege) effective privileges via has_*_privilege()."""
    out: dict = {}
    groups: dict = {
        "database": [], "schema": [], "relation": [], "sequence": [], "function": [],
    }
    db_oid = state["database"]["identity"]["oid"]
    for role in role_names:
        for priv in DATABASE_PRIVILEGES:
            groups["database"].append((role, state["database"]["key"], db_oid, priv))
    for schema in state["schemas"]:
        if not schema.get("affected"):
            continue
        for role in role_names:
            for priv in SCHEMA_PRIVILEGES:
                groups["schema"].append(
                    (role, schema["key"], schema["identity"]["oid"], priv)
                )
    for rel in state["relations"]:
        kind = "sequence" if rel["kind"] == "sequence" else "relation"
        privs = SEQUENCE_PRIVILEGES if rel["kind"] == "sequence" else RELATION_PRIVILEGES
        for role in role_names:
            for priv in privs:
                groups[kind].append((role, rel["key"], rel["identity"]["oid"], priv))
    for fn in state["functions"]:
        for role in role_names:
            for priv in FUNCTION_PRIVILEGES:
                groups["function"].append((role, fn["key"], fn["identity"]["oid"], priv))
    fun = {
        "database": "has_database_privilege(u.rol::name, u.oid::oid, u.priv::text)",
        "schema": "has_schema_privilege(u.rol::name, u.oid::oid, u.priv::text)",
        "relation": "has_table_privilege(u.rol::name, u.oid::oid, u.priv::text)",
        "sequence": "has_sequence_privilege(u.rol::name, u.oid::oid, u.priv::text)",
        "function": "has_function_privilege(u.rol::name, u.oid::oid, u.priv::text)",
    }
    # one round trip per object class instead of one per (role, object, priv)
    for kind, checks in groups.items():
        if not checks:
            continue
        cursor.execute(
            "SELECT u.rol, u.objkey, u.priv, "
            + fun[kind]
            + " FROM unnest(%s::text[], %s::text[], %s::oid[], %s::text[]) "
            "AS u(rol, objkey, oid, priv)",
            (
                [c[0] for c in checks],
                [c[1] for c in checks],
                [c[2] for c in checks],
                [c[3] for c in checks],
            ),
        )
        for role, key, priv, value in cursor.fetchall():
            out.setdefault(role, {}).setdefault(key, {})[priv] = bool(value)
    return out


def capture_state(conn) -> dict:
    """Capture the full deterministic privilege state (READ-ONLY)."""
    conn.rollback()
    with conn.cursor() as cursor:
        cursor.execute("SET LOCAL default_transaction_read_only = on")
        cursor.execute("SELECT current_setting('server_version_num')")
        server_version_num = int(cursor.fetchone()[0])
        if server_version_num < 170000:
            raise SnapshotError(
                f"PostgreSQL 17 is required for this tool (got server_version_num="
                f"{server_version_num}); fail closed"
            )
        state: dict = {
            "database": _capture_database(cursor),
            "schemas": _capture_schemas(cursor),
            "relations": _capture_relations(cursor),
            "functions": _capture_functions(cursor),
            "roles": _capture_roles(cursor),
            "memberships": _capture_memberships(cursor),
            "default_privileges": _capture_default_privileges(cursor),
        }
        role_names = sorted(state["roles"])
        state["shdepend"] = _capture_shdepend(cursor, role_names)
        state["effective_privileges"] = _capture_effective_privileges(
            cursor, state, role_names
        )
        cursor.execute("SHOW server_version")
        server_version = cursor.fetchone()[0]
    conn.rollback()
    state["capture_info"] = {
        "server_version": server_version,
        "server_version_num": server_version_num,
        "affected_schema": AFFECTED_SCHEMA,
        "phase2_relations": list(PHASE2_RELATIONS),
        "phase2_functions": list(PHASE2_FUNCTIONS),
    }
    assert_no_secrets(state)
    return state


# --------------------------------------------------------------------------- #
# snapshot files
# --------------------------------------------------------------------------- #
def build_snapshot(state: dict, meta_extra: dict | None = None) -> dict:
    meta = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "format": FORMAT,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    meta.update(meta_extra or {})
    assert_no_secrets(meta)
    return {
        "format": FORMAT,
        "meta": meta,
        "payload_sha256": payload_sha256(state),
        "state": state,
    }


def write_snapshot(path, snapshot: dict) -> dict:
    """Write snapshot + ``<name>.sha256`` sidecar; return integrity summary."""
    path = Path(path)
    assert_no_secrets(snapshot)
    text = json.dumps(snapshot, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    path.write_text(text, encoding="utf-8")
    digest = file_sha256(path)
    sidecar = sidecar_path(path)
    sidecar.write_text(digest + "  " + path.name + "\n", encoding="utf-8")
    return {
        "path": str(path),
        "sidecar": str(sidecar),
        "file_sha256": digest,
        "payload_sha256": snapshot["payload_sha256"],
    }


def sidecar_path(path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".sha256")


def verify_snapshot_file(path) -> dict:
    """Full integrity verification — fail closed on ANY mismatch (T13/T14)."""
    path = Path(path)
    if not path.exists():
        raise SnapshotIntegrityError(f"SNAPSHOT_FILE_MISSING: {path}")
    sidecar = sidecar_path(path)
    if not sidecar.exists():
        raise SnapshotIntegrityError(f"SNAPSHOT_CHECKSUM_FILE_MISSING: {sidecar}")
    expected_file_hash = sidecar.read_text(encoding="utf-8").split()[0].strip()
    actual_file_hash = file_sha256(path)
    if expected_file_hash != actual_file_hash:
        raise SnapshotIntegrityError(
            "SNAPSHOT_FILE_CHECKSUM_MISMATCH: the snapshot file was modified "
            "after it was taken"
        )
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - corrupt file
        raise SnapshotIntegrityError(f"SNAPSHOT_UNPARSEABLE: {exc}") from exc
    if snapshot.get("format") != FORMAT:
        raise SnapshotIntegrityError(
            f"SNAPSHOT_FORMAT_UNSUPPORTED: {snapshot.get('format')!r}"
        )
    state = snapshot.get("state")
    if not isinstance(state, dict):
        raise SnapshotIntegrityError("SNAPSHOT_STATE_MISSING")
    actual_payload_hash = payload_sha256(state)
    if actual_payload_hash != snapshot.get("payload_sha256"):
        raise SnapshotIntegrityError(
            "SNAPSHOT_PAYLOAD_CHECKSUM_MISMATCH: the captured state was modified"
        )
    assert_no_secrets(snapshot)
    return snapshot


def load_snapshot(path) -> tuple[dict, dict]:
    snapshot = verify_snapshot_file(path)
    return snapshot["meta"], snapshot["state"]


# --------------------------------------------------------------------------- #
# state diff / comparison
# --------------------------------------------------------------------------- #
def _objects_by_key(state: dict) -> dict[str, dict]:
    out = {state["database"]["key"]: state["database"]}
    for section in ("schemas", "relations", "functions"):
        for entry in state[section]:
            out[entry["key"]] = entry
    return out


def _acl_change_records(key: str, before: list, after: list) -> list[dict]:
    records = []
    before_set = {tuple(e) for e in before}
    after_set = {tuple(e) for e in after}
    for entry in sorted(after_set - before_set):
        records.append(
            {
                "kind": "acl_entry",
                "object": key,
                "action": "added",
                "entry": list(entry),
            }
        )
    for entry in sorted(before_set - after_set):
        records.append(
            {
                "kind": "acl_entry",
                "object": key,
                "action": "removed",
                "entry": list(entry),
            }
        )
    return records


def diff_states(before: dict, after: dict) -> list[dict]:
    """Canonical, sorted list of every difference between two states."""
    changes: list[dict] = []
    before_objs = _objects_by_key(before)
    after_objs = _objects_by_key(after)
    for key in sorted(set(before_objs) | set(after_objs)):
        b = before_objs.get(key)
        a = after_objs.get(key)
        if b is None or a is None:
            changes.append(
                {
                    "kind": "object_presence",
                    "object": key,
                    "action": "appeared" if b is None else "disappeared",
                }
            )
            continue
        # identity (oid/owner/signature/body hash/columns)
        b_ident = b.get("identity", {})
        a_ident = a.get("identity", {})
        for field in sorted(set(b_ident) | set(a_ident)):
            if b_ident.get(field) != a_ident.get(field):
                changes.append(
                    {
                        "kind": "identity",
                        "object": key,
                        "field": field,
                        "before": b_ident.get(field),
                        "after": a_ident.get(field),
                    }
                )
        # exact ACL (effective entries)
        changes.extend(
            _acl_change_records(key, b.get("acl_effective", []), a.get("acl_effective", []))
        )
        # function configuration
        if "config" in b or "config" in a:
            b_cfg = b.get("config", {})
            a_cfg = a.get("config", {})
            for field in sorted(set(b_cfg) | set(a_cfg)):
                if b_cfg.get(field) != a_cfg.get(field):
                    changes.append(
                        {
                            "kind": "function_config",
                            "object": key,
                            "field": field,
                            "before": b_cfg.get(field),
                            "after": a_cfg.get(field),
                        }
                    )
        # stored-ACL representation (explicit entries / NULL flag)
        b_repr = (b.get("acl_is_null"), b.get("acl_explicit"))
        a_repr = (a.get("acl_is_null"), a.get("acl_explicit"))
        if b_repr != a_repr and b.get("acl_effective") == a.get("acl_effective"):
            changes.append(
                {
                    "kind": "acl_representation",
                    "object": key,
                    "before": {"acl_is_null": b_repr[0], "acl_explicit": b_repr[1]},
                    "after": {"acl_is_null": a_repr[0], "acl_explicit": a_repr[1]},
                }
            )
    # roles
    for name in sorted(set(before["roles"]) | set(after["roles"])):
        b = before["roles"].get(name)
        a = after["roles"].get(name)
        if b is None:
            changes.append({"kind": "role", "role": name, "action": "added", "attributes": a})
        elif a is None:
            changes.append({"kind": "role", "role": name, "action": "removed", "attributes": b})
        elif b != a:
            changes.append(
                {
                    "kind": "role",
                    "role": name,
                    "action": "attributes_changed",
                    "before": b,
                    "after": a,
                }
            )
    # role memberships
    b_mem = {tuple(m) for m in before.get("memberships", [])}
    a_mem = {tuple(m) for m in after.get("memberships", [])}
    for row in sorted(a_mem - b_mem):
        changes.append({"kind": "membership", "action": "added", "row": list(row)})
    for row in sorted(b_mem - a_mem):
        changes.append({"kind": "membership", "action": "removed", "row": list(row)})
    # default privileges
    def _defacl_key(entry):
        ident = entry["identity"]
        return (ident["grantee_role"], ident["schema"], ident["object_type"])

    b_def = {_defacl_key(e): e for e in before.get("default_privileges", [])}
    a_def = {_defacl_key(e): e for e in after.get("default_privileges", [])}
    for key in sorted(set(b_def) | set(a_def)):
        b = b_def.get(key)
        a = a_def.get(key)
        if b != a:
            changes.append(
                {
                    "kind": "default_privileges",
                    "object": f"default_acl:{key[0]}:{key[1] or '(global)'}:{key[2]}",
                    "before": b,
                    "after": a,
                }
            )
    # shared dependencies of roles (cross-database dependency proof)
    def _shdep_key(row):
        return (row["role"], row["class"], row["objid"], row["objsubid"], row["deptype"], row["dbid"])

    b_dep = {_shdep_key(r) for r in before.get("shdepend", [])}
    a_dep = {_shdep_key(r) for r in after.get("shdepend", [])}
    for row in sorted(a_dep - b_dep):
        changes.append({"kind": "shdepend", "action": "added", "row": list(row)})
    for row in sorted(b_dep - a_dep):
        changes.append({"kind": "shdepend", "action": "removed", "row": list(row)})
    # effective per-role privileges (second, independent proof).
    # Roles that exist in only one state are covered by the role changes;
    # comparing them here would only produce noise (absent role = no grants).
    b_eff = before.get("effective_privileges", {})
    a_eff = after.get("effective_privileges", {})
    shared_roles = sorted(set(before.get("roles", {})) & set(after.get("roles", {})))
    for role in shared_roles:
        b_role = b_eff.get(role, {})
        a_role = a_eff.get(role, {})
        for key in sorted(set(b_role) | set(a_role)):
            b_obj = b_role.get(key, {})
            a_obj = a_role.get(key, {})
            for priv in sorted(set(b_obj) | set(a_obj)):
                if b_obj.get(priv) != a_obj.get(priv):
                    changes.append(
                        {
                            "kind": "effective_privilege",
                            "role": role,
                            "object": key,
                            "privilege": priv,
                            "before": b_obj.get(priv),
                            "after": a_obj.get(priv),
                        }
                    )
    changes.sort(key=lambda c: json.dumps(c, sort_keys=True, ensure_ascii=True))
    return changes


def compare_states(before: dict, after: dict) -> dict:
    """Comparison report: exact ACL equality + representation-only notes.

    ``acl_equal`` is the strict proof demanded by issue #185: every
    (grantor, grantee, privilege, grantable) tuple of every affected object
    is identical before and after.  ``representation_only`` lists stored-ACL
    cosmetic differences (NULL vs materialized default entries) that do not
    change any tuple and never count as an ACL difference.
    """
    changes = diff_states(before, after)
    representation_only = [c for c in changes if c["kind"] == "acl_representation"]
    substantive = [c for c in changes if c["kind"] != "acl_representation"]
    acl_differences = [
        c for c in substantive
        if c["kind"] in ("acl_entry", "identity", "object_presence", "function_config")
    ]
    effective_differences = [c for c in substantive if c["kind"] == "effective_privilege"]
    return {
        "acl_equal": not acl_differences,
        "effective_privileges_equal": not effective_differences,
        "equal": not substantive,
        "acl_differences": acl_differences,
        "other_differences": [
            c for c in substantive
            if c["kind"] not in ("acl_entry", "identity", "object_presence", "function_config")
        ],
        "effective_privilege_differences": effective_differences,
        "representation_only": representation_only,
        "changes": changes,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _connect(args):
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover
        raise SnapshotError("psycopg2 is required to talk to PostgreSQL") from exc
    conn = psycopg2.connect(
        host=args.host,
        port=args.port,
        dbname=args.dbname,
        user=args.user,
        connect_timeout=getattr(args, "connect_timeout", 10),
    )
    conn.autocommit = True
    return conn


def _add_conn_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--dbname", required=True)
    parser.add_argument("--user", default="postgres")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL_NAME)
    sub = parser.add_subparsers(dest="command", required=True)

    p_snap = sub.add_parser("snapshot", help="capture a READ-ONLY privilege snapshot")
    _add_conn_args(p_snap)
    p_snap.add_argument("--output", required=True)

    p_ver = sub.add_parser("verify", help="verify snapshot integrity (checksums)")
    p_ver.add_argument("--snapshot", required=True)

    p_cmp = sub.add_parser("compare", help="compare two snapshots exactly")
    p_cmp.add_argument("--before", required=True)
    p_cmp.add_argument("--after", required=True)
    p_cmp.add_argument("--output", default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            conn = _connect(args)
            try:
                state = capture_state(conn)
            finally:
                conn.close()
            snapshot = build_snapshot(
                state,
                {
                    "database": args.dbname,
                    "captured_by": args.user,
                    "host": args.host,
                    "port": args.port,
                },
            )
            summary = write_snapshot(args.output, snapshot)
            print(json.dumps(summary, sort_keys=True, indent=2))
            return 0
        if args.command == "verify":
            snapshot = verify_snapshot_file(args.snapshot)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "payload_sha256": snapshot["payload_sha256"],
                        "file_sha256": file_sha256(args.snapshot),
                    },
                    sort_keys=True,
                    indent=2,
                )
            )
            return 0
        if args.command == "compare":
            _, before = load_snapshot(args.before)
            _, after = load_snapshot(args.after)
            report = compare_states(before, after)
            text = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True)
            if args.output:
                Path(args.output).write_text(text + "\n", encoding="utf-8")
            print(text)
            return 0 if report["equal"] else 1
    except SnapshotError as exc:
        print(f"FAIL_CLOSED: {exc}", file=sys.stderr)
        return 2
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
