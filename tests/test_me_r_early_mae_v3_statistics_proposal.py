"""Pure synthetic tests; never connect to PostgreSQL or production services."""
from datetime import date, timedelta

import pytest

from app.research.me_r_long_early_mae_v3.statistics_proposal import (
    Pair, checkpoint_status, primary_interval,
)


def sample(n=100, effect=0.2):
    start = date(2026, 10, 1)
    return [
        Pair(
            source_id=i + 1,
            symbol=f"S{i % 20:02d}",
            oos_day=(start + timedelta(days=i % 25)).isoformat(),
            baseline_net_r=-0.1,
            primary_net_r=-0.1 + effect + (i % 7 - 3) / 100,
        )
        for i in range(n)
    ]


def test_checkpoint_all_dimensions_and_finalized_minimum():
    assert checkpoint_status(filled_n=29, symbols=10, days=5, paired_finalized_n=29) == "BELOW_OPERATIONAL"
    assert checkpoint_status(filled_n=30, symbols=10, days=5, paired_finalized_n=30) == "OPERATIONAL_ONLY"
    assert checkpoint_status(filled_n=100, symbols=20, days=20, paired_finalized_n=99) == "OPERATIONAL_ONLY"
    assert checkpoint_status(filled_n=100, symbols=20, days=20, paired_finalized_n=100) == "COUNT_GATE_MET_VERDICT_NOT_AUTHORIZED"


def test_invalid_checkpoint_counts_fail_closed():
    with pytest.raises(ValueError, match="FINALIZED_EXCEEDS_FILLED"):
        checkpoint_status(filled_n=10, symbols=10, days=5, paired_finalized_n=11)
    with pytest.raises(ValueError, match="INVALID_COUNTS"):
        checkpoint_status(filled_n=True, symbols=20, days=20, paired_finalized_n=1)


def test_seed_repeatability_and_order_invariance():
    rows = sample()
    a = primary_interval(rows, reps=400)
    b = primary_interval(list(reversed(rows)), reps=400)
    assert a == b
    assert a["primary_variant"] == "MAE_15m_050"
    assert a["status"] == "SENSITIVITY_ONLY_NOT_APPROVED_CONFIRMATORY_CI"


def test_cluster_envelope_contains_both_marginal_intervals():
    out = primary_interval(sample(), reps=400)
    lo, hi = out["screening_envelope_ci95"]
    for key in ("symbol_cluster_ci95", "day_cluster_ci95"):
        a, b = out[key]
        assert lo <= a <= b <= hi


def test_constant_delta_exact_interval():
    out = primary_interval(sample(effect=0.25), reps=200)
    assert abs(out["mean_delta_net_r"] - 0.25) < 0.04
    assert out["screening_envelope_ci95"][0] < out["screening_envelope_ci95"][1]


def test_duplicate_identity_and_nonfinite_rejected():
    rows = sample()
    with pytest.raises(ValueError, match="DUPLICATE_SOURCE"):
        primary_interval(rows + [rows[0]], reps=200)
    bad = [Pair(1, "BTCUSDT", "2026-10-01", float("nan"), 0)]
    with pytest.raises(ValueError, match="INVALID_PAIR"):
        primary_interval(bad, reps=200)


def test_single_cluster_rejected():
    rows = [Pair(i, "BTCUSDT", "2026-10-01", 0, 0.1) for i in range(1, 101)]
    with pytest.raises(ValueError, match="INSUFFICIENT_CLUSTERS"):
        primary_interval(rows, reps=200)


def test_no_runtime_modules_imported_by_statistics_proposal():
    import inspect
    from app.research.me_r_long_early_mae_v3 import statistics_proposal
    text = inspect.getsource(statistics_proposal)
    assert "psycopg" not in text
    assert "systemctl" not in text
    assert "pandas" not in text
    assert "numpy" not in text
