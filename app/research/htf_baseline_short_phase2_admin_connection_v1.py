"""Issue #205 — Phase-2 freeze tool ADMIN connection routing V1.

Why this module exists
----------------------
The Phase-2 freeze tool (``tools/research/htf_baseline_short_phase2_freeze_record_v1.py``,
subcommands ``mint`` / ``claim`` / ``verify``) is an OPERATOR ADMINISTRATIVE
procedure. Its only database write is the single sanctioned activation call
``research.htf_baseline_short_phase2_activate(...)`` (initial ``claim``), whose
EXECUTE privilege migration 075 grants EXCLUSIVELY to the role
``trad_bot_htf_p2_admin``.

The observer runtime connection loader
(``app.research.htf_baseline_short_phase2_observer_runner_v1``: ``load_db_credentials``
/ ``connect_db_from_env``) is deliberately observer-only: it REQUIRES
``HTF_P2_DB_USER`` to name the dedicated observer login and refuses shared or
administrative roles. Routing the freeze tool through that loader (the pre-
issue-#205 defect) made every ``mint``/``claim``/``verify`` REFUSE with "HTF_P2_DB_USER
must explicitly name the dedicated observer role (no default user)" — a
correct fail-closed result of the WRONG authentication context. That loader is
left completely unchanged; this module is the separate admin channel.

The two channels are independent by construction:

===================  =================================  ==========================
aspect               observer channel                   admin channel (this module)
===================  =================================  ==========================
environment          ``HTF_P2_DB_*``                    ``HTF_P2_ADMIN_DB_*``
role context         ``trad_bot_htf_p2_observer``       ``SET ROLE trad_bot_htf_p2_admin``
table privileges     SELECT only (3 Phase-2 relations)  SELECT only (3 Phase-2 relations)
function privileges  EXECUTE insert_observation only    EXECUTE activate only
database writes      insert_observation() only          activate() ONLY (initial claim)
session write mode   read-write                         READ-ONLY for mint / verify /
                                                        repair; read-write ONLY for the
                                                        initial claim
===================  =================================  ==========================

Neither loader reads the other's environment variables, neither loads the
shared application ``.env``, and no password ever reaches a log line, an
exception message or the repository.

Architecture: controlled ``SET ROLE`` into the NOLOGIN admin role
-----------------------------------------------------------------
``trad_bot_htf_p2_admin`` remains ``NOLOGIN`` (migration 075 treats LOGIN as an
unsafe attribute; this module re-verifies it on EVERY connection — converting
the admin role to LOGIN without separate architectural authorization is
detected and refused). The operator therefore authenticates as an operator
LOGIN role that is a MEMBER of the admin role (the production channel is the
``postgres`` operator login, the sole login member audited in the issue), and
the session then drops into the least-privilege admin context through a
controlled ``SET ROLE trad_bot_htf_p2_admin``. After ``SET ROLE`` the session
runs with the admin role's privileges ONLY (superuser rights of the login are
dropped for privilege checks), so the activation function — SECURITY DEFINER
with a pinned ``search_path`` — is the single sanctioned write path and direct
DML on the Phase-2 relations is denied BY THE DATABASE.

Connection setup is a strict, fail-closed sequence (every step may refuse):

1. explicit credentials from ``HTF_P2_ADMIN_DB_*`` only — no defaults, no
   password-less fallback, no ``.env``, no ``HTF_P2_DB_*`` fallback;
2. pinned target identity verified against the SERVER-REPORTED identity
   (``current_database()`` / ``inet_server_addr()`` / ``inet_server_port()`` /
   ``session_user``, plus the optional cluster ``system_identifier`` pin via
   ``pg_control_system()``) — never against the client-side request;
3. reviewed RBAC invariants BEFORE any privilege is assumed: admin role exists,
   NOLOGIN, non-superuser; observer role exists and still holds NO EXECUTE on
   the activation function while keeping its insert EXECUTE (observer RBAC
   expansion is detected and refused); the login may ``SET ROLE`` to the admin
   role;
4. ``SET ROLE trad_bot_htf_p2_admin``; ``current_user`` re-checked; effective
   privilege matrix verified (activation EXECUTE required for ``activate``
   mode only; SELECT present and INSERT/UPDATE/DELETE/TRUNCATE ABSENT on all
   three Phase-2 relations in both modes — no direct-DML shortcut);
5. transaction write mode pinned per operation: ``mint``, ``verify`` and
   ``--repair-activation-json`` run in a PostgreSQL READ-ONLY transaction
   (``SET TRANSACTION READ ONLY`` + ``default_transaction_read_only``), so
   "writes nothing to the database" is enforced by PostgreSQL, not by
   convention. Only the initial ``claim`` obtains a read-write session.

Any missing credential, mismatched target, unexpected role attribute,
insufficient privilege or authorization error raises
:class:`AdminConnectionError` and the tool exits fail-closed BEFORE any freeze
artifact or database write happens.

Transaction scope of the session context (issue #205 rollback-safety)
---------------------------------------------------------------------
``SET ROLE`` and ``SET`` are TRANSACTIONAL in PostgreSQL: executed inside an
open transaction they are reverted by ``ROLLBACK``. A setup that leaves them
uncommitted therefore silently returns ``current_user`` to the operator LOGIN
(e.g. the ``postgres`` superuser) on the first ``conn.rollback()`` — the
discovered defect: direct DML would then pass the permission checks and hit a
trigger error instead of ``permission denied``. This module instead:

* runs the identity/RBAC checks in a PREPARATORY transaction that is ROLLED
  BACK — nothing business-side can ever be committed there — and then
  establishes ``SET ROLE trad_bot_htf_p2_admin`` and the session write-mode
  pin (``SET default_transaction_read_only``) in a setup transaction
  containing ONLY those ``SET`` statements, **committed** so the
  administrative context is session-persistent and a later ROLLBACK of work
  transactions can never restore the login's original privileges;
* re-verifies the effective context in a FRESH transaction right after the
  commit: ``current_user`` must be the admin role, ``session_user`` must be
  the operator login recorded at connect time (the NOLOGIN admin role can
  never be a ``session_user``), the per-mode privilege matrix and the
  write-mode pin must hold;
* exposes :func:`recover_admin_db` — the ONLY sanctioned transaction-recovery
  entry point: it rolls the (possibly failed) transaction back and re-verifies
  the effective admin context INCLUDING the recorded ``session_user`` login
  identity; on any mismatch the connection is CLOSED and
  :class:`AdminConnectionError` is raised. The session must never continue on
  the login's privileges, and a privilege loss is never silent.

Per-operation privilege matrix (issue #205 deliverable 2)
--------------------------------------------------------
===================  ===============  ==================  =========================
operation            mode             privileges required  database writes
===================  ===============  ==================  =========================
mint                 ``read_only``    SELECT (3 relations)  NONE (DB-enforced)
verify               ``read_only``    SELECT (3 relations)  NONE (DB-enforced)
claim --repair...    ``read_only``    SELECT (3 relations)  NONE (DB-enforced)
claim (initial)      ``activate``     SELECT + EXECUTE       only
===================  ===============  ==================  =========================

So ``mint``/``verify``/repair do NOT require the activation privilege at all,
and the privilege to write exists only inside the initial claim — where the
sole write is the reviewed ``research.htf_baseline_short_phase2_activate(...)``
entry point.
"""
from __future__ import annotations

