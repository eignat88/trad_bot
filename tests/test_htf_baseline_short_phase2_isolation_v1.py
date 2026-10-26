"""Issue #176 Phase-2 mandatory test matrix (10): isolation — the Phase-2
repair must not change Point-B, paper, live, scanner or any frozen artifact.

Historical exception (FIX-5) — documented, NOT a waiver of the control
-----------------------------------------------------------------------
Baseline: ``BASE_SHA = 4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba`` (frozen,
never changed by this exception).

Commit ``41536cc5bc967331846d045eaae18a0702da8b89`` ("feat(research): scoped
ME V3 draining lifecycle", PR #193, issue unrelated to #197) changed EXACTLY
ONE LINE of the frozen file ``app/research/prospective_observer.py``:

    source (BASE_SHA):
        active = self._active_status(str(row[0]))
    result (41536cc5):
        active = (str(row[0]) == "RUNNING") if exp_id ==
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS" else
        self._active_status(str(row[0]))

Compatibility rationale: the ME V3 draining lifecycle needs capture to stay
RUNNING-only (outcome drain is a separate, operator-gated path) while EVERY
other experiment — Point-B included — keeps the ORIGINAL ``_active_status()``
branch semantics unchanged. The change is therefore scoped by experiment id
and cannot alter Point-B-dependent logic.

The exception allows EXCLUSIVELY that one-line replacement:

* the other seven frozen artifacts stay strictly byte-identical to BASE_SHA;
* ``prospective_observer.py`` must equal BASE_SHA with exactly the source
  fragment above replaced by exactly the result fragment — nothing else;
* any other change to the file (including reverting the line, changing the
  Point-B branch or the ME V3 branch, or any edit elsewhere) FAILS;
* the file stays in ``FROZEN_PATHS`` and ``BASE_SHA`` is never modified.
"""
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

# --- FIX-5 historical exception (see module docstring) ----------------------
PROSPECTIVE_OBSERVER_PATH = "app/research/prospective_observer.py"
#: the ONLY commit whose delta in the frozen file is tolerated (PR #193)
EXCEPTION_COMMIT = "41536cc5bc967331846d045eaae18a0702da8b89"
#: exact source fragment at BASE_SHA (one occurrence)
ALLOWED_SOURCE_LINE = "                active = self._active_status(str(row[0]))"
#: exact resulting fragment after the PR #193 one-line replacement
ALLOWED_RESULT_LINE = (
    "                active = (str(row[0]) == \"RUNNING\") if exp_id == "
    '"MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS" '
    "else self._active_status(str(row[0]))"
)
#: experiment ids whose branch semantics are pinned by this exception
POINT_B_EXPERIMENT_ID = "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"
ME_V3_EXPERIMENT_ID = "MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS"

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
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",  # repo content is UTF-8; never trust the locale codec
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(
            f"git command failed ({result.returncode}): "
            f"{result.stderr.strip()[:300]}"
        )
    return result.stdout


def _pr_base() -> str:
    """Resolve merge-base with main for PR-scope checks."""
    base = _git("merge-base", "HEAD", "origin/main").strip()
    if not base:
        pytest.fail("cannot resolve PR merge-base")
    return base


def _norm(text: str) -> str:
    """Line-ending normalization ONLY (git eol smudge filters are platform
    specific); the content comparison itself stays exact."""
    return text.replace("\r\n", "\n")


def _changed_lines(diff_text: str) -> tuple[list[str], list[str]]:
    """Exact removed/added content lines of a unified diff (headers skipped)."""
    removed: list[str] = []
    added: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            removed.append(_norm(line[1:]))
        elif line.startswith("+"):
            added.append(_norm(line[1:]))
    return removed, added


def _verify_prospective_observer_compat(*, base_text: str, head_text: str) -> None:
    """FIX-5: allow EXACTLY the documented PR #193 one-line replacement.

    ``head_text`` must equal ``base_text`` with exactly one occurrence of
    :data:`ALLOWED_SOURCE_LINE` replaced by :data:`ALLOWED_RESULT_LINE` and
    NOTHING else. Any other change to the protected file — an extra edit
    anywhere, a tampered Point-B branch, a tampered ME V3 branch, or a plain
    revert that drops the fix — raises :class:`AssertionError` (fail closed).
    """
    base_text = _norm(base_text)
    head_text = _norm(head_text)
    assert base_text.count(ALLOWED_SOURCE_LINE) == 1, (
        "BASE_SHA content no longer contains the documented source fragment "
        "exactly once — the historical exception (FIX-5) no longer applies"
    )
    assert ALLOWED_RESULT_LINE not in base_text
    expected_text = base_text.replace(ALLOWED_SOURCE_LINE, ALLOWED_RESULT_LINE, 1)
    assert head_text == expected_text, (
        f"{PROSPECTIVE_OBSERVER_PATH} differs from BASE_SHA by MORE than the "
        "single documented PR #193 (41536cc5) one-line replacement; any other "
        "change to this protected file is forbidden"
    )


