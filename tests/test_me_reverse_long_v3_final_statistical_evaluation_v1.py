"""Reproducibility and manifest-integrity checks for
ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.

READ-ONLY: tests run against the immutable extraction snapshot and the
committed evidence only; they never open a database connection and never
rewrite committed artifacts.

Integrity order (Codex P2 fix, PR #188 discussion_r4351569305):

1. the COMMITTED manifest is verified against the files on disk FIRST
   (``verify_manifest`` is strictly read-only);
2. only afterwards may artifacts be regenerated, and only into a separate
   temporary directory — the committed manifest must never be overwritten
   during verification;
3. negative regression tests change the frozen extraction SQL / cohort
   snapshot inside a temporary copy and assert that verification FAILS
   against the unchanged committed manifest, reproducing the original
   defect (in-place regeneration silently re-hashing drifted inputs) and
   confirming the fix.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPO_ROOT
    / "tools"
    / "research"
    / "me_reverse_long_v3_final_statistical_evaluation_v1.py"
)
OUT_DIR = REPO_ROOT / "docs" / "research"
BASENAME = "ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1"
MANIFEST_PATH = OUT_DIR / f"{BASENAME}_manifest.json"

SQL_LOGICAL = (
    "audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/"
    "extract/01_extract_frozen_cohort.sql"
)
COHORT_LOGICAL = (
    "audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/"
    "extract/me_v3_eval_20261022/04_cohort_full.csv"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("me_v3_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["me_v3_eval"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


@pytest.fixture(scope="module")
def rows(mod):
    return mod.load_rows()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _committed_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _materialize_tree(tmp_path: Path) -> Path:
    """Copy every manifest-referenced file into tmp_path on its logical path."""
    manifest = _committed_manifest()
    rels = list(manifest.get("files", {}))
    rels += list(manifest.get("local_only_files", {}).get("files", {}))
    for rel in rels:
        src = REPO_ROOT / rel
        if not src.exists():
            continue
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return tmp_path


# ---------------------------------------------------------------------------
# Analysis-level checks (read-only, no artifact writes)
# ---------------------------------------------------------------------------
def test_snapshot_row_count_137(mod, rows):
    assert len(rows) == 137


def test_join_cardinality_one_to_one(mod, rows):
    ids = [r["observation_id"] for r in rows]
    assert len(set(ids)) == len(ids)
    source_keys = {(mod.EXPERIMENT_ID, r["source_signal_id"]) for r in rows}
    assert len(source_keys) == len(rows)
    assert all(r["outcome_status"] for r in rows)  # every observation joined


def test_finalization_maturity_split(rows):
    statuses = {}
    for r in rows:
        statuses[r["outcome_status"]] = statuses.get(r["outcome_status"], 0) + 1
    assert statuses == {"FINALIZED": 136, "PENDING": 1}


def test_missingness_zero_on_finalized(rows):
    fin = [r for r in rows if r["outcome_status"] == "FINALIZED"]
    for r in fin:
        assert r["gross_r"] is not None
        assert r["net_r_normal"] is not None
        assert r["net_r_elevated"] is not None
        assert r["exit_reason"]
        assert r["execution_entry_price"] is not None
        assert r["exit_price"] is not None
        assert r["exhaustion_magnitude"] is not None  # no MISSING_FEATURE rows


def test_no_pre_freeze_contamination(mod, rows):
    assert all(r["signal_time"] > mod.FREEZE_TS for r in rows)


def test_classification_matches_frozen_threshold(mod, rows):
    for r in rows:
        expected = "PASS" if r["exhaustion_magnitude"] <= mod.FROZEN_THRESHOLD else "REJECT"
        assert r["filter_group"] == expected


def test_integrity_audit_clean(mod, rows):
    result = mod.integrity_audit(rows)
    assert result["checks_failed"] == 0, result["checks"]
    assert result["integrity_verdict"] == "CLEAN"


def test_operator_reconciliation_all_match(mod, rows):
    stats = mod.group_stats(rows)
    recon = mod.reconcile_operator(stats)
    bad = [x for x in recon if x["match"] != "MATCH"]
    assert not bad, bad


def test_control_is_aggregate_unfiltered_baseline(mod, rows):
    fin = [r for r in rows if r["outcome_status"] == "FINALIZED"]
    stats = mod.group_stats(rows)
    control = stats["CONTROL_ALL_ELIGIBLE"]
    assert control["n"] == len(fin) == 136
    total = stats["PASS"]["total_net_r"] + stats["REJECT"]["total_net_r"]
    assert abs(control["total_net_r"] - total) < 1e-6
    # CONTROL is not an independent cohort: it contains both compared groups.
    assert control["n"] == stats["PASS"]["n"] + stats["REJECT"]["n"]


def test_deterministic_seeds_produce_stable_intervals(mod, rows):
    a = mod.uncertainty(rows)
    b = mod.uncertainty(rows)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---------------------------------------------------------------------------
# Manifest integrity: verification FIRST, regeneration only into temp dirs
# ---------------------------------------------------------------------------
def test_verify_manifest_before_regeneration_is_read_only(mod):
    """Step 1 of the integrity order: verify committed evidence, write nothing."""
    before_bytes = MANIFEST_PATH.read_bytes()
    before_mtime = MANIFEST_PATH.stat().st_mtime_ns

    report = mod.verify_manifest()

    assert report["ok"] is True, report
    assert report["mismatched"] == []
    assert report["missing_required"] == []
    assert report["checked_required"] == report["matched_required"]
    assert report["checked_required"] == len(_committed_manifest()["files"])
    # verification must never overwrite the manifest under test
    assert MANIFEST_PATH.read_bytes() == before_bytes
    assert MANIFEST_PATH.stat().st_mtime_ns == before_mtime


def test_regeneration_into_temp_dir_matches_committed_artifacts(mod, tmp_path):
    """Step 2: rerun outputs go to a temporary directory and are byte-identical."""
    committed_before = {name: _sha256(OUT_DIR / name) for name in mod.OUTPUT_NAMES}
    out_tmp = tmp_path / "rerun_out"
    mod.main(out_dir=out_tmp, snapshot_dir=mod.SNAPSHOT_DIR)
    for name in mod.OUTPUT_NAMES:
        assert _sha256(out_tmp / name) == committed_before[name], name

    regen = json.loads((out_tmp / f"{BASENAME}_manifest.json").read_text(encoding="utf-8"))
    committed = _committed_manifest()
    assert regen["files"] == committed["files"]
    assert regen["protocol_fingerprint"] == committed["protocol_fingerprint"]
    assert regen["git"] == committed["git"]
    for rel, digest in regen["local_only_files"]["files"].items():
        assert committed["local_only_files"]["files"][rel] == digest
    # committed evidence untouched by the rerun
    for name in mod.OUTPUT_NAMES:
        assert _sha256(OUT_DIR / name) == committed_before[name], name


# ---------------------------------------------------------------------------
# Negative regression tests (temporary copies only)
# ---------------------------------------------------------------------------
def test_negative_tampered_extraction_sql_detected(mod, tmp_path):
    tree = _materialize_tree(tmp_path)
    assert mod.verify_manifest(MANIFEST_PATH, tree)["ok"] is True

    tampered = tree / SQL_LOGICAL
    tampered.write_bytes(tampered.read_bytes() + b"-- drifted input\n")

    manifest_before = MANIFEST_PATH.read_bytes()
    report = mod.verify_manifest(MANIFEST_PATH, tree)

    assert report["ok"] is False
    assert SQL_LOGICAL in report["mismatched"]
    # verification must not rewrite the manifest to hide the drift
    assert MANIFEST_PATH.read_bytes() == manifest_before
    # and the tampered file must not match its committed hash
    assert _sha256(tampered) != _committed_manifest()["files"][SQL_LOGICAL]


def test_negative_tampered_cohort_snapshot_detected(mod, tmp_path):
    tree = _materialize_tree(tmp_path)
    assert mod.verify_manifest(MANIFEST_PATH, tree)["ok"] is True

    tampered = tree / COHORT_LOGICAL
    tampered.write_bytes(tampered.read_bytes() + b"tampered-row\n")

    manifest_before = MANIFEST_PATH.read_bytes()
    report = mod.verify_manifest(MANIFEST_PATH, tree)

    assert report["ok"] is False
    assert COHORT_LOGICAL in report["mismatched"]
    assert MANIFEST_PATH.read_bytes() == manifest_before
    assert _sha256(tampered) != _committed_manifest()["files"][COHORT_LOGICAL]


def test_regression_reproduces_original_defect_and_confirms_fix(mod, tmp_path):
    """Reproduce the original P2 defect and confirm the fix.

    Defect (as found in review): ``main()`` ran in place and overwrote the
    manifest BEFORE verification, so a drifted frozen SQL input was re-hashed
    into a fresh manifest and every check passed.

    Fix: verification runs first against the committed manifest and fails on
    the drift; regeneration happens only into a separate directory and its
    manifest demonstrably differs from the committed one.
    """
    tree = _materialize_tree(tmp_path)
    tampered = tree / SQL_LOGICAL
    tampered.write_bytes(tampered.read_bytes() + b"-- SELECT of a DIFFERENT experiment\n")

    # --- FIX: verify before any regeneration detects the drift -------------
    manifest_before = MANIFEST_PATH.read_bytes()
    report = mod.verify_manifest(MANIFEST_PATH, tree)
    assert report["ok"] is False
    assert SQL_LOGICAL in report["mismatched"]
    assert MANIFEST_PATH.read_bytes() == manifest_before

    # --- DEFECT reproduction: in-place regeneration masks the drift -------
    regen_manifest_path = tree / "docs" / "research" / f"{BASENAME}_manifest.json"
    mod.main(
        out_dir=tree / "docs" / "research",
        snapshot_dir=tree / "audit_db" / "me_reverse_long_v3_final_statistical_evaluation_v1_20261022" / "extract" / "me_v3_eval_20261022",
    )
    regen = json.loads(regen_manifest_path.read_text(encoding="utf-8"))

    # the regenerated manifest happily re-hashes the tampered input ...
    assert regen["files"][SQL_LOGICAL] == _sha256(tampered)
    assert mod.verify_manifest(regen_manifest_path, tree)["ok"] is True
    # ... which is exactly the masking the defect allowed; the drift remains
    # detectable because the regenerated manifest differs from the committed one
    committed = _committed_manifest()
    assert regen["files"][SQL_LOGICAL] != committed["files"][SQL_LOGICAL]
    assert regen != committed
    assert MANIFEST_PATH.read_bytes() == manifest_before