import logging
import os
import stat as stat_module

log = logging.getLogger("htf.phase2.admin_db")

#: Role entered via controlled ``SET ROLE`` (NOLOGIN; never a login user).
ADMIN_ROLE = "trad_bot_htf_p2_admin"
#: Dedicated runtime observer role — NEVER accepted by this admin channel.
OBSERVER_ROLE = "trad_bot_htf_p2_observer"

#: The single sanctioned activation entry point (exact function signature).
ACTIVATE_FUNCTION_SIGNATURE = (
    "research.htf_baseline_short_phase2_activate(text,timestamp with time zone,text)"
)
#: The observer runtime write entry point (must stay observer-executable).
INSERT_FUNCTION_SIGNATURE = (
    "research.htf_baseline_short_phase2_insert_observation("
    "text,text,text,text,text,timestamp with time zone,timestamp with time zone,"
    "timestamp with time zone,numeric,numeric,boolean,boolean,jsonb)"
)
#: The isolated Phase-2 storage surface (migration 074/075).
PHASE2_RELATIONS = (
    "research.htf_baseline_short_phase2_experiment",
    "research.htf_baseline_short_phase2_observation",
    "research.htf_baseline_short_phase2_freeze_registry",
)

#: mint / verify / repair: PostgreSQL-enforced read-only session.
READ_ONLY_MODE = "read_only"
#: initial claim: read-write session whose single write is activate().
ACTIVATE_MODE = "activate"

