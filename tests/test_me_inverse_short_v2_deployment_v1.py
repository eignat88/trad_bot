"""ME_LONG_V2_INVERSE_SHORT_V2 — P1-3 deployment readiness tests.

Covers, without touching any real database or systemd:

* the six-check preflight contract — every check is ``PASS`` / ``FAIL`` /
  ``NOT_VERIFIED``, ``NOT_VERIFIED`` never auto-passes, ``READY_FOR_
  ACTIVATION`` requires ALL six confirmed in one run, only read-only SQL
  ever reaches the (fake) connection, nothing is ever committed, and no
  credential/secret value can appear in the JSON report or the CLI
  output (exit code 0 only when ready);
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
import sys
import time
from pathlib import Path

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
def _write_scanner_unit(path: Path, root: Path, env_file: Path) -> Path:
    """Install a fixture `trad-bot-scanner.service` for ``root``."""
    r = root.as_posix().rstrip("/")
    path.write_text(
        "[Unit]\n"
        "Description=Trad Bot Scanner\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={r}\n"
        f"EnvironmentFile={env_file.as_posix()}\n"
        f"ExecStart={r}/.venv/bin/python {r}/scanner_runner.py\n"
        "Restart=on-failure\n"
        "User=tradbot\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    return path


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


def _make_scanner_checkout(base: Path, name: str = "scanner") -> Path:
    """A temporary Scanner checkout with the CURRENT sources."""
    root = base / name
    for rel in SOURCE_FILES:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((PROJECT_ROOT / rel).read_bytes())
    return root


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
         scanner_root: Path = PROJECT_ROOT, scanner_unit_path: Path | None = None,
         scanner_process=_PROCESS_AUTO):
    """run_preflight with fixture Scanner checkout/unit/process metadata.

    ``scanner_root`` defaults to the project's own sources (a real, wired
    Scanner checkout); ``scanner_unit_path`` defaults to the unit installed
    next to the env files, pointing at that checkout; ``scanner_process``
    defaults to matching metadata — scenarios override each ingredient.
    """
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


class TestPreflightCli:
    def test_main_returns_zero_only_when_ready(self, env_files, monkeypatch,
                                               capsys):
        runtime, scanner = env_files
        monkeypatch.setattr(
            "pg8000.dbapi.connect", _fake_connect_factory(_happy_db_results()),
        )
        unit = runtime.parent / "scanner-unit.service"
        # Effective-runtime metadata comes from fixtures — never from the
        # real systemd//proc of this test host (P2-4).
        monkeypatch.setattr(
            preflight, "_detect_scanner_process",
            lambda _unit_path=None: _scanner_process_fixture(
                PROJECT_ROOT, unit=unit,
            ),
        )
        code = preflight.main([
            "--env-file", str(runtime),
            "--scanner-env-file", str(scanner),
            "--scanner-root", str(PROJECT_ROOT),
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