def _base_prospective_observer_text() -> str:
    return _git("show", f"{BASE_SHA}:{PROSPECTIVE_OBSERVER_PATH}")


def _head_prospective_observer_text() -> str:
    return (ROOT / PROSPECTIVE_OBSERVER_PATH).read_text(encoding="utf-8")


def test_frozen_point_b_stack_byte_identical_to_base():
    # 1) the other seven frozen artifacts: strictly byte-identical to the
    #    audited base (compare through git itself — handles eol smudge
    #    filters; empty diff = identical)
    strict = tuple(p for p in FROZEN_PATHS if p != PROSPECTIVE_OBSERVER_PATH)
    diff = _git("diff", "--name-only", BASE_SHA, "--", *strict)
    changed = [line.strip() for line in diff.splitlines() if line.strip()]
    assert changed == [], f"frozen artifacts modified: {changed}"

    # 2) prospective_observer.py stays fully under control (FIX-5): the ONLY
    #    tolerated delta against BASE_SHA is the documented one-line PR #193
    #    replacement, verified against the exact source/result fragments
    _verify_prospective_observer_compat(
        base_text=_base_prospective_observer_text(),
        head_text=_head_prospective_observer_text(),
    )


def test_prospective_observer_delta_is_exactly_the_documented_pr193_line():
    """FIX-5 §3: exact source AND resulting fragments + documented provenance.
    """
    # (a) the git-visible delta against BASE_SHA is exactly the replacement
    removed, added = _changed_lines(
        _git("diff", BASE_SHA, "--", PROSPECTIVE_OBSERVER_PATH)
    )
    assert removed == [ALLOWED_SOURCE_LINE]
    assert added == [ALLOWED_RESULT_LINE]

    # (b) provenance: commit 41536cc5 (PR #193) introduced exactly this delta
    prov_removed, prov_added = _changed_lines(
        _git(
            "diff",
            f"{EXCEPTION_COMMIT}~1",
            EXCEPTION_COMMIT,
            "--",
            PROSPECTIVE_OBSERVER_PATH,
        )
    )
    assert prov_removed == removed
    assert prov_added == added

    # (c) expected content hash, derived from BASE_SHA content plus the
    #     documented replacement (LF-normalized) — origin pinned to BASE_SHA
    #     and 41536cc5
    expected = _norm(_base_prospective_observer_text()).replace(
        ALLOWED_SOURCE_LINE, ALLOWED_RESULT_LINE, 1
    )
    head_text = _norm(_head_prospective_observer_text())
    assert hashlib.sha256(head_text.encode("utf-8")).hexdigest() == (
        hashlib.sha256(expected.encode("utf-8")).hexdigest()
    )


def test_frozen_baseline_pins_and_control_scope_are_immutable():
    # the FIX-5 exception must never become a waiver (forbidden-actions guard)
    assert BASE_SHA == "4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba"
    assert PROSPECTIVE_OBSERVER_PATH in FROZEN_PATHS
    assert len(FROZEN_PATHS) == 8


def test_exception_verifier_rejects_any_other_change():
    """FIX-5 §4: any change to the protected file beyond the single documented
    replacement FAILS — including changes to Point-B-dependent logic."""
    base = "def wrapper():\n" f"{ALLOWED_SOURCE_LINE}\n" "    return active\n"
    allowed = base.replace(ALLOWED_SOURCE_LINE, ALLOWED_RESULT_LINE, 1)
    _verify_prospective_observer_compat(base_text=base, head_text=allowed)

    # 1) plain revert (drops the ME V3 fix) -> FAIL
    with pytest.raises(AssertionError):
        _verify_prospective_observer_compat(base_text=base, head_text=base)

    # 2) allowed line PLUS an unrelated edit elsewhere in the file -> FAIL
    with pytest.raises(AssertionError):
        _verify_prospective_observer_compat(
            base_text=base, head_text=allowed + "# sneaky\n"
        )

    # 3) tampered Point-B branch (original _active_status() semantics
    #    changed) -> FAIL: Point-B-dependent logic changes stay detectable
    tampered_point_b = allowed.replace(
        "else self._active_status(str(row[0]))", "else True"
    )
    with pytest.raises(AssertionError):
        _verify_prospective_observer_compat(base_text=base, head_text=tampered_point_b)

    # 4) tampered ME V3 branch (RUNNING-only narrowed or broadened) -> FAIL
    tampered_me_v3 = allowed.replace('== "RUNNING"', '!= "CANCELLED"')
    with pytest.raises(AssertionError):
        _verify_prospective_observer_compat(base_text=base, head_text=tampered_me_v3)

    # 5) the replacement duplicated on a second line -> FAIL
    duplicated = allowed.replace(
        "    return active\n", f"    return active\n{ALLOWED_RESULT_LINE}\n"
    )
    with pytest.raises(AssertionError):
        _verify_prospective_observer_compat(base_text=base, head_text=duplicated)

    # 6) a base without the documented source fragment -> FAIL
    with pytest.raises(AssertionError):
        _verify_prospective_observer_compat(
            base_text="no fragment\n", head_text="no fragment\n"
        )


