"""Issue #176 Phase-2 mandatory test matrix (10): isolation — the Phase-2
repair must not change Point-B, paper, live, scanner or any frozen artifact."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE_SHA = "4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba"

# artifacts that must remain byte-identical to the audited base commit
FROZEN_PATHS = (
    "app/research/htf_keylevel_point_b.py",
    "app/research/prospective_observer.py",
    "app/research/prospective_registry.json",
    "app/research/htf_baseline_short_multi_level_lifecycle_v1.py",
    "app/research/htf_baseline_short_stateful_episode_v1.py",
    "app/research/htf_baseline_short_causal_level_adapter_v1.py",
    "app/research/htf_keylevel_baseline_short_causal_observer_v1.py",
    "tools/research/htf_baseline_short_multi_level_replay_v1.py",
)

ALLOWED_NEW_PREFIXES = (
    "app/research/htf_baseline_short_phase2_",
    "tests/test_htf_baseline_short_phase2_",
    "docs/research/HTF_BASELINE_SHORT_PHASE2_",
    "deploy/systemd/trad-bot-htf-baseline-short-observer.service",
    "sql/migrations/074_htf_baseline_short_phase2_registration.sql",
    "sql/migrations/075_htf_baseline_short_phase2_privilege_hardening.sql",
    "tools/research/htf_baseline_short_phase2_",
)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        pytest.skip(f"git unavailable: {result.stderr.strip()[:200]}")
    return result.stdout


def test_frozen_point_b_stack_byte_identical_to_base():
    # compare through git itself (handles eol smudge filters); empty diff = identical
    diff = _git("diff", "--name-only", BASE_SHA, "--", *FROZEN_PATHS)
    changed = [line.strip() for line in diff.splitlines() if line.strip()]
    assert changed == [], f"frozen artifacts modified: {changed}"


def test_no_tracked_file_modified_against_base():
    diff = _git("diff", "--name-status", BASE_SHA)
    changed = [
        line.strip()
        for line in diff.splitlines()
        if line.strip() and line.strip()[0] in {"M", "D", "R", "C", "T"}
    ]
    assert changed == [], f"tracked files modified/deleted vs base: {changed}"


def test_new_files_are_confined_to_phase2_surface():
    new_paths: set[str] = set()
    status = _git("status", "--porcelain")
    for line in status.splitlines():
        if line.startswith("??"):
            new_paths.add(line[3:].strip().strip('"'))
    diff = _git("diff", "--name-status", BASE_SHA)
    for line in diff.splitlines():
        if line.startswith("A"):
            new_paths.add(line[2:].strip().strip('"'))
    unexpected = [
        path
        for path in sorted(new_paths)
        if not path.startswith(".cache/") and not path.startswith(ALLOWED_NEW_PREFIXES)
    ]
    assert unexpected == [], f"files outside the Phase-2 surface: {unexpected}"


def test_migration_touches_only_new_phase2_tables():
    sql = (
        ROOT / "sql" / "migrations" / "074_htf_baseline_short_phase2_registration.sql"
    ).read_text(encoding="utf-8")
    # only CREATE on the new Phase-2 tables; no ALTER/DROP/TRUNCATE anywhere
    creates = re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?([\w.]+)", sql)
    assert set(creates) == {
        "research.htf_baseline_short_phase2_experiment",
        "research.htf_baseline_short_phase2_observation",
        "research.htf_baseline_short_phase2_freeze_registry",
    }
    upper = sql.upper()
    for forbidden in ("ALTER TABLE", "DROP TABLE", "TRUNCATE", "DELETE FROM", "UPDATE RESEARCH.PROSPECTIVE"):
        assert forbidden not in upper, f"migration contains {forbidden}"
    # Point-B storage untouched
    assert "prospective_observation" not in sql
    assert "prospective_experiment" not in sql
    assert "research.prospective" not in sql


def test_no_phase2_module_imports_paper_or_live():
    offenders = []
    for path in (ROOT / "app" / "research").glob("htf_baseline_short_phase2_*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("paper", "live_trading", "app.trading", "app.execution", "order", "position"):
            if re.search(rf"^\s*(import|from)\s+.*{needle}", text, re.MULTILINE):
                offenders.append(f"{path.name}: {needle}")
    assert offenders == []


def test_registry_unchanged_and_no_phase2_experiment_registered():
    import json

    registry_path = ROOT / "app" / "research" / "prospective_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    text = registry_path.read_text(encoding="utf-8")
    assert "HTF_KEYLEVEL_BASELINE_SHORT_CAUSAL_OBSERVER_V1" not in text
    experiments = registry if isinstance(registry, list) else registry.get("experiments", [])
    point_b = {
        entry.get("experiment_id"): entry.get("freeze_ts")
        for entry in experiments
        if entry.get("experiment_id", "").startswith("HTF_KEYLEVEL")
    }
    assert point_b  # Point-B entries still present
    assert all(ts == "2026-10-06T14:00:00Z" for ts in point_b.values())  # freeze intact
    # Phase-2 registration lives only in its dedicated migration, as PENDING
    migration = (
        ROOT / "sql" / "migrations" / "074_htf_baseline_short_phase2_registration.sql"
    ).read_text(encoding="utf-8")
    assert "'PENDING', NULL, FALSE" in migration


def test_no_service_or_activation_artifact_activated():
    unit = (
        ROOT / "deploy" / "systemd" / "trad-bot-htf-baseline-short-observer.service"
    ).read_text(encoding="utf-8")
    assert "ConditionPathExists=" in unit  # disabled by default gate present
    assert "DISABLED BY DEFAULT" in unit  # description declares the default
    assert "WantedBy=multi-user.target" in unit
    # nothing auto-enables the unit
    assert "systemctl enable" not in unit.lower()


def test_point_b_freeze_timestamp_never_reused_in_phase2_code():
    offenders = []
    for path in list((ROOT / "app" / "research").glob("htf_baseline_short_phase2_*.py")) + [
        ROOT / "sql" / "migrations" / "074_htf_baseline_short_phase2_registration.sql"
    ]:
        if "2026-10-06T14:00:00Z" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == [], f"Point-B freeze reused: {offenders}"