#: Environment namespace of THIS channel only (the observer uses HTF_P2_DB_*).
ADMIN_DB_ENV_PREFIX = "HTF_P2_ADMIN_DB_"

DB_CONNECT_TIMEOUT_S = 5


class AdminConnectionError(RuntimeError):
    """Missing/unsafe admin credentials or unmet connection invariants.

    Fail closed: the caller must abort the operator action without writing any
    freeze artifact and without touching the database. The message never
    contains a password.
    """


# ------------------------------------------------------------ password file
def check_admin_password_file_stat(st_mode: int, st_uid: int, euid: int | None = None) -> None:
    """Validate admin password-file metadata; raises AdminConnectionError.

    Pure logic (usable on any platform): the file must be a regular file, must
    not be group/world accessible (``mode & 0o077`` must be 0; 0600/0400 pass)
    and must be owned by the operator user or root. ``euid`` defaults to the
    current effective uid.

    Intentionally mirrors the observer channel's hardened reader
    (``check_password_file_stat``) with the SAME threat model; it is kept as a
    separate implementation so the admin channel has NO code dependency on the
    observer runtime, and a parity test pins both behaviors together.
    """
    if not stat_module.S_ISREG(st_mode):
        raise AdminConnectionError(
            "admin password file must be a regular file (not a symlink or device)"
        )
    if stat_module.S_IMODE(st_mode) & 0o077:
        raise AdminConnectionError(
            "admin password file must not be group/world accessible (require 0600 or stricter)"
        )
    if os.name == "posix":
        owner = os.geteuid() if euid is None else euid
        if st_uid not in (owner, 0):
            raise AdminConnectionError(
                "admin password file must be owned by the operator user or root"
            )


def _read_admin_password_file(path: str) -> str:
    """Read the admin password file defensively (symlink/TOCTOU resistant).

    The password value never appears in exceptions or logs.
    """
    if os.name == "posix":
        try:
            pre = os.lstat(path)
        except OSError as exc:
            raise AdminConnectionError(f"admin password file unreadable: {exc}") from exc
        if stat_module.S_ISLNK(pre.st_mode):
            raise AdminConnectionError("admin password file must not be a symlink")
        # reject non-regular files (FIFO/sockets/devices) BEFORE open — opening
        # a FIFO would block waiting for a writer.
        if not stat_module.S_ISREG(pre.st_mode):
            raise AdminConnectionError(
                "admin password file must be a regular file (not a symlink or device)"
            )
        # O_NOFOLLOW blocks symlink swaps; O_NONBLOCK never blocks if a regular
        # file is swapped for a FIFO between lstat and open (the fstat check
        # below then rejects the opened object).
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise AdminConnectionError(f"admin password file unreadable: {exc}") from exc
        try:
            fst = os.fstat(fd)  # stat the opened object: TOCTOU-safe
            check_admin_password_file_stat(fst.st_mode, fst.st_uid)
            data = os.read(fd, 65536)
        finally:
            os.close(fd)
    else:
        # Windows: POSIX mode/uid semantics do not apply; content checks below
        try:
            data = open(path, "rb").read(65536)
        except OSError as exc:
            raise AdminConnectionError(f"admin password file unreadable: {exc}") from exc
    lines = data.decode("utf-8", errors="replace").splitlines()
    password = next((line.strip() for line in lines if line.strip()), "")
    if not password:
        raise AdminConnectionError("admin password file contains no password")
    return password


