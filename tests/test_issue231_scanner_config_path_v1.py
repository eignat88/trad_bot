"""ISSUE231 — isolated Scanner-only configuration source
(``SCANNER_CONFIG_PATH``) regression suite.

Contract under test (maps 1:1 to the requirements):

1. DEFAULT SOURCE — with ``SCANNER_CONFIG_PATH`` unset the Scanner keeps
   loading the historical ``<repo>/config.yaml`` through the very same
   ``load_settings()`` call: byte-identical behaviour, limiter still OFF.
2. OVERRIDE SOURCE — when the variable is set to a valid ABSOLUTE path the
   Scanner loads that JSON file instead; its parameters win, every other
   parameter is preserved (the override is a full copy of the production
   config, never a minimal stub).
3. FAIL-CLOSED VALIDATION — empty string, relative path, missing file,
   directory, unreadable file and malformed JSON all raise
   ``ScannerConfigError``; there is NO implicit fallback to the shared
   ``config.yaml`` once an override was requested.
4. NO NETWORK BEFORE VALIDATION — config resolution happens before
   ``BybitClient`` is constructed, so a bad canary config can never issue a
   Bybit request.
5. CWD INDEPENDENCE — resolution never consults the process
   WorkingDirectory.
6. OS-NATIVE ABSOLUTE PATHS — the absolute check uses the native ``Path``
   flavour of the running OS, so Windows drive/UNC paths are rejected on
   POSIX and drive-less POSIX paths are rejected on Windows; a
   foreign-flavour value can never degrade into a relative path anchored
   to the WorkingDirectory.
7. ISOLATION — only ``scanner_runner.py`` reads ``SCANNER_CONFIG_PATH``;
   Paper/Research/HTF/Analytics keep the shared config, where the global
   Bybit rate limiter stays default-off.
8. LOGGING — startup logs name the source, path, limiter state, ENV
   permission and shared-state path, and never leak secrets.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

import scanner_runner
import app.exchange.bybit_client as bybit_client_module
from app.config.settings import load_settings
from app.exchange.global_rate_limiter import LimiterConfig
from scanner_runner import (
    CONFIG_SOURCE_DEFAULT,
    CONFIG_SOURCE_OVERRIDE,
    SCANNER_CONFIG_ENV,
    ScannerConfigError,
    _is_absolute_config_path,
    resolve_scanner_config_path,
)

ENV_FLAG = "GLOBAL_BYBIT_RATE_LIMITER_ENABLED"
FLAG = "global_bybit_rate_limiter_enabled"
ALLOW_ENV = "global_bybit_rate_limiter_allow_env"
STATE_PATH_KEY = "global_limiter_state_path"
REPO_ROOT = Path(__file__).resolve().parents[1]
CANARY_STATE_PATH = "/var/lib/trad_bot/state/global_bybit_limiter_v1.json"

#: Parameters a Scanner-only canary may change. Everything else must be
#: byte-identical to the shared production configuration.
CANARY_KEYS = {FLAG, ALLOW_ENV, STATE_PATH_KEY}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """The override and the limiter ENV gate must never leak in."""
    monkeypatch.delenv(SCANNER_CONFIG_ENV, raising=False)
    monkeypatch.delenv(ENV_FLAG, raising=False)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Poison the transport: any Bybit request fails the test immediately."""

    def _poisoned(*args, **kwargs):
        raise AssertionError("unexpected HTTP attempted (urlopen poisoned)")

    monkeypatch.setattr(bybit_client_module, "urlopen", _poisoned)


