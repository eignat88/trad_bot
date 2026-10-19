"""Pure regression tests for Issue #170 Phase 1 (no database required)."""
from datetime import datetime, timezone

from tools.research.htf_keylevel_baseline_short_execution_validation_v1 import (
    EXPERIMENT_ID, QUERY, audit_rows, classify,
)


def row(**changes):
    values = {
        "oid": 11, "source": 123, "symbol": "BTCUSDT",
        "direction": "SHORT",
        "ts": datetime(2026, 10, 6, 14, 5, tzinfo=timezone.utc),
        "entry": 100.0, "stop": 101.0, "target": 98.0,
        "features": {"cohort": "BASELINE"},
        "has_outcome": True, "is_final": True,
    }
    values.update(changes)
    return tuple(values[key] for key in (
        "oid", "source", "symbol", "direction", "ts", "entry",
        "stop", "target", "features", "has_outcome", "is_final",
    ))


def test_valid_short_source():
    result = audit_rows([row()])
    assert result["strict_valid_finalized"] == 1
    assert result["valid_symbols"] == 1
    assert result["valid_oos_dates"] == 1
    assert result["execution_verdict"] == "NOT_EVALUATED"
    assert result["database_mutations"] == 0


def test_strict_freeze_boundary():
    issues = classify(row(ts="2026-10-06T14:00:00Z"), set(), set())
    assert "pre_or_at_freeze" in issues


def test_short_sign_and_target_validation():
    assert "invalid_short_geometry" in classify(row(stop=99.0), set(), set())
    assert "invalid_short_geometry" in classify(row(target=102.0), set(), set())
    assert "invalid_or_nonpositive_price" in classify(row(entry=float("nan")), set(), set())


def test_duplicate_source_and_observation():
    result = audit_rows([row(), row()])
    assert result["strict_valid_finalized"] == 1
    assert result["issues"]["missing_or_duplicate_source_signal_id"] == 1
    assert result["issues"]["missing_or_duplicate_observation_id"] == 1


def test_immature_and_missing_outcomes():
    report = audit_rows([
        row(oid=1, source=1, has_outcome=False, is_final=False),
        row(oid=2, source=2, has_outcome=True, is_final=False),
    ])
    assert report["strict_valid_finalized"] == 0
    assert report["issues"]["missing_outcome"] == 1
    assert report["issues"]["immature_outcome"] == 1


def test_invalid_features_and_time():
    report = audit_rows([
        row(oid=1, source=1, features="{notjson}"),
        row(oid=2, source=2, ts="invalid"),
        row(oid=3, source=3, features='{"cohort":"POINT_B"}'),
    ])
    assert report["issues"]["missing_or_invalid_features"] == 1
    assert report["issues"]["invalid_signal_time"] == 1
    assert report["issues"]["wrong_cohort"] == 1


def test_query_locked_to_source_experiment_and_short_direction():
    assert "p.experiment_id = %s" in QUERY
    assert "p.direction = 'SHORT'" in QUERY
    assert "LEFT JOIN research.prospective_outcome" in QUERY
    assert EXPERIMENT_ID.endswith("BASELINE_V1_PROSPECTIVE")
