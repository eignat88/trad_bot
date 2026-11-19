"""ME_LONG_V2_INVERSE_SHORT_OOS2_V2 — P1-3 deployment readiness tests.

Covers, without touching any real systemd (PostgreSQL only in the
explicit legacy-inventory scenarios):

* the eight-check preflight contract — every check is ``PASS`` / ``FAIL`` /
  ``NOT_VERIFIED``, ``NOT_VERIFIED`` never auto-passes, ``READY_FOR_
  ACTIVATION`` requires ALL eight confirmed in one run, only read-only SQL
  ever reaches the (fake) connection, nothing is ever committed, and no
  credential/secret value can appear in the JSON report or the CLI
  output (exit code 0 only when ready);
* the Issue #287 additions — ``OOS2_GUARD_CONFIG_READY`` (the strict
  frozen guard config: an unknown key or any non-frozen value — in the
  CWD config file or via env override — FAILs) and
  ``DB_OOS2_LEGACY_INVENTORY`` (the legacy OOS-2 v1 ledger is READ-ONLY:
  v2 activation requires the legacy contour to admit nothing,
  ``DRAINING``/``CANCELLED``);
* the P2 Scanner runtime audit — the preflight checks the EXPLICIT
  production Scanner checkout (``--scanner-root``), its systemd unit
  (``ExecStart``/``WorkingDirectory``/``EnvironmentFile``) and fixture
  process metadata; an old Scanner checkout, a unit pointing elsewhere,
  stale sources vs process start, missing unit/process metadata all block
  ``READY_FOR_ACTIVATION`` even when the V2 release is fully ready
  (never touching a real systemd or /proc, never restarting anything);
* the deployment assets — V2 systemd unit, V2 runtime env example,
  Scanner env fragment, and the Issue #287 audit/cutover doc's
  migration/preflight/drain/rollback flow;
* two-process consistency wiring — the preflight's default file locations
  match the unit directives and the Scanner unit's EnvironmentFile;
* runtime path isolation — V2 lock/heartbeat/log paths differ from V1.

No real systemd, /proc or production configuration are required; the
legacy-inventory scenarios use the TEST_DB_* PostgreSQL test database
(read-only except the legacy v1 activation row state, which the test
fixture writes and restores).
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import me_inverse_short_oos2_v2_activate  # noqa: E402
from scripts import me_inverse_short_oos2_v2_preflight as preflight  # noqa: E402

RUNTIME_UNIT = "trad-bot-me-inverse-short-oos2-v2-runtime.service"
V1_UNIT = "trad-bot-me-inverse-short-runtime.service"

RUNTIME_PASSWORD = "S3CRET_RUNTIME_PW_9f3a"
SCANNER_PASSWORD = "S3CRET_SCANNER_PW_77bd"

SOURCE_FILES = (
    "scanner_runner.py",
    "app/scanners/orchestrator.py",
    "app/paper/me_long_v2_inverse_short_oos2_v2/capture.py",
    "app/paper/me_long_v2_inverse_short_oos2_v2/engine.py",
    "app/paper/me_long_v2_inverse_short_oos2_v2/protocol.py",
)

_MUTATING_PREFIXES = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "ALTER",
    "TRUNCATE", "GRANT", "REVOKE", "COPY", "CALL", "DO",
)


# ---------------------------------------------------------------------------
# Fake read-only database (records every statement; hands out a scripted
# sequence of result rows; never commits)
# ---------------------------------------------------------------------------
class _ScriptedCursor:
    def __init__(self, connection: "_ScriptedConnection") -> None:
        self._connection = connection

    def execute(self, sql: str, params=None) -> None:
        self._connection.executed.append((sql, params))
        if sql.strip().upper().startswith("SELECT") and self._connection._results:
            self._connection.pending_results.append(
                self._connection._results.pop(0),
            )

    def fetchone(self):
        if self._connection.pending_results:
            return self._connection.pending_results.pop(0)
        return None

    def close(self) -> None:
        self._connection.cursor_closed = True


class _ScriptedConnection:
    def __init__(self, results: list) -> None:
        self._results = list(results)  # scripted fetchone values, in order
        self.executed: list[tuple] = []
        self.pending_results: list = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0
        self.cursor_closed = False
        self.autocommit = False

    def cursor(self) -> _ScriptedCursor:
        return _ScriptedCursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1

    def close(self) -> None:
        self.closed += 1


def _fake_connect_factory(results: list):
    """``connect(**kwargs) -> connection`` with statement recording."""
    _fake_connect_factory.last_connection = None

    def _factory(**kwargs):
        connection = _ScriptedConnection(results)
        connection.connect_kwargs = kwargs
        _fake_connect_factory.last_connection = connection
        return connection

    return _factory


# The scripted fetchone rows below mirror the exact SELECT order of
# ``_evaluate_db`` (v2): migration-091 to_regclass x5 -> activation row ->
# event/position/close_outbox counts -> legacy to_regclass x2 -> legacy
# activation status -> legacy OPEN-position count.
_SCHEMA_ROW = (
    "dds.me_long_v2_inverse_short_oos2_v2_activation",
    "dds.me_long_v2_inverse_short_oos2_v2_event",
    "dds.me_long_v2_inverse_short_oos2_v2_position",
    "dds.me_long_v2_inverse_short_oos2_v2_close_leg",
    "dds.me_long_v2_inverse_short_oos2_v2_close_outbox",
)
_LEGACY_NAMES_ROW = (
    "dds.me_long_v2_inverse_short_oos2_v1_activation",
    "dds.me_long_v2_inverse_short_oos2_v1_position",
)


def _happy_db_results() -> list:
    return [
        # to_regclass x5 (migration 091 schema present):
        _SCHEMA_ROW,
        # activation row (READY_TO_START, NULL, NULL):
        ("ME_LONG_V2_INVERSE_SHORT_OOS2_V2", "READY_TO_START", None, None),
        # event / position / outbox counts:
        (0, 0, 0),
        # legacy to_regclass x2 (READ-ONLY inventory half):
        _LEGACY_NAMES_ROW,
        # legacy activation status (DRAINING — the contour admits nothing):
        ("DRAINING",),
        # legacy OPEN positions count:
        (0,),
    ]


def _assert_read_only(connection) -> None:
    assert connection.committed == 0, "preflight must never COMMIT"
    assert connection.rolled_back >= 1, "the read-only txn must roll back"
    assert connection.closed == 1
    assert connection.executed, "expected read-only statements"
    for sql, _params in connection.executed:
        first = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        assert first.startswith(_MUTATING_PREFIXES) is False, (
            f"mutating statement reached the connection: {sql.splitlines()[0]!r}",
        )
        assert first in ("SELECT", "BEGIN", "WITH", "SHOW", "SET"), first


# ---------------------------------------------------------------------------
# Env file helpers
# ---------------------------------------------------------------------------
def _runtime_env_text(
    *, flag: str = "0", password: str = RUNTIME_PASSWORD,
    db_name: str = "trad_bot", extra: str = "",
) -> str:
    return (
        f"ME_INVERSE_SHORT_OOS2_V2_ENABLED={flag}\n"
        "DB_HOST=127.0.0.1\n"
        "DB_PORT=5432\n"
        f"DB_NAME={db_name}\n"
        "DB_USER=postgres\n"
        f"DB_PASSWORD={password}\n"
        + extra
    )


def _scanner_env_text(
    *, flag: str = "0", password: str = SCANNER_PASSWORD,
    db_name: str = "trad_bot", include_db: bool = True,
) -> str:
    db = (
        "DB_HOST=127.0.0.1\n"
        "DB_PORT=5432\n"
        f"DB_NAME={db_name}\n"
        "DB_USER=postgres\n"
        f"DB_PASSWORD={password}\n"
    ) if include_db else ""
    return (
        "# scanner fragment (foreign lines must be tolerated)\n"
        "export UNRELATED_SCANNER_SETTING=42\n"
        f"ME_INVERSE_SHORT_OOS2_V2_ENABLED={flag}\n"
        + db
        + "SOME_OTHER_KEY='value with spaces'\n"
    )


# ---------------------------------------------------------------------------
# P2 fixtures: Scanner checkout / systemd unit / process metadata — all
# fabricated locally.  Unit tests NEVER read a real systemd or /proc and
# NEVER restart anything.
# ---------------------------------------------------------------------------
def _write_scanner_unit(
    path: Path, root: Path, env_file: Path, *, python_exe: str | None = None,
) -> Path:
    """Install a fixture `trad-bot-scanner.service` for ``root``.

    ``python_exe`` overrides the ExecStart interpreter (default: the
    checkout's own ``<root>/.venv/bin/python``).
    """
    r = root.as_posix().rstrip("/")
    python = python_exe if python_exe is not None else f"{r}/.venv/bin/python"
    path.write_text(
        "[Unit]\n"
        "Description=Trad Bot Scanner\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={r}\n"
        f"EnvironmentFile={env_file.as_posix()}\n"
        f"ExecStart={python} {r}/scanner_runner.py\n"
        "Restart=on-failure\n"
        "User=tradbot\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    return path


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Create a FILE symlink; skip the test where the host forbids it.

    Windows without Developer Mode / admin lacks
    ``SeCreateSymbolicLinkPrivilege`` (WinError 1314); such hosts skip the
    symlink scenario (it executes on Linux), while DIRECTORY links fall
    back to privilege-free junctions below.
    """
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(
            f"file symlinks unavailable on this host ({exc}); "
            "the symlink-based virtualenv scenario runs on Linux",
        )


def _link_dir_or_skip(link: Path, target: Path) -> None:
    """Directory link: symlink where allowed, junction fallback on Windows."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    import subprocess
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0 or not link.exists():
        pytest.skip(
            "directory links unavailable on this host "
            f"({(result.stderr or result.stdout or '').strip()[:120]}); "
            "this scenario runs on Linux",
        )


def _make_fixture_venv(
    root: Path, *, style: str = "copy", system_target: Path | None = None,
) -> Path:
    """A REAL ``<root>/.venv/{pyvenv.cfg,bin/python}`` layout (never run).

    ``style="copy"``    — interpreter is a real file inside the venv.
    ``style="symlink"`` — interpreter is a symlink to a system interpreter
                          OUTSIDE the checkout (the standard Linux venv
                          shape that broke the pre-fix validation).
    """
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\n"
        "version = 3.12.3\n"
        "include-system-site-packages = false\n",
        encoding="utf-8",
    )
    exe = venv / "bin" / "python"
    if style == "copy":
        exe.write_text(
            "# fixture interpreter — never executed by the tests\n",
            encoding="utf-8",
        )
    elif style == "symlink":
        if system_target is None:
            system_target = root.parent / "system-python" / "python3"
            system_target.parent.mkdir(parents=True, exist_ok=True)
            system_target.write_text(
                "# system interpreter fixture — never executed\n",
                encoding="utf-8",
            )
        system_target.chmod(0o755)
        _symlink_or_skip(exe, system_target)
    else:
        raise ValueError(f"unknown venv style: {style}")
    exe.chmod(0o755)
    return venv


def _make_scanner_checkout(
    base: Path, name: str = "scanner", *, venv_style: str = "copy",
) -> Path:
    """A temporary Scanner checkout with the CURRENT sources + a venv."""
    root = base / name
    for rel in SOURCE_FILES:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((PROJECT_ROOT / rel).read_bytes())
    _make_fixture_venv(root, style=venv_style)
    return root


def _scanner_process_fixture(
    root: Path, *, unit: Path, started_at: float | None = None,
) -> dict:
    """Read-only process metadata exactly as _detect_scanner_process models it.

    ``started_at`` defaults to now+1h so the fixture dominates any checkout
    mtime deterministically; scenarios modelling a stale process pass an
    explicit past timestamp.
    """
    r = root.as_posix().rstrip("/")
    return {
        "available": True, "active": True, "state": "active", "pid": 4242,
        "fragment": unit.as_posix(),
        "cmdline": [f"{r}/.venv/bin/python", f"{r}/scanner_runner.py"],
        "cwd": r,
        "started_at": time.time() + 3600.0 if started_at is None
        else started_at,
    }


@pytest.fixture
def env_files(tmp_path):
    runtime = tmp_path / "v2-runtime.env"
    scanner = tmp_path / "scanner.env"
    # The RUNTIME env must match the RUNTIME side of the DB tuple; the
    # happy path uses the same tuple for both processes.
    runtime.write_text(
        _runtime_env_text(password=RUNTIME_PASSWORD), encoding="utf-8",
    )
    scanner.write_text(
        _scanner_env_text(password=RUNTIME_PASSWORD), encoding="utf-8",
    )
    # The Scanner systemd unit of the default Scanner checkout (P2-1).
    _write_scanner_unit(tmp_path / "scanner-unit.service", PROJECT_ROOT, scanner)
    return runtime, scanner


_PROCESS_AUTO = object()


def _run(runtime: Path, scanner: Path, results: list, *, repo_root=None,
         scanner_root: Path | None = None, scanner_unit_path: Path | None = None,
         scanner_process=_PROCESS_AUTO):
    """run_preflight with fixture Scanner checkout/unit/process metadata.

    ``scanner_root`` defaults to a REAL temporary Scanner checkout built
    next to the env files (sources + a valid fixture virtualenv — the
    interpreter validation needs an actual ``pyvenv.cfg`` layout);
    ``scanner_unit_path`` defaults to the unit installed next to the env
    files, pointing at that checkout; ``scanner_process`` defaults to
    matching metadata — scenarios override each ingredient.
    """
    if scanner_root is None:
        scanner_root = _make_scanner_checkout(
            runtime.parent, "scanner-checkout",
        )
    if scanner_unit_path is None:
        scanner_unit_path = _write_scanner_unit(
            runtime.parent / "scanner-unit.service", scanner_root, scanner,
        )
    if scanner_process is _PROCESS_AUTO:
        scanner_process = _scanner_process_fixture(
            scanner_root, unit=scanner_unit_path,
        )
    connect = _fake_connect_factory(results)
    result = preflight.run_preflight(
        runtime,
        connect=connect,
        scanner_env_path=scanner,
        repo_root=repo_root,
        scanner_root=scanner_root,
        scanner_unit_path=scanner_unit_path,
        scanner_process=scanner_process,
    )
    return result, _fake_connect_factory.last_connection


# ---------------------------------------------------------------------------
# 1. Eight-check preflight contract
# ---------------------------------------------------------------------------
class TestPreflightEightChecks:
    def test_all_eight_pass_and_ready(self, env_files):
        runtime, scanner = env_files
        result, connection = _run(
            runtime, scanner, _happy_db_results(),
            repo_root=PROJECT_ROOT,
        )
        assert set(result["checks"]) == set(preflight.CHECK_NAMES)
        assert len(preflight.CHECK_NAMES) == 8
        for name, check in result["checks"].items():
            assert check["status"] in (
                preflight.STATUS_PASS, preflight.STATUS_FAIL,
                preflight.STATUS_NOT_VERIFIED,
            ), name
            assert check["status"] == preflight.STATUS_PASS, (name, check)
        assert result["ready_for_activation"] is True
        assert result["result"] == "READY_FOR_ACTIVATION"
        assert result["activation_authorized"] is False
        # The new Issue #287 checks carry their own evidence:
        guard = result["checks"]["OOS2_GUARD_CONFIG_READY"]
        assert "guard config equals the frozen protocol values" in guard["detail"]
        legacy = result["checks"]["DB_OOS2_LEGACY_INVENTORY"]
        assert "no new admissions" in legacy["detail"]
        _assert_read_only(connection)

    @pytest.mark.parametrize(
        ("runtime_text", "scanner_text", "db_results", "failing_check",
         "expected_status", "expected_reason"),
        [
            # runtime flag already ON at preflight:
            (_runtime_env_text(flag="1"), None, None,
             "OOS2_RUNTIME_CONFIG_READY", "FAIL", "SERVICE_FLAG_MUST_BE_OFF"),
            # scanner flag already ON at preflight:
            (None, _scanner_env_text(flag="1"), None,
             "OOS2_SOURCE_CAPTURE_READY", "FAIL", "SCANNER_FLAG_NOT_OFF"),
            # two processes pointing at different databases:
            (None, _scanner_env_text(password=SCANNER_PASSWORD), None,
             "OOS2_SOURCE_CAPTURE_READY", "FAIL", "SCANNER_RUNTIME_DB_MISMATCH"),
            # migration 091 schema absent:
            (None, None, "no_schema", "DB_READY", "FAIL", "OOS2_SCHEMA_MISSING"),
            # activation row not READY_TO_START:
            (None, None, "running", "OOS2_ACTIVATION_READY", "FAIL",
             "ACTIVATION_NOT_READY"),
            # fingerprint already written:
            (None, None, "frozen", "OOS2_ACTIVATION_READY", "FAIL",
             "ALREADY_FROZEN"),
            # pre-existing OOS rows:
            (None, None, "has_data", "OOS2_ACTIVATION_READY", "FAIL",
             "EXISTING_OOS_DATA"),
            # activation row absent:
            (None, None, "no_row", "OOS2_ACTIVATION_READY", "FAIL",
             "ACTIVATION_ROW_MISSING"),
            # legacy OOS-2 v1 still admitting:
            (None, None, "legacy_admitting", "DB_OOS2_LEGACY_INVENTORY", "FAIL",
             "LEGACY_STILL_ADMITTING"),
            # legacy OOS-2 v1 activation row absent:
            (None, None, "legacy_missing", "DB_OOS2_LEGACY_INVENTORY", "FAIL",
             "LEGACY_ACTIVATION_MISSING"),
            # legacy OOS-2 v1 tables absent:
            (None, None, "legacy_no_schema", "DB_OOS2_LEGACY_INVENTORY", "FAIL",
             "LEGACY_SCHEMA_MISSING"),
        ],
        ids=[
            "runtime-flag-on", "scanner-flag-on", "db-mismatch",
            "schema-missing", "activation-running", "already-frozen",
            "existing-data", "activation-row-missing",
            "legacy-still-admitting", "legacy-activation-missing",
            "legacy-schema-missing",
        ],
    )
    def test_failures_block_ready(
        self, env_files, monkeypatch, runtime_text, scanner_text, db_results,
        failing_check, expected_status, expected_reason,
    ):
        runtime, scanner = env_files
        if runtime_text is not None:
            runtime.write_text(runtime_text, encoding="utf-8")
        if scanner_text is not None:
            scanner.write_text(scanner_text, encoding="utf-8")
        results = {
            "no_schema": [(None, None, None, None, None)],
            "running": [
                _SCHEMA_ROW,
                ("ME_LONG_V2_INVERSE_SHORT_OOS2_V2", "RUNNING", None, None),
            ],
            "frozen": [
                _SCHEMA_ROW,
                ("ME_LONG_V2_INVERSE_SHORT_OOS2_V2", "READY_TO_START",
                 "0" * 64, None),
            ],
            "has_data": [
                _SCHEMA_ROW,
                ("ME_LONG_V2_INVERSE_SHORT_OOS2_V2", "READY_TO_START", None, None),
                (3, 0, 0),
            ],
            "no_row": [
                _SCHEMA_ROW,
                None,
            ],
            # Happy activation, but the legacy OOS-2 v1 contour is NOT
            # drained (still admitting) -> the cutover precondition fails:
            "legacy_admitting": [
                _SCHEMA_ROW,
                ("ME_LONG_V2_INVERSE_SHORT_OOS2_V2", "READY_TO_START", None, None),
                (0, 0, 0),
                _LEGACY_NAMES_ROW,
                ("RUNNING",),
                (0,),
            ],
            # Legacy activation row absent -> inventory FAILs (never a
            # silent PASS):
            "legacy_missing": [
                _SCHEMA_ROW,
                ("ME_LONG_V2_INVERSE_SHORT_OOS2_V2", "READY_TO_START", None, None),
                (0, 0, 0),
                _LEGACY_NAMES_ROW,
                None,
                (0,),
            ],
            # Legacy OOS-2 v1 tables absent -> inventory FAILs:
            "legacy_no_schema": [
                _SCHEMA_ROW,
                ("ME_LONG_V2_INVERSE_SHORT_OOS2_V2", "READY_TO_START", None, None),
                (0, 0, 0),
                (None, None),
            ],
        }
        payload = _happy_db_results() if db_results is None else results[db_results]
        result, connection = _run(
            runtime, scanner, payload, repo_root=PROJECT_ROOT,
        )
        check = result["checks"][failing_check]
        assert check["status"] == expected_status, check
        assert expected_reason in check["reason"], check
        assert result["ready_for_activation"] is False
        assert result["result"] == "NOT_READY"
        if connection is None:
            # The runtime config was refused before any credential existed:
            # NO connection was ever attempted (stricter than read-only).
            assert failing_check == "OOS2_RUNTIME_CONFIG_READY"
        else:
            _assert_read_only(connection)

    def test_missing_runtime_env_is_not_verified_never_pass(self, tmp_path):
        runtime = tmp_path / "absent.env"
        scanner = tmp_path / "scanner.env"
        scanner.write_text(_scanner_env_text(), encoding="utf-8")
        result, connection = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        assert result["checks"]["OOS2_RUNTIME_CONFIG_READY"]["status"] == \
            preflight.STATUS_NOT_VERIFIED
        # Dependent checks degrade to NOT_VERIFIED, never PASS:
        assert result["checks"]["DB_READY"]["status"] == \
            preflight.STATUS_NOT_VERIFIED
        assert result["checks"]["OOS2_ACTIVATION_READY"]["status"] == \
            preflight.STATUS_NOT_VERIFIED
        assert result["checks"]["DB_OOS2_LEGACY_INVENTORY"]["status"] == \
            preflight.STATUS_NOT_VERIFIED
        assert result["checks"]["V1_ISOLATION_VALID"]["status"] == \
            preflight.STATUS_NOT_VERIFIED
        assert result["ready_for_activation"] is False
        # Without credentials no connection is ever attempted:
        assert connection is None

    def test_missing_scanner_env_is_not_verified_never_pass(self, env_files):
        runtime, _scanner = env_files
        result, _connection = _run(
            runtime, env_files[1].parent / "absent-scanner.env",
            _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        source = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert source["status"] == preflight.STATUS_NOT_VERIFIED
        assert "SCANNER_ENV_UNAVAILABLE" in source["reason"]
        assert result["ready_for_activation"] is False

    def test_scanner_without_flag_is_not_verified(self, env_files):
        runtime, scanner = env_files
        scanner.write_text(
            _scanner_env_text(password=RUNTIME_PASSWORD, include_db=True).replace(
                "ME_INVERSE_SHORT_OOS2_V2_ENABLED=0\n", "",
            ),
            encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        source = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert source["status"] == preflight.STATUS_NOT_VERIFIED
        assert "SCANNER_FLAG_NOT_CONFIGURED" in source["reason"]
        assert result["ready_for_activation"] is False

    def test_scanner_without_db_tuple_is_not_verified(self, env_files):
        runtime, scanner = env_files
        scanner.write_text(
            _scanner_env_text(include_db=False), encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        source = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert source["status"] == preflight.STATUS_NOT_VERIFIED
        assert "SCANNER_DB_KEYS_NOT_COMPARABLE" in source["reason"]
        assert result["ready_for_activation"] is False

    def test_fingerprint_pin_is_enforced(self, env_files, monkeypatch):
        runtime, scanner = env_files
        monkeypatch.setattr(
            preflight, "protocol_fingerprint", lambda: "0" * 64,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        check = result["checks"]["PROTOCOL_FINGERPRINT_VALID"]
        assert check["status"] == preflight.STATUS_FAIL
        assert check["reason"] == "PROTOCOL_FINGERPRINT_MISMATCH"
        assert result["ready_for_activation"] is False

    def test_source_path_audit_failure_blocks_source_capture(self, env_files,
                                                              tmp_path):
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "mutated-scanner")
        runner = root / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        needle = (
            "                    "
            "_capture_me_inverse_short_oos2_v2_source(\n"
            "                        repository, inv_candidate,\n"
            "                    )\n"
        )
        assert needle in source
        runner.write_text(
            source.replace(needle, "                    pass\n"),
            encoding="utf-8",
        )
        # The V2 RELEASE (repo_root) stays intact and ready — only the
        # ACTUAL Scanner checkout loses its V2 delivery (P2-2): the audit
        # runs against --scanner-root, never against the release.
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root,
            scanner_unit_path=_write_scanner_unit(
                tmp_path / "scanner-unit-mutated.service", root, scanner,
            ),
        )
        assert result["checks"]["OOS2_RUNTIME_CONFIG_READY"]["status"] == \
            preflight.STATUS_PASS
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "OOS2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP" in check["reason"]
        assert result["ready_for_activation"] is False

    def test_v1_flag_inside_v2_env_fails_isolation(self, env_files):
        runtime, scanner = env_files
        runtime.write_text(
            _runtime_env_text(extra="ME_INVERSE_SHORT_V1_ENABLED=1\n"),
            encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        check = result["checks"]["V1_ISOLATION_VALID"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "V1_FLAG_IN_V2_RUNTIME_ENV" in check["reason"]
        assert result["ready_for_activation"] is False

    def test_database_failure_fails_db_ready_only(self, env_files):
        runtime, scanner = env_files

        def _broken(**_kwargs):
            raise RuntimeError("connection refused: user=postgres pw=leaky")

        result = preflight.run_preflight(
            runtime, connect=_broken, scanner_env_path=scanner,
            repo_root=PROJECT_ROOT,
        )
        assert result["checks"]["DB_READY"]["status"] == preflight.STATUS_FAIL
        assert (
            result["checks"]["OOS2_ACTIVATION_READY"]["status"]
            == preflight.STATUS_NOT_VERIFIED
        )
        assert (
            result["checks"]["DB_OOS2_LEGACY_INVENTORY"]["status"]
            == preflight.STATUS_NOT_VERIFIED
        )
        # Driver exception text (which may embed credentials) is withheld:
        assert "leaky" not in json.dumps(result)
        assert result["ready_for_activation"] is False


# ---------------------------------------------------------------------------
# 1b. Issue #287 — OOS2_GUARD_CONFIG_READY (the strict frozen guard config)
# ---------------------------------------------------------------------------
class TestGuardConfigCheck:
    """The guard values are fingerprint-covered protocol parameters: the
    strict loader accepts ONLY the frozen values (12 OPEN / single symbol /
    12h cooldown) and rejects unknown keys outright.  ``load_oos2_v2_guard_
    config(path=None)`` reads ``config.me_inverse_short_oos2_v2.json`` from
    the CWD and strict ``ME_INVERSE_SHORT_OOS2_V2_*`` env overrides — any
    deviation is a GUARD_MISMATCH-class failure that blocks activation.
    """

    CONFIG_FILE = "config.me_inverse_short_oos2_v2.json"

    def test_no_config_file_passes_with_frozen_defaults(self, env_files):
        runtime, scanner = env_files
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        check = result["checks"]["OOS2_GUARD_CONFIG_READY"]
        assert check["status"] == preflight.STATUS_PASS, check
        assert (
            "guard config equals the frozen protocol values: "
            "max_open_positions=12, single_active_symbol=True, "
            "symbol_entry_cooldown_hours=12"
        ) == check["detail"], check
        assert result["ready_for_activation"] is True

    def test_config_file_with_frozen_values_passes(self, env_files, tmp_path,
                                                   monkeypatch):
        runtime, scanner = env_files
        monkeypatch.chdir(tmp_path)
        (tmp_path / self.CONFIG_FILE).write_text(
            json.dumps({
                "max_open_positions": 12,
                "single_active_symbol": True,
                "symbol_entry_cooldown_hours": 12,
            }),
            encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        check = result["checks"]["OOS2_GUARD_CONFIG_READY"]
        assert check["status"] == preflight.STATUS_PASS, check
        assert result["ready_for_activation"] is True

    def test_config_file_with_unknown_key_fails(self, env_files, tmp_path,
                                                monkeypatch):
        runtime, scanner = env_files
        monkeypatch.chdir(tmp_path)
        (tmp_path / self.CONFIG_FILE).write_text(
            json.dumps({"max_open_positions": 12, "bogus_key": 1}),
            encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        check = result["checks"]["OOS2_GUARD_CONFIG_READY"]
        assert check["status"] == preflight.STATUS_FAIL, check
        assert check["reason"] == "UNKNOWN_CONFIG_KEYS", check
        assert result["ready_for_activation"] is False
        assert result["result"] == "NOT_READY"

    def test_config_file_with_non_frozen_value_fails(self, env_files, tmp_path,
                                                     monkeypatch):
        runtime, scanner = env_files
        monkeypatch.chdir(tmp_path)
        (tmp_path / self.CONFIG_FILE).write_text(
            json.dumps({"max_open_positions": 5}), encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        check = result["checks"]["OOS2_GUARD_CONFIG_READY"]
        assert check["status"] == preflight.STATUS_FAIL, check
        assert check["reason"] == "GUARD_MISMATCH", check
        assert result["ready_for_activation"] is False

    def test_env_override_deviation_fails(self, env_files, monkeypatch):
        runtime, scanner = env_files
        monkeypatch.setenv("ME_INVERSE_SHORT_OOS2_V2_MAX_OPEN_POSITIONS", "5")
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        check = result["checks"]["OOS2_GUARD_CONFIG_READY"]
        assert check["status"] == preflight.STATUS_FAIL, check
        assert check["reason"] == "GUARD_MISMATCH", check
        assert result["ready_for_activation"] is False

    def test_env_override_equal_to_frozen_value_passes(self, env_files,
                                                       monkeypatch):
        runtime, scanner = env_files
        monkeypatch.setenv("ME_INVERSE_SHORT_OOS2_V2_MAX_OPEN_POSITIONS", "12")
        monkeypatch.setenv("ME_INVERSE_SHORT_OOS2_V2_SYMBOL_ENTRY_COOLDOWN_HOURS",
                           "12")
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        check = result["checks"]["OOS2_GUARD_CONFIG_READY"]
        assert check["status"] == preflight.STATUS_PASS, check
        assert result["ready_for_activation"] is True


# ---------------------------------------------------------------------------
# 4. P2 — the Scanner runtime audit: the preflight verifies the ACTUAL
#    production Scanner checkout + its systemd unit + the running process
#    (fixture metadata only: no real systemd, no /proc, never a restart).
# ---------------------------------------------------------------------------
class TestScannerRuntimeAudit:
    """Codex P2 (discussion_r4540652743): a fully ready V2 release must
    never yield READY_FOR_ACTIVATION while the real Scanner is old."""

    def test_release_ready_but_old_scanner_checkout_fails(
        self, env_files, tmp_path,
    ):
        """V2 release новый, Scanner checkout старый -> FAIL."""
        runtime, scanner = env_files
        old = _make_scanner_checkout(tmp_path, "old-scanner")
        runner = old / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        needle = (
            "                    "
            "_capture_me_inverse_short_oos2_v2_source(\n"
            "                        repository, inv_candidate,\n"
            "                    )\n"
        )
        assert needle in source
        runner.write_text(
            source.replace(needle, "                    pass\n"),
            encoding="utf-8",
        )
        unit = _write_scanner_unit(
            tmp_path / "unit-old.service", old, scanner,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=old, scanner_unit_path=unit,
        )
        # The RELEASE itself is ready (V2_RUNTIME_CODE_READY passes)…
        assert result["checks"]["OOS2_RUNTIME_CONFIG_READY"]["status"] == \
            preflight.STATUS_PASS
        # …but the Scanner-side compound fails on the old checkout:
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "scanner_source_routing:OOS2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
            in check["reason"]
        )
        assert result["ready_for_activation"] is False
        assert result["result"] == "NOT_READY"

    def test_missing_scanner_checkout_is_not_verified(self, env_files,
                                                      tmp_path):
        """V2 release новый, Scanner checkout отсутствует -> NOT_VERIFIED."""
        runtime, scanner = env_files
        absent = tmp_path / "no-such-checkout"
        unit = _write_scanner_unit(
            tmp_path / "unit-absent.service", absent, scanner,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=absent, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_checkout_resolved:SCANNER_CHECKOUT_MISSING" in \
            check["reason"]
        assert result["ready_for_activation"] is False

    def test_unit_pointing_to_another_checkout_fails(self, env_files,
                                                     tmp_path):
        """Scanner source содержит hook, но systemd указывает на другой
        checkout -> FAIL."""
        runtime, scanner = env_files
        good = _make_scanner_checkout(tmp_path, "good-scanner")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        unit = _write_scanner_unit(
            tmp_path / "unit-elsewhere.service", elsewhere, scanner,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=good, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "scanner_unit_matches_checkout:SCANNER_UNIT_CHECKOUT_MISMATCH"
            in check["reason"]
        )
        assert result["ready_for_activation"] is False

    def test_sources_updated_after_process_start_is_not_verified(
        self, env_files, tmp_path,
    ):
        """Scanner source обновлён, работающий процесс старый ->
        NOT_VERIFIED (loaded-code honesty, P2-4)."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "fresh")  # mtimes = now
        unit = _write_scanner_unit(tmp_path / "unit-fresh.service", root,
                                   scanner)
        process = _scanner_process_fixture(
            root, unit=unit, started_at=time.time() - 7200.0,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
            scanner_process=process,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert (
            "scanner_sources_effective:SCANNER_SOURCES_CHANGED_AFTER_START"
            in check["reason"]
        )
        assert result["ready_for_activation"] is False

    def test_missing_scanner_unit_is_not_verified(self, env_files, tmp_path):
        """Scanner systemd unit недоступен -> NOT_VERIFIED."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "unitless")
        missing_unit = tmp_path / "unit-was-never-installed.service"
        process = _scanner_process_fixture(root, unit=missing_unit)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=missing_unit,
            scanner_process=process,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_unit_available:SCANNER_UNIT_UNAVAILABLE" in \
            check["reason"]
        assert result["ready_for_activation"] is False

    def test_uninspectable_process_is_not_verified(self, env_files, tmp_path):
        """No process metadata -> NOT_VERIFIED, never PASS (P2-4: a static
        audit is never runtime evidence)."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "opaque")
        unit = _write_scanner_unit(tmp_path / "unit-opaque.service", root,
                                   scanner)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
            scanner_process={"available": False,
                             "reason": "SYSTEMCTL_UNAVAILABLE"},
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert (
            "scanner_process_matches_unit:SCANNER_PROCESS_NOT_VERIFIABLE"
            in check["reason"]
        )
        assert result["ready_for_activation"] is False

    def test_process_running_another_checkout_fails(self, env_files,
                                                    tmp_path):
        """The unit and checkout are right, but the RUNNING process
        executes another tree -> FAIL."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "declared")
        other = tmp_path / "actually-running"
        other.mkdir()
        unit = _write_scanner_unit(tmp_path / "unit-declared.service", root,
                                   scanner)
        process = _scanner_process_fixture(other, unit=unit)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
            scanner_process=process,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "scanner_process_matches_unit:SCANNER_PROCESS_UNIT_MISMATCH"
            in check["reason"]
        )
        assert result["ready_for_activation"] is False

    def test_verified_scanner_runtime_allows_ready(self, env_files,
                                                   tmp_path):
        """Scanner source и runtime подтверждены -> PASS (the §3 row-8
        flag-off case included: PASS by configuration)."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "verified")
        unit = _write_scanner_unit(tmp_path / "unit-verified.service", root,
                                   scanner)
        process = _scanner_process_fixture(root, unit=unit)
        result, connection = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
            scanner_process=process,
        )
        for name in preflight.CHECK_NAMES:
            assert result["checks"][name]["status"] == preflight.STATUS_PASS, (
                name, result["checks"][name],
            )
        assert result["ready_for_activation"] is True
        assert result["result"] == "READY_FOR_ACTIVATION"
        source_detail = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]["detail"]
        assert "no audited source changed after process start" in source_detail
        assert "Scanner flag configured and OFF at preflight" in source_detail
        assert "unit EnvironmentFile equals" in source_detail
        config_detail = result["checks"]["OOS2_RUNTIME_CONFIG_READY"]["detail"]
        assert "OOS2 release passes the static source-path audit" in config_detail
        _assert_read_only(connection)

    def test_v1_capture_missing_in_scanner_fails(self, env_files, tmp_path):
        """V1 capture отсутствует (доставка) в Scanner checkout -> FAIL."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "v1-missing")
        runner = root / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        v1_needle = (
            "                    "
            "_capture_me_inverse_short_source(repository, inv_candidate)\n"
        )
        assert v1_needle in source
        runner.write_text(
            source.replace(v1_needle, "                    pass\n"),
            encoding="utf-8",
        )
        unit = _write_scanner_unit(tmp_path / "unit-v1.service", root,
                                   scanner)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "scanner_source_routing:V1_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
            in check["reason"]
        )
        assert result["ready_for_activation"] is False

    def test_v2_hook_missing_in_scanner_fails(self, env_files, tmp_path):
        """V2 hook отсутствует в Scanner checkout -> FAIL."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "hook-missing")
        runner = root / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        tree = ast.parse(source)
        hook = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_capture_me_inverse_short_oos2_v2_source"
        )
        lines = source.splitlines(keepends=True)
        del lines[hook.lineno - 1:hook.end_lineno]
        runner.write_text("".join(lines), encoding="utf-8")
        unit = _write_scanner_unit(tmp_path / "unit-nov2.service", root,
                                   scanner)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_source_routing:OOS2_HOOK_NOT_DEFINED" in check["reason"]
        assert result["ready_for_activation"] is False

    def test_gate_dependency_in_scanner_hook_fails(self, env_files,
                                                   tmp_path):
        """P2-2: a Scanner hook consulting trading decisions -> FAIL even
        though the V2 release itself is clean."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "gated")
        runner = root / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        mutated = source.replace(
            "_get_me_inverse_short_oos2_v2_store",
            "_get_me_inverse_short_oos2_v2_store_gate_accepted",
        )
        assert mutated != source, "control needle moved; update it"
        runner.write_text(mutated, encoding="utf-8")
        unit = _write_scanner_unit(tmp_path / "unit-gated.service", root,
                                   scanner)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        assert result["checks"]["OOS2_RUNTIME_CONFIG_READY"]["status"] == \
            preflight.STATUS_PASS
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "scanner_source_routing:OOS2_HOOK_TRADING_GATE_DEPENDENCY"
            in check["reason"]
        )
        assert result["ready_for_activation"] is False

    def test_release_without_wiring_fails_runtime_code_ready(
        self, env_files, tmp_path,
    ):
        """The release half of the split: a V2 release WITHOUT the wiring
        fails OOS2_RUNTIME_CONFIG_READY (runtime_code_ready), while the
        verified Scanner checkout alone can never make it ready."""
        runtime, scanner = env_files
        release = _make_scanner_checkout(tmp_path, "broken-release")
        runner = release / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        needle = (
            "                    "
            "_capture_me_inverse_short_oos2_v2_source(\n"
            "                        repository, inv_candidate,\n"
            "                    )\n"
        )
        assert needle in source
        runner.write_text(
            source.replace(needle, "                    pass\n"),
            encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=release,
        )
        check = result["checks"]["OOS2_RUNTIME_CONFIG_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "runtime_code_ready:OOS2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
            in check["reason"]
        )
        assert result["checks"]["OOS2_SOURCE_CAPTURE_READY"]["status"] == \
            preflight.STATUS_PASS
        assert result["ready_for_activation"] is False


class TestScannerVirtualenvValidation:
    """Codex P2 (discussion_r4541445199): a standard symlink-based Linux
    virtualenv must NOT read as ``SCANNER_UNIT_CHECKOUT_MISMATCH`` — the
    venv LOCATION, not the system interpreter behind the symlink, is what
    belongs to the Scanner checkout; foreign / missing / undeterminable
    environments must never PASS.  All fixtures are REAL files under
    ``tmp_path`` (no Path.resolve() mocks, no interpreter ever executed).
    """

    def test_symlink_based_venv_passes(self, env_files, tmp_path):
        """The Codex reproduction: `<root>/.venv/bin/python` → system python
        outside the checkout (standard Linux venv) reads PASS after the fix
        (it read FAIL/SCANNER_UNIT_CHECKOUT_MISMATCH before it).
        """
        runtime, scanner = env_files
        root = _make_scanner_checkout(
            tmp_path, "symlink-venv-checkout", venv_style="symlink",
        )
        # The system interpreter really lives outside the checkout and the
        # link really is a symlink on this filesystem:
        assert root.joinpath(".venv/bin/python").is_symlink()
        resolved = root.joinpath(".venv/bin/python").resolve()
        assert not str(resolved).startswith(str(root.resolve()))
        unit = _write_scanner_unit(
            tmp_path / "symlink-unit.service", root, scanner,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_PASS
        assert "pyvenv.cfg present" in check["detail"]
        assert result["checks"]["OOS2_RUNTIME_CONFIG_READY"]["status"] == \
            preflight.STATUS_PASS
        assert result["ready_for_activation"] is True
        assert result["result"] == "READY_FOR_ACTIVATION"

    def test_copy_based_venv_passes(self, env_files, tmp_path):
        """A copy-based virtualenv (interpreter file inside the venv) PASSes."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "copy-venv-checkout")
        exe = root / ".venv/bin/python"
        assert exe.is_file() and not exe.is_symlink()
        unit = _write_scanner_unit(
            tmp_path / "copy-unit.service", root, scanner,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_PASS
        assert "virtualenv" in check["detail"]
        assert result["ready_for_activation"] is True

    def test_python_from_another_checkout_fails(self, env_files, tmp_path):
        """ExecStart interpreter from a DIFFERENT checkout → FAIL."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "scanner-checkout")
        other = _make_scanner_checkout(tmp_path, "other-checkout")
        unit = _write_scanner_unit(
            tmp_path / "other-python-unit.service", root, scanner,
            python_exe=(other / ".venv/bin/python").as_posix(),
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_PYTHON_OUTSIDE_CHECKOUT" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_prefix_sibling_checkout_fails(self, env_files, tmp_path):
        """`.../trad_bot_other/...` vs root `.../trad_bot` is NOT a member
        (prefix trap) → FAIL."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "trad_bot")
        sibling = _make_scanner_checkout(tmp_path, "trad_bot_other")
        unit = _write_scanner_unit(
            tmp_path / "sibling-unit.service", root, scanner,
            python_exe=(sibling / ".venv/bin/python").as_posix(),
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_PYTHON_OUTSIDE_CHECKOUT" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_venv_dir_link_to_foreign_checkout_fails(self, env_files,
                                                     tmp_path):
        """`.venv` substituted by a directory link to a FOREIGN virtualenv
        (lexical path looks local) → FAIL, never PASS."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "linked-venv-checkout")
        foreign_root = _make_scanner_checkout(tmp_path, "foreign-root")
        foreign_venv = foreign_root / ".venv"
        shutil.rmtree(root / ".venv")
        _link_dir_or_skip(root / ".venv", foreign_venv)
        unit = _write_scanner_unit(
            tmp_path / "linked-venv-unit.service", root, scanner,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_VENV_SYMLINK_ESCAPE" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_missing_pyvenv_cfg_is_not_verified(self, env_files, tmp_path):
        """Without pyvenv.cfg the virtualenv cannot be established →
        NOT_VERIFIED (never a silent PASS)."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "no-cfg-checkout")
        (root / ".venv/pyvenv.cfg").unlink()
        unit = _write_scanner_unit(
            tmp_path / "no-cfg-unit.service", root, scanner,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_unit_matches_checkout:SCANNER_PYVENV_CFG_MISSING" \
            in check["reason"]
        assert result["ready_for_activation"] is False
        assert result["result"] == "NOT_READY"

    def test_missing_python_executable_is_not_verified(self, env_files,
                                                       tmp_path):
        """Missing interpreter file → NOT_VERIFIED, never PASS."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "no-python-checkout")
        (root / ".venv/bin/python").unlink()
        unit = _write_scanner_unit(
            tmp_path / "no-python-unit.service", root, scanner,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_unit_matches_checkout:SCANNER_PYTHON_MISSING" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_relative_python_in_execstart_fails(self, env_files, tmp_path):
        """ExecStart with a RELATIVE interpreter path → FAIL."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "relative-python-checkout")
        unit = _write_scanner_unit(
            tmp_path / "relative-python-unit.service", root, scanner,
            python_exe=".venv/bin/python",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_PYTHON_NOT_ABSOLUTE" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_path_traversal_python_fails(self, env_files, tmp_path):
        """`ExecStart` with `..` escaping the checkout → FAIL (lexical
        normalization, no symlink following)."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "traversal-checkout")
        r = root.as_posix().rstrip("/")
        unit = _write_scanner_unit(
            tmp_path / "traversal-unit.service", root, scanner,
            python_exe=f"{r}/../escaped/.venv/bin/python",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_PYTHON_OUTSIDE_CHECKOUT" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_old_scanner_with_valid_venv_fails_by_source_routing(
            self, env_files, tmp_path):
        """Valid virtualenv + OLD scanner sources → the failure comes from
        source routing alone (no python-env reason pollutes the report)."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "old-sources-checkout")
        runner = root / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        needle = (
            "                    "
            "_capture_me_inverse_short_oos2_v2_source(\n"
            "                        repository, inv_candidate,\n"
            "                    )\n"
        )
        assert needle in source
        runner.write_text(
            source.replace(needle, "                    pass\n"),
            encoding="utf-8",
        )
        unit = _write_scanner_unit(
            tmp_path / "old-sources-unit.service", root, scanner,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "scanner_source_routing:OOS2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
            in check["reason"]
        )
        assert "SCANNER_PYTHON" not in check["reason"]
        assert "SCANNER_VENV" not in check["reason"]
        assert "SCANNER_PYVENV" not in check["reason"]
        assert result["ready_for_activation"] is False

    def test_process_mismatch_with_valid_venv_fails(self, env_files,
                                                    tmp_path):
        """Valid virtualenv + process running another tree → FAIL on the
        process↔unit comparison (the venv does not mask it)."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "process-mismatch-checkout")
        unit = _write_scanner_unit(
            tmp_path / "process-mismatch-unit.service", root, scanner,
        )
        other = _make_scanner_checkout(tmp_path, "running-other")
        process = _scanner_process_fixture(other, unit=unit)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
            scanner_process=process,
        )
        check = result["checks"]["OOS2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_process_matches_unit:SCANNER_PROCESS_UNIT_MISMATCH" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_fully_correct_checkout_unit_process_venv_passes(
            self, env_files, tmp_path):
        """Everything correct (checkout + unit + process + copy-based venv)
        → all eight PASS, READY_FOR_ACTIVATION."""
        runtime, scanner = env_files
        root = _make_scanner_checkout(tmp_path, "all-good-checkout")
        unit = _write_scanner_unit(
            tmp_path / "all-good-unit.service", root, scanner,
        )
        process = _scanner_process_fixture(root, unit=unit)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
            scanner_process=process,
        )
        for name in preflight.CHECK_NAMES:
            assert result["checks"][name]["status"] == \
                preflight.STATUS_PASS, (name, result["checks"][name])
        assert result["ready_for_activation"] is True
        assert result["result"] == "READY_FOR_ACTIVATION"
        assert "virtualenv" in result["checks"]["OOS2_SOURCE_CAPTURE_READY"][
            "detail"]


class TestPreflightCli:
    def test_main_returns_zero_only_when_ready(self, env_files, monkeypatch,
                                               capsys):
        runtime, scanner = env_files
        monkeypatch.setattr(
            "pg8000.dbapi.connect", _fake_connect_factory(_happy_db_results()),
        )
        # A REAL Scanner checkout + unit next to the env files (the venv
        # validation needs an actual pyvenv.cfg layout).
        checkout = _make_scanner_checkout(runtime.parent, "scanner-checkout")
        unit = _write_scanner_unit(
            runtime.parent / "scanner-unit.service", checkout, scanner,
        )
        # Effective-runtime metadata comes from fixtures — never from the
        # real systemd//proc of this test host (P2-4).
        monkeypatch.setattr(
            preflight, "_detect_scanner_process",
            lambda _unit_path=None: _scanner_process_fixture(
                checkout, unit=unit,
            ),
        )
        code = preflight.main([
            "--env-file", str(runtime),
            "--scanner-env-file", str(scanner),
            "--scanner-root", str(checkout),
            "--scanner-unit", str(unit),
        ])
        out = capsys.readouterr().out
        report = json.loads(out)
        assert code == 0
        assert report["result"] == "READY_FOR_ACTIVATION"
        # No secret value may ever reach the report:
        assert RUNTIME_PASSWORD not in out
        assert SCANNER_PASSWORD not in out

    def test_main_returns_one_and_hides_secrets_on_failure(
        self, env_files, monkeypatch, capsys,
    ):
        runtime, scanner = env_files
        runtime.write_text(
            _runtime_env_text(flag="1"), encoding="utf-8",
        )
        monkeypatch.setattr(
            "pg8000.dbapi.connect", _fake_connect_factory(_happy_db_results()),
        )
        code = preflight.main([
            "--env-file", str(runtime),
            "--scanner-env-file", str(scanner),
        ])
        out = capsys.readouterr().out
        report = json.loads(out)
        assert code == 1
        assert report["result"] == "NOT_READY"
        assert report["checks"]["OOS2_RUNTIME_CONFIG_READY"]["status"] == "FAIL"
        assert RUNTIME_PASSWORD not in out
        assert SCANNER_PASSWORD not in out

    def test_main_never_raises_and_never_leaks_on_db_error(
        self, env_files, monkeypatch, capsys,
    ):
        runtime, scanner = env_files

        def _broken(**_kwargs):
            raise RuntimeError(f"auth failed password={SCANNER_PASSWORD}")

        monkeypatch.setattr("pg8000.dbapi.connect", _broken)
        code = preflight.main([
            "--env-file", str(runtime),
            "--scanner-env-file", str(scanner),
        ])
        out = capsys.readouterr().out
        assert code == 1
        assert SCANNER_PASSWORD not in out
        assert RUNTIME_PASSWORD not in out

    def test_cli_exposes_the_scanner_checkout_arguments(self, capsys):
        """P2-1: the production Scanner checkout/unit are explicit CLI
        parameters with the production defaults."""
        with pytest.raises(SystemExit) as excinfo:
            preflight.main(["--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--scanner-root" in out
        assert "--scanner-unit" in out
        assert preflight.DEFAULT_SCANNER_ROOT.as_posix() in out
        assert preflight.DEFAULT_SCANNER_UNIT.as_posix() in out


class TestPreflightCompatibility:
    """The exports the activation CLI relies on keep their contract."""

    def test_activate_script_imports_the_shared_exports(self):
        assert preflight.read_service_env is not None
        assert preflight.PreflightRefused is not None
        assert (
            me_inverse_short_oos2_v2_activate.read_service_env
            is preflight.read_service_env
        )
        assert (
            me_inverse_short_oos2_v2_activate.PreflightRefused
            is preflight.PreflightRefused
        )

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            ("export DB_HOST=1.2.3.4\n", "SERVICE_ENV_UNSUPPORTED_SYNTAX"),
            ("ME_INVERSE_SHORT_OOS2_V2_ENABLED=1\n", "SERVICE_ENV_INCOMPLETE"),
        ],
        ids=["export-syntax", "incomplete"],
    )
    def test_read_service_env_still_refuses(self, tmp_path, content, reason):
        env = tmp_path / "bad.env"
        env.write_text(content, encoding="utf-8")
        with pytest.raises(preflight.PreflightRefused) as excinfo:
            preflight.read_service_env(env)
        assert str(excinfo.value) == reason

    def test_read_service_env_requires_flag_off(self, tmp_path):
        env = tmp_path / "on.env"
        # Complete key set, flag ON -> explicit refusal, not silence:
        env.write_text(_runtime_env_text(flag="1"), encoding="utf-8")
        with pytest.raises(preflight.PreflightRefused) as excinfo:
            preflight.read_service_env(env)
        assert str(excinfo.value) == "SERVICE_FLAG_MUST_BE_OFF"

    def test_read_service_env_never_returns_password_caller_must_treat_as_secret(
        self, tmp_path,
    ):
        env = tmp_path / "ok.env"
        env.write_text(_runtime_env_text(), encoding="utf-8")
        values = preflight.read_service_env(env)
        assert values["ME_INVERSE_SHORT_OOS2_V2_ENABLED"] == "0"
        assert values["DB_NAME"] == "trad_bot"
        # The parser is the only place values are held; the report builders
        # never embed them (asserted in the CLI tests above).
        assert set(values) == set(preflight.ENV_KEYS)


# ---------------------------------------------------------------------------
# 2. Deployment assets
# ---------------------------------------------------------------------------
class TestDeployAssets:
    def test_v2_unit_exists_with_isolated_paths(self):
        v2 = PROJECT_ROOT / "deploy" / "systemd" / RUNTIME_UNIT
        v1 = PROJECT_ROOT / "deploy" / "systemd" / V1_UNIT
        assert v2.is_file()
        assert v1.is_file()
        v2_text = v2.read_text(encoding="utf-8")
        v1_text = v1.read_text(encoding="utf-8")

        assert (
            "EnvironmentFile=/etc/trad-bot/me-inverse-short-oos2-v2-runtime.env"
            in v2_text
        )
        assert "app.paper.me_long_v2_inverse_short_oos2_v2.production_runtime" in v2_text
        # Release-tree isolation: the v2 unit runs from its OWN release root
        # (parity with the V3 unit's dedicated tree), so a v2 deploy can
        # never touch the tree the legacy OOS-2 v1 unit runs from while it
        # drains.  ("/opt/trad_bot_me_inverse_short_oos2_release" is NOT a
        # substring of "..._oos2_v2_release", so the exclusion is exact.)
        assert "/opt/trad_bot_me_inverse_short_oos2_v2_release" in v2_text
        assert "/opt/trad_bot_me_inverse_short_oos2_release" not in v2_text
        assert "RestartPreventExitStatus=3" in v2_text
        assert "ME_INVERSE_SHORT_OOS2_V2_ENABLED=0" in v2_text  # documented OFF
        # No V1 references anywhere in the V2 unit:
        assert "me_long_v2_inverse_short_v1" not in v2_text
        assert "me-inverse-short-runtime.env" not in v2_text
        assert "ME_INVERSE_SHORT_V1_ENABLED" not in v2_text
        # V1 unit itself untouched:
        assert "me_long_v2_inverse_short_v1" in v1_text
        assert (
            "EnvironmentFile=/etc/trad-bot/me-inverse-short-runtime.env"
            in v1_text
        )

    def test_v2_runtime_env_example(self):
        example = (
            PROJECT_ROOT / "deploy" / "env" / "me-inverse-short-oos2-v2-runtime.env.example"
        )
        assert example.is_file()
        text = example.read_text(encoding="utf-8")
        assert "ME_INVERSE_SHORT_OOS2_V2_ENABLED=0" in text
        assert "DB_NAME=trad_bot" in text
        assert "DB_PASSWORD=CHANGE_ME" in text  # placeholder, never a secret
        for key in (
            "ME_INVERSE_SHORT_MD_PRICE_MAX_AGE_S",
            "ME_INVERSE_SHORT_MD_BAR_MAX_AGE_S",
            "ME_INVERSE_SHORT_MD_FUNDING_MAX_AGE_S",
            "ME_INVERSE_SHORT_MD_BAR_INTERVAL",
        ):
            assert key in text, key
        # No V1 key may be suggested in the V2 env surface:
        assert "ME_INVERSE_SHORT_V1_ENABLED" not in text
        # Parses under the strict preflight reader:
        values = preflight.read_service_env(example)
        assert values["ME_INVERSE_SHORT_OOS2_V2_ENABLED"] == "0"

    def test_scanner_env_example(self):
        """The Scanner-side fragment example is the SHARED scanner env
        surface (not a per-contour v2 asset); assert what it really
        documents: the Scanner's own env file, the full DB_* mirror and
        the OOS-2-family capture flag kept OFF."""
        example = (
            PROJECT_ROOT / "deploy" / "env" / "me-inverse-short-scanner.env.example"
        )
        assert example.is_file()
        text = example.read_text(encoding="utf-8")
        assert "ME_INVERSE_SHORT_OOS2_ENABLED=0" in text  # OOS-2-family flag
        assert "/opt/trad_bot/.env" in text  # the Scanner's own env file
        for key in preflight.DB_KEYS:
            assert f"{key}=" in text, key
        assert "ME_INVERSE_SHORT_V1_ENABLED" not in text

    def test_cutover_doc_has_the_migration_preflight_drain_rollback_flow(self):
        """The Issue #287 audit/design/cutover record — v2 doc expectations
        point at ``docs/me_inverse_short_oos2_entry_guards_v1.md`` (the
        ``me_inverse_short_oos2_production_runtime_v1.md`` runbook is the
        V1 file)."""
        doc = (
            PROJECT_ROOT / "docs" / "me_inverse_short_oos2_entry_guards_v1.md"
        )
        assert doc.is_file()
        text = doc.read_text(encoding="utf-8")
        # The migration map names the frozen legacy migrations and the NEW
        # v2 migration:
        assert "091_me_long_v2_inverse_short_oos2_v2.sql" in text
        assert "088_me_long_v2_inverse_short_oos2_v1.sql" in text
        # The approval-gated cutover flow: apply migration -> deploy inert
        # -> run the v2 preflight (read-only) -> drain legacy -> activate:
        assert "each step separately approved" in text
        assert "ME_INVERSE_SHORT_OOS2_V2_ENABLED=0" in text  # inert deploy
        assert "scripts/me_inverse_short_oos2_v2_preflight.py" in text
        assert "scripts/me_inverse_short_oos2_v2_activate.py" in text
        assert "ME_LONG_V2_INVERSE_SHORT_OOS2_V2" in text
        # The legacy cutover precondition (v2 admits only while the legacy
        # contour admits nothing) and the legacy drain via ITS OWN tooling:
        assert "`DRAINING` / `CANCELLED`" in text
        assert "scripts/me_inverse_short_oos2_activate.py --drain" in text
        assert "--drain" in text
        # Rollback is exit-only: stop admitting, keep protecting positions:
        assert "Rollback (exit-only)" in text
        assert "freeze_ts" in text
        assert "NOT VERIFIED" in text  # unavailable facts are recorded as such
        # The three fingerprint-covered guard parameters are documented:
        assert "max_open_positions = 12" in text
        assert "single_active_symbol = true" in text
        assert "symbol_entry_cooldown_hours = 12" in text

    def test_preflight_defaults_match_the_deploy_assets(self):
        v2_unit = (
            PROJECT_ROOT / "deploy" / "systemd" / RUNTIME_UNIT
        ).read_text(encoding="utf-8")
        scanner_unit = (
            PROJECT_ROOT / "deploy" / "systemd" / "trad-bot-scanner.service"
        ).read_text(encoding="utf-8")
        # POSIX spellings: unit files are always POSIX text.
        assert preflight.DEFAULT_RUNTIME_ENV.as_posix() in v2_unit
        assert preflight.DEFAULT_RUNTIME_ENV.as_posix() == \
            "/etc/trad-bot/me-inverse-short-oos2-v2-runtime.env"
        # The scanner default matches the Scanner unit's EnvironmentFile:
        assert preflight.DEFAULT_SCANNER_ENV.as_posix() in scanner_unit
        assert preflight.DEFAULT_SCANNER_ENV.as_posix() == "/opt/trad_bot/.env"
        # P2-1: CLI scanner checkout/unit defaults agree with the systemd
        # unit's WorkingDirectory / ExecStart / script path:
        assert preflight.DEFAULT_SCANNER_ROOT == Path("/opt/trad_bot")
        assert preflight.DEFAULT_SCANNER_ROOT.as_posix() in scanner_unit
        assert preflight.DEFAULT_SCANNER_UNIT.name == "trad-bot-scanner.service"
        exec_start = next(
            line for line in scanner_unit.splitlines()
            if line.startswith("ExecStart=")
        )
        script_token = exec_start.split()[-1]
        assert Path(script_token).name == "scanner_runner.py"
        assert Path(script_token).parent == preflight.DEFAULT_SCANNER_ROOT
        workdir = next(
            line for line in scanner_unit.splitlines()
            if line.startswith("WorkingDirectory=")
        ).split("=", 1)[1]
        assert Path(workdir) == preflight.DEFAULT_SCANNER_ROOT
        # The unit file exists under the name the CLI default points at:
        assert (
            PROJECT_ROOT / "deploy" / "systemd"
            / preflight.DEFAULT_SCANNER_UNIT.name
        ).is_file()

    def test_v2_module_entrypoint_exists_for_the_unit(self):
        module = (
            PROJECT_ROOT / "app" / "paper" / "me_long_v2_inverse_short_oos2_v2"
            / "production_runtime.py"
        )
        assert module.is_file()
        source = module.read_text(encoding="utf-8")
        assert "EXIT_ALREADY_RUNNING = 3" in source
        assert "DEFAULT_LOCK_FILE" in source
        assert "me_inverse_short_oos2_v2_runtime" in source


# ---------------------------------------------------------------------------
# 3. Runtime path isolation (lock / heartbeat / log)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def runtime_modules():
    v2 = importlib.import_module(
        "app.paper.me_long_v2_inverse_short_oos2_v2.production_runtime",
    )
    v1 = importlib.import_module(
        "app.paper.me_long_v2_inverse_short_v1.production_runtime",
    )
    return v2, v1


class TestRuntimePathIsolation:
    def test_v2_paths_are_v2_specific(self, runtime_modules):
        v2, _v1 = runtime_modules
        assert v2.DEFAULT_LOCK_FILE.name == "me_inverse_short_oos2_v2_runtime.lock"
        assert v2.DEFAULT_HEARTBEAT_FILE.name == \
            "me_inverse_short_oos2_v2_runtime_heartbeat.json"
        assert v2.DEFAULT_LOG_FILE.name == "me_inverse_short_oos2_v2_runtime.log"

    def test_v2_paths_never_collide_with_v1(self, runtime_modules):
        v2, v1 = runtime_modules
        assert v2.DEFAULT_LOCK_FILE != v1.DEFAULT_LOCK_FILE
        assert v2.DEFAULT_HEARTBEAT_FILE != v1.DEFAULT_HEARTBEAT_FILE
        assert v2.DEFAULT_LOG_FILE != v1.DEFAULT_LOG_FILE
        # And the baselines themselves are distinct strings:
        assert v2.DEFAULT_LOCK_FILE.name != v1.DEFAULT_LOCK_FILE.name
        assert v2.DEFAULT_HEARTBEAT_FILE.name != v1.DEFAULT_HEARTBEAT_FILE.name
        assert v2.DEFAULT_LOG_FILE.name != v1.DEFAULT_LOG_FILE.name


# ---------------------------------------------------------------------------
# 5. DB_OOS2_LEGACY_INVENTORY against the real TEST database (TEST_DB_*)
# ---------------------------------------------------------------------------
LEGACY_ACTIVATION_TABLE = "dds.me_long_v2_inverse_short_oos2_v1_activation"
LEGACY_EXPERIMENT_ID = "ME_LONG_V2_INVERSE_SHORT_OOS2_V1"


def _test_db_settings() -> dict | None:
    host = os.environ.get("TEST_DB_HOST")
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("TEST_DB_PORT", "5432")),
        "database": os.environ.get("TEST_DB_NAME", "trad_bot_oos2_it_287"),
        "user": os.environ.get("TEST_DB_USER", "postgres"),
        "password": os.environ.get("TEST_DB_PASSWORD", ""),
    }


class TestLegacyInventoryPg:
    """``_evaluate_db`` against the REAL migration-091 schema and the REAL
    legacy OOS-2 v1 ledger (TEST_DB_* -> trad_bot_oos2_it_287).

    The fixture may write ONLY the legacy OOS-2 v1 activation row (state
    scenarios, restored verbatim on teardown) — that is TEST-side state
    control; the v2 product code stays strictly read-only there.  The
    connect adapter redirects the runtime-env tuple at the TEST database.
    """

    @pytest.fixture
    def pg(self):
        settings = _test_db_settings()
        if settings is None:
            pytest.skip("TEST_DB_* not configured (PostgreSQL scenario)")
        import pg8000.dbapi

        def direct():
            return pg8000.dbapi.connect(timeout=10, **settings)

        def connect(**_kwargs):
            # _evaluate_db passes the runtime-env tuple; these scenarios
            # run the SAME read-only SQL against the TEST database.
            return direct()

        conn = direct()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, protocol_fingerprint, freeze_ts "
            f"FROM {LEGACY_ACTIVATION_TABLE} WHERE experiment_id = %s",
            (LEGACY_EXPERIMENT_ID,),
        )
        saved = cursor.fetchone()
        conn.commit()
        conn.close()

        def set_legacy_state(status: str | None) -> None:
            """Install a legacy v1 activation state (None = row absent)."""
            conn = direct()
            cursor = conn.cursor()
            cursor.execute(
                f"DELETE FROM {LEGACY_ACTIVATION_TABLE} WHERE experiment_id = %s",
                (LEGACY_EXPERIMENT_ID,),
            )
            if status is not None:
                if status == "READY_TO_START":
                    cursor.execute(
                        f"INSERT INTO {LEGACY_ACTIVATION_TABLE} "
                        "(experiment_id, status) VALUES (%s, %s)",
                        (LEGACY_EXPERIMENT_ID, status),
                    )
                else:
                    # The frozen CHECK: a non-READY_TO_START status carries
                    # BOTH the fingerprint and the freeze.
                    cursor.execute(
                        f"INSERT INTO {LEGACY_ACTIVATION_TABLE} "
                        "(experiment_id, status, protocol_fingerprint, "
                        "freeze_ts) VALUES (%s, %s, %s, %s)",
                        (
                            LEGACY_EXPERIMENT_ID, status, "f" * 64,
                            datetime(2030, 1, 1, tzinfo=timezone.utc),
                        ),
                    )
            conn.commit()
            conn.close()

        yield SimpleNamespace(connect=connect, set_legacy_state=set_legacy_state)

        # Restore the legacy row exactly as the fixture found it.
        conn = direct()
        cursor = conn.cursor()
        cursor.execute(
            f"DELETE FROM {LEGACY_ACTIVATION_TABLE} WHERE experiment_id = %s",
            (LEGACY_EXPERIMENT_ID,),
        )
        if saved is not None:
            cursor.execute(
                f"INSERT INTO {LEGACY_ACTIVATION_TABLE} "
                "(experiment_id, status, protocol_fingerprint, freeze_ts) "
                "VALUES (%s, %s, %s, %s)",
                (LEGACY_EXPERIMENT_ID, *saved),
            )
        conn.commit()
        conn.close()

    @staticmethod
    def _values() -> dict:
        settings = _test_db_settings()
        return {
            "DB_HOST": settings["host"],
            "DB_PORT": str(settings["port"]),
            "DB_NAME": settings["database"],
            "DB_USER": settings["user"],
            "DB_PASSWORD": settings["password"],
        }

    def test_drained_legacy_contour_passes_the_inventory(self, pg):
        pg.set_legacy_state("DRAINING")
        db_ready, _activation, legacy = preflight._evaluate_db(
            self._values(), pg.connect,
        )
        assert db_ready["status"] == preflight.STATUS_PASS, db_ready
        assert legacy["status"] == preflight.STATUS_PASS, legacy
        assert "DRAINING" in legacy["detail"]
        assert "no new admissions" in legacy["detail"]

    def test_cancelled_legacy_contour_passes_the_inventory(self, pg):
        pg.set_legacy_state("CANCELLED")
        db_ready, _activation, legacy = preflight._evaluate_db(
            self._values(), pg.connect,
        )
        assert db_ready["status"] == preflight.STATUS_PASS, db_ready
        assert legacy["status"] == preflight.STATUS_PASS, legacy
        assert "CANCELLED" in legacy["detail"]

    def test_admitting_legacy_contour_blocks_cutover(self, pg):
        pg.set_legacy_state("RUNNING")
        db_ready, _activation, legacy = preflight._evaluate_db(
            self._values(), pg.connect,
        )
        assert db_ready["status"] == preflight.STATUS_PASS, db_ready
        assert legacy["status"] == preflight.STATUS_FAIL, legacy
        assert legacy["reason"] == "LEGACY_STILL_ADMITTING", legacy

    def test_missing_legacy_activation_row_fails_the_inventory(self, pg):
        pg.set_legacy_state(None)
        db_ready, _activation, legacy = preflight._evaluate_db(
            self._values(), pg.connect,
        )
        assert db_ready["status"] == preflight.STATUS_PASS, db_ready
        assert legacy["status"] == preflight.STATUS_FAIL, legacy
        assert legacy["reason"] == "LEGACY_ACTIVATION_MISSING", legacy

    def test_inventory_never_writes(self, pg):
        pg.set_legacy_state("DRAINING")
        preflight._evaluate_db(self._values(), pg.connect)
        conn = pg.connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, protocol_fingerprint, freeze_ts "
            f"FROM {LEGACY_ACTIVATION_TABLE} WHERE experiment_id = %s",
            (LEGACY_EXPERIMENT_ID,),
        )
        row = cursor.fetchone()
        conn.rollback()
        conn.close()
        # The read-only inventory left the legacy ledger untouched:
        assert row == ["DRAINING", "f" * 64, datetime(2030, 1, 1, tzinfo=timezone.utc)], row
