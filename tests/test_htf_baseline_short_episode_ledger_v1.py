"""Issue #174 contact-episode identity tests; synthetic, READ-ONLY."""
from types import SimpleNamespace

from tools.research import htf_baseline_short_episode_ledger_v1 as ledger

STEP = 300000
START = 1791900000000


def candles(touches):
    return [
        SimpleNamespace(timestamp=START + i * STEP, open=100.0,
                        high=101.0 if i in touches else 99.0,
                        low=99.0 if i in touches else 98.0,
                        close=100.0, volume=1.0)
        for i in range(25)
    ]


def install(monkeypatch):
    level = SimpleNamespace(
        timestamp=START-86400000,
        available_timestamp=START-STEP,
        price=100.0, level_type="resistance"
    )
    monkeypatch.setattr(ledger, "_causal_atr_series",
                        lambda bars: [1.0] * len(bars))
    monkeypatch.setattr(ledger, "_find_htf_key_levels",
                        lambda h, b, a, d: (level, (100.0, 100.0, .5)))


def test_adjacent_candles_share_episode(monkeypatch):
    install(monkeypatch)
    bars = candles({5, 6, 7, 12})
    a = ledger.event_episode("BTCUSDT", bars, [], START + 5*STEP)
    b = ledger.event_episode("BTCUSDT", bars, [], START + 7*STEP)
    assert a["episode_id"] == b["episode_id"]
    assert a["episode_contact_bars_asof"] == 3
    assert a["episode_start_ms"] == START + 5*STEP


def test_disconnected_contacts_separate_episodes(monkeypatch):
    install(monkeypatch)
    bars = candles({5, 6, 7, 12})
    a = ledger.event_episode("BTCUSDT", bars, [], START + 5*STEP)
    b = ledger.event_episode("BTCUSDT", bars, [], START + 12*STEP)
    assert a["level_id"] == b["level_id"]
    assert a["episode_id"] != b["episode_id"]


def test_input_history_beyond_200_not_used(monkeypatch):
    install(monkeypatch)
    bars = candles({5, 6, 7, 12})
    past = [
        SimpleNamespace(timestamp=START-(225-i)*STEP, open=80.0,
                        high=81.0, low=79.0, close=80.0, volume=1.0)
        for i in range(201)
    ]
    x = ledger.event_episode("BTCUSDT", bars, [], START+5*STEP)
    y = ledger.event_episode("BTCUSDT", past+bars, [], START+5*STEP)
    assert x["episode_id"] == y["episode_id"]


def test_missing_selected_touch_fails_closed(monkeypatch):
    import pytest
    install(monkeypatch)
    with pytest.raises(ValueError, match="selected touch missing"):
        ledger.event_episode("BTCUSDT", candles({5}), [], START + 50*STEP)
