from app.research.me_r_long_early_mae_v3.lifecycle import can_finalize

BASE = {
    "variant_status": "EVALUATED", "coverage_reason": None,
    "net_r_normal": 1.0, "net_r_elevated": 0.9,
    "delta_net_r_normal": 0.1, "delta_net_r_elevated": 0.05,
    "variant_exit_at": "2026-01-01T00:10:00+00:00",
}

def rows():
    return [{**BASE, "variant_id": v} for v in [
        "MAE_10m_040","MAE_10m_050","MAE_15m_040","MAE_15m_050",
        "MAE_15m_060","MAE_15m_075","MAE_30m_050"]]

def test_complete_tuple_can_finalize():
    assert can_finalize(rows())

def test_missing_or_incomplete_tuple_cannot_finalize():
    assert not can_finalize(rows()[:-1])
    changed = rows()
    changed[0]["variant_status"] = "INCOMPLETE_COVERAGE"
    assert not can_finalize(changed)

def test_incomplete_never_has_economic_rows():
    changed = rows()
    changed[0]["coverage_reason"] = "MISSING_CANDLE"
    assert not can_finalize(changed)
