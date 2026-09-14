"""Shared test fixtures and PostgreSQL connection helper.

Environment variables (all optional — sensible defaults for local dev):

    TEST_DB_UNIX_SOCK   Unix socket file path (pg8000)
    TEST_DB_HOST        TCP host (default: localhost)
    TEST_DB_PORT        TCP port (default: 5432)
    TEST_DB_NAME        Database name (default: trad_bot_migration_test)
    TEST_DB_USER        Database user (default: postgres)
    TEST_DB_PASSWORD    Database password (default: empty)

When TEST_DB_UNIX_SOCK is set, pg8000 connects through the unix socket
(used on VPS where peer auth avoids passwords).  Otherwise TCP is used.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from uuid import uuid4

import pg8000
import pytest

_TMP_ROOT = Path(__file__).parent / ".tmp"


# ======================================================================
# Common PostgreSQL test connection
# ======================================================================

def connect_test_db(
    *,
    unix_sock: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    database: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> pg8000.Connection:
    """Open a pg8000 connection using env-derived or explicit parameters.

    Priority:
        1. Explicit keyword arguments (highest)
        2. TEST_DB_* environment variables
        3. Hard-coded defaults (localhost, 5432, trad_bot_migration_test, postgres, "")
    """
    _unix_sock = unix_sock or os.getenv("TEST_DB_UNIX_SOCK")
    _host = host or os.getenv("TEST_DB_HOST", "localhost")
    _port = port or int(os.getenv("TEST_DB_PORT", "5432"))
    _database = database or os.getenv("TEST_DB_NAME", "trad_bot_migration_test")
    _user = user or os.getenv("TEST_DB_USER", "postgres")
    _password = password if password is not None else os.getenv("TEST_DB_PASSWORD", "")

    if _unix_sock:
        return pg8000.connect(
            unix_sock=_unix_sock,
            database=_database,
            user=_user,
            password=_password,
        )

    return pg8000.connect(
        host=_host,
        port=_port,
        database=_database,
        user=_user,
        password=_password,
    )


def connect_test_db_psycopg2(
    *,
    database: Optional[str] = None,
    user: Optional[str] = None,
):
    """Open a psycopg2 connection using env-derived parameters.

    Supports unix socket (TEST_DB_UNIX_SOCK → host=/var/run/postgresql)
    and TCP (TEST_DB_HOST/TEST_DB_PORT).
    """
    import psycopg2

    _unix_sock = os.getenv("TEST_DB_UNIX_SOCK")
    _host = os.getenv("TEST_DB_HOST", "localhost")
    _port = int(os.getenv("TEST_DB_PORT", "5432"))
    _database = database or os.getenv("TEST_DB_NAME", "trad_bot_migration_test")
    _user = user or os.getenv("TEST_DB_USER", "postgres")

    if _unix_sock:
        # psycopg2 uses host=<socket_dir> for peer auth
        from pathlib import Path as _P
        return psycopg2.connect(host=str(_P(_unix_sock).parent), database=_database, user=_user)

    return psycopg2.connect(host=_host, port=_port, database=_database, user=_user)


def make_scanner_repo(*, database: Optional[str] = None, user: Optional[str] = None):
    """Create a ScannerRepository using TEST_DB_* env vars.

    For integration tests that need to write to config/dds tables.
    """
    from app.db.repository import ScannerRepository

    _unix_sock = os.getenv("TEST_DB_UNIX_SOCK")
    _host = os.getenv("TEST_DB_HOST", "localhost")
    _port = int(os.getenv("TEST_DB_PORT", "5432"))
    _database = database or os.getenv("TEST_DB_NAME", "trad_bot_migration_test")
    _user = user or os.getenv("TEST_DB_USER", "postgres")

    if _unix_sock:
        from pathlib import Path as _P
        host = str(_P(_unix_sock).parent)
    else:
        host = _host

    return ScannerRepository(
        host=host, port=_port, database=_database,
        user=_user, backend="postgres",
    )


def _find_psql() -> str:
    """Locate the psql binary, searching common PostgreSQL install paths."""
    import shutil
    # Try PATH first
    found = shutil.which("psql")
    if found:
        return found
    # Common Windows install paths
    for candidate in [
        r"C:\Program Files\PostgreSQL\17\bin\psql.exe",
        r"C:\Program Files\PostgreSQL\16\bin\psql.exe",
        r"C:\Program Files\PostgreSQL\15\bin\psql.exe",
    ]:
        if os.path.isfile(candidate):
            return candidate
    # Fallback — let subprocess raise the error
    return "psql"


def psql_args(
    *,
    database: Optional[str] = None,
    user: Optional[str] = None,
) -> list[str]:
    """Build the psql command-line prefix from env or defaults.

    Uses unix socket directory (-h dir) when TEST_DB_UNIX_SOCK is set,
    otherwise uses TCP host/port.

    Returns a list suitable for subprocess.run([...]).
    """
    _unix_sock = os.getenv("TEST_DB_UNIX_SOCK")
    _host = os.getenv("TEST_DB_HOST", "localhost")
    _port = os.getenv("TEST_DB_PORT", "5432")
    _database = database or os.getenv("TEST_DB_NAME", "trad_bot_migration_test")
    _user = user or os.getenv("TEST_DB_USER", "postgres")

    args: list[str] = [_find_psql()]

    if _unix_sock:
        # psql needs the socket DIRECTORY, not the file
        # /var/run/postgresql/.s.PGSQL.5432 -> /var/run/postgresql
        sock_path = Path(_unix_sock)
        args.extend(["-h", str(sock_path.parent)])
    else:
        args.extend(["-h", _host, "-p", _port])

    args.extend(["-U", _user, "-d", _database])
    return args


def run_psql_file(
    sql_path: Path,
    *,
    database: Optional[str] = None,
    user: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a SQL file through psql with env-derived connection params."""
    args = psql_args(database=database, user=user)
    args.extend(["-v", "ON_ERROR_STOP=1", "-f", str(sql_path)])
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ======================================================================
# Temp directory — workspace-local scratch root
# ======================================================================

@pytest.fixture
def tmp_path() -> Path:
    """Provide an isolated temporary directory without pytest's 0700 factory.

    On Windows, pytest's built-in ``tmp_path`` creates directories with mode
    ``0700``.  That ACL is unusable in the constrained runner used by this
    project, while ordinary workspace directories are writable.  Use a
    per-test directory under the ignored test scratch root instead.

    Uses workspace-local .tmp/ to avoid OS temp directory permission issues
    with file locking (fcntl/lockf work reliably on local filesystem).
    """
    _TMP_ROOT.mkdir(exist_ok=True)
    path = _TMP_ROOT / uuid4().hex
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ======================================================================
# Session-scoped DB connection (existing fixture — now env-derived)
# ======================================================================

@pytest.fixture(scope="session")
def db_session():
    """Create a session-scoped test database connection."""
    conn = connect_test_db()
    yield conn
    conn.close()
