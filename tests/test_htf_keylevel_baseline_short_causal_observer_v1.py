"""Contract regression tests: causal reaction-only BASELINE SHORT, no DB."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from app.research import htf_keylevel_baseline_short_causal_observer_v1 as mod

START = 1_780_000_000_000 // mod.BAR_MS * mod.BAR_MS


def fixtures():
    exec_bars = [
        SimpleNamespace(timestamp=START+i*mod.BAR_MS, open=100.0, high=101.0,
                        low=99.0, close=100.0, volume=10.0)
        for i in range(20)
    ]
    # Hourly source is closed well before the execution prefix.
    htf_start = (START // mod.HTF_MS - 40) * mod.HTF_MS
    htf_bars = [
        SimpleNamespace(timestamp=htf_start+i*mod.HTF_MS, open=100.0,
                        high=105.0, low=95.0, close=100.0, volume=10.0)
        for i in range(30)
    ]
    now = datetime.fromtimestamp((START+20*mod.BAR_MS)/1000, timezone.utc)
    return exec_bars, htf_bars, now


def install_stubs(monkeypatch, reaction_end=19):
    level = SimpleNamespace(price=100.0, level_type="resistance",
                            available_timestamp=START)
    monkeypatch.setattr(mod, "_causal_atr_series", lambda c: [1.0]*len(c))
    monkeypatch.setattr(mod, "_find_htf_key_levels",
                        lambda h, c, a, d: (level, (100.0, 100.0, 0.5)))
    monkeypatch.setattr(
        mod, "_find_reaction_end",
        lambda **kw: (reaction_end, 100.0, 99.0, 101.0, 98.0, 2, 1.0, 1.0)
    )


def run(bars, htf, now):
    return mod.detect_causal_baseline_short(
        symbol="BTCUSDT", execution_candles=bars,
        htf_candles=htf, evaluation_asof=now,
    )


def test_fresh_reaction_emits_without_point_b(monkeypatch):
    install_stubs(monkeypatch)
    bars, htf, now = fixtures()
    signal = run(bars, htf, now)
    assert signal is not None
    assert signal.known_at_ms == START+20*mod.BAR_MS
    assert signal.observed_at_ms == signal.known_at_ms
    assert signal.direction == "SHORT"
    assert signal.invalidation_price == 101.0
    assert signal.target_1 == 98.0
    assert signal.event_id == run(bars, htf, now).event_id


def test_historical_reaction_does_not_backdate(monkeypatch):
    install_stubs(monkeypatch, reaction_end=18)
    bars, htf, now = fixtures()
    assert run(bars, htf, now) is None


def test_unclosed_execution_candle_rejected(monkeypatch):
    install_stubs(monkeypatch)
    bars, htf, now = fixtures()
    assert run(bars, htf, datetime.fromtimestamp(now.timestamp()-60, timezone.utc)) is None


def test_missing_execution_bar_rejected(monkeypatch):
    install_stubs(monkeypatch)
    bars, htf, now = fixtures()
    bars[9].timestamp += mod.BAR_MS
    assert run(bars, htf, now) is None


def test_stale_snapshot_rejected(monkeypatch):
    install_stubs(monkeypatch)
    bars, htf, now = fixtures()
    future = datetime.fromtimestamp(now.timestamp()+600, timezone.utc)
    assert run(bars, htf, future) is None


def test_unclosed_htf_bar_rejected(monkeypatch):
    install_stubs(monkeypatch)
    bars, htf, now = fixtures()
    htf[-1].timestamp = ((int(now.timestamp() * 1000) // mod.HTF_MS) * mod.HTF_MS)
    assert run(bars, htf, now) is None


def test_asof_must_be_aware():
    bars, htf, _ = fixtures()
    with pytest.raises(ValueError, match="timezone-aware"):
        run(bars, htf, datetime(2026, 10, 19, 12))


def test_no_database_and_no_order_calls(monkeypatch):
    install_stubs(monkeypatch)
    bars, htf, now = fixtures()
    result = run(bars, htf, now)
    assert result.experiment_id == mod.EXPERIMENT_ID
    assert "CAUSAL" in result.experiment_id
