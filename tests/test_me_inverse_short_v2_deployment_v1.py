"""ME_LONG_V2_INVERSE_SHORT_V2 — P1-3 deployment readiness tests.

Covers, without touching any real database or systemd:

* the six-check preflight contract — every check is ``PASS`` / ``FAIL`` /
  ``NOT_VERIFIED``, ``NOT_VERIFIED`` never auto-passes, ``READY_FOR_
  ACTIVATION`` requires ALL six confirmed in one run, only read-only SQL
  ever reaches the (fake) connection, nothing is ever committed, and no
  credential/secret value can appear in the JSON report or the CLI
  output (exit code 0 only when ready);
* the P1 effective-systemd audit — the preflight resolves the EFFECTIVE
  unit configuration (base fragment + ordered drop-in overrides with
  ``ExecStart=`` resets, as loaded by systemd) from mocked
  ``systemctl show`` output and compares ExecStart/WorkingDirectory/
  EnvironmentFiles/MainPID/FragmentPath and the running process against
  it: a correct drop-in over an old base unit passes, a stale process, a
  workdir/python/fragment mismatch or a V2 flag left on fails, and
  missing/unconfirmable/pending systemd metadata is always
  ``NOT_VERIFIED`` (never a false PASS, never a restart);
* the P2 Scanner runtime audit — the preflight checks the EXPLICIT
  production Scanner checkout (``--scanner-root``), its systemd unit
  (``ExecStart``/``WorkingDirectory``/``EnvironmentFile``) and fixture
  process metadata; an old Scanner checkout, a unit pointing elsewhere,
  stale sources vs process start, missing unit/process metadata all block
  ``READY_FOR_ACTIVATION`` even when the V2 release is fully ready
  (never touching a real systemd or /proc, never restarting anything);
* the deployment assets — V2 systemd unit, V2 runtime env example,
  Scanner env fragment, and the runbook's install/verify/rollback steps;
* two-process consistency wiring — the preflight's default file locations
  match the unit directives and the Scanner unit's EnvironmentFile;
* runtime path isolation — V2 lock/heartbeat/log paths differ from V1.

No PostgreSQL, no systemd and no production configuration are required.
"""
from __future__ import annotations

import ast
import importlib
import json
import shlex
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import me_inverse_short_v2_activate  # noqa: E402
from scripts import me_inverse_short_v2_preflight as preflight  # noqa: E402

RUNTIME_UNIT = "trad-bot-me-inverse-short-v2-runtime.service"
V1_UNIT = "trad-bot-me-inverse-short-runtime.service"

RUNTIME_PASSWORD = "S3CRET_RUNTIME_PW_9f3a"
SCANNER_PASSWORD = "S3CRET_SCANNER_PW_77bd"

