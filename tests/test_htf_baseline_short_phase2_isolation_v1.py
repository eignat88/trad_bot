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

Historical exception (FIX-7) — documented, NOT a waiver of the control
-----------------------------------------------------------------------
Baseline: ``BASE_SHA = 4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba`` (frozen,
never changed by this exception — the same baseline as FIX-5).

Commit ``745f4e963337f48539a20f9c3237b5cdcaf67558`` ("research: finalize
ME SHORT A execution validation positive (#201)", PR #201, issue unrelated
to #197) changed EXACTLY ONE registry entry of the frozen file
``app/research/prospective_registry.json`` — the ME SHORT geometry-A
execution-validation entry ``ME_SHORT_GEOM_A_EXECUTION_V1`` (the
"ME_SHORT_GEOM_A_V1" registry entry of the PR #201 record):

    source (BASE_SHA):
        "status": "READY_TO_START"
    result (745f4e9):
        "status": "COMPLETED",
        "lifecycle_verdict": "VALIDATED_POSITIVE_EXECUTION_EDGE_CONFIRMED",
        "lifecycle_reason": "<finalized decision-checkpoint summary>",
        "decision_checkpoint": { <17 frozen OOS decision metrics> }

Compatibility rationale: PR #201 only recorded the terminal lifecycle
verdict of the ME SHORT A execution-validation cohort — a pure lifecycle
record. EVERY other registry entry — including EVERY Point-B
(``HTF_KEYLEVEL*``) entry with its ``2026-10-06T14:00:00Z`` freeze
timestamp — is byte-identical to BASE_SHA, and no OOS metric of any other
entry moves. The delta is therefore scoped to one experiment id and cannot
alter Point-B-dependent logic.

The exception allows EXCLUSIVELY that one-entry replacement:

* the six frozen artifacts without an exception stay strictly
  byte-identical to BASE_SHA (``prospective_observer.py`` keeps its own
  FIX-5 exception above; FIX-5's "other seven" wording predates FIX-7);
* ``prospective_registry.json`` must equal BASE_SHA with exactly the source
  fragment above replaced by exactly the result fragment — complete
  registry content equality against BASE_SHA plus this exact audited
  modification, NOTHING else;
* any other registry change (a Point-B-related entry, any extra mutation,
  missing verdict/reason/decision_checkpoint fields, changed OOS metrics,
  or a plain revert of the PR #201 record) FAILS;
* the file stays in ``FROZEN_PATHS`` and ``BASE_SHA`` is never modified.

Provenance is re-validated from git history on every run: the git-visible
delta against BASE_SHA, the delta of commit 745f4e9 itself, and the fact
that 745f4e9 is the ONLY registry-changing commit since BASE_SHA must all
agree with the audited fragments pinned below.
"""
from __future__ import annotations

import hashlib
import json
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

# --- FIX-7 historical exception (see module docstring) ----------------------
REGISTRY_PATH = "app/research/prospective_registry.json"
#: the ONLY commit whose delta in the frozen registry is tolerated (PR #201)
REGISTRY_EXCEPTION_COMMIT = "745f4e963337f48539a20f9c3237b5cdcaf67558"
#: the ONLY registry entry the audited PR #201 delta may touch
REGISTRY_EXCEPTION_EXPERIMENT_ID = "ME_SHORT_GEOM_A_EXECUTION_V1"
#: Point-B freeze timestamp pinned by the frozen protocol (never moved)
POINT_B_FREEZE_TS = "2026-10-06T14:00:00Z"
#: unique anchor of the audited entry's tail in BASE_SHA (appears exactly
#: once in the whole registry — verified fail-closed in the verifier)
_REGISTRY_ANCHOR = (
    '      "discovery_source": "ME_SHORT_GEOMETRY_ABC_PAIRED_VALIDATION_V1",\n'
    '      "paired_with": null,\n'
    '      "started_at": null,\n'
)
#: exact source fragment at BASE_SHA (one occurrence)
REGISTRY_SOURCE_FRAGMENT = _REGISTRY_ANCHOR + '      "status": "READY_TO_START"'
#: exact resulting fragment after the PR #201 one-entry replacement
REGISTRY_RESULT_FRAGMENT = _REGISTRY_ANCHOR + (
    '      "status": "COMPLETED",\n'
    '      "lifecycle_verdict": '
    '"VALIDATED_POSITIVE_EXECUTION_EDGE_CONFIRMED",\n'
    '      "lifecycle_reason": "Clean strictly post-freeze prospective '
    "execution validation passed. At decision checkpoint: 368 finalized "
    "observations, 43 symbols, 8 OOS days, zero pre-freeze contamination. "
    "Normal Net E[R]=+5.0735, Net PF=4.8046, Total Net R=+1867.03. Elevated "
    "Net E[R]=+4.5765, Net PF=3.7302. Bootstrap 95% CI for mean normal Net "
    "R=[+3.177142,+7.196802], P(mean>0)=1.0. Robustness remained positive "
    "without top-10 winners (Net E[R]=+2.3466, PF=2.7119) and with winners "
    "capped at +10R (Net E[R]=+0.9744). Research execution edge confirmed. "
    'This verdict does not authorize live trading.",\n'
    '      "decision_checkpoint": {\n'
    '        "date": "2026-10-26",\n'
    '        "finalized_n": 368,\n'
    '        "symbols": 43,\n'
    '        "oos_days": 8,\n'
    '        "pre_freeze_contamination": 0,\n'
    '        "normal_net_er": 5.0735,\n'
    '        "normal_net_pf": 4.8046,\n'
    '        "normal_total_net_r": 1867.03,\n'
    '        "elevated_net_er": 4.5765,\n'
    '        "elevated_net_pf": 3.7302,\n'
    '        "bootstrap_ci_95": [\n'
    '          3.177142,\n'
    '          7.196802\n'
    '        ],\n'
    '        "bootstrap_p_mean_gt_zero": 1.0,\n'
    '        "without_top_10_net_er": 2.3466,\n'
    '        "without_top_10_net_pf": 2.7119,\n'
    '        "cap_10r_net_er": 0.9744,\n'
    '        "max_drawdown_r": -61.2,\n'
    '        "max_consecutive_losses": 29\n'
    '      }'
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


def _base_registry_text() -> str:
    return _git("show", f"{BASE_SHA}:{REGISTRY_PATH}")


def _head_registry_text() -> str:
    return (ROOT / REGISTRY_PATH).read_text(encoding="utf-8")


def _audited_registry_change() -> tuple[list[str], list[str]]:
    """Removed/added content lines of the audited FIX-7 splice — the exact
    git-visible delta shape of the single documented replacement."""
    source = _norm(REGISTRY_SOURCE_FRAGMENT).splitlines()
    result = _norm(REGISTRY_RESULT_FRAGMENT).splitlines()
    common = 0
    while (
        common < len(source)
        and common < len(result)
        and source[common] == result[common]
    ):
        common += 1
    return source[common:], result[common:]


def _audited_registry_tail() -> dict:
    """Semantic form of the audited PR #201 addition, parsed from the very
    same pinned fragment (single source of truth for the verifier)."""
    return json.loads("{\n" + _norm(REGISTRY_RESULT_FRAGMENT) + "\n}")


def _registry_entries(registry) -> list:
    return registry if isinstance(registry, list) else registry.get("experiments", [])


def _verify_registry_entries_semantics(*, base_registry, head_registry) -> None:
    """FIX-7 semantic layer: entry-by-entry deep equality against BASE_SHA
    plus exactly the audited PR #201 modification (precise fail-closed
    attribution for Point-B entries, extra mutations and OOS metrics)."""
    base_entries = _registry_entries(base_registry)
    head_entries = _registry_entries(head_registry)
    assert [e.get("experiment_id") for e in head_entries] == [
        e.get("experiment_id") for e in base_entries
    ], "registry experiment sequence changed — forbidden"

    base_ids = [e.get("experiment_id") for e in base_entries]
    assert base_ids.count(REGISTRY_EXCEPTION_EXPERIMENT_ID) == 1, (
        "BASE_SHA registry must contain the audited entry exactly once — "
        "the historical exception (FIX-7) no longer applies"
    )

    audited_tail = _audited_registry_tail()
    for base_entry, head_entry in zip(base_entries, head_entries):
        exp_id = base_entry.get("experiment_id")
        if exp_id == REGISTRY_EXCEPTION_EXPERIMENT_ID:
            # audited-entry precondition at BASE_SHA (fail closed if the
            # baseline ever stops matching the documented PR #201 delta)
            assert base_entry.get("status") == "READY_TO_START", (
                "BASE_SHA audited-entry precondition changed — the "
                "historical exception (FIX-7) no longer applies"
            )
            assert base_entry.get("discovery_source") == (
                "ME_SHORT_GEOMETRY_ABC_PAIRED_VALIDATION_V1"
            )
            for key in (
                "lifecycle_verdict",
                "lifecycle_reason",
                "decision_checkpoint",
            ):
                assert key not in base_entry, (
                    "BASE_SHA audited-entry precondition changed — the "
                    "historical exception (FIX-7) no longer applies"
                )
            audited_expected = dict(base_entry)
            audited_expected.update(audited_tail)
            assert head_entry == audited_expected, (
                f"{REGISTRY_EXCEPTION_EXPERIMENT_ID} must equal its BASE_SHA "
                "entry plus EXACTLY the audited PR #201 "
                "status/lifecycle_verdict/lifecycle_reason/decision_checkpoint "
                "modification (missing fields or changed OOS metrics forbidden)"
            )
        else:
            assert head_entry == base_entry, (
                f"registry entry {exp_id} modified — the ONLY tolerated "
                "registry delta is the audited PR #201 modification of "
                f"{REGISTRY_EXCEPTION_EXPERIMENT_ID}"
            )

    # explicit Point-B pin on top of the per-entry equality above
    for head_entry in head_entries:
        exp_id = str(head_entry.get("experiment_id", ""))
        if exp_id.startswith("HTF_KEYLEVEL"):
            assert head_entry.get("freeze_ts") == POINT_B_FREEZE_TS, (
                f"Point-B entry {exp_id} freeze timestamp changed — forbidden"
            )


def _verify_registry_compat(*, base_text: str, head_text: str) -> None:
    """FIX-7: allow EXACTLY the documented PR #201 one-entry replacement.

    ``head_text`` must equal ``base_text`` with exactly one occurrence of
    :data:`REGISTRY_SOURCE_FRAGMENT` replaced by
    :data:`REGISTRY_RESULT_FRAGMENT` and NOTHING else — complete registry
    content equality against BASE_SHA plus this exact audited modification.
    Any other change to the protected registry — a Point-B entry edit, an
    extra mutation anywhere, missing PR #201 verdict fields, changed OOS
    metrics, or a plain revert — raises :class:`AssertionError` (fail closed).
    """
    base_text = _norm(base_text)
    head_text = _norm(head_text)
    assert base_text.count(REGISTRY_SOURCE_FRAGMENT) == 1, (
        "BASE_SHA registry no longer contains the audited source fragment "
        "exactly once — the historical exception (FIX-7) no longer applies"
    )
    assert REGISTRY_RESULT_FRAGMENT not in base_text
    expected_text = base_text.replace(
        REGISTRY_SOURCE_FRAGMENT, REGISTRY_RESULT_FRAGMENT, 1
    )

    # semantic layer first (precise attribution), then exact text equality
    try:
        base_registry = json.loads(base_text)
        head_registry = json.loads(head_text)
    except ValueError as exc:
        raise AssertionError(f"{REGISTRY_PATH} is not valid JSON: {exc}") from exc
    _verify_registry_entries_semantics(
        base_registry=base_registry, head_registry=head_registry
    )

    assert head_text == expected_text, (
        f"{REGISTRY_PATH} differs from BASE_SHA by MORE than the single "
        f"documented PR #201 (745f4e9) modification of the "
        f"{REGISTRY_EXCEPTION_EXPERIMENT_ID} entry; any other change to this "
        "protected artifact is forbidden"
    )


def test_frozen_point_b_stack_byte_identical_to_base():
    # 1) the six frozen artifacts without a historical exception: strictly
    #    byte-identical to the audited base (compare through git itself —
    #    handles eol smudge filters; empty diff = identical)
    strict = tuple(
        p
        for p in FROZEN_PATHS
        if p not in (PROSPECTIVE_OBSERVER_PATH, REGISTRY_PATH)
    )
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

    # 3) prospective_registry.json stays fully under control (FIX-7): the
    #    ONLY tolerated delta against BASE_SHA is the documented one-entry
    #    PR #201 (745f4e9) modification of ME_SHORT_GEOM_A_EXECUTION_V1 —
    #    complete content equality against BASE_SHA plus that modification
    _verify_registry_compat(
        base_text=_base_registry_text(),
        head_text=_head_registry_text(),
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


def test_registry_delta_is_exactly_the_documented_pr201_modification():
    """FIX-7: exact audited registry delta + documented PR #201 provenance
    validated from git history (source AND resulting fragments pinned to the
    single audited commit 745f4e963337f48539a20f9c3237b5cdcaf67558).
    """
    expected_removed, expected_added = _audited_registry_change()

    # (a) the git-visible delta against BASE_SHA is exactly the audited
    #     one-entry replacement
    removed, added = _changed_lines(
        _git("diff", BASE_SHA, "--", REGISTRY_PATH)
    )
    assert removed == expected_removed
    assert added == expected_added

    # (b) provenance: commit 745f4e9 (PR #201) introduced exactly this delta
    prov_removed, prov_added = _changed_lines(
        _git(
            "diff",
            f"{REGISTRY_EXCEPTION_COMMIT}~1",
            REGISTRY_EXCEPTION_COMMIT,
            "--",
            REGISTRY_PATH,
        )
    )
    assert prov_removed == removed
    assert prov_added == added

    # (c) 745f4e9 is the ONLY registry-changing commit since BASE_SHA
    touchers = [
        line.strip()
        for line in _git(
            "log", "--format=%H", f"{BASE_SHA}..HEAD", "--", REGISTRY_PATH
        ).splitlines()
        if line.strip()
    ]
    assert touchers == [REGISTRY_EXCEPTION_COMMIT], (
        f"registry provenance drifted since BASE_SHA: {touchers}"
    )

    # (d) documented identity: the audited commit is PR #201 and names the
    #     finalized entry
    subject = _git("log", "-1", "--format=%s", REGISTRY_EXCEPTION_COMMIT).strip()
    body = _git("log", "-1", "--format=%B", REGISTRY_EXCEPTION_COMMIT).strip()
    assert subject.endswith("(#201)")
    assert REGISTRY_EXCEPTION_EXPERIMENT_ID in body

    # (e) expected content hash, derived from BASE_SHA content plus the
    #     documented replacement (LF-normalized) — origin pinned to BASE_SHA
    #     and 745f4e9
    expected = _norm(_base_registry_text()).replace(
        REGISTRY_SOURCE_FRAGMENT, REGISTRY_RESULT_FRAGMENT, 1
    )
    head_text = _norm(_head_registry_text())
    assert hashlib.sha256(head_text.encode("utf-8")).hexdigest() == (
        hashlib.sha256(expected.encode("utf-8")).hexdigest()
    )


def _synthetic_registry_base() -> str:
    """Minimal registry with the audited entry precondition and a Point-B
    entry — the FIX-7 verifier contract in isolation (no repo state)."""
    return (
        "{\n"
        '  "experiments": [\n'
        "    {\n"
        '      "experiment_id": '
        '"HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE",\n'
        '      "freeze_ts": "2026-10-06T14:00:00Z",\n'
        '      "status": "RUNNING"\n'
        "    },\n"
        "    {\n"
        '      "experiment_id": "ME_SHORT_GEOM_A_EXECUTION_V1",\n'
        '      "scanner_name": "MOMENTUM_EXHAUSTION",\n'
        + REGISTRY_SOURCE_FRAGMENT
        + "\n    },\n"
        "    {\n"
        '      "experiment_id": "ME_SHORT_GEOM_B_V1",\n'
        '      "status": "READY_TO_START"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )


def test_registry_verifier_rejects_any_other_change():
    """FIX-7: any registry change beyond the single documented PR #201
    one-entry modification FAILS — Point-B modifications, extra registry
    mutations, missing PR #201 fields, changed OOS metrics and unintended
    reverts are all rejected (fail closed)."""
    base = _synthetic_registry_base()
    allowed = base.replace(REGISTRY_SOURCE_FRAGMENT, REGISTRY_RESULT_FRAGMENT, 1)

    # positive control: the EXACT audited modification PASSES
    _verify_registry_compat(base_text=base, head_text=allowed)

    # 1) unintended revert: the whole PR #201 record dropped -> FAIL
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=base)
    partial_revert = allowed.replace(
        '"status": "COMPLETED"', '"status": "READY_TO_START"'
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=partial_revert)

    # 2) Point-B modification (freeze timestamp moved) -> FAIL
    point_b = allowed.replace(
        '"freeze_ts": "2026-10-06T14:00:00Z"',
        '"freeze_ts": "2026-10-07T14:00:00Z"',
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=point_b)

    # 3) extra registry mutations -> FAIL (semantic drift and byte drift)
    extra_field = allowed.replace(
        '"experiment_id": "ME_SHORT_GEOM_B_V1"',
        '"experiment_id": "ME_SHORT_GEOM_B_V2"',
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=extra_field)
    extra_whitespace = allowed.replace(
        '"experiments": [', '"experiments":  [', 1
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=extra_whitespace)
    duplicated = allowed.replace(
        '      "experiment_id": "ME_SHORT_GEOM_B_V1",\n'
        '      "status": "READY_TO_START"',
        '      "experiment_id": "ME_SHORT_GEOM_B_V1",\n'
        + REGISTRY_RESULT_FRAGMENT,
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=duplicated)

    # 3b) the audited delta applied to a DIFFERENT entry -> FAIL
    wrong_base = base.replace(
        '"experiment_id": "ME_SHORT_GEOM_A_EXECUTION_V1"',
        '"experiment_id": "ME_SHORT_GEOM_OTHER_V1"',
        1,
    )
    wrong_head = wrong_base.replace(
        REGISTRY_SOURCE_FRAGMENT, REGISTRY_RESULT_FRAGMENT, 1
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=wrong_base, head_text=wrong_head)

    # 4) missing PR #201 fields -> FAIL
    missing_verdict = allowed.replace(
        '      "lifecycle_verdict": '
        '"VALIDATED_POSITIVE_EXECUTION_EDGE_CONFIRMED",\n',
        "",
        1,
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=missing_verdict)
    missing_reason = re.sub(
        r"      \"lifecycle_reason\": .*\n", "", allowed, count=1
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=missing_reason)
    missing_checkpoint = re.sub(
        r",\n      \"decision_checkpoint\": \{.*?\n      \}",
        "",
        allowed,
        count=1,
        flags=re.DOTALL,
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=missing_checkpoint)

    # 5) changed OOS metrics / verdict wording -> FAIL
    bad_metrics = allowed.replace(
        '"normal_net_er": 5.0735', '"normal_net_er": 9.99', 1
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=bad_metrics)
    bad_ci = allowed.replace("          3.177142,", "          3.277142,", 1)
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=bad_ci)
    bad_drawdown = allowed.replace(
        '"max_drawdown_r": -61.2', '"max_drawdown_r": -50.0', 1
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=bad_drawdown)
    bad_verdict = allowed.replace(
        "VALIDATED_POSITIVE_EXECUTION_EDGE_CONFIRMED",
        "VALIDATED_NEGATIVE",
        1,
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=bad_verdict)
    tampered_reason = allowed.replace(
        " This verdict does not authorize live trading.", "", 1
    )
    with pytest.raises(AssertionError):
        _verify_registry_compat(base_text=base, head_text=tampered_reason)


def test_frozen_baseline_pins_and_control_scope_are_immutable():
    # the FIX-5/FIX-7 exceptions must never become waivers (forbidden-actions
    # guard)
    assert BASE_SHA == "4ef60d1e6bb7d2fbbd7fe2b4e3de0591995dc5ba"
    assert PROSPECTIVE_OBSERVER_PATH in FROZEN_PATHS
    assert REGISTRY_PATH in FROZEN_PATHS
    assert len(FROZEN_PATHS) == 8
    # FIX-7 pins: the registry exception is anchored to the exact audited
    # PR #201 commit and entry — it can never silently widen
    assert EXCEPTION_COMMIT == "41536cc5bc967331846d045eaae18a0702da8b89"
    assert REGISTRY_EXCEPTION_COMMIT == (
        "745f4e963337f48539a20f9c3237b5cdcaf67558"
    )
    assert REGISTRY_EXCEPTION_EXPERIMENT_ID == "ME_SHORT_GEOM_A_EXECUTION_V1"
    assert POINT_B_FREEZE_TS == "2026-10-06T14:00:00Z"


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
        # Issue #205: admin connection routing — the freeze tool stops using
        # the observer-only connection helper and opens its own admin channel
        # (new module, Phase-2 surface). The tool itself and its two existing
        # test modules change strictly inside the Phase-2 surface: tool routing
        # (read-only mint/verify/repair, activate-only claim) and the matching
        # test fixtures/assertions; no other tracked file is touched.
        "tools/research/htf_baseline_short_phase2_freeze_record_v1.py",
        "tests/test_htf_baseline_short_phase2_freeze_record_v1.py",
        "tests/test_htf_baseline_short_phase2_freeze_record_postgres_v1.py",
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
    # NOTE: tolerated registry change control itself lives in the FIX-7
    # verifier above; this test pins the Point-B entries and the Phase-2
    # registration boundary.
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
