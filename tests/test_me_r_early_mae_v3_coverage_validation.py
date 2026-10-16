"""Pure synthetic checks of the research-only coverage harness."""
import pytest

from app.research.me_r_long_early_mae_v3.coverage_validation import (
    SCENARIOS, generate_pairs, run_coverage,
)


def test_fixed_seed_reproducible_and_unique_source_ids():
    for scenario in SCENARIOS:
        a = generate_pairs(scenario, seed=42)
        assert a == generate_pairs(scenario, seed=42)
        assert len(a) == len({p.source_id for p in a}) == 100
        assert len({p.symbol for p in a}) >= 2
        assert len({p.oos_day for p in a}) >= 2


def test_random_incidence_not_modular_schedule():
    rows = generate_pairs("CROSSED_SHOCK", seed=42)
    assert len({(p.symbol, p.oos_day) for p in rows}) > 75


def test_coverage_is_diagnostic_only():
    a = run_coverage("CROSSED_SHOCK", datasets=5, reps=100, seed=17)
    b = run_coverage("CROSSED_SHOCK", datasets=5, reps=100, seed=17)
    assert a == b
    assert a.valid + a.invalid == 5
    assert 0 <= a.coverage_all <= 1
    assert a.status == "DIAGNOSTIC_ONLY_NO_CONFIRMATORY_APPROVAL"


def test_invalid_design_and_counts_fail_closed():
    with pytest.raises(ValueError, match="UNKNOWN_SCENARIO"):
        generate_pairs("OTHER", seed=1)
    with pytest.raises(ValueError, match="INVALID_DESIGN"):
        generate_pairs("IID", seed=1, symbols=1)
    with pytest.raises(ValueError, match="INVALID_DATASETS"):
        run_coverage("IID", datasets=0, reps=100)