def _write_config(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _canary_payload() -> dict:
    """Full production config + the three canary parameters.

    The canary file must carry EVERY existing Scanner setting; replacing the
    production config with the minimal illustration JSON is forbidden.
    """
    payload = json.loads(
        (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")
    )
    payload[FLAG] = True
    payload[ALLOW_ENV] = False
    payload[STATE_PATH_KEY] = CANARY_STATE_PATH
    return payload


# ================================================== 1. default source (compat)
def test_env_unset_resolves_to_default_config():
    path, source = resolve_scanner_config_path()
    assert source == CONFIG_SOURCE_DEFAULT
    assert path == scanner_runner.CONFIG_PATH
    assert path == REPO_ROOT / "config.yaml"


def test_env_unset_keeps_existing_behaviour_and_limiter_off(caplog):
    """Backward compatibility: identical settings, limiter still OFF."""
    with caplog.at_level(logging.INFO, logger="scanner_runner"):
        settings = scanner_runner._load_runner_settings()

    assert settings.global_bybit_rate_limiter_enabled is False
    assert settings.global_bybit_rate_limiter_allow_env is False
    assert "source=DEFAULT" in caplog.text
    assert str(scanner_runner.CONFIG_PATH) in caplog.text
    # the historical fields must still be logged
    assert "universe_mode=" in caplog.text
    assert "env_file=" in caplog.text


def test_default_config_matches_direct_load_settings():
    """The DEFAULT path is the same call as before the patch."""
    direct = load_settings(
        path=scanner_runner.CONFIG_PATH, env_file=scanner_runner.ENV_PATH
    )
    via_runner = scanner_runner._load_runner_settings()
    assert dataclasses.asdict(via_runner) == dataclasses.asdict(direct)


# ==================================================== 2. override is honoured
def test_absolute_override_is_selected(tmp_path, monkeypatch, caplog):
    override = _write_config(tmp_path / "scanner_canary.json", _canary_payload())
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(override))

    path, source = resolve_scanner_config_path()
    assert source == CONFIG_SOURCE_OVERRIDE
    assert path == override

    with caplog.at_level(logging.INFO, logger="scanner_runner"):
        settings = scanner_runner._load_runner_settings()

    assert settings.global_bybit_rate_limiter_enabled is True
    assert "source=OVERRIDE" in caplog.text
    assert str(override) in caplog.text


def test_override_parameters_apply_and_everything_else_is_preserved(tmp_path,
                                                                   monkeypatch):
    override = _write_config(tmp_path / "scanner_canary.json", _canary_payload())
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(override))

    canary = scanner_runner._load_runner_settings()
    monkeypatch.delenv(SCANNER_CONFIG_ENV, raising=False)
    default = scanner_runner._load_runner_settings()

    assert canary.global_bybit_rate_limiter_enabled is True
    assert canary.global_bybit_rate_limiter_allow_env is False
    assert canary.global_limiter_state_path == CANARY_STATE_PATH

    canary_dict = dataclasses.asdict(canary)
    default_dict = dataclasses.asdict(default)
    changed = {
        key for key in default_dict if canary_dict[key] != default_dict[key]
    }
    # ``allow_env`` is explicitly ``false`` in the canary file and the
    # production default is already ``false``, so it may not appear in the
    # diff — but it must never differ from the expected canary value.
    assert changed <= CANARY_KEYS, (
        f"canary config changed more than the limiter keys: {sorted(changed)}"
    )
    assert FLAG in changed and STATE_PATH_KEY in changed
    assert canary.global_bybit_rate_limiter_allow_env is False
    # Spot-check the trading-relevant settings stayed production-identical.
    assert canary.symbols == default.symbols
    assert canary.trading_mode == default.trading_mode
    assert canary.blocked_scanner_directions == default.blocked_scanner_directions
    assert canary.scanner_regime_whitelist == default.scanner_regime_whitelist
    assert canary.expectancy_filter_enabled == default.expectancy_filter_enabled
    assert canary.dca == default.dca


def test_override_enables_limiter_for_the_scanner_only(tmp_path, monkeypatch):
    override = _write_config(tmp_path / "scanner_canary.json", _canary_payload())
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(override))

    settings = scanner_runner._load_runner_settings()
    assert settings.global_bybit_rate_limiter_enabled is True
    assert settings.global_bybit_rate_limiter_allow_env is False
    # The activation comes from the config file alone (two-factor ENV gate
    # not granted), and the resulting LimiterConfig is the enabled one.
    assert LimiterConfig.from_settings(settings).enabled is True

    # The shared/default source still reports OFF for every other consumer.
    monkeypatch.delenv(SCANNER_CONFIG_ENV, raising=False)
    default_settings = scanner_runner._load_runner_settings()
    assert default_settings.global_bybit_rate_limiter_enabled is False


