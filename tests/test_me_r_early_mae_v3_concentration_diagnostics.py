"""Research-only structural concentration unit tests."""
import pytest
from app.research.me_r_long_early_mae_v3.statistics_proposal import Pair
from app.research.me_r_long_early_mae_v3.concentration_diagnostics import (
    concentration_diagnostics, structural_quality_gate,
)


def balanced():
    return [Pair(i+1, f"S{i%20:02d}", f"D{i//20:02d}", 0., .1)
            for i in range(400)]


def test_balanced_crossed_metrics():
    out = concentration_diagnostics(balanced())
    assert out["n"] == 400
    assert out["symbol"]["clusters"] == 20
    assert out["day"]["clusters"] == 20
    assert out["symbol"]["effective_clusters"] == pytest.approx(20)
    assert out["day"]["effective_clusters"] == pytest.approx(20)
    assert out["incidence_density"] == 1
    assert out["cell"]["clusters"] == 400


def test_provisional_gate_not_approval():
    out = structural_quality_gate(balanced())
    assert out["decision"] == "PROVISIONAL_SCREEN_ONLY"
    assert "UNAPPROVED" in out["status"]


def test_highly_concentrated_fails_closed():
    rows = [Pair(i+1, "BTC" if i < 300 else f"S{i%20}",
                 "D0" if i < 300 else f"D{i%20}", 0., .1)
            for i in range(400)]
    out = structural_quality_gate(rows)
    assert out["decision"] == "INCONCLUSIVE"
    assert any("EFFECTIVE_BELOW" in reason for reason in out["reasons"])


def test_invalid_data_and_thresholds():
    rows = balanced()
    with pytest.raises(ValueError, match="DUPLICATE_SOURCE"):
        concentration_diagnostics(rows + [rows[0]])
    with pytest.raises(ValueError, match="INVALID_CLUSTER_MINIMUM"):
        structural_quality_gate(rows, min_symbols=1)
    with pytest.raises(ValueError, match="INVALID_EFFECTIVE_MINIMUM"):
        structural_quality_gate(rows, min_effective_days=0)
    with pytest.raises(ValueError, match="EMPTY_COHORT"):
        concentration_diagnostics([])