# ------------------------------------------------------------------ credentials
def load_admin_db_credentials() -> dict:
    """Load the OPERATOR/ADMIN credentials from ``HTF_P2_ADMIN_DB_*`` only.

    Fail-closed contract (issue #205 FIX-1):
      * ``HTF_P2_ADMIN_DB_USER`` is REQUIRED (no default user) and must name an
        operator LOGIN role — the dedicated observer role and the NOLOGIN admin
        role itself are refused explicitly;
      * ``HTF_P2_ADMIN_DB_HOST``, ``HTF_P2_ADMIN_DB_PORT`` and
        ``HTF_P2_ADMIN_DB_NAME`` are REQUIRED (no implicit host/port/database)
        — the target is pinned explicitly, per run;
      * the password comes from ``HTF_P2_ADMIN_DB_PASSWORD`` or, preferably,
        from the file named by ``HTF_P2_ADMIN_DB_PASSWORD_FILE`` (first
        non-empty line). No password -> refuse to connect (never trust/peer
        fallback);
      * ``HTF_P2_ADMIN_DB_SYSTEM_IDENTIFIER`` optionally pins the cluster
        identity (``pg_control_system()``); when set it is verified strictly;
      * the observer ``HTF_P2_DB_*`` namespace and the shared application
        ``.env`` are NEVER consulted.
    """
    user = os.environ.get(ADMIN_DB_ENV_PREFIX + "USER", "").strip()
    if not user:
        raise AdminConnectionError(
            "HTF_P2_ADMIN_DB_USER is required (no default user). The observer "
            "HTF_P2_DB_* credentials can NEVER satisfy the admin tool — provide "
            "the explicit operator/admin login channel (fail closed)"
        )
    if user == OBSERVER_ROLE:
        raise AdminConnectionError(
            "HTF_P2_ADMIN_DB_USER must not be the dedicated observer role: "
            "observer credentials are refused by the admin channel (fail closed)"
        )
    if user == ADMIN_ROLE:
        raise AdminConnectionError(
            "HTF_P2_ADMIN_DB_USER must name an operator LOGIN role: "
            "trad_bot_htf_p2_admin is NOLOGIN and is entered only via the "
            "controlled SET ROLE step (fail closed)"
        )
    host = os.environ.get(ADMIN_DB_ENV_PREFIX + "HOST", "").strip()
    if not host:
        raise AdminConnectionError(
            "HTF_P2_ADMIN_DB_HOST is required (no default host; the target cluster is pinned explicitly)"
        )
    port_raw = os.environ.get(ADMIN_DB_ENV_PREFIX + "PORT", "").strip()
    if not port_raw:
        raise AdminConnectionError(
            "HTF_P2_ADMIN_DB_PORT is required (no default port; the target cluster is pinned explicitly)"
        )
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise AdminConnectionError("HTF_P2_ADMIN_DB_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise AdminConnectionError("HTF_P2_ADMIN_DB_PORT must be in 1..65535")
    name = os.environ.get(ADMIN_DB_ENV_PREFIX + "NAME", "").strip()
    if not name:
        raise AdminConnectionError(
            "HTF_P2_ADMIN_DB_NAME is required (no implicit database; the target database is pinned explicitly)"
        )
    password = os.environ.get(ADMIN_DB_ENV_PREFIX + "PASSWORD")
    if password is None or password == "":
        pw_file = os.environ.get(ADMIN_DB_ENV_PREFIX + "PASSWORD_FILE", "").strip()
        if not pw_file:
            raise AdminConnectionError(
                "HTF_P2_ADMIN_DB_PASSWORD or HTF_P2_ADMIN_DB_PASSWORD_FILE is "
                "required (refusing password-less admin connection)"
            )
        password = _read_admin_password_file(pw_file)
    system_identifier = os.environ.get(
        ADMIN_DB_ENV_PREFIX + "SYSTEM_IDENTIFIER", ""
    ).strip() or None
    return {
        "host": host,
        "port": port,
        "user": user,
        "database": name,
        "password": password,
        "system_identifier": system_identifier,
    }


# ------------------------------------------------------------- target identity
def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def server_host_matches(expected: str, actual: str | None) -> bool:
    """Pinned-host comparison (pure logic): loopback spellings are equivalent."""
    if actual is None:
        return False
    exp = str(expected).strip().lower()
    act = str(actual).strip().lower()
    if exp == act:
        return True
    loopback = {"127.0.0.1", "::1", "0:0:0:0:0:0:0:1", "localhost"}
    return exp in loopback and act in loopback


def verify_admin_target_identity(conn, creds: dict) -> None:
    """Verify the SERVER-REPORTED identity against the pinned target (fail closed).

    The comparison is deliberately made against what the SERVER reports
    (``current_database()`` / ``inet_server_addr()`` / ``inet_server_port()`` /
    ``session_user``) so a tunnel, proxy or misrouted connection cannot silently
    land on another cluster/database than the operator pinned. When a cluster
    ``system_identifier`` pin is configured it must match exactly, and an
    unreadable identifier fails closed as well.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT current_database(), inet_server_addr(), inet_server_port(), session_user"
        )
        row = cur.fetchone()
    finally:
        cur.close()
    if row is None:
        raise AdminConnectionError("cannot read server identity (fail closed)")
    database, server_addr, server_port, session_user = row
    if database != creds["database"]:
        raise AdminConnectionError(
            f"connected database {database!r} != pinned HTF_P2_ADMIN_DB_NAME "
            f"{creds['database']!r} (wrong database/cluster; fail closed)"
        )
    if _as_int(server_port) != int(creds["port"]):
        raise AdminConnectionError(
            f"server port {server_port!r} != pinned HTF_P2_ADMIN_DB_PORT "
            f"{creds['port']} (wrong cluster or tunnel drift; fail closed)"
        )
    if not server_host_matches(creds["host"], server_addr):
        raise AdminConnectionError(
            f"server address {server_addr!r} != pinned HTF_P2_ADMIN_DB_HOST "
            f"{creds['host']!r} (wrong cluster; fail closed)"
        )
    if session_user != creds["user"]:
        raise AdminConnectionError(
            f"authenticated session_user {session_user!r} != HTF_P2_ADMIN_DB_USER "
            f"{creds['user']!r} (unexpected authentication context; fail closed)"
        )
    if creds.get("system_identifier"):
        cur = conn.cursor()
        try:
            cur.execute("SELECT system_identifier FROM pg_control_system()")
            row = cur.fetchone()
        except Exception as exc:  # unreadable pin == unverifiable target
            raise AdminConnectionError(
                "HTF_P2_ADMIN_DB_SYSTEM_IDENTIFIER is pinned but the cluster "
                f"identifier cannot be read ({exc}); fail closed"
            ) from exc
        finally:
            cur.close()
        if row is None or str(row[0]) != str(creds["system_identifier"]):
            raise AdminConnectionError(
                f"cluster system_identifier {None if row is None else row[0]!r} != "
                f"pinned HTF_P2_ADMIN_DB_SYSTEM_IDENTIFIER "
                f"{creds['system_identifier']!r} (wrong cluster; fail closed)"
            )
    else:
        log.warning(
            "admin db cluster system identifier NOT pinned; set "
            "HTF_P2_ADMIN_DB_SYSTEM_IDENTIFIER for production activation runs"
        )


# --------------------------------------------------------------- RBAC invariants
def _role_attrs(conn, role_name: str):
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = %s",
            (role_name,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    if row is None:
        return None
    return {
        "rolcanlogin": bool(row[0]),
        "rolsuper": bool(row[1]),
        "rolcreatedb": bool(row[2]),
        "rolcreaterole": bool(row[3]),
    }


def _has_function_privilege(conn, role_name: str, signature: str) -> bool:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
            (role_name, signature),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    return bool(row and row[0])


def _may_set_role(conn, login_user: str) -> bool:
    """The login may enter the admin context iff it is a member of the admin
    role or a superuser (``SET ROLE`` semantics)."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER'), "
            "COALESCE((SELECT usesuper FROM pg_user WHERE usename = %s), false)",
            (login_user, ADMIN_ROLE, login_user),
        )
        row = cur.fetchone()
    finally:
        cur.close()
    return bool(row and (row[0] or row[1]))


