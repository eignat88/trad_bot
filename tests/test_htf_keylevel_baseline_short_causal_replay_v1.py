"""No-network tests for the bounded causal replay diagnostic."""
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app.models import Candle
from tools.research import htf_keylevel_baseline_short_causal_replay_v1 as r

T = int(datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp()*1000)


def candles(n=415):
    return [Candle(T+i*r.BAR_MS, 100, 101, 99, 100, 1) for i in range(n)]


def test_complete_hour_only():
    rows = candles(14)
    h = r.assemble_complete_1h(rows, T+14*r.BAR_MS)
    assert len(h) == 1
    assert h[0].timestamp == T
    assert h[0].volume == 12


def test_hour_not_closed_excluded():
    rows = candles(12)
    assert r.assemble_complete_1h(rows, T+11*r.BAR_MS) == []


def test_missing_bar_does_not_produce_hour():
    rows = candles(12)
    del rows[3]
    assert r.assemble_complete_1h(rows, T+12*r.BAR_MS) == []


def test_replay_strict_causal(monkeypatch):
    def fake_detector(*, symbol, execution_candles, htf_candles, evaluation_asof):
        asof = int(evaluation_asof.timestamp()*1000)
        assert max(x.timestamp + r.BAR_MS for x in execution_candles) <= asof
        assert all(x.timestamp + r.HTF_MS <= asof for x in htf_candles)
        return SimpleNamespace(known_at_ms=asof, observed_at_ms=asof, event_id="one")
    monkeypatch.setattr(r, "detect_causal_baseline_short", fake_detector)
    out = r.replay("BTCUSDT", candles())
    assert out["stats"]["unique_events"] == 1
    assert out["stats"]["duplicate_emissions"] == 14
    assert out["stats"]["evaluated_prefixes"] == 15


def test_replay_refuses_backdated_signal(monkeypatch):
    def fake_detector(*, symbol, execution_candles, htf_candles, evaluation_asof):
        asof = int(evaluation_asof.timestamp()*1000)
        return SimpleNamespace(known_at_ms=asof-r.BAR_MS, observed_at_ms=asof, event_id="one")
    monkeypatch.setattr(r, "detect_causal_baseline_short", fake_detector)
    with pytest.raises(ValueError, match="CAUSAL_TIMESTAMP_VIOLATION"):
        r.replay("BTCUSDT", candles())


def test_gap_rejected():
    rows = candles()
    rows[9] = Candle(rows[9].timestamp+r.BAR_MS, 100, 101, 99, 100, 1)
    with pytest.raises(ValueError, match="NONCONTIGUOUS"):
        r.replay("BTCUSDT", rows)


def test_diagnostic_funnel_no_level(monkeypatch):
    rows = candles(400)
    htf = r.assemble_complete_1h(rows, rows[-1].timestamp+r.BAR_MS)
    monkeypatch.setattr(r, "_causal_atr_series", lambda c: [1.0]*len(c))
    monkeypatch.setattr(r, "_find_htf_key_levels", lambda *args: (None, None))
    assert r.diagnostic_funnel_stage(rows[-200:], htf, rows[-1].timestamp+r.BAR_MS) == "NO_HTF_LEVEL"


def test_diagnostic_funnel_fresh_reaction(monkeypatch):
    rows = candles(400)
    asof = rows[-1].timestamp+r.BAR_MS
    htf = r.assemble_complete_1h(rows, asof)
    level = SimpleNamespace(price=100.0, level_type="resistance",
                            available_timestamp=rows[-200].timestamp)
    monkeypatch.setattr(r, "_causal_atr_series", lambda c: [1.0]*len(c))
    monkeypatch.setattr(r, "_find_htf_key_levels", lambda *args: (level, (100.0, 100.0, .5)))
    monkeypatch.setattr(r, "_find_reaction_end", lambda **kwargs: (len(kwargs["closed"])-1, 0, 0, 101, 99, 2, 1.0, 1.0))
    assert r.diagnostic_funnel_stage(rows[-200:], htf, asof) == "REACTION_FRESH"


def test_diagnostic_funnel_historical_not_fresh(monkeypatch):
    rows = candles(400)
    asof = rows[-1].timestamp+r.BAR_MS
    htf = r.assemble_complete_1h(rows, asof)
    level = SimpleNamespace(price=100.0, level_type="resistance",
                            available_timestamp=rows[-200].timestamp)
    monkeypatch.setattr(r, "_causal_atr_series", lambda c: [1.0]*len(c))
    monkeypatch.setattr(r, "_find_htf_key_levels", lambda *args: (level, (100.0, 100.0, .5)))
    monkeypatch.setattr(r, "_find_reaction_end", lambda **kwargs: (len(kwargs["closed"])-2, 0, 0, 101, 99, 2, 1.0, 1.0))
    assert r.diagnostic_funnel_stage(rows[-200:], htf, asof) == "REACTION_HISTORICAL"
