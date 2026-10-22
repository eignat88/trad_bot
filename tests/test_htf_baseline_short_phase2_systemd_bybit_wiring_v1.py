"""Issue #184 mandatory test matrix (13): systemd Bybit wiring + activation
fail-closed hardening for the HTF Phase-2 observer unit.

The unit must launch the runner with an EXPLICIT ``--candle-source bybit``,
keep every activation gate, stay disabled by default and stay isolated from
scanner/paper/evaluator. Checks 1-8 verify unit content AND (where possible)
the real CLI behavior; checks 9-13 are functional and drive the REAL runner
entry point and the REAL fail-closed gates — never a string-search substitute.

Everything here is deterministic and offline: the Bybit client and the
database layer are replaced with dummies/spies and nothing touches the
network, PostgreSQL or the VPS.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.research.htf_baseline_short_phase2_observer_runner_v1 as mod
import app.research.htf_baseline_short_phase2_persistence_v1 as persistence
import app.research.htf_keylevel_point_b as point_b
from app.models import Candle
from app.research.htf_baseline_short_phase2_observer_runner_v1 import (
    EXIT_NOT_RUNNABLE,
    EXIT_OK,
    DbCredentialsError,
    FileCandleSource,
    InMemoryWriter,
    NotRunnableError,
    Phase2ObserverRunner,
    RunnerConfig,
    build_boundary,
    build_candle_source,
    load_activation_config,
    load_db_credentials,
    main,
    require_runnable,
)

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "systemd" / "trad-bot-htf-baseline-short-observer.service"
UNIT_MODULE = "app.research.htf_baseline_short_phase2_observer_runner_v1"
EXPECTED_ACTIVATION_CONFIG = "/opt/trad_bot/etc/htf_phase2_activation.json"
EXPECTED_ENV_FILE = "/opt/trad_bot/etc/htf_phase2_observer.env"
EXPECTED_FLAG_FILE = "/opt/trad_bot/etc/htf_phase2_observer_enabled"
EXPERIMENT_ID = "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1"
FREEZE = "2030-01-01T00:00:00Z"


# ------------------------------------------------------------------ helpers


def unit_text() -> str:
    return UNIT.read_text(encoding="utf-8")


def exec_start() -> str:
    lines = [
        line.strip()
        for line in unit_text().splitlines()
        if line.strip().startswith("ExecStart=")
    ]
    assert len(lines) == 1, f"expected exactly one ExecStart directive, got {lines}"
    return lines[0][len("ExecStart="):]


def exec_start_args() -> list[str]:
    """The exact CLI argv the unit hands to the runner (tokens after `-m mod`)."""
    tokens = exec_start().split()
    assert "-m" in tokens, "ExecStart must run the runner via -m"
    assert UNIT_MODULE in tokens, "ExecStart must run the Phase-2 observer runner"
    return tokens[tokens.index(UNIT_MODULE) + 1:]


class _FakeConn:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class _StubRunner:
    constructions = 0
    run_loop_result = 0

    def __init__(self, *args, **kwargs):
        type(self).constructions += 1

    def install_signal_handlers(self):
        pass

    def run_loop(self):
        return type(self).run_loop_result


class _DummySettings:
    pass


class _DummyBybitClient:
    """Offline stand-in: construction must never touch the network."""

    def __init__(self, settings=None, session=None):
        self.settings = settings


def _wire_offline(monkeypatch, *, stub_gate=True, stub_runner=True):
    """Replace every side-effectful dependency of ``main()`` with spies.

    The real ``build_candle_source``, the real activation-config loader (unless
    stubbed) and the real fail-closed gate logic stay in charge; only the
    network client, the database connection and the observation writer are
    replaced by recorders.
    """
    seen = {
        "source": None,
        "args": None,
        "config": None,
        "activation_path": None,
        "connects": 0,
        "writers": 0,
        "observations": 0,
    }

    monkeypatch.setattr("app.config.Settings", _DummySettings)
    monkeypatch.setattr("app.exchange.bybit_client.BybitClient", _DummyBybitClient)

    real_build = mod.build_candle_source

    def spy_build(args, config):
        source = real_build(args, config)
        seen["source"] = type(source).__name__
        seen["args"] = args
        seen["config"] = config
        return source

    monkeypatch.setattr(mod, "build_candle_source", spy_build)

    def spy_connect():
        seen["connects"] += 1
        return _FakeConn()

    monkeypatch.setattr(mod, "connect_db_from_env", spy_connect)

    def spy_writer_factory(conn):
        seen["writers"] += 1

        class _Recorder:
            def emit(self, events, boundary):
                seen["observations"] += len(events)
                return SimpleNamespace(inserted=0, deduplicated=0, rejected=0)

        return _Recorder()

    monkeypatch.setattr(persistence, "Phase2ObservationWriter", spy_writer_factory)

    if stub_gate:
        def spy_load_config(path):
            seen["activation_path"] = str(path)
            return {
                "experiment_id": EXPERIMENT_ID,
                "freeze_ts": FREEZE,
                "protocol_fingerprint": "0" * 64,
            }

        monkeypatch.setattr(mod, "load_activation_config", spy_load_config)
        monkeypatch.setattr(
            mod,
            "require_runnable",
            lambda db_conn, config, *, activation, protocol_freeze_ts=None: "boundary",
        )
    if stub_runner:
        _StubRunner.constructions = 0
        _StubRunner.run_loop_result = 0
        monkeypatch.setattr(mod, "Phase2ObserverRunner", _StubRunner)
    return seen


# ---------------------------- 1. explicit Bybit source (content + functional)


def test_unit_exec_start_selects_bybit_candle_source(monkeypatch):
    assert "--candle-source" in exec_start_args()
    args = exec_start_args()
    assert args[args.index("--candle-source") + 1] == "bybit"

    # functional: the runner accepts the EXACT unit argv and builds the Bybit
    # source (the offline dummy client replaces the network client only)
    seen = _wire_offline(monkeypatch)
    assert main(exec_start_args()) == 0
    assert seen["source"] == "BybitPhase2CandleSource"


# ------------------------------------ 2. --use-registration is passed (both)


def test_unit_passes_use_registration(monkeypatch):
    assert "--use-registration" in exec_start_args()

    seen = _wire_offline(monkeypatch)
    assert main(exec_start_args()) == 0
    assert seen["args"].use_registration is True
    assert seen["config"].dry_run is False  # production gate path, not dry-run


# --------------------------- 3. --activation-config with the expected path


def test_unit_activation_config_path_expected(monkeypatch):
    args = exec_start_args()
    assert "--activation-config" in args
    assert args[args.index("--activation-config") + 1] == EXPECTED_ACTIVATION_CONFIG

    seen = _wire_offline(monkeypatch)
    assert main(exec_start_args()) == 0
    assert seen["activation_path"] == EXPECTED_ACTIVATION_CONFIG


# --------------------------------- 4. dedicated EnvironmentFile + credentials


def test_unit_uses_dedicated_environment_file(monkeypatch):
    env_lines = [
        line.strip()
        for line in unit_text().splitlines()
        if line.strip().startswith("EnvironmentFile=")
    ]
    assert env_lines == [f"EnvironmentFile={EXPECTED_ENV_FILE}"]

    # functional isolation: shared/scanner credential names can never satisfy
    # the observer credential loader
    for var in (
        "HTF_P2_DB_USER", "HTF_P2_DB_NAME", "HTF_P2_DB_PASSWORD",
        "HTF_P2_DB_PASSWORD_FILE", "HTF_P2_DB_HOST", "HTF_P2_DB_PORT",
        "DB_USER", "DB_PASSWORD", "PGPASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DB_USER", "trad_bot")
    monkeypatch.setenv("DB_PASSWORD", "scanner-pw")
    monkeypatch.setenv("PGPASSWORD", "scanner-pw")
    with pytest.raises(DbCredentialsError):
        load_db_credentials()

    # dedicated role naming is enforced; shared/admin roles are refused
    for shared_role in ("postgres", "trad_bot", "analytics_runner"):
        monkeypatch.setenv("HTF_P2_DB_USER", shared_role)
        monkeypatch.setenv("HTF_P2_DB_NAME", "trad_bot")
        monkeypatch.setenv("HTF_P2_DB_PASSWORD", "irrelevant")
        with pytest.raises(DbCredentialsError):
            load_db_credentials()

    # positive control: the dedicated observer role loads without any network
    monkeypatch.setenv("HTF_P2_DB_USER", "htf_phase2_observer")
    creds = load_db_credentials()
    assert creds["user"] == "htf_phase2_observer"


# --------------------------------------------- 5. ConditionPathExists (gate)


def test_unit_keeps_condition_path_exists_flag_gate():
    unit = unit_text()
    assert f"ConditionPathExists={EXPECTED_FLAG_FILE}" in unit
    # the gate belongs to [Unit] and is evaluated before ExecStart ever runs
    unit_section = unit.split("[Service]", 1)[0]
    assert f"ConditionPathExists={EXPECTED_FLAG_FILE}" in unit_section


# ------------------------------------------ 6. shared .env never referenced


def test_unit_never_loads_shared_env():
    unit = unit_text()
    assert "/opt/trad_bot/.env" not in unit
    assert "EnvironmentFile=-" not in unit  # no optional fallback directive
    # no hardcoded secrets on any active (non-comment) line
    for line in unit.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert not re.search(
            r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*=\s*\S", stripped
        ), f"possible inline secret in unit line: {stripped!r}"


# ------------------------------------------- 7. restart policy (fail-closed)


def test_unit_restart_policy_matches_exit_code_contract():
    unit = unit_text()
    assert "Restart=on-failure" in unit
    assert "RestartPreventExitStatus=3" in unit
    assert "Restart=always" not in unit
    # exit 3 is the runner's EXIT_NOT_RUNNABLE: the fail-closed refusal code
    assert EXIT_NOT_RUNNABLE == 3
    assert "TimeoutStopSec=60" in unit
    assert "KillSignal=SIGTERM" in unit
    assert "UMask=0077" in unit


# ------------------------------- 8. no ExecStartPre / no automatic artifacts


def test_unit_has_no_exec_start_pre_automation(monkeypatch, tmp_path):
    unit = unit_text()
    assert "ExecStartPre" not in unit
    assert "ExecStartPost" not in unit
    assert "touch " not in unit  # nothing creates the flag file from the unit

    # functional: a refused startup run creates no flag/freeze/activation or
    # checkpoint artifacts at all (the checkpoint override points into tmp)
    seen = _wire_offline(monkeypatch, stub_gate=False)
    missing = tmp_path / "missing_activation.json"
    code = main(
        exec_start_args()
        + ["--activation-config", str(missing), "--checkpoint", str(tmp_path / "state.json")]
    )
    assert code == EXIT_NOT_RUNNABLE
    assert list(tmp_path.iterdir()) == []  # not even the checkpoint was created
    assert seen["writers"] == 0


# ------------------------ 9. missing activation config -> fail-closed (real)


def test_missing_activation_config_fails_closed_no_writes(monkeypatch, tmp_path):
    missing = tmp_path / "no_such_activation.json"
    with pytest.raises(NotRunnableError):
        load_activation_config(missing)  # the REAL loader refuses

    seen = _wire_offline(monkeypatch, stub_gate=False)
    code = main(
        exec_start_args()
        + ["--activation-config", str(missing), "--checkpoint", str(tmp_path / "state.json")]
    )
    assert code == EXIT_NOT_RUNNABLE  # fail-closed, not a crash
    assert seen["connects"] == 0  # no database session was ever opened
    assert seen["writers"] == 0  # no observation writer was ever constructed
    assert seen["observations"] == 0  # zero OOS observations
    assert list(tmp_path.iterdir()) == []


# --------------- 10. freeze / fingerprint mismatch -> refusal (real gate fn)


def _fake_reads(monkeypatch, *, state_fingerprint, database_freeze, observer_enabled=True):
    state = persistence.ExperimentState(
        experiment_id=EXPERIMENT_ID,
        status="RUNNING",
        freeze_ts=FREEZE,
        protocol_fingerprint=state_fingerprint,
        observer_enabled=observer_enabled,
    )
    monkeypatch.setattr(persistence, "load_experiment_state", lambda conn, exp=EXPERIMENT_ID: state)
    monkeypatch.setattr(
        persistence,
        "freeze_sources",
        lambda conn, exp=EXPERIMENT_ID: {
            "database": database_freeze,
            "observer_enabled": observer_enabled,
        },
    )


def test_freeze_or_fingerprint_mismatch_refuses_startup(monkeypatch):
    config = RunnerConfig()
    fingerprint = config.protocol_fingerprint

    def activation(**overrides):
        data = {
            "experiment_id": EXPERIMENT_ID,
            "freeze_ts": FREEZE,
            "protocol_fingerprint": fingerprint,
        }
        data.update(overrides)
        return data

    # ---- positive control: all three authorities agree -> boundary is built
    _fake_reads(monkeypatch, state_fingerprint=fingerprint, database_freeze=FREEZE)
    boundary = require_runnable(
        None, config, activation=activation(), protocol_freeze_ts=FREEZE
    )
    assert boundary.experiment_id == EXPERIMENT_ID

    # ---- every disagreement or missing source must refuse (fail closed)
    refusal_cases = {
        "runtime code fingerprint != registration": dict(
            fake=dict(state_fingerprint="1" * 64, database_freeze=FREEZE),
            activation=activation(),
        ),
        "activation config fingerprint outdated": dict(
            fake=dict(state_fingerprint=fingerprint, database_freeze=FREEZE),
            activation=activation(protocol_fingerprint="2" * 64),
        ),
        "activation config experiment_id mismatch": dict(
            fake=dict(state_fingerprint=fingerprint, database_freeze=FREEZE),
            activation=activation(experiment_id="SOMETHING_ELSE"),
        ),
        "activation freeze != database freeze": dict(
            fake=dict(state_fingerprint=fingerprint, database_freeze="2031-06-06T06:06:06Z"),
            activation=activation(),
        ),
        "database freeze authority empty": dict(
            fake=dict(state_fingerprint=fingerprint, database_freeze=None),
            activation=activation(),
        ),
        "protocol freeze authority empty": dict(
            fake=dict(state_fingerprint=fingerprint, database_freeze=FREEZE),
            activation=activation(),
            kwargs=dict(protocol_freeze_ts=""),
        ),
        "runtime activation config missing": dict(
            fake=dict(state_fingerprint=fingerprint, database_freeze=FREEZE),
            activation=None,
        ),
    }
    for name, case in refusal_cases.items():
        _fake_reads(monkeypatch, **case["fake"])
        with pytest.raises(NotRunnableError):
            require_runnable(
                None,
                config,
                activation=case["activation"],
                protocol_freeze_ts=case.get("kwargs", {}).get("protocol_freeze_ts", FREEZE),
            ), name


# -------------------- 11. unauthorized start creates NO OOS observations


def test_unauthorized_start_creates_no_oos_observations(monkeypatch, tmp_path):
    """Installing/starting the unit without operator authorization must fail
    closed BEFORE any observation exists — verified at every observable layer:
    no DB connection, no observation writer, no runner loop, no files."""
    missing = tmp_path / "missing_activation.json"
    seen = _wire_offline(monkeypatch, stub_gate=False, stub_runner=True)

    code = main(
        exec_start_args()
        + ["--activation-config", str(missing), "--checkpoint", str(tmp_path / "state.json")]
    )
    assert code == EXIT_NOT_RUNNABLE
    assert seen["source"] == "BybitPhase2CandleSource"  # source alone is harmless
    assert seen["connects"] == 0  # no DB session -> nothing can be written
    assert seen["writers"] == 0  # Phase2ObservationWriter never constructed
    assert seen["observations"] == 0  # zero OOS observations
    assert mod.Phase2ObserverRunner.constructions == 0  # no observer loop started
    assert list(tmp_path.iterdir()) == []  # no artifacts created


# ------------------ 12. scanner / paper / evaluator settings are unchanged


def test_scanner_paper_evaluator_settings_unchanged():
    result = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    base = result.stdout.strip()
    assert base, "cannot resolve PR merge-base"
    result = subprocess.run(
        ["git", "diff", "--name-only", base],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    forbidden = re.compile(
        r"(scanner|paper|live|evaluator|config\.yaml|app/trading|app/execution)",
        re.IGNORECASE,
    )
    offenders = [path for path in changed if forbidden.search(path)]
    assert offenders == [], (
        f"files outside the allowed surface changed (scanner/paper/live/evaluator): {offenders}"
    )
    # explicit named artifacts the observer must never touch
    for protected in ("config.yaml", "app/research/prospective_registry.json"):
        assert protected not in changed, f"protected artifact modified: {protected}"


# ------------------------------------- 13. CSV offline mode still works


BAR = 300_000
HOUR = 3_600_000
HOUR0 = 1_759_996_800_000


def _make_candles() -> list[Candle]:
    """68 hours of 5m bars with exactly one causal 1H resistance level
    (same frozen fixture geometry as the Phase-2 runtime tests)."""
    highs = [100.0] * 68
    for i in (44, 52):
        highs[i] = 109.9
    highs[56] = 110.0
    for i in (61, 62, 65, 66):
        highs[i] = 110.0
    candles: list[Candle] = []
    for hour_index, high in enumerate(highs):
        for bar_index in range(12):
            ts = HOUR0 + hour_index * HOUR + bar_index * BAR
            touch_hour = high >= 109.5
            candles.append(
                Candle(ts, 100.0, high, 99.0, 100.0 if not touch_hour else 109.5, 1.0)
            )
    return candles


def test_csv_is_still_the_python_default(tmp_path, monkeypatch):
    """The CSV default must NOT change: offline replay keeps working."""
    data_dir = tmp_path / "candles"
    data_dir.mkdir()
    # namespace WITHOUT candle_source -> the frozen csv default applies
    source = build_candle_source(SimpleNamespace(data_dir=str(data_dir)), RunnerConfig())
    assert isinstance(source, FileCandleSource)

    # the CLI contract is preserved: csv without --data-dir fails closed
    with pytest.raises(NotRunnableError):
        build_candle_source(SimpleNamespace(data_dir=None), RunnerConfig())


def test_csv_offline_replay_is_deterministic(tmp_path, monkeypatch):
    data_dir = tmp_path / "candles"
    data_dir.mkdir()
    candles = _make_candles()
    with (data_dir / "BTCUSDT_5m.csv").open("w", encoding="utf-8", newline="") as handle:
        handle.write("timestamp,open,high,low,close,volume\n")
        for c in candles:
            handle.write(f"{c.timestamp},{c.open},{c.high},{c.low},{c.close},{c.volume}\n")
    # frozen detector stub: always confirm at the latest closed bar
    monkeypatch.setattr(
        point_b, "_find_reaction_end", lambda **kwargs: (len(kwargs["closed"]) - 1, 0),
    )
    boundary = build_boundary(
        experiment_id=EXPERIMENT_ID,
        protocol_freeze_ts="2025-01-01T00:00:00Z",
        registry_freeze_ts="2025-01-01T00:00:00Z",
        database_freeze_ts="2025-01-01T00:00:00Z",
        protocol_fingerprint_hex="0" * 64,
    )

    def run_once(name):
        writer = InMemoryWriter()
        runner = Phase2ObserverRunner(
            RunnerConfig(
                experiment_id=EXPERIMENT_ID,
                symbols=("BTCUSDT",),
                checkpoint_path=str(tmp_path / name),
                warmup_bars=300,
            ),
            boundary=boundary,
            source=FileCandleSource(data_dir),
            writer=writer,
        )
        assert runner.run_once() == EXIT_OK
        return writer.rows

    first = run_once("state_a.json")
    second = run_once("state_b.json")
    assert first  # the fixture produces observations offline
    assert first == second  # byte-for-byte deterministic on identical input