def verify_admin_rbac_invariants(conn, creds: dict) -> None:
    """Verify the REVIEWED PostgreSQL RBAC surface before any privilege is used.

    Refuses (fail closed) when the audited role contract drifted: the admin
    role must stay NOLOGIN / non-superuser, the observer role must keep its
    insert EXECUTE and must NOT hold EXECUTE on the activation function
    (observer RBAC expansion), and the login must be allowed to ``SET ROLE``
    into the admin role.
    """
    admin = _role_attrs(conn, ADMIN_ROLE)
    if admin is None:
        raise AdminConnectionError(
            f"reviewed RBAC surface missing: role {ADMIN_ROLE} does not exist "
            "(migrations 074/075 not applied? fail closed)"
        )
    if admin["rolcanlogin"]:
        raise AdminConnectionError(
            f"{ADMIN_ROLE} must remain NOLOGIN (converting it to LOGIN requires "
            "separate architectural authorization); fail closed"
        )
    if admin["rolsuper"] or admin["rolcreatedb"] or admin["rolcreaterole"]:
        raise AdminConnectionError(
            f"{ADMIN_ROLE} carries unexpected role attributes "
            f"(super={admin['rolsuper']}, createdb={admin['rolcreatedb']}, "
            f"createrole={admin['rolcreaterole']}); fail closed"
        )
    observer = _role_attrs(conn, OBSERVER_ROLE)
    if observer is None:
        raise AdminConnectionError(
            f"reviewed RBAC surface missing: role {OBSERVER_ROLE} does not exist (fail closed)"
        )
    if not observer["rolcanlogin"] or observer["rolsuper"]:
        raise AdminConnectionError(
            f"{OBSERVER_ROLE} role attributes drifted "
            f"(login={observer['rolcanlogin']}, super={observer['rolsuper']}); fail closed"
        )
    if _has_function_privilege(conn, OBSERVER_ROLE, ACTIVATE_FUNCTION_SIGNATURE):
        raise AdminConnectionError(
            "observer RBAC EXPANDED: trad_bot_htf_p2_observer holds EXECUTE on the "
            "activation function — refuse everything, restore the reviewed grants "
            "(migration 075) and escalate; fail closed"
        )
    if not _has_function_privilege(conn, OBSERVER_ROLE, INSERT_FUNCTION_SIGNATURE):
        raise AdminConnectionError(
            "observer RBAC surface drift: trad_bot_htf_p2_observer lost EXECUTE on "
            "the insert function; fail closed"
        )
    if not _may_set_role(conn, creds["user"]):
        raise AdminConnectionError(
            f"login {creds['user']!r} is not authorized to SET ROLE "
            f"{ADMIN_ROLE} (not a member); fail closed"
        )


