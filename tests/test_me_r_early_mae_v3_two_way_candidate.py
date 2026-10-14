"""Pure synthetic regression tests for research-only two-way t candidate."""
import pytest

from app.research.me_r_long_early_mae_v3.statistics_proposal import Pair
from app.research.me_r_long_early_mae_v3.two_way_candidate import two_way_t_candidate
from app.research.me_r_long_early_mae_v3.paired_candidate_comparison import compare


def sample():
    return [
        Pair(i+1, f"S{i % 20:02d}", f"D{i % 25:02d}", 0.,
             .15 + ((i*13)%37 - 18)/100)
        for i in range(100)
    ]


def test_candidate_deterministic_and_labeled_unapproved():
    a = two_way_t_candidate(sample())
    b = two_way_t_candidate(list(reversed(sample())))
    assert abs(a["mean_delta_net_r"]-b["mean_delta_net_r"]) < 1e-12
    assert abs(a["variance"]-b["variance"]) < 1e-12
    assert a["critical"] > 1.96
    lo, hi = a["ci95_candidate"]
    assert lo < a["mean_delta_net_r"] < hi
    assert "NOT_APPROVED" in a["status"]


def test_insufficient_clusters_rejected():
    rows = [Pair(i+1, "BTC", f"D{i % 20}", 0., i/100) for i in range(100)]
    with pytest.raises(ValueError, match="INSUFFICIENT_CLUSTERS"):
        two_way_t_candidate(rows)


def test_nonpositive_variance_fail_closed():
    rows = [Pair(i+1, f"S{i % 20}", f"D{i % 25}", 0., .2) for i in range(100)]
    with pytest.raises(ValueError, match="NONPOSITIVE_TWO_WAY_VARIANCE"):
        two_way_t_candidate(rows)


def test_duplicate_source_rejected():
    rows = sample()
    with pytest.raises(ValueError, match="DUPLICATE_SOURCE"):
        two_way_t_candidate(rows+[rows[0]])


def test_paired_comparison_reproducible_and_no_verdict():
    a = compare("CROSSED_SHOCK", datasets=4, reps=100, seed=45)
    assert a == compare("CROSSED_SHOCK", datasets=4, reps=100, seed=45)
    assert set(a["methods"]) == {"envelope", "two_way_t"}
    assert "CONFIRMATORY_BLOCKED" in a["status"]
    for result in a["methods"].values():
        assert 0 <= result["coverage_all"] <= 1
        assert result["invalid"] >= 0


def test_invalid_scenario_rejected():
    with pytest.raises(ValueError, match="UNKNOWN_SCENARIO"):
        compare("NOT_A_SCENARIO", datasets=2, reps=100)