# ============================================ 3. fail-closed override errors
def test_missing_override_fails_closed_without_fallback(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(missing))

    with pytest.raises(ScannerConfigError, match="does not exist"):
        resolve_scanner_config_path()
    with pytest.raises(ScannerConfigError, match=SCANNER_CONFIG_ENV):
        scanner_runner._load_runner_settings()


def test_directory_override_fails_closed(tmp_path, monkeypatch):
    directory = tmp_path / "a_directory"
    directory.mkdir()
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(directory))

    with pytest.raises(ScannerConfigError, match="is a directory"):
        resolve_scanner_config_path()


@pytest.mark.parametrize("value", ["", "   ", "\t", "\n "])
def test_empty_override_fails_closed(tmp_path, monkeypatch, value):
    monkeypatch.setenv(SCANNER_CONFIG_ENV, value)
    with pytest.raises(ScannerConfigError, match="empty"):
        resolve_scanner_config_path()


@pytest.mark.parametrize("value", [
    "config.json",
    "configs/scanner_canary.json",
    "./scanner_canary.json",
    "C:scanner_canary.json",
])
def test_relative_override_fails_closed(tmp_path, monkeypatch, value):
    monkeypatch.setenv(SCANNER_CONFIG_ENV, value)
    with pytest.raises(ScannerConfigError, match="absolute"):
        resolve_scanner_config_path()


# =================================== OS-native absolute path contract (PR #242)
#: Absolute only on a Windows host (drive + UNC forms).
WINDOWS_ABSOLUTE_PATHS = [
    r"C:\srv\trad_bot\scanner_canary.json",
    "C:/srv/trad_bot/scanner_canary.json",
    r"\\server\share\scanner_canary.json",
]
#: Absolute only on a POSIX host — rejected by Windows, where a drive-less
#: ``/srv/x`` resolves against the current drive (WorkingDirectory-dependent).
POSIX_ABSOLUTE_PATHS = ["/srv/trad_bot/scanner_canary.json"]
#: Relative on every OS.
RELATIVE_PATHS = [
    "config.json",
    "./scanner_canary.json",
    "configs/scanner_canary.json",
    "C:scanner_canary.json",
]

#: On this host: the flavour that is absolute elsewhere but not here.
if os.name == "nt":
    FOREIGN_ABSOLUTE_PATHS = POSIX_ABSOLUTE_PATHS
else:
    FOREIGN_ABSOLUTE_PATHS = WINDOWS_ABSOLUTE_PATHS


def test_absolute_check_is_exactly_native_path_is_absolute():
    """The predicate must equal ``Path(value).is_absolute()``.

    Regression for the blocking PR #242 finding: the previous
    ``PurePosixPath(...) or PureWindowsPath(...)`` check disagreed with the
    native ``Path`` actually built from the value, so a foreign-flavour
    path passed validation and then became a *relative* path anchored to
    the WorkingDirectory.
    """
    for value in WINDOWS_ABSOLUTE_PATHS + POSIX_ABSOLUTE_PATHS + RELATIVE_PATHS:
        assert _is_absolute_config_path(value) == Path(value).is_absolute(), value


@pytest.mark.parametrize("value", FOREIGN_ABSOLUTE_PATHS)
def test_foreign_absolute_path_rejected_on_this_os(value, monkeypatch):
    """A path absolute only for the OTHER OS fails before any filesystem I/O."""
    assert not _is_absolute_config_path(value)
    monkeypatch.setenv(SCANNER_CONFIG_ENV, value)
    with pytest.raises(ScannerConfigError, match="absolute"):
        resolve_scanner_config_path()


def test_windows_paths_rejected_when_running_on_posix(monkeypatch):
    """Simulate the Linux host: native flavour is ``PurePosixPath``.

    Proves the production (Linux) rule on any development machine: Windows
    drive and UNC paths are rejected instead of degrading to a relative
    ``PosixPath``.
    """
    monkeypatch.setattr(scanner_runner, "Path", PurePosixPath)
    for value in WINDOWS_ABSOLUTE_PATHS:
        assert not _is_absolute_config_path(value)
        with pytest.raises(ScannerConfigError, match="absolute"):
            scanner_runner.resolve_scanner_config_path(
                {SCANNER_CONFIG_ENV: value}
            )


