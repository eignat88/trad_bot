"""ME_LONG_V2_INVERSE_SHORT_V3 — deployment artifacts contract (Issue #285).

Local-artifact checks for the V3 preflight/activate tools, the systemd unit,
the env example and the operations runbook: they must exist, enforce the
frozen guard values, fail closed on wrong inputs, and carry V3-only identity
(preflight/activate/systemd/env).  No database is required; nothing is
deployed or activated by these tests.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT = ROOT / "scripts" / "me_inverse_short_v3_preflight.py"
ACTIVATE = ROOT / "scripts" / "me_inverse_short_v3_activate.py"
RUNBOOK = ROOT / "docs" / "me_inverse_short_v3_production_runtime_v1.md"
UNIT = ROOT / "deploy" / "systemd" / "trad-bot-me-inverse-short-v3-runtime.service"
ENV_EXAMPLE = ROOT / "deploy" / "env" / "me-inverse-short-v3-runtime.env.example"

PYTHON = sys.executable


class TestDeploymentArtifactsExist:
    @pytest.mark.parametrize("path", [PREFLIGHT, ACTIVATE, RUNBOOK, UNIT, ENV_EXAMPLE])
    def test_artifact_exists(self, path: Path):
        assert path.exists(), f"missing deployment artifact: {path}"


class TestPreflightTool:
    def test_preflight_static_checks_pass(self):
        result = subprocess.run(
            [PYTHON, str(PREFLIGHT), "--json"], capture_output=True, text=True,
            cwd=str(ROOT), timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["ok"] is True
        names = {c["check"] for c in report["checks"]}
        assert {
            "MIGRATION_090_PRESENT_AND_UNIQUE",
            "GUARD_CONFIG_STRICT",
            "FINGERPRINT_PIN",
            "PROTOCOL_PARITY_V2",
            "SOURCE_CAPTURE_READY",
            "RUNTIME_ARTIFACTS",
            "ISOLATION_VALID",
        } <= names
        for check in report["checks"]:
            assert check["status"] in ("PASS", "WARN", "SKIP"), check

    def test_preflight_refuses_foreign_env_keys(self, tmp_path):
        env_file = tmp_path / "env"
        env_file.write_text(
            "DB_HOST=h\nDB_PORT=1\nDB_NAME=d\nDB_USER=u\nDB_PASSWORD=p\n"
            "ME_INVERSE_SHORT_V2_ENABLED=1\n",
            "utf-8",
        )
        result = subprocess.run(
            [PYTHON, str(PREFLIGHT), "--env-file", str(env_file)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        )
        assert result.returncode == 1
        assert "foreign contour key" in result.stderr

    def test_preflight_refuses_missing_db_credentials(self, tmp_path):
        env_file = tmp_path / "env"
        env_file.write_text("DB_HOST=h\n", "utf-8")
        result = subprocess.run(
            [PYTHON, str(PREFLIGHT), "--env-file", str(env_file)],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        )
        assert result.returncode == 1
        assert "missing" in result.stderr

    def test_read_service_env_require_disabled(self, tmp_path):
        sys.path.insert(0, str(ROOT))
        from scripts.me_inverse_short_v3_preflight import (
            PreflightRefused,
            read_service_env,
        )

        env_file = tmp_path / "env"
        env_file.write_text(
            "DB_HOST=h\nDB_PORT=1\nDB_NAME=d\nDB_USER=u\nDB_PASSWORD=p\n"
            "ME_INVERSE_SHORT_V3_ENABLED=1\n",
            "utf-8",
        )
        with pytest.raises(PreflightRefused, match="FIRST factor"):
            read_service_env(env_file, require_disabled=True)
        values = read_service_env(env_file)
        assert values["DB_USER"] == "u"


class TestActivateTool:
    def _module(self):
        import importlib

        sys.path.insert(0, str(ROOT))
        return importlib.import_module("scripts.me_inverse_short_v3_activate")

    def test_plan_activation_requires_the_v2_drain_precondition(self):
        mod = self._module()
        now = datetime.now(timezone.utc)
        kwargs = dict(
            current_fingerprint="f" * 64,
            freeze_ts=now - timedelta(minutes=1),
            existing_status="READY_TO_START",
            existing_fingerprint=None,
            existing_freeze=None,
            re_freeze=False,
            note="",
        )
        # V2 still admitting -> REFUSED (Issue #285 V2->V3 drain semantics).
        with pytest.raises(mod.ActivationError, match="V2 activation"):
            mod.plan_activation(v2_status="RUNNING", **kwargs)
        # V2 drained -> the plan is the atomic activation UPDATE.
        sql, params = mod.plan_activation(v2_status="DRAINING", **kwargs)
        assert params[0] == "RUNNING"
        assert params[4] == mod.EXPERIMENT_ID

    def test_parse_freeze_ts_rejects_future_and_naive(self):
        mod = self._module()
        now = datetime.now(timezone.utc)
        with pytest.raises(mod.ActivationError, match="future"):
            mod.parse_freeze_ts(
                (now + timedelta(hours=1)).isoformat(), now=now)
        with pytest.raises(mod.ActivationError, match="timezone-aware"):
            mod.parse_freeze_ts("2026-11-10T12:00:00", now=now)
        parsed = mod.parse_freeze_ts(
            (now - timedelta(minutes=1)).isoformat(), now=now)
        assert parsed.tzinfo is not None

    def test_activate_cli_requires_env_file_for_writes(self):
        result = subprocess.run(
            [PYTHON, str(ACTIVATE), "--activate", "--freeze-ts",
             datetime.now(timezone.utc).isoformat()],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        )
        assert result.returncode == 1
        assert "env-file is required" in result.stderr


class TestSystemdAndEnvIsolation:
    def test_unit_is_v3_only_and_off_by_default(self):
        unit = UNIT.read_text(encoding="utf-8")
        assert "me_long_v2_inverse_short_v3.production_runtime" in unit
        assert "me-inverse-short-v3-runtime.env" in unit
        assert "me-inverse-short-v2-runtime.env" not in unit
        assert "me_inverse_short_v2_release" not in unit
        assert "RestartPreventExitStatus=3" in unit

    def test_env_example_ships_disabled_and_documented_guards(self):
        env = ENV_EXAMPLE.read_text(encoding="utf-8")
        assert "ME_INVERSE_SHORT_V3_ENABLED=0" in env
        assert "ME_INVERSE_SHORT_V2_ENABLED" not in env
        assert "ME_INVERSE_SHORT_OOS2_ENABLED" not in env
        assert "MAX_OPEN_POSITIONS=12" in env
        assert "SYMBOL_ENTRY_COOLDOWN_HOURS=12" in env


class TestRunbook:
    def test_runbook_documents_migration_fingerprint_and_guards(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        assert "090_me_long_v2_inverse_short_v3.sql" in text
        assert "c10eb00f2777c2ca9b42d465ccbad4dc7908d86676b678fddcff5722f6f80aa4" in text
        assert "OPEN V2 + OPEN V3 <= 12" in text
        assert "12h per-symbol entry cooldown" in text

    def test_runbook_documents_rollback_and_drain(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        assert "Rollback" in text
        assert "DRAINING" in text
        assert "--drain" in text
        # Rollback must be V3-scoped: V2 is never stopped by V3 rollback.
        assert "V2 is never stopped, rolled back or modified" in text

    def test_runbook_documents_preflight_and_telemetry(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        assert "me_inverse_short_v3_preflight.py" in text
        assert "entry_guard" in text
        assert "ME_INVERSE_SHORT_V3_SOURCE_CAPTURE_ERROR" in text
