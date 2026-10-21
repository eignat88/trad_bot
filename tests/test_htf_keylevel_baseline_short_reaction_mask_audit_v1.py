"""Research-only masking audit regressions, no network or database."""
from types import SimpleNamespace

from app.models import Candle
from tools.research import htf_keylevel_baseline_short_reaction_mask_audit_v1 as audit


def bars(n=25):
    return [Candle(i*300000, 100, 101, 99, 100, 1) for i in range(n)]


def stub_level(monkeypatch, reaction):
    level = SimpleNamespace(price=100, level_type="resistance", available_timestamp=0)
    monkeypatch.setattr(audit, "_causal_atr_series", lambda c: [1]*len(c))
    monkeypatch.setattr(audit, "_find_htf_key_levels", lambda *args: (level, (100, 100, .5)))
    monkeypatch.setattr(audit, "_find_reaction_end", reaction)


def test_old_reaction_does_not_mask_later_fresh(monkeypatch):
    rows = bars()
    def reaction(**kw):
        return (20 if kw["touch_index"] == 0 else 24, 100, 99, 101, 98, 2, 1, 1)
    stub_level(monkeypatch, reaction)
    report = audit.inspect_reaction_masking(rows, rows)
    assert report["fresh_after_historical"] is True
    assert report["would_emit"] is True
    assert report["first_confirmed_reaction_lag_bars"] == 4
    assert report["later_touch_count"] == 23


def test_only_historical_reactions(monkeypatch):
    rows = bars()
    stub_level(monkeypatch, lambda **kw: (20, 100, 99, 101, 98, 2, 1, 1))
    report = audit.inspect_reaction_masking(rows, rows)
    assert report["confirmed_touch_count"] == 24
    assert report["fresh_touch_count"] == 0
    assert report["would_emit"] is False


def test_masking_summary_does_not_claim_unique_trades():
    result = audit.masking_summary([
        {"level_found": True, "touch_count": 2, "later_touch_count": 1,
         "confirmed_touch_count": 1, "first_confirmed_reaction_lag_bars": 4,
         "asof_ms": 1},
        {"level_found": True, "touch_count": 2, "later_touch_count": 1,
         "confirmed_touch_count": 1, "first_confirmed_reaction_lag_bars": 3,
         "asof_ms": 2},
    ])
    assert result["decision_counts"]["decisions_with_later_touches"] == 2
    assert result["first_reaction_lag_bars_min"] == 3
    assert "Per-decision" in result["interpretation"]