def test_posix_paths_rejected_when_running_on_windows(monkeypatch):
    """Simulate the Windows host: native flavour is ``PureWindowsPath``.

    Drive-less POSIX paths would resolve against the current drive there,
    so they must be rejected as WorkingDirectory-dependent.
    """
    monkeypatch.setattr(scanner_runner, "Path", PureWindowsPath)
    for value in POSIX_ABSOLUTE_PATHS:
        assert not _is_absolute_config_path(value)
        with pytest.raises(ScannerConfigError, match="absolute"):
            scanner_runner.resolve_scanner_config_path(
                {SCANNER_CONFIG_ENV: value}
            )


def test_accepted_override_is_native_absolute(tmp_path, monkeypatch):
    """Legitimate native absolute paths keep working, unchanged."""
    override = _write_config(tmp_path / "scanner_canary.json", _canary_payload())
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(override))

    path, source = resolve_scanner_config_path()
    assert source == CONFIG_SOURCE_OVERRIDE
    assert isinstance(path, Path)          # native flavour, never a foreign pure path
    assert path.is_absolute()
    assert path == Path(str(override))     # identical to what will be opened
    # the DEFAULT source is native-absolute too (cwd-independent by design)
    assert isinstance(scanner_runner.CONFIG_PATH, Path)
    assert scanner_runner.CONFIG_PATH.is_absolute()


def test_malformed_json_override_fails_closed(tmp_path, monkeypatch):
    broken = tmp_path / "broken.json"
    broken.write_text('{"global_bybit_rate_limiter_enabled": tru', encoding="utf-8")
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(broken))

    with pytest.raises(ScannerConfigError) as excinfo:
        scanner_runner._load_runner_settings()
    assert str(broken) in str(excinfo.value)
    assert "source=OVERRIDE" in str(excinfo.value)
    # Fail-closed: never the production config, never a coerced flag.
    assert not isinstance(excinfo.value, type(None))


def test_unreadable_override_fails_closed(tmp_path, monkeypatch):
    locked = _write_config(tmp_path / "unreadable.json", _canary_payload())
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(locked))

    real_access = os.access

    def _deny_this_file(path, mode, *args, **kwargs):
        if Path(path) == locked and (mode & os.R_OK):
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(scanner_runner.os, "access", _deny_this_file)

    with pytest.raises(ScannerConfigError, match="not readable"):
        resolve_scanner_config_path()
    with pytest.raises(ScannerConfigError, match="not readable"):
        scanner_runner._load_runner_settings()


def test_scanner_config_error_is_a_value_error():
    """Fail-closed errors keep the historical ValueError contract."""
    assert issubclass(ScannerConfigError, ValueError)


# ======================================= 4. no Bybit work before validation
def test_main_fails_before_bybit_client_is_constructed(tmp_path, monkeypatch):
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(tmp_path / "missing.json"))
    monkeypatch.setattr(scanner_runner, "setup_logging", lambda: None)

    constructed: list[object] = []

    class _BombClient:
        def __init__(self, *args, **kwargs):
            constructed.append(args)
            raise AssertionError("BybitClient must not be constructed")

    monkeypatch.setattr(scanner_runner, "BybitClient", _BombClient)

    with pytest.raises(ScannerConfigError):
        scanner_runner.main()

    assert constructed == []


def test_main_loads_settings_before_constructing_client():
    """Source-level guard: the ordering above can never be inverted."""
    import inspect

    source = inspect.getsource(scanner_runner.main)
    load_at = source.index("_load_runner_settings()")
    client_at = source.index("BybitClient(")
    assert load_at < client_at