# ----------------------------------------------------------- effective context
def verify_effective_admin_context(
    conn, *, mode: str, expected_session_user: str | None = None
) -> None:
    """Verify the POST-``SET ROLE`` context and the per-mode privilege matrix.

    BOTH identities are checked: ``current_user`` must be the administrative
    role and ``session_user`` must be a real operator login — the NOLOGIN admin
    role can never be a ``session_user``, so anything else means the session
    identity is not what ``SET ROLE`` established. ``expected_session_user``
    pins the login identity recorded at connect time, so transaction recovery
    can prove the session did not silently switch logins.
    """
    if mode not in (READ_ONLY_MODE, ACTIVATE_MODE):
        raise AdminConnectionError(f"unknown admin connection mode {mode!r} (fail closed)")
    cur = conn.cursor()
    try:
        cur.execute("SELECT current_user, session_user")
        row = cur.fetchone()
    finally:
        cur.close()
    current = row[0] if row else None
    session = row[1] if row and len(row) > 1 else None
    if current != ADMIN_ROLE:
        raise AdminConnectionError(
            f"SET ROLE did not establish the admin context (current_user={current!r}); fail closed"
        )
    if not session or session == ADMIN_ROLE:
        raise AdminConnectionError(
            f"session_user={session!r} is not an operator login "
            f"({ADMIN_ROLE} is NOLOGIN and can never be a session_user); fail closed"
        )
    if expected_session_user is not None and session != expected_session_user:
        raise AdminConnectionError(
            f"session_user={session!r} differs from the login {expected_session_user!r} "
            "recorded at connect time (session identity changed after recovery?); fail closed"
        )
    for relation in PHASE2_RELATIONS:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT has_table_privilege(%s, 'SELECT'), has_table_privilege(%s, 'INSERT'), "
                "has_table_privilege(%s, 'UPDATE'), has_table_privilege(%s, 'DELETE'), "
                "has_table_privilege(%s, 'TRUNCATE')",
                (relation, relation, relation, relation, relation),
            )
            row = cur.fetchone()
        finally:
            cur.close()
        select_ok, insert_ok, update_ok, delete_ok, truncate_ok = (bool(v) for v in row)
        if not select_ok:
            raise AdminConnectionError(
                f"insufficient rights: {ADMIN_ROLE} lacks SELECT on {relation} (fail closed)"
            )
        if insert_ok or update_ok or delete_ok or truncate_ok:
            raise AdminConnectionError(
                f"direct DML shortcut detected: {ADMIN_ROLE} holds write privileges on "
                f"{relation} — the admin channel must only ever write through "
                "research.htf_baseline_short_phase2_activate(...) (fail closed)"
            )
    if mode == ACTIVATE_MODE and not _has_function_privilege(
        conn, ADMIN_ROLE, ACTIVATE_FUNCTION_SIGNATURE
    ):
        raise AdminConnectionError(
            f"insufficient rights: {ADMIN_ROLE} lacks EXECUTE on the activation "
            "function; the sanctioned claim path is unavailable (fail closed)"
        )
    cur = conn.cursor()
    try:
        cur.execute("SHOW transaction_read_only")
        row = cur.fetchone()
    finally:
        cur.close()
    actual = (row[0] if row else "").strip().lower()
    expected = "on" if mode == READ_ONLY_MODE else "off"
    if actual != expected:
        raise AdminConnectionError(
            f"transaction_read_only={actual!r} but mode {mode!r} requires {expected!r} "
            "(fail closed)"
        )


