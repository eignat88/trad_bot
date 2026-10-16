from __future__ import annotations

import pytest

from app.research.me_r_long_early_mae_v3.method_selection_validation_v1 import (
    wilson_lower, run_screen,
)


def test_wilson_bounds():
    assert 0 < wilson_lower(100, 100) < 1
    assert wilson_lower(0, 100) == pytest.approx(0.)
    with pytest.raises(ValueError, match="INVALID_COVERAGE_COUNTS"):
        wilson_lower(11, 10)


def test_holdout_minimum_is_predeclared():
    with pytest.raises(ValueError, match="UNDERPOWERED_HOLDOUT"):
        run_screen(mode="holdout", datasets=999, reps=300)
    with pytest.raises(ValueError, match="UNDERPOWERED_HOLDOUT"):
        run_screen(mode="holdout", datasets=1000, reps=299)


def test_unknown_mode_rejected():
    with pytest.raises(ValueError, match="UNKNOWN_MODE"):
        run_screen(mode="confirmatory", datasets=1, reps=1)


def test_small_pilot_is_diagnostic_and_never_approves_method():
    out = run_screen(mode="pilot", datasets=2, reps=10)
    assert out["screening_pass"] is None
    assert out["confirmatory_disposition"] == "NO_METHOD_APPROVED"
    assert out["requires_operator_method_approval"] is True
    assert len(out["results"]) == 12
    for row in out["results"]:
        for method in row["methods"].values():
            assert 0 <= method["wilson95_lower_all"] <= 1