# ==================================================== 5. cwd independence
def test_resolution_is_cwd_independent(tmp_path, monkeypatch):
    override = _write_config(tmp_path / "scanner_canary.json", _canary_payload())
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(override))

    results = set()
    for cwd in (tmp_path, REPO_ROOT, scanner_runner.PROJECT_ROOT):
        monkeypatch.chdir(cwd)
        results.add(resolve_scanner_config_path())
    assert len(results) == 1
    assert results.pop() == (override, CONFIG_SOURCE_OVERRIDE)

    monkeypatch.delenv(SCANNER_CONFIG_ENV, raising=False)
    defaults = set()
    for cwd in (tmp_path, REPO_ROOT, scanner_runner.PROJECT_ROOT):
        monkeypatch.chdir(cwd)
        defaults.add(resolve_scanner_config_path())
    assert defaults == {(scanner_runner.CONFIG_PATH, CONFIG_SOURCE_DEFAULT)}


# ==================================================== 6. consumer isolation
def test_only_scanner_runner_reads_the_override_variable():
    """Paper/Research/HTF/Analytics must keep the shared config file."""
    forbidden: list[str] = []
    for root in ("app", "deploy", "scripts", "tools", "monitoring", "sql"):
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".service", ".sh"}:
                continue
            if SCANNER_CONFIG_ENV in path.read_text(encoding="utf-8",
                                                     errors="ignore"):
                forbidden.append(str(path.relative_to(REPO_ROOT)))
    for runner in ("paper_runner.py", "bot.py"):
        text = (REPO_ROOT / runner).read_text(encoding="utf-8", errors="ignore")
        if SCANNER_CONFIG_ENV in text:
            forbidden.append(runner)
    assert forbidden == [], f"non-Scanner consumers read {SCANNER_CONFIG_ENV}: {forbidden}"


def test_shared_config_stays_limiter_off_for_other_consumers(tmp_path):
    """The DEFAULT source (used by everyone else) never enables the limiter."""
    settings = load_settings(
        path=scanner_runner.CONFIG_PATH, env_file=tmp_path / ".env.absent"
    )
    assert settings.global_bybit_rate_limiter_enabled is False
    assert settings.global_bybit_rate_limiter_allow_env is False
    assert LimiterConfig.from_settings(settings).enabled is False


def test_scanner_config_env_is_not_an_activation_switch(tmp_path, monkeypatch):
    """Setting SCANNER_CONFIG_PATH alone must not switch the limiter on."""
    plain = _write_config(
        tmp_path / "plain.json",
        json.loads((REPO_ROOT / "config.yaml").read_text(encoding="utf-8")),
    )
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(plain))
    settings = scanner_runner._load_runner_settings()
    assert settings.global_bybit_rate_limiter_enabled is False


# ============================================================ 7. logging
def test_startup_log_reports_source_state_and_no_secrets(tmp_path, monkeypatch,
                                                         caplog):
    payload = _canary_payload()
    payload["db_password"] = "DB-SECRET-XYZ-123"
    override = _write_config(tmp_path / "scanner_canary.json", payload)
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(override))
    monkeypatch.setenv("BYBIT_API_KEY", "AK-SECRET-ABC-999")
    monkeypatch.setenv("BYBIT_API_SECRET", "AS-SECRET-DEF-777")

    with caplog.at_level(logging.INFO, logger="scanner_runner"):
        scanner_runner._load_runner_settings()

    # Required observability (source, path, limiter, ENV permission, state).
    assert "source=OVERRIDE" in caplog.text
    assert str(override) in caplog.text
    assert "global_limiter=ON" in caplog.text
    assert "allow_env=DENIED" in caplog.text
    assert f"state_path={CANARY_STATE_PATH}" in caplog.text
    assert "env_file=" in caplog.text

    # No secrets of any kind.
    assert "DB-SECRET-XYZ-123" not in caplog.text
    assert "AK-SECRET-ABC-999" not in caplog.text
    assert "AS-SECRET-DEF-777" not in caplog.text


def test_failure_log_names_source_without_file_contents(tmp_path, monkeypatch,
                                                       caplog):
    broken = tmp_path / "broken.json"
    broken.write_text('{"unexpected": ', encoding="utf-8")
    monkeypatch.setenv(SCANNER_CONFIG_ENV, str(broken))

    with caplog.at_level(logging.CRITICAL, logger="scanner_runner"):
        with pytest.raises(ScannerConfigError):
            scanner_runner._load_runner_settings()

    assert "scanner config load FAILED" in caplog.text
    assert "source=OVERRIDE" in caplog.text
    assert str(broken) in caplog.text