# ----------------------------------------------------------------- driver layer
def _open_raw_connection(creds: dict, driver: str | None = None):
    """Open the raw connection to the pinned target (pg8000 first, psycopg2).

    SCRAM-SHA-256 is supported natively by pg8000 (preferred); psycopg2
    (libpq) is the fallback. The password is passed only to the driver and is
    NEVER logged. ``driver`` is a test-only explicit override.
    """
    log.info(
        "admin db connect host=%s port=%s db=%s user=%s (password redacted)",
        creds["host"], creds["port"], creds["database"], creds["user"],
    )
    use_pg8000 = driver != "psycopg2"
    if use_pg8000:
        try:
            import pg8000.dbapi
        except ImportError:
            if driver == "pg8000":
                raise AdminConnectionError("pg8000 driver requested but not installed")
            use_pg8000 = False
        else:
            return pg8000.dbapi.connect(
                host=creds["host"],
                port=creds["port"],
                user=creds["user"],
                database=creds["database"],
                password=creds["password"],
                timeout=DB_CONNECT_TIMEOUT_S,
            )
    import psycopg2

    return psycopg2.connect(
        host=creds["host"],
        port=creds["port"],
        user=creds["user"],
        database=creds["database"],
        password=creds["password"],
        connect_timeout=DB_CONNECT_TIMEOUT_S,
    )


def _exec_one(conn, sql: str, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params) if params is not None else cur.execute(sql)
    finally:
        cur.close()


