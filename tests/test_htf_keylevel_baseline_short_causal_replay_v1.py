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