SOURCE_FILES = (
    "scanner_runner.py",
    "app/scanners/orchestrator.py",
    "app/paper/me_long_v2_inverse_short_v2/capture.py",
    "app/paper/me_long_v2_inverse_short_v2/engine.py",
    "app/paper/me_long_v2_inverse_short_v2/protocol.py",
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

    def _factory(**kwargs):
        connection = _ScriptedConnection(results)
        connection.connect_kwargs = kwargs
        _fake_connect_factory.last_connection = connection
        return connection

    return _factory


def _happy_db_results() -> list:
    return [
        # to_regclass x4 (schema present):
        ("dds.me_long_v2_inverse_short_v2_activation",
         "dds.me_long_v2_inverse_short_v2_event",
         "dds.me_long_v2_inverse_short_v2_position",
         "dds.me_long_v2_inverse_short_v2_close_outbox"),
        # activation row (READY_TO_START, NULL, NULL):
        ("ME_LONG_V2_INVERSE_SHORT_V2", "READY_TO_START", None, None),
        # event / position / outbox counts:
        (0, 0, 0),
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
        f"ME_INVERSE_SHORT_V2_ENABLED={flag}\n"
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
        f"ME_INVERSE_SHORT_V2_ENABLED={flag}\n"
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


# ---------------------------------------------------------------------------
# P1 fixtures: EFFECTIVE systemd configuration via mocked `systemctl show`
# output in systemd's own display formats (never a real systemctl).
# ---------------------------------------------------------------------------
def _systemctl_show_text(
    *, fragment_path: str, exec_start_argv: list[str] | None = None,
    exec_path: str | None = None, exec_code: str = "exited",
    exec_status: str = "0/0",
    working_directory: str = "", environment_files: list[str] = (),
    drop_in_paths: list[str] = (), main_pid: int = 4242,
    load_state: str = "loaded", active_state: str = "active",
    sub_state: str = "running", need_daemon_reload: bool = False,
) -> str:
    """Deterministic `systemctl show` output in the documented formats.

    ExecStart is emitted as the structured record systemd prints — with a
    timestamp CONTAINING SPACES, so a naive whitespace split of the whole
    line would corrupt the argv extraction; ``exec_code`` defaults to the
    exited-process spelling while the PRODUCTION VPS prints
    ``code=(null) ; status=0/0`` for a LIVE (running) process — the P1
    regression fixture uses that; ``exec_path`` overrides the ``path=``
    field independently of ``argv[0]`` (the P2 ``@``/substitution
    fixtures).  EnvironmentFiles as one ``<path> (ignore_errors=…)`` line
    per entry; DropInPaths as the shell-quoted space-joined single line
    (systemd ``shell_maybe_quote`` — ``shlex.quote`` here, so Windows
    backslashes survive the round-trip).
    """
    lines = [
        f"LoadState={load_state}",
        f"ActiveState={active_state}",
        f"SubState={sub_state}",
        f"MainPID={main_pid}",
        f"FragmentPath={fragment_path}",
        "DropInPaths=" + " ".join(
            shlex.quote(drop_in) for drop_in in drop_in_paths
        ),
        f"NeedDaemonReload={'yes' if need_daemon_reload else 'no'}",
    ]
    if exec_start_argv:
        path_field = (
            exec_path if exec_path is not None else exec_start_argv[0]
        )
        lines.append(
            "ExecStart={ path=" + path_field
            + " ; argv[]=" + " ".join(exec_start_argv)
            + " ; ignore_errors=no"
            + " ; start_time=[Wed 2026-11-11 12:00:00 MSK]"
            + " ; stop_time=[n/a]"
            + f" ; pid={main_pid}"
            + f" ; code={exec_code} ; status={exec_status} }}"
        )
    lines.append(f"WorkingDirectory={working_directory}")
    for env_file in environment_files:
        lines.append(f"EnvironmentFiles={env_file} (ignore_errors=no)")
    return "\n".join(lines) + "\n"


def _fake_systemctl_runner(
    text: str = "", *, returncode: int = 0,
    error: BaseException | None = None,
):
    """A deterministic systemctl stand-in: records calls, returns ``text``."""
    calls: list = []

    def _runner(command, **kwargs):
        calls.append((list(command), dict(kwargs)))
        if error is not None:
            raise error
        return SimpleNamespace(returncode=returncode, stdout=text, stderr="")

    _runner.calls = calls
    return _runner


# Captured at import: helper fixtures always use the REAL query+parser even
# when a test monkeypatches ``preflight._query_effective_unit`` (which
# ``run_preflight`` must see patched — this avoids recursive patching).
_QUERY_EFFECTIVE_UNIT_ORIGINAL = preflight._query_effective_unit


def _effective_from_unit(unit_path: Path, process=None, **overrides) -> dict:
    """Effective config systemd WOULD report for this unit file (no drop-ins).

    Drives the REAL parser (``_parse_effective_unit``) through the mocked
    ``systemctl show`` runner, so every default scenario exercises the
    effective-unit resolution path; scenarios with drop-ins build their own
    show text explicitly.
    """
    directives = preflight._read_unit_directives(unit_path)
    if directives is None:
        show_text = _systemctl_show_text(
            fragment_path="", exec_start_argv=None, load_state="not-found",
            active_state="inactive", sub_state="dead", main_pid=0,
        )
    else:
        exec_start = (directives.get("ExecStart") or [""])[0]
        main_pid = 4242
        if isinstance(process, dict) and process.get("pid"):
            main_pid = int(process["pid"])
        show_text = _systemctl_show_text(
            fragment_path=str(unit_path),
            exec_start_argv=exec_start.split() if exec_start else None,
            working_directory=(directives.get("WorkingDirectory") or [""])[0],
            environment_files=directives.get("EnvironmentFile") or [],
            main_pid=main_pid,
            **overrides,
        )
    return _QUERY_EFFECTIVE_UNIT_ORIGINAL(
        unit_path, runner=_fake_systemctl_runner(show_text),
    )


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
_EFFECTIVE_AUTO = object()


def _run(runtime: Path, scanner: Path, results: list, *, repo_root=None,
         scanner_root: Path | None = None, scanner_unit_path: Path | None = None,
         scanner_process=_PROCESS_AUTO, scanner_effective=_EFFECTIVE_AUTO):
    """run_preflight with fixture Scanner checkout/unit/process/effective.

    ``scanner_root`` defaults to a REAL temporary Scanner checkout built
    next to the env files (sources + a valid fixture virtualenv — the
    interpreter validation needs an actual ``pyvenv.cfg`` layout);
    ``scanner_unit_path`` defaults to the unit installed next to the env
    files, pointing at that checkout; ``scanner_process`` defaults to
    matching metadata and ``scanner_effective`` to the configuration
    systemd would report for that unit — scenarios override each
    ingredient.
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
    if scanner_effective is _EFFECTIVE_AUTO:
        scanner_effective = _effective_from_unit(
            scanner_unit_path, scanner_process,
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
        scanner_effective=scanner_effective,
    )
    return result, _fake_connect_factory.last_connection


# ---------------------------------------------------------------------------
# 1. Six-check preflight contract
# ---------------------------------------------------------------------------
class TestPreflightSixChecks:
    def test_all_six_pass_and_ready(self, env_files):
        runtime, scanner = env_files
        result, connection = _run(
            runtime, scanner, _happy_db_results(),
            repo_root=PROJECT_ROOT,
        )
        assert set(result["checks"]) == set(preflight.CHECK_NAMES)
        for name, check in result["checks"].items():
            assert check["status"] in (
                preflight.STATUS_PASS, preflight.STATUS_FAIL,
                preflight.STATUS_NOT_VERIFIED,
            ), name
            assert check["status"] == preflight.STATUS_PASS, (name, check)
        assert result["ready_for_activation"] is True
        assert result["result"] == "READY_FOR_ACTIVATION"
        assert result["activation_authorized"] is False
        _assert_read_only(connection)

    @pytest.mark.parametrize(
        ("runtime_text", "scanner_text", "db_results", "failing_check",
         "expected_status", "expected_reason"),
        [
            # runtime flag already ON at preflight:
            (_runtime_env_text(flag="1"), None, None,
             "V2_RUNTIME_CONFIG_READY", "FAIL", "SERVICE_FLAG_MUST_BE_OFF"),
            # scanner flag already ON at preflight:
            (None, _scanner_env_text(flag="1"), None,
             "V2_SOURCE_CAPTURE_READY", "FAIL", "SCANNER_FLAG_NOT_OFF"),
            # two processes pointing at different databases:
            (None, _scanner_env_text(password=SCANNER_PASSWORD), None,
             "V2_SOURCE_CAPTURE_READY", "FAIL", "SCANNER_RUNTIME_DB_MISMATCH"),
            # migration 087 schema absent:
            (None, None, "no_schema", "DB_READY", "FAIL", "V2_SCHEMA_MISSING"),
            # activation row not READY_TO_START:
            (None, None, "running", "V2_ACTIVATION_READY", "FAIL",
             "ACTIVATION_NOT_READY"),
            # fingerprint already written:
            (None, None, "frozen", "V2_ACTIVATION_READY", "FAIL",
             "ALREADY_FROZEN"),
            # pre-existing OOS rows:
            (None, None, "has_data", "V2_ACTIVATION_READY", "FAIL",
             "EXISTING_OOS_DATA"),
            # activation row absent:
            (None, None, "no_row", "V2_ACTIVATION_READY", "FAIL",
             "ACTIVATION_ROW_MISSING"),
        ],
        ids=[
            "runtime-flag-on", "scanner-flag-on", "db-mismatch",
            "schema-missing", "activation-running", "already-frozen",
            "existing-data", "activation-row-missing",
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
            "no_schema": [(None, None, None, None)],
            "running": [
                ("dds.me_long_v2_inverse_short_v2_activation",
                 "dds.me_long_v2_inverse_short_v2_event",
                 "dds.me_long_v2_inverse_short_v2_position",
                 "dds.me_long_v2_inverse_short_v2_close_outbox"),
                ("ME_LONG_V2_INVERSE_SHORT_V2", "RUNNING", None, None),
            ],
            "frozen": [
                ("dds.me_long_v2_inverse_short_v2_activation",
                 "dds.me_long_v2_inverse_short_v2_event",
                 "dds.me_long_v2_inverse_short_v2_position",
                 "dds.me_long_v2_inverse_short_v2_close_outbox"),
                ("ME_LONG_V2_INVERSE_SHORT_V2", "READY_TO_START",
                 "0" * 64, None),
            ],
            "has_data": [
                ("dds.me_long_v2_inverse_short_v2_activation",
                 "dds.me_long_v2_inverse_short_v2_event",
                 "dds.me_long_v2_inverse_short_v2_position",
                 "dds.me_long_v2_inverse_short_v2_close_outbox"),
                ("ME_LONG_V2_INVERSE_SHORT_V2", "READY_TO_START", None, None),
                (3, 0, 0),
            ],
            "no_row": [
                ("dds.me_long_v2_inverse_short_v2_activation",
                 "dds.me_long_v2_inverse_short_v2_event",
                 "dds.me_long_v2_inverse_short_v2_position",
                 "dds.me_long_v2_inverse_short_v2_close_outbox"),
                None,
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
        _assert_read_only(connection)

    def test_missing_runtime_env_is_not_verified_never_pass(self, tmp_path):
        runtime = tmp_path / "absent.env"
        scanner = tmp_path / "scanner.env"
        scanner.write_text(_scanner_env_text(), encoding="utf-8")
        result, connection = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        assert result["checks"]["V2_RUNTIME_CONFIG_READY"]["status"] == \
            preflight.STATUS_NOT_VERIFIED
        # Dependent checks degrade to NOT_VERIFIED, never PASS:
        assert result["checks"]["DB_READY"]["status"] == \
            preflight.STATUS_NOT_VERIFIED
        assert result["checks"]["V2_ACTIVATION_READY"]["status"] == \
            preflight.STATUS_NOT_VERIFIED
        assert result["checks"]["V1_ISOLATION_VALID"]["status"] == \
            preflight.STATUS_NOT_VERIFIED
        assert result["ready_for_activation"] is False
        _assert_read_only(connection)

    def test_missing_scanner_env_is_not_verified_never_pass(self, env_files):
        runtime, _scanner = env_files
        result, _connection = _run(
            runtime, env_files[1].parent / "absent-scanner.env",
            _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        source = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert source["status"] == preflight.STATUS_NOT_VERIFIED
        assert "SCANNER_ENV_UNAVAILABLE" in source["reason"]
        assert result["ready_for_activation"] is False

    def test_scanner_without_flag_is_not_verified(self, env_files):
        runtime, scanner = env_files
        scanner.write_text(
            _scanner_env_text(password=RUNTIME_PASSWORD, include_db=True).replace(
                "ME_INVERSE_SHORT_V2_ENABLED=0\n", "",
            ),
            encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
        )
        source = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        source = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
            "_capture_me_inverse_short_v2_source(repository, inv_candidate)\n"
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
        assert result["checks"]["V2_RUNTIME_CONFIG_READY"]["status"] == \
            preflight.STATUS_PASS
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "V2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP" in check["reason"]
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
            result["checks"]["V2_ACTIVATION_READY"]["status"]
            == preflight.STATUS_NOT_VERIFIED
        )
        # Driver exception text (which may embed credentials) is withheld:
        assert "leaky" not in json.dumps(result)
        assert result["ready_for_activation"] is False


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
            "_capture_me_inverse_short_v2_source(repository, inv_candidate)\n"
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
        assert result["checks"]["V2_RUNTIME_CONFIG_READY"]["status"] == \
            preflight.STATUS_PASS
        # …but the Scanner-side compound fails on the old checkout:
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "scanner_source_routing:V2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        source_detail = result["checks"]["V2_SOURCE_CAPTURE_READY"]["detail"]
        assert "no audited source changed after process start" in source_detail
        assert "Scanner flag configured and OFF at preflight" in source_detail
        assert "unit EnvironmentFile equals" in source_detail
        config_detail = result["checks"]["V2_RUNTIME_CONFIG_READY"]["detail"]
        assert "V2 release passes the static source-path audit" in config_detail
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
            and node.name == "_capture_me_inverse_short_v2_source"
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_source_routing:V2_HOOK_NOT_DEFINED" in check["reason"]
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
            "_get_me_inverse_short_v2_store",
            "_get_me_inverse_short_v2_store_gate_accepted",
        )
        assert mutated != source, "control needle moved; update it"
        runner.write_text(mutated, encoding="utf-8")
        unit = _write_scanner_unit(tmp_path / "unit-gated.service", root,
                                   scanner)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
        )
        assert result["checks"]["V2_RUNTIME_CONFIG_READY"]["status"] == \
            preflight.STATUS_PASS
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "scanner_source_routing:V2_HOOK_TRADING_GATE_DEPENDENCY"
            in check["reason"]
        )
        assert result["ready_for_activation"] is False

    def test_release_without_wiring_fails_runtime_code_ready(
        self, env_files, tmp_path,
    ):
        """The release half of the split: a V2 release WITHOUT the wiring
        fails V2_RUNTIME_CONFIG_READY (runtime_code_ready), while the
        verified Scanner checkout alone can never make it ready."""
        runtime, scanner = env_files
        release = _make_scanner_checkout(tmp_path, "broken-release")
        runner = release / "scanner_runner.py"
        source = runner.read_text(encoding="utf-8")
        needle = (
            "                    "
            "_capture_me_inverse_short_v2_source(repository, inv_candidate)\n"
        )
        assert needle in source
        runner.write_text(
            source.replace(needle, "                    pass\n"),
            encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=release,
        )
        check = result["checks"]["V2_RUNTIME_CONFIG_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "runtime_code_ready:V2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
            in check["reason"]
        )
        assert result["checks"]["V2_SOURCE_CAPTURE_READY"]["status"] == \
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_PASS
        assert "pyvenv.cfg present" in check["detail"]
        assert result["checks"]["V2_RUNTIME_CONFIG_READY"]["status"] == \
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
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
            "_capture_me_inverse_short_v2_source(repository, inv_candidate)\n"
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert (
            "scanner_source_routing:V2_DELIVERY_MISSING_FROM_PRODUCTION_LOOP"
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
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_process_matches_unit:SCANNER_PROCESS_UNIT_MISMATCH" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_fully_correct_checkout_unit_process_venv_passes(
            self, env_files, tmp_path):
        """Everything correct (checkout + unit + process + copy-based venv)
        → all six PASS, READY_FOR_ACTIVATION."""
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
        assert "virtualenv" in result["checks"]["V2_SOURCE_CAPTURE_READY"][
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
        monkeypatch.setattr(
            preflight, "_query_effective_unit",
            lambda _unit_path=None, **_kwargs: _effective_from_unit(unit),
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
        assert report["checks"]["V2_RUNTIME_CONFIG_READY"]["status"] == "FAIL"
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
            me_inverse_short_v2_activate.read_service_env
            is preflight.read_service_env
        )
        assert (
            me_inverse_short_v2_activate.PreflightRefused
            is preflight.PreflightRefused
        )

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            ("export DB_HOST=1.2.3.4\n", "SERVICE_ENV_UNSUPPORTED_SYNTAX"),
            ("ME_INVERSE_SHORT_V2_ENABLED=1\n", "SERVICE_ENV_INCOMPLETE"),
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
        assert values["ME_INVERSE_SHORT_V2_ENABLED"] == "0"
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
            "EnvironmentFile=/etc/trad-bot/me-inverse-short-v2-runtime.env"
            in v2_text
        )
        assert "app.paper.me_long_v2_inverse_short_v2.production_runtime" in v2_text
        assert "/opt/trad_bot_me_inverse_short_v2_release" in v2_text
        assert "RestartPreventExitStatus=3" in v2_text
        assert "ME_INVERSE_SHORT_V2_ENABLED=0" in v2_text  # documented OFF
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
            PROJECT_ROOT / "deploy" / "env" / "me-inverse-short-v2-runtime.env.example"
        )
        assert example.is_file()
        text = example.read_text(encoding="utf-8")
        assert "ME_INVERSE_SHORT_V2_ENABLED=0" in text
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
        assert values["ME_INVERSE_SHORT_V2_ENABLED"] == "0"

    def test_scanner_env_example(self):
        example = (
            PROJECT_ROOT / "deploy" / "env" / "me-inverse-short-scanner.env.example"
        )
        assert example.is_file()
        text = example.read_text(encoding="utf-8")
        assert "ME_INVERSE_SHORT_V2_ENABLED=0" in text
        assert "/opt/trad_bot/.env" in text  # the Scanner's own env file
        for key in preflight.DB_KEYS:
            assert f"{key}=" in text, key
        assert "ME_INVERSE_SHORT_V1_ENABLED" not in text

    def test_runbook_has_the_install_verification_rollback_flow(self):
        runbook = (
            PROJECT_ROOT / "docs" / "me_inverse_short_v2_production_runtime_v1.md"
        )
        assert runbook.is_file()
        text = runbook.read_text(encoding="utf-8")
        # 12 numbered steps:
        for step in range(1, 13):
            assert f"### Step {step} " in text, step
        # STOP conditions are explicit throughout:
        assert text.count("**STOP**") >= 10
        # Six-check preflight documented by name:
        for name in preflight.CHECK_NAMES:
            assert f"`{name}`" in text, name
        assert "scripts/me_inverse_short_v2_preflight.py" in text
        assert "READY_FOR_ACTIVATION" in text
        assert "NOT_VERIFIED" in text
        # Migration 087 + rollback + drain documented:
        assert "087_me_long_v2_inverse_short_v2.sql" in text
        assert "### Step 12 — Stop / drain / rollback" in text
        assert "--drain" in text
        # The two-process scanner half is present:
        assert "me-inverse-short-scanner.env.example" in text
        # P2: the Scanner source installation stage with its explicit
        # CLI parameters and the release/scanner readiness split:
        assert (
            "### Step 9 — Scanner source installation and effective "
            "runtime verification" in text
        )
        assert "V2_RUNTIME_CODE_READY" in text
        assert "--scanner-root /opt/trad_bot" in text
        assert "--scanner-unit /etc/systemd/system/trad-bot-scanner.service" \
            in text
        # Scanner updates/restarts stay a separate operational GO:
        assert "separate" in text and "GO" in text

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
            "/etc/trad-bot/me-inverse-short-v2-runtime.env"
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
            PROJECT_ROOT / "app" / "paper" / "me_long_v2_inverse_short_v2"
            / "production_runtime.py"
        )
        assert module.is_file()
        source = module.read_text(encoding="utf-8")
        assert "EXIT_ALREADY_RUNNING = 3" in source
        assert "DEFAULT_LOCK_FILE" in source
        assert "me_inverse_short_v2_runtime" in source


# ---------------------------------------------------------------------------
# 3. Runtime path isolation (lock / heartbeat / log)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def runtime_modules():
    v2 = importlib.import_module(
        "app.paper.me_long_v2_inverse_short_v2.production_runtime",
    )
    v1 = importlib.import_module(
        "app.paper.me_long_v2_inverse_short_v1.production_runtime",
    )
    return v2, v1


class TestRuntimePathIsolation:
    def test_v2_paths_are_v2_specific(self, runtime_modules):
        v2, _v1 = runtime_modules
        assert v2.DEFAULT_LOCK_FILE.name == "me_inverse_short_v2_runtime.lock"
        assert v2.DEFAULT_HEARTBEAT_FILE.name == \
            "me_inverse_short_v2_runtime_heartbeat.json"
        assert v2.DEFAULT_LOG_FILE.name == "me_inverse_short_v2_runtime.log"

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
# 7. P1 — the EFFECTIVE systemd configuration (base fragment + ordered
#    drop-in overrides) resolved from mocked `systemctl show` output.
#    Nothing here runs a real systemctl, reads a real /proc or restarts
#    anything: deterministic fixtures only.
# ---------------------------------------------------------------------------
def _effective_show(unit_path: Path, **show_kwargs) -> dict:
    """Parse mocked `systemctl show` text through the REAL query+parser."""
    show_text = _systemctl_show_text(**show_kwargs)
    return preflight._query_effective_unit(
        unit_path, runner=_fake_systemctl_runner(show_text),
    )


def _dropin_scenario(tmp_path, env_files, *, venv_style="copy",
                     **show_overrides):
    """Base unit → OLD release; effective config → NEW release (drop-in).

    The drop-in file is REAL on disk — with an ``ExecStart=`` reset plus
    the new ExecStart/WorkingDirectory — and is listed in the mocked
    ``DropInPaths``, exactly like a staged Scanner switch.  Additional
    keyword arguments (``exec_code``, ``exec_status``, ``exec_path`` …)
    are forwarded to the mocked ``systemctl show`` output.  Returns
    ``(runtime, scanner, old, new, unit, dropin, process, effective)``.
    """
    runtime, scanner = env_files
    old = _make_scanner_checkout(tmp_path, "old-release",
                                 venv_style=venv_style)
    new = _make_scanner_checkout(tmp_path, "v2-release",
                                 venv_style=venv_style)
    unit = _write_scanner_unit(tmp_path / "scanner-unit.service", old,
                               scanner)
    dropin_dir = Path(f"{unit}.d")
    dropin_dir.mkdir(parents=True, exist_ok=True)
    dropin = dropin_dir / "20-scanner-v2-release.conf"
    dropin.write_text(
        "[Service]\n"
        "ExecStart=\n"
        f"ExecStart={new.as_posix()}/.venv/bin/python "
        f"{new.as_posix()}/scanner_runner.py\n"
        f"WorkingDirectory={new.as_posix()}\n"
        "Environment=ME_INVERSE_SHORT_V2_ENABLED=0\n",
        encoding="utf-8",
    )
    process = _scanner_process_fixture(new, unit=unit)
    effective = _effective_show(
        unit,
        fragment_path=str(unit),
        exec_start_argv=[
            f"{new.as_posix()}/.venv/bin/python",
            f"{new.as_posix()}/scanner_runner.py",
        ],
        working_directory=new.as_posix(),
        environment_files=[scanner.as_posix()],
        drop_in_paths=[str(dropin)],
        **show_overrides,
    )
    return runtime, scanner, old, new, unit, dropin, process, effective


def _two_venvs_one_interpreter(tmp_path) -> tuple[Path, Path, Path]:
    """TWO different virtualenvs sharing ONE final interpreter.

    ``checkout-a`` (the audited Scanner checkout) and ``checkout-b``
    (foreign) each keep their OWN ``.venv/pyvenv.cfg``, while both
    ``.venv/bin`` directories are LINKED to a single interpreter
    directory inside ``checkout-a`` (directory symlink on Linux /
    privilege-free junction on Windows) — the exact
    ``python -> /usr/bin/python3.12`` shape under which a
    resolve()-based identity key treats the two virtualenvs as the same
    file.  Returns ``(root, foreign, shared_interpreter)``.
    """
    root = _make_scanner_checkout(tmp_path, "checkout-a")
    foreign = _make_scanner_checkout(tmp_path, "checkout-b")
    shared = root / "shared_bin"
    shared.mkdir(parents=True, exist_ok=True)
    interpreter = shared / "python"
    interpreter.write_text(
        "# shared interpreter fixture — never executed\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    for checkout in (root, foreign):
        bin_link = checkout / ".venv" / "bin"
        shutil.rmtree(bin_link)
        _link_dir_or_skip(bin_link, shared)
    return root, foreign, interpreter


def _assert_ready(result):
    assert result["ready_for_activation"] is True
    assert result["result"] == "READY_FOR_ACTIVATION"
    for name in preflight.CHECK_NAMES:
        assert result["checks"][name]["status"] == preflight.STATUS_PASS, (
            name, result["checks"][name],
        )


class TestEffectiveSystemdDropIns:
    """P1 acceptance: effective unit with ordered drop-ins — a correct
    drop-in over an old base unit passes, clear mismatches FAIL, and
    missing/unconfirmable/pending systemd metadata is NOT_VERIFIED."""

    def test_base_unit_old_but_effective_dropin_matches_root_passes(
        self, env_files, tmp_path,
    ):
        runtime, scanner, old, new, unit, dropin, process, effective = \
            _dropin_scenario(tmp_path, env_files)
        # The BASE unit still points at the OLD release — the base-file-only
        # comparison used to false-fail here (SCANNER_UNIT_CHECKOUT_MISMATCH);
        # the effective drop-in is what systemd actually loaded:
        base = unit.read_text(encoding="utf-8")
        assert f"{old.as_posix()}/scanner_runner.py" in base
        assert f"{new.as_posix()}/scanner_runner.py" not in base
        assert effective["exec_start"]["argv"][0] == \
            f"{new.as_posix()}/.venv/bin/python"
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        _assert_ready(result)
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert "SCANNER_UNIT_CHECKOUT_MISMATCH" not in check["reason"]
        assert "with 1 drop-in override(s)" in check["detail"]

    def test_multiple_sequential_dropins_pass(self, env_files, tmp_path):
        """Several drop-ins in lexical load order (canary, release, SRR
        capture): the effective config is their merge → PASS."""
        runtime, scanner, old, new, unit, dropin, process, _ = \
            _dropin_scenario(tmp_path, env_files)
        dropin_dir = Path(f"{unit}.d")
        canary = dropin_dir / "10-scanner-canary.conf"
        canary.write_text(
            "[Service]\nEnvironment=SCANNER_CANARY_INTERVAL=60\n",
            encoding="utf-8",
        )
        capture = dropin_dir / "30-srr-capture.conf"
        capture.write_text(
            "[Service]\nEnvironment=ME_SHORT_A_CAPTURE_ENABLED=1\n",
            encoding="utf-8",
        )
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(canary), str(dropin), str(capture)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        _assert_ready(result)
        assert "with 3 drop-in override(s)" in \
            result["checks"]["V2_SOURCE_CAPTURE_READY"]["detail"]

    def test_execstart_reset_in_dropin_is_honored(self, env_files, tmp_path):
        """The drop-in's ``ExecStart=`` reset + replacement is what systemd
        loads: the effective ExecStart is the new command only."""
        runtime, scanner, old, new, unit, dropin, process, effective = \
            _dropin_scenario(tmp_path, env_files)
        dropin_text = dropin.read_text(encoding="utf-8")
        assert "ExecStart=\n" in dropin_text  # the reset line on disk
        base = unit.read_text(encoding="utf-8")
        assert f"{old.as_posix()}/scanner_runner.py" in base
        assert effective["exec_start"]["argv"] == [
            f"{new.as_posix()}/.venv/bin/python",
            f"{new.as_posix()}/scanner_runner.py",
        ]
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        _assert_ready(result)

    def test_stale_scanner_on_old_release_fails(self, env_files, tmp_path):
        """Old Scanner process running despite the proposed new drop-in —
        never PASS (clear process/effective mismatch → FAIL)."""
        runtime, scanner, old, new, unit, dropin, _, effective = \
            _dropin_scenario(tmp_path, env_files)
        stale_process = _scanner_process_fixture(old, unit=unit)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=stale_process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_process_matches_unit:SCANNER_PROCESS_UNIT_MISMATCH" \
            in check["reason"]
        assert result["ready_for_activation"] is False
        assert result["result"] == "NOT_READY"

    def test_effective_workdir_mismatch_fails(self, env_files, tmp_path):
        runtime, scanner = env_files
        new = _make_scanner_checkout(tmp_path, "v2-release")
        elsewhere = _make_scanner_checkout(tmp_path, "elsewhere")
        unit = _write_scanner_unit(tmp_path / "scanner-unit.service", new,
                                   scanner)
        dropin_dir = Path(f"{unit}.d")
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin = dropin_dir / "20-scanner-v2-release.conf"
        dropin.write_text(
            f"[Service]\nWorkingDirectory={elsewhere.as_posix()}\n",
            encoding="utf-8",
        )
        process = _scanner_process_fixture(new, unit=unit)
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=elsewhere.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_UNIT_CHECKOUT_MISMATCH" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_effective_python_from_other_checkout_fails(
        self, env_files, tmp_path,
    ):
        runtime, scanner = env_files
        new = _make_scanner_checkout(tmp_path, "v2-release")
        other = _make_scanner_checkout(tmp_path, "other-checkout")
        unit = _write_scanner_unit(tmp_path / "scanner-unit.service", new,
                                   scanner)
        dropin_dir = Path(f"{unit}.d")
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin = dropin_dir / "20-scanner-v2-release.conf"
        dropin.write_text(
            "[Service]\nExecStart=\n"
            f"ExecStart={other.as_posix()}/.venv/bin/python "
            f"{new.as_posix()}/scanner_runner.py\n"
            f"WorkingDirectory={new.as_posix()}\n",
            encoding="utf-8",
        )
        process = _scanner_process_fixture(new, unit=unit)
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_start_argv=[
                f"{other.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_PYTHON_OUTSIDE_CHECKOUT" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_old_effective_release_with_new_scanner_root_fails(
        self, env_files, tmp_path,
    ):
        """Effective config still on the OLD release while --scanner-root
        audits the candidate one → structural FAIL, never PASS."""
        runtime, scanner = env_files
        old = _make_scanner_checkout(tmp_path, "old-release")
        new = _make_scanner_checkout(tmp_path, "v2-release")
        unit = _write_scanner_unit(tmp_path / "scanner-unit.service", old,
                                   scanner)
        process = _scanner_process_fixture(old, unit=unit)
        effective = _effective_from_unit(unit, process)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_UNIT_CHECKOUT_MISMATCH" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_dropin_environment_flag_does_not_substitute_env_file(
        self, env_files, tmp_path,
    ):
        """An ``Environment=ME_INVERSE_SHORT_V2_ENABLED=0`` drop-in line
        never substitutes for the Scanner .env check: without the flag in
        the env FILE the result is NOT_VERIFIED, never PASS."""
        runtime, scanner, old, new, unit, dropin, process, effective = \
            _dropin_scenario(tmp_path, env_files)
        assert "Environment=ME_INVERSE_SHORT_V2_ENABLED=0" in \
            dropin.read_text(encoding="utf-8")
        scanner.write_text(
            "# scanner fragment WITHOUT the V2 flag\n"
            "DB_HOST=127.0.0.1\n"
            "DB_PORT=5432\n"
            "DB_NAME=trad_bot\n"
            "DB_USER=postgres\n"
            f"DB_PASSWORD={RUNTIME_PASSWORD}\n",
            encoding="utf-8",
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_flag:SCANNER_FLAG_NOT_CONFIGURED" in check["reason"]
        assert result["ready_for_activation"] is False

    def test_effective_environmentfiles_without_scanner_env_fails(
        self, env_files, tmp_path,
    ):
        runtime, scanner, old, new, unit, dropin, process, _ = \
            _dropin_scenario(tmp_path, env_files)
        other_env = tmp_path / "other-scanner.env"
        other_env.write_text(
            f"ME_INVERSE_SHORT_V2_ENABLED=0\nDB_PASSWORD={RUNTIME_PASSWORD}\n",
            encoding="utf-8",
        )
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[str(other_env)],
            drop_in_paths=[str(dropin)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_env:SCANNER_UNIT_ENV_MISMATCH" in check["reason"]
        assert result["ready_for_activation"] is False

    def test_missing_systemd_metadata_is_not_verified(
        self, env_files, tmp_path,
    ):
        runtime, scanner, old, new, unit, dropin, process, _ = \
            _dropin_scenario(tmp_path, env_files)
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process,
            scanner_effective={
                "available": False,
                "reason": "SCANNER_SYSTEMCTL_UNAVAILABLE",
            },
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_effective_unit:SCANNER_SYSTEMCTL_UNAVAILABLE" \
            in check["reason"]
        assert result["ready_for_activation"] is False
        assert result["result"] != "READY_FOR_ACTIVATION"

    def test_unconfirmable_mainpid_is_not_verified(self, env_files, tmp_path):
        runtime, scanner, old, new, unit, dropin, process, _ = \
            _dropin_scenario(tmp_path, env_files)
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
            main_pid=0,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_process_matches_unit:SCANNER_MAINPID_UNCONFIRMABLE" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_pending_daemon_reload_is_not_verified(self, env_files, tmp_path):
        runtime, scanner, old, new, unit, dropin, process, _ = \
            _dropin_scenario(tmp_path, env_files)
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
            need_daemon_reload=True,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_effective_unit:SCANNER_UNIT_RELOAD_PENDING" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_orphaned_dropin_is_not_verified(self, env_files, tmp_path):
        """A drop-in listed as LOADED but missing on disk → NOT_VERIFIED."""
        runtime, scanner, old, new, unit, dropin, process, _ = \
            _dropin_scenario(tmp_path, env_files)
        removed = Path(f"{unit}.d") / "99-removed.conf"  # never written
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin), str(removed)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_effective_unit:SCANNER_UNIT_DROPIN_ORPHANED" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_staged_dropin_without_daemon_reload_is_not_verified(
        self, env_files, tmp_path,
    ):
        """A drop-in on disk that systemd has NOT loaded (classic staged
        switch without daemon-reload) → NOT_VERIFIED, never PASS."""
        runtime, scanner, old, new, unit, dropin, process, _ = \
            _dropin_scenario(tmp_path, env_files)
        staged = Path(f"{unit}.d") / "99-staged-not-loaded.conf"
        staged.write_text("[Service]\nEnvironment=STAGED=1\n",
                          encoding="utf-8")
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],  # the staged file is NOT loaded
            need_daemon_reload=False,     # even when systemd hasn't noticed
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_effective_unit:SCANNER_UNIT_DROPIN_PENDING" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_fragment_path_mismatch_fails(self, env_files, tmp_path):
        runtime, scanner, old, new, unit, dropin, process, _ = \
            _dropin_scenario(tmp_path, env_files)
        effective = _effective_show(
            unit,
            fragment_path=str(tmp_path / "lib-systemd-scanner.service"),
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_effective_unit:SCANNER_UNIT_FRAGMENT_MISMATCH" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_loadstate_not_found_is_not_verified(self, env_files, tmp_path):
        runtime, scanner, old, new, unit, dropin, process, _ = \
            _dropin_scenario(tmp_path, env_files)
        effective = _effective_show(
            unit,
            fragment_path="",
            exec_start_argv=None,
            load_state="not-found",
            active_state="inactive",
            sub_state="dead",
            main_pid=0,
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_NOT_VERIFIED
        assert "scanner_effective_unit:SCANNER_UNIT_NOT_LOADED" \
            in check["reason"]
        assert result["ready_for_activation"] is False

    def test_symlink_venv_with_effective_dropin_passes(
        self, env_files, tmp_path,
    ):
        """The standard Linux symlink venv through an effective drop-in
        PASSES (runs on Linux; skipped where file symlinks are denied —
        a skip never counts as success)."""
        runtime, scanner, old, new, unit, dropin, process, effective = \
            _dropin_scenario(tmp_path, env_files, venv_style="symlink")
        exe = new.joinpath(".venv/bin/python")
        assert exe.is_symlink()
        resolved = exe.resolve()
        assert not str(resolved).startswith(str(new.resolve()))
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        _assert_ready(result)
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert "pyvenv.cfg present" in check["detail"]

    def test_foreign_venv_with_effective_dropin_fails(
        self, env_files, tmp_path,
    ):
        """A foreign virtualenv substituted under the effective drop-in →
        FAIL (never PASS)."""
        runtime, scanner = env_files
        new = _make_scanner_checkout(tmp_path, "v2-release")
        foreign = _make_scanner_checkout(tmp_path, "foreign-checkout")
        shutil.rmtree(new / ".venv")
        _link_dir_or_skip(new / ".venv", foreign / ".venv")
        unit = _write_scanner_unit(tmp_path / "scanner-unit.service", new,
                                   scanner)
        dropin_dir = Path(f"{unit}.d")
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin = dropin_dir / "20-scanner-v2-release.conf"
        dropin.write_text(
            "[Service]\nExecStart=\n"
            f"ExecStart={new.as_posix()}/.venv/bin/python "
            f"{new.as_posix()}/scanner_runner.py\n"
            f"WorkingDirectory={new.as_posix()}\n",
            encoding="utf-8",
        )
        process = _scanner_process_fixture(new, unit=unit)
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_VENV_SYMLINK_ESCAPE" \
            in check["reason"]
        assert result["ready_for_activation"] is False


    def test_running_scanner_production_execstart_format_passes(
        self, env_files, tmp_path,
    ):
        """The LIVE production VPS record — ``code=(null) ; status=0/0``
        for a process that has not exited — must not trip
        ``SCANNER_UNIT_EXEC_UNPARSABLE`` (P1): a healthy running Scanner
        in the exact observed format still yields all six PASS."""
        runtime, scanner, old, new, unit, dropin, process, effective = \
            _dropin_scenario(
                tmp_path, env_files,
                exec_code="(null)", exec_status="0/0",
            )
        assert effective["available"] is True, effective
        assert effective["exec_start"]["argv"] == [
            f"{new.as_posix()}/.venv/bin/python",
            f"{new.as_posix()}/scanner_runner.py",
        ]
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        _assert_ready(result)

    def test_argv0_substitution_of_python_fails(self, env_files, tmp_path):
        """The P2 substitution attack: ``argv[0]`` advertises the audited
        checkout interpreter while ``path=`` — the file systemd actually
        executes — is a FOREIGN python.  The membership check binds to
        ``path=`` (never argv[0] alone) → FAIL, never PASS."""
        runtime, scanner = env_files
        new = _make_scanner_checkout(tmp_path, "v2-release")
        foreign = _make_scanner_checkout(tmp_path, "foreign-checkout")
        unit = _write_scanner_unit(tmp_path / "scanner-unit.service", new,
                                   scanner)
        dropin_dir = Path(f"{unit}.d")
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin = dropin_dir / "20-scanner-v2-release.conf"
        dropin.write_text(
            "[Service]\nExecStart=\n"
            f"ExecStart=@{foreign.as_posix()}/.venv/bin/python "
            f"{new.as_posix()}/.venv/bin/python "
            f"{new.as_posix()}/scanner_runner.py\n"
            f"WorkingDirectory={new.as_posix()}\n",
            encoding="utf-8",
        )
        process = _scanner_process_fixture(new, unit=unit)
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_path=f"{foreign.as_posix()}/.venv/bin/python",
            exec_start_argv=[
                f"{new.as_posix()}/.venv/bin/python",
                f"{new.as_posix()}/scanner_runner.py",
            ],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_PYTHON_OUTSIDE_CHECKOUT" \
            in check["reason"]
        assert result["ready_for_activation"] is False
        assert result["result"] == "NOT_READY"

    def test_execstart_at_prefix_argv0_divergence_fails(
        self, env_files, tmp_path,
    ):
        """systemd ``@`` prefix (systemd.service(5)): the SECOND
        command-line token becomes argv[0] instead of the actual
        filename, so ``path=`` (the audited checkout interpreter) and
        argv[0] (the script) diverge → rejected as
        ``SCANNER_UNIT_EXEC_PATH_MISMATCH``, never PASS."""
        runtime, scanner = env_files
        new = _make_scanner_checkout(tmp_path, "v2-release")
        unit = _write_scanner_unit(tmp_path / "scanner-unit.service", new,
                                   scanner)
        dropin_dir = Path(f"{unit}.d")
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin = dropin_dir / "20-scanner-v2-release.conf"
        dropin.write_text(
            "[Service]\nExecStart=\n"
            f"ExecStart=@{new.as_posix()}/.venv/bin/python "
            f"{new.as_posix()}/scanner_runner.py\n"
            f"WorkingDirectory={new.as_posix()}\n",
            encoding="utf-8",
        )
        process = _scanner_process_fixture(new, unit=unit)
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_path=f"{new.as_posix()}/.venv/bin/python",
            # under `@` argv[0] is the second token (the script), not the
            # executable:
            exec_start_argv=[f"{new.as_posix()}/scanner_runner.py"],
            working_directory=new.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=new, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_UNIT_EXEC_PATH_MISMATCH" \
            in check["reason"]
        assert result["ready_for_activation"] is False


    def test_foreign_venv_argv0_with_same_resolved_python_fails(
        self, env_files, tmp_path,
    ):
        """P2 regression: ``argv[0]`` from ANOTHER virtualenv that
        resolves to the SAME final interpreter (``/usr/bin/python3.12``
        shape) must still be rejected — executable identity is compared
        LEXICALLY, never through symlink resolution.  The resolve()-based
        key used to call the two venvs identical, let the spoofed
        ``argv[0]`` pass the identity check, and reported ready."""
        runtime, scanner = env_files
        root, foreign, _ = _two_venvs_one_interpreter(tmp_path)
        unit = _write_scanner_unit(tmp_path / "scanner-unit.service", root,
                                   scanner)
        dropin_dir = Path(f"{unit}.d")
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin = dropin_dir / "20-scanner-v2-release.conf"
        dropin.write_text(
            "[Service]\nExecStart=\n"
            f"ExecStart={root.as_posix()}/.venv/bin/python "
            f"{root.as_posix()}/scanner_runner.py\n"
            f"WorkingDirectory={root.as_posix()}\n",
            encoding="utf-8",
        )
        process = _scanner_process_fixture(root, unit=unit)
        # The running process faithfully mirrors the spoofed unit, so the
        # process-vs-unit subcheck passes and ONLY the executable
        # identity check can catch the substitution:
        process["cmdline"] = [
            f"{foreign.as_posix()}/.venv/bin/python",
            f"{root.as_posix()}/scanner_runner.py",
        ]
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_path=f"{root.as_posix()}/.venv/bin/python",
            exec_start_argv=[
                f"{foreign.as_posix()}/.venv/bin/python",
                f"{root.as_posix()}/scanner_runner.py",
            ],
            working_directory=root.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        check = result["checks"]["V2_SOURCE_CAPTURE_READY"]
        assert check["status"] == preflight.STATUS_FAIL
        assert "scanner_unit_matches_checkout:SCANNER_UNIT_EXEC_PATH_MISMATCH" \
            in check["reason"]
        assert result["ready_for_activation"] is False
        assert result["result"] == "NOT_READY"

    def test_same_venv_with_correct_paths_passes(self, env_files, tmp_path):
        """Positive guard: the SAME virtualenv with correct paths
        (``path=`` and ``argv[0]`` identical) still yields all six PASS
        even though the interpreter is reached through the standard
        symlink/junction chain to the shared system interpreter —
        symlink-based virtualenvs stay allowed."""
        runtime, scanner = env_files
        root, _foreign, _ = _two_venvs_one_interpreter(tmp_path)
        unit = _write_scanner_unit(tmp_path / "scanner-unit.service", root,
                                   scanner)
        dropin_dir = Path(f"{unit}.d")
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin = dropin_dir / "20-scanner-v2-release.conf"
        dropin.write_text(
            "[Service]\nExecStart=\n"
            f"ExecStart={root.as_posix()}/.venv/bin/python "
            f"{root.as_posix()}/scanner_runner.py\n"
            f"WorkingDirectory={root.as_posix()}\n",
            encoding="utf-8",
        )
        process = _scanner_process_fixture(root, unit=unit)
        effective = _effective_show(
            unit,
            fragment_path=str(unit),
            exec_path=f"{root.as_posix()}/.venv/bin/python",
            exec_start_argv=[
                f"{root.as_posix()}/.venv/bin/python",
                f"{root.as_posix()}/scanner_runner.py",
            ],
            working_directory=root.as_posix(),
            environment_files=[scanner.as_posix()],
            drop_in_paths=[str(dropin)],
        )
        result, _ = _run(
            runtime, scanner, _happy_db_results(), repo_root=PROJECT_ROOT,
            scanner_root=root, scanner_unit_path=unit,
            scanner_process=process, scanner_effective=effective,
        )
        _assert_ready(result)


class TestEffectiveUnitShowParsing:
    """The `systemctl show` parser itself: documented display formats,
    explicit ambiguity handling, and a strictly read-only command."""

    def test_execstart_parses_despite_spaced_timestamps(self):
        text = _systemctl_show_text(
            fragment_path="/etc/systemd/system/trad-bot-scanner.service",
            exec_start_argv=[
                "/opt/trad_bot/.venv/bin/python",
                "/opt/trad_bot/scanner_runner.py",
            ],
            working_directory="/opt/trad_bot",
            environment_files=["/opt/trad_bot/.env"],
        )
        parsed = preflight._parse_effective_unit(text)
        assert parsed["available"] is True
        assert parsed["exec_start"]["argv"] == [
            "/opt/trad_bot/.venv/bin/python",
            "/opt/trad_bot/scanner_runner.py",
        ]
        assert parsed["exec_start"]["path"] == \
            "/opt/trad_bot/.venv/bin/python"
        assert parsed["working_directory"] == "/opt/trad_bot"
        assert parsed["environment_files"] == ["/opt/trad_bot/.env"]
        assert parsed["fragment_path"] == \
            "/etc/systemd/system/trad-bot-scanner.service"
        assert parsed["main_pid"] == 4242
        assert parsed["need_daemon_reload"] is False

    def test_execstart_path_field_is_not_whitespace_split(self):
        """The ``path=`` field may contain spaces: extraction is anchored
        on the `` ; argv[]=`` separator, never on whitespace, and the
        spaced timestamp must not leak into argv either."""
        text = _systemctl_show_text(
            fragment_path="/x.service",
            exec_start_argv=[
                "/opt/trad bot/.venv/bin/python",
                "/opt/trad bot/scanner_runner.py",
            ],
        )
        parsed = preflight._parse_effective_unit(text)
        assert parsed["available"] is True
        assert parsed["exec_start"]["path"] == \
            "/opt/trad bot/.venv/bin/python"
        argv = parsed["exec_start"]["argv"]
        assert " ".join(argv) == (
            "/opt/trad bot/.venv/bin/python /opt/trad bot/scanner_runner.py"
        )
        assert not any("MSK" in token or "2026" in token for token in argv)

    def test_environmentfiles_lines_and_optional_prefix(self):
        text = _systemctl_show_text(
            fragment_path="/x.service",
            exec_start_argv=["/p/py", "/p/scanner_runner.py"],
            environment_files=["/opt/trad_bot/.env", "-/opt/extra.env"],
        )
        parsed = preflight._parse_effective_unit(text)
        assert parsed["environment_files"] == [
            "/opt/trad_bot/.env", "/opt/extra.env",
        ]

    def test_dropinpaths_shell_quoted_list(self):
        text = _systemctl_show_text(
            fragment_path="/x.service",
            exec_start_argv=["/p/py", "/p/scanner_runner.py"],
        ).replace(
            "DropInPaths=\n",
            'DropInPaths=/etc/a.conf "/etc/b c.conf"\n',
        )
        parsed = preflight._parse_effective_unit(text)
        assert parsed["drop_in_paths"] == ["/etc/a.conf", "/etc/b c.conf"]

    def test_malformed_execstart_is_never_passed(self):
        parsed = preflight._parse_effective_unit(
            "LoadState=loaded\nExecStart={ not a record }\n",
        )
        assert parsed["available"] is False
        assert parsed["reason"] == "SCANNER_UNIT_EXEC_UNPARSABLE"

    def test_multiple_execstart_entries_are_ambiguous(self):
        text = _systemctl_show_text(
            fragment_path="/x.service",
            exec_start_argv=["/p/py", "/p/scanner_runner.py"],
        )
        exec_line = next(
            line for line in text.splitlines()
            if line.startswith("ExecStart=")
        )
        parsed = preflight._parse_effective_unit(text + exec_line + "\n")
        assert parsed["available"] is False
        assert parsed["reason"] == "SCANNER_UNIT_EXEC_AMBIGUOUS"

    def test_not_found_load_state_is_not_loaded(self):
        parsed = preflight._parse_effective_unit(_systemctl_show_text(
            fragment_path="", exec_start_argv=None, load_state="not-found",
            active_state="inactive", sub_state="dead", main_pid=0,
        ))
        assert parsed["available"] is False
        assert parsed["reason"] == "SCANNER_UNIT_NOT_LOADED"

    def test_query_is_a_read_only_systemctl_show(self):
        runner = _fake_systemctl_runner(_systemctl_show_text(
            fragment_path="/etc/systemd/system/trad-bot-scanner.service",
            exec_start_argv=["/p/py", "/p/scanner_runner.py"],
        ))
        parsed = preflight._query_effective_unit(
            Path("/etc/systemd/system/trad-bot-scanner.service"),
            runner=runner,
        )
        assert parsed["available"] is True
        assert len(runner.calls) == 1
        command, kwargs = runner.calls[0]
        assert command[:3] == [
            "systemctl", "show", "trad-bot-scanner.service",
        ]
        assert "--no-pager" in command
        properties = command[-1]
        for prop in (
            "LoadState", "ActiveState", "SubState", "MainPID",
            "FragmentPath", "DropInPaths", "NeedDaemonReload", "ExecStart",
            "WorkingDirectory", "EnvironmentFiles",
        ):
            assert prop in properties
        # No mutation verb may ever appear in the command:
        for verb in (
            "start", "stop", "restart", "reload", "daemon-reload",
            "enable", "disable", "mask", "kill", "reset-failed",
        ):
            assert verb not in command
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

    def test_query_failures_are_not_verified(self):
        failing = preflight._query_effective_unit(
            Path("/x.service"),
            runner=_fake_systemctl_runner("", returncode=1),
        )
        assert failing["available"] is False
        assert failing["reason"] == "SCANNER_SYSTEMCTL_SHOW_FAILED"
        raising = preflight._query_effective_unit(
            Path("/x.service"),
            runner=_fake_systemctl_runner(
                "", error=FileNotFoundError("systemctl"),
            ),
        )
        assert raising["available"] is False
        assert raising["reason"] == "SCANNER_SYSTEMCTL_UNAVAILABLE"

    def test_running_process_execstart_code_null_parses(self):
        """P1 regression — the ACTUAL production VPS `systemctl show`
        output for a LIVE (running) Scanner process ends with
        ``code=(null) ; status=0/0`` (the process has not exited yet).
        An over-strict ``code=\\w+`` pattern used to reject this healthy
        record as ``SCANNER_UNIT_EXEC_UNPARSABLE``; it must parse and
        keep the full path/argv extraction intact."""
        text = "\n".join([
            "LoadState=loaded",
            "ActiveState=active",
            "SubState=running",
            "MainPID=24680",
            "FragmentPath=/etc/systemd/system/trad-bot-scanner.service",
            "DropInPaths=/etc/systemd/system/trad-bot-scanner.service.d/"
            "20-scanner-release.conf",
            "NeedDaemonReload=no",
            "ExecStart={ path=/opt/trad_bot/.venv/bin/python ; "
            "argv[]=/opt/trad_bot/.venv/bin/python "
            "/opt/trad_bot_pr267_release/scanner_runner.py ; "
            "ignore_errors=no ; start_time=[Wed 2026-11-11 12:00:00 MSK] ; "
            "stop_time=[n/a] ; pid=24680 ; code=(null) ; status=0/0 }",
            "WorkingDirectory=/opt/trad_bot_pr267_release",
            "EnvironmentFiles=/opt/trad_bot/.env (ignore_errors=no)",
        ]) + "\n"
        parsed = preflight._parse_effective_unit(text)
        assert parsed["available"] is True, parsed
        assert parsed["exec_start"]["path"] == "/opt/trad_bot/.venv/bin/python"
        assert parsed["exec_start"]["argv"] == [
            "/opt/trad_bot/.venv/bin/python",
            "/opt/trad_bot_pr267_release/scanner_runner.py",
        ]
        assert parsed["working_directory"] == "/opt/trad_bot_pr267_release"
        assert parsed["environment_files"] == ["/opt/trad_bot/.env"]
        assert parsed["main_pid"] == 24680
        assert parsed["need_daemon_reload"] is False

    def test_running_process_execstart_code_null_end_to_end(self):
        """The same live record flows through the full query path: the
        effective unit is available (never SCANNER_UNIT_EXEC_UNPARSABLE)
        and the argv[0]/path pair is what the process check compares."""
        text = "\n".join([
            "LoadState=loaded",
            "ActiveState=active",
            "SubState=running",
            "MainPID=24680",
            "FragmentPath=/etc/systemd/system/trad-bot-scanner.service",
            "DropInPaths=",
            "NeedDaemonReload=no",
            "ExecStart={ path=/opt/trad_bot/.venv/bin/python ; "
            "argv[]=/opt/trad_bot/.venv/bin/python "
            "/opt/trad_bot_pr267_release/scanner_runner.py ; "
            "ignore_errors=no ; start_time=[Wed 2026-11-11 12:00:00 MSK] ; "
            "stop_time=[n/a] ; pid=24680 ; code=(null) ; status=0/0 }",
            "WorkingDirectory=/opt/trad_bot_pr267_release",
            "EnvironmentFiles=/opt/trad_bot/.env (ignore_errors=no)",
        ]) + "\n"
        parsed = preflight._query_effective_unit(
            Path("/etc/systemd/system/trad-bot-scanner.service"),
            runner=_fake_systemctl_runner(text),
        )
        assert parsed["available"] is True, parsed
        assert parsed["reason"] != "SCANNER_UNIT_EXEC_UNPARSABLE"
        assert parsed["exec_start"]["path"] == \
            parsed["exec_start"]["argv"][0]