def connect_admin_db(mode: str, *, driver: str | None = None):
    """Open a verified admin-channel connection for ``mode`` (fail closed).

    Returns a connection already inside the ``trad_bot_htf_p2_admin`` context,
    with the transaction write mode pinned to the operation. Every failure —
    missing credentials, wrong cluster/database, drifted RBAC, insufficient
    rights, refused ``SET ROLE`` — raises :class:`AdminConnectionError` after
    closing the raw connection, BEFORE the tool touches any artifact.
    """
    if mode not in (READ_ONLY_MODE, ACTIVATE_MODE):
        raise AdminConnectionError(f"unknown admin connection mode {mode!r} (fail closed)")
    creds = load_admin_db_credentials()
    try:
        conn = _open_raw_connection(creds, driver=driver)
    except AdminConnectionError:
        raise
    except Exception as exc:  # auth/transport failure -> fail closed, no leak
        raise AdminConnectionError(
            f"admin database connection failed ({exc}); fail closed"
        ) from exc
    try:
        # Phase 1 — PREPARATORY transaction: identity + RBAC checks are pure
        # SELECTs and the transaction is ROLLED BACK (never committed), so it
        # is structurally impossible to commit business changes here.
        # pin the transaction write mode FIRST — SET TRANSACTION is only legal
        # before any query of the transaction.
        if mode == READ_ONLY_MODE:
            _exec_one(conn, "SET TRANSACTION READ ONLY")
        else:
            _exec_one(conn, "SET TRANSACTION READ WRITE")
        verify_admin_target_identity(conn, creds)
        verify_admin_rbac_invariants(conn, creds)
        conn.rollback()
        # Phase 2 — SESSION-CONTEXT SETUP transaction: contains ONLY the
        # ``SET ROLE`` / session write-mode statements and is COMMITTED.
        # Issue #205 rollback-safety: SET ROLE / SET are transactional in
        # PostgreSQL — left inside the open transaction they would be silently
        # reverted by the first conn.rollback(), restoring the operator login's
        # original (superuser) privileges. The session context is therefore
        # established here and COMMITTED in its own transaction, making it
        # session-persistent across any later rollback of work transactions.
        _exec_one(conn, f"SET ROLE {ADMIN_ROLE}")
        _exec_one(
            conn,
            "SET default_transaction_read_only = on"
            if mode == READ_ONLY_MODE
            else "SET default_transaction_read_only = off",
        )
        conn.commit()
        # Phase 3 — verify in a FRESH transaction: proves the committed session
        # context (admin role + operator session identity + per-mode write pin
        # + privilege matrix) really holds before the session is handed over.
        conn.admin_session_user = creds["user"]
        verify_effective_admin_context(
            conn, mode=mode, expected_session_user=creds["user"]
        )
    except AdminConnectionError:
        close_admin_db(conn)
        raise
    except Exception as exc:  # unexpected driver/server error -> fail closed
        close_admin_db(conn)
        raise AdminConnectionError(
            f"admin connection setup failed ({exc}); fail closed"
        ) from exc
    log.info(
        "admin db session ready role=%s mode=%s db=%s (password redacted)",
        ADMIN_ROLE, mode, creds["database"],
    )
    return conn


def close_admin_db(conn) -> None:
    """Close a database connection defensively (explicit resource cleanup)."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("admin db connection close failed: %s", exc)


def recover_admin_db(
    conn, *, mode: str, expected_session_user: str | None = None
) -> None:
    """Sanctioned transaction recovery for a live admin session (issue #205).

    ``conn.rollback()`` is a permitted transaction-recovery step for an admin
    session (clearing an aborted transaction between attempts). Because
    :func:`connect_admin_db` COMMITS the ``SET ROLE`` / session write-mode
    context, a rollback of work transactions keeps ``current_user =
    trad_bot_htf_p2_admin`` — it can never silently restore the operator
    login's original privileges.

    This helper is the fail-closed counterpart: it rolls the (possibly failed)
    transaction back and RE-VERIFIES the effective admin context
    (``current_user``, the ``session_user`` identity recorded at connect time,
    the per-mode privilege matrix and the write-mode pin). If the context is
    lost anyway — explicit ``RESET ROLE``, session reset, driver-level
    reconnect to another login — the connection is CLOSED and
    :class:`AdminConnectionError` is raised. The caller must never continue on
    the login's privileges.
    """
    if mode not in (READ_ONLY_MODE, ACTIVATE_MODE):
        raise AdminConnectionError(f"unknown admin connection mode {mode!r} (fail closed)")
    if conn is None:
        raise AdminConnectionError("no admin session to recover (fail closed)")
    if expected_session_user is None:
        expected_session_user = getattr(conn, "admin_session_user", None)
    try:
        conn.rollback()
    except Exception as exc:
        close_admin_db(conn)
        raise AdminConnectionError(
            f"admin session transaction recovery failed ({exc}); "
            "connection closed, fail closed"
        ) from exc
    try:
        verify_effective_admin_context(
            conn, mode=mode, expected_session_user=expected_session_user
        )
    except AdminConnectionError as exc:
        close_admin_db(conn)
        raise AdminConnectionError(
            f"admin context lost after transaction recovery: {exc}; "
            "connection closed, fail closed"
        ) from exc