class _FakeCursor:
    def __init__(self, status):
        self._status = status

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchone(self):
        return (self._status,)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, status):
        self._status = status

    def cursor(self):
        return _FakeCursor(self._status)


def _db_active(exp_id: str, status: str) -> bool:
    from app.research.prospective_observer import ProspectiveOOSObserver

    observer = ProspectiveOOSObserver(_FakeConn(status), {})
    return observer._is_db_active(exp_id)


def test_point_b_keeps_original_active_status_branch():
    # FIX-5 §6: Point-B still runs the ORIGINAL _active_status() semantics —
    # every non-terminal state keeps capture active, terminal states never do
    pb = POINT_B_EXPERIMENT_ID
    assert _db_active(pb, "RUNNING") is True
    assert _db_active(pb, "DRAINING") is True
    assert _db_active(pb, "PAUSED") is True
    assert _db_active(pb, "CANCELLED") is False
    assert _db_active(pb, "COMPLETED") is False
    assert _db_active(pb, "CLOSED_NEGATIVE") is False


def test_me_v3_uses_dedicated_running_only_branch():
    # FIX-5 §6: ME V3 capture is RUNNING-only (the drain path is separate and
    # operator-gated); every other experiment is untouched
    v3 = ME_V3_EXPERIMENT_ID
    assert _db_active(v3, "RUNNING") is True
    assert _db_active(v3, "DRAINING") is False
    assert _db_active(v3, "PAUSED") is False
    assert _db_active(v3, "CANCELLED") is False
    assert _db_active(v3, "COMPLETED") is False


def test_no_tracked_file_modified_against_base():
    diff = _git("diff", "--name-status", _pr_base())
    changed = [
        line.strip()
        for line in diff.splitlines()
        if line.strip() and line.strip()[0] in {"M", "D", "R", "C", "T"}
    ]
    allowed_modified = (
        ".github/workflows/main-pr-safety.yml",
        # Issue #186: only these exact Phase-2 approval-record files may change;
        # do not broaden the allowlist to directory prefixes.
        "app/research/htf_baseline_short_phase2_protocol_v1.py",
        "docs/research/HTF_BASELINE_SHORT_PHASE2_PROTOCOL_V1.md",
        "tests/test_htf_baseline_short_phase2_protocol_v1.py",
        "app/research/htf_baseline_short_phase2_observer_runner_v1.py",
        "deploy/systemd/trad-bot-htf-baseline-short-observer.service",
        "tests/test_htf_baseline_short_phase2_isolation_v1.py",
        # FIX-1 review 5615772746: the CP4 coverage-gap fixture needed more
        # synthetic history before its gap-resume point (causal lookback
        # contract); still strictly inside the Phase-2 test surface
        "tests/test_htf_baseline_short_phase2_runtime_v1.py",
        # issue #184: Bybit wiring — the activation runbook is updated to the
        # explicit `--candle-source bybit` ExecStart and the effective-unit /
        # fail-closed verification procedure (docs-only, Phase-2 surface)
        "docs/research/HTF_BASELINE_SHORT_PHASE2_ACTIVATION_RUNBOOK_V1.md",
        # Issue #185 FIX-4: the migration runbook's rollback section is being
        # corrected in-place (the DROP OWNED BY claim was wrong and is replaced
        # by the snapshot-driven, fail-closed rollback procedure). Only this
        # Phase-2 runbook document is touched; its VERIFICATION_SQL contract
        # block is preserved byte-for-byte.
        "docs/research/HTF_BASELINE_SHORT_PHASE2_MIGRATION_074_075_RUNBOOK_V1.md",
    )

    unexpected = []
    for entry in changed:
        status, path = entry.split("\t", 1)
        if status != "M" or path not in allowed_modified:
            unexpected.append(entry)

    assert unexpected == [], (
        f"tracked files modified/deleted outside Phase-2 surface: {unexpected}"
    )


def test_new_files_are_confined_to_phase2_surface():
    new_paths: set[str] = set()
    status = _git("status", "--porcelain")
    for line in status.splitlines():
        if line.startswith("??"):
            new_paths.add(line[3:].strip().strip('"'))
    diff = _git("diff", "--name-status", _pr_base())
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
