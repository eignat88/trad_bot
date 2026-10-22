"""Reproducibility checks for ME_REVERSE_LONG_V3_FINAL_STATISTICAL_EVALUATION_V1.

READ-ONLY: tests run against the immutable extraction snapshot only and never
open a database connection.  They verify join cardinality, finalization
maturity, missingness, snapshot stability (byte-identical re-runs) and the
frozen CONTROL interpretation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
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


def test_snapshot_row_count_137(mod, rows):
    assert len(rows) == 137


def test_join_cardinality_one_to_one(mod, rows):
    ids = [r["observation_id"] for r in rows]
    assert len(set(ids)) == len(ids)
    source_keys = {(mod.EXPERIMENT_ID, r["source_signal_id"]) for r in rows}
    assert len(source_keys) == len(rows)
    assert all(r["outcome_status"] for r in rows)  # every observation joined


def test_finalization_maturity_split(mod, rows):
    statuses = {}
    for r in rows:
        statuses[r["outcome_status"]] = statuses.get(r["outcome_status"], 0) + 1
    assert statuses == {"FINALIZED": 136, "PENDING": 1}


def test_missingness_zero_on_finalized(mod, rows):
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


def test_outputs_byte_identical_on_rerun():
    tracked = [
        OUT_DIR / f"{BASENAME}.json",
        OUT_DIR / f"{BASENAME}.csv",
        OUT_DIR / f"{BASENAME}_by_day.csv",
        OUT_DIR / f"{BASENAME}_by_symbol.csv",
        OUT_DIR / f"{BASENAME}_by_regime.csv",
        OUT_DIR / f"{BASENAME}_robustness.csv",
    ]
    before = {p: _sha256(p) for p in tracked}
    mod = _load_module()
    mod.main()
    after = {p: _sha256(p) for p in tracked}
    assert before == after


def test_manifest_covers_snapshot_and_outputs():
    manifest_path = OUT_DIR / f"{BASENAME}_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for rel, digest in manifest["files"].items():
        path = REPO_ROOT / rel
        assert path.exists(), rel
        assert _sha256(path) == digest, rel
    assert manifest["protocol_fingerprint"]["status"] == "DERIVED_NOT_STORED"
    assert "audit_db/me_reverse_long_v3_final_statistical_evaluation_v1_20261022/extract/me_v3_eval_20261022/04_cohort_full.csv" in manifest["files"]
    # Large raw full-table extracts are hashed but intentionally not committed.
    assert "local_only_files" in manifest
    for rel, digest in manifest["local_only_files"]["files"].items():
        path = REPO_ROOT / rel
        if path.exists():
            assert _sha256(path) == digest, rel
