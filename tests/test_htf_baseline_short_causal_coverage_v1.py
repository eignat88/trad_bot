"""Issue #174 offline coverage harness tests; no database or network."""
from datetime import datetime, timezone

import pytest

from app.models import Candle
from tools.research.htf_baseline_short_causal_coverage_v1 import (
    BAR_MS, HOUR_MS, closed_htf, diagnose, load_bars, utc_ms,
)


def bars(n=40, start=1_782_000_000_000):
    start = start // HOUR_MS * HOUR_MS
    return [
        Candle(start+i*BAR_MS, 100, 102, 99, 100, 10)
        for i in range(n)
    ]


def test_utc_rejects_naive():
    with pytest.raises(ValueError, match="timezone"):
        utc_ms("2026-10-14T00:00:00")


def test_htf_requires_all_twelve_bars_and_closed_hour():
    src = bars(24)
    first = closed_htf(src[:11], src[10].timestamp+BAR_MS)
    assert first == []
    first = closed_htf(src[:12], src[11].timestamp+BAR_MS)
    assert len(first) == 1
    assert first[0].timestamp == src[0].timestamp
    both = closed_htf(src, src[-1].timestamp+BAR_MS)
    assert len(both) == 2


def test_missing_hour_bar_is_not_aggregated():
    src = bars(24)
    src.pop(4)
    htf = closed_htf(src, src[-1].timestamp+BAR_MS)
    assert len(htf) == 1
    assert htf[0].timestamp == src[11].timestamp


def test_future_htf_rejected_by_asof():
    src = bars(24)
    assert len(closed_htf(src, src[12].timestamp)) == 1


def test_diagnostic_does_not_count_repeated_prefixes_as_trades(monkeypatch):
    import tools.research.htf_baseline_short_causal_coverage_v1 as mod
    src = bars(400)
    event = type("E", (), {
        "event_id": "same", "known_at_ms": 0, "observed_at_ms": 0,
        "touch_open_ms": src[0].timestamp, "reaction_open_ms": src[0].timestamp
    })()
    def fake_detector(*, evaluation_asof, **kwargs):
        t = int(evaluation_asof.timestamp()*1000)
        event.known_at_ms = t
        event.observed_at_ms = t
        return event
    monkeypatch.setattr(mod.observer, "detect_causal_baseline_short", fake_detector)
    monkeypatch.setattr(mod, "event_episode", lambda *args: {"level_id": "level", "episode_id": "episode"})
    start = src[320].timestamp+BAR_MS
    end = src[330].timestamp+BAR_MS
    r = diagnose("BTCUSDT", src, start, end, windows=(200,))
    d = r["windows"]["200"]
    assert d["decision_funnel"]["decision_prefixes"] == 10
    assert d["unique_events"] == 1
    assert d["decision_funnel"]["repeat_emission"] == 9


def test_real_detector_absence_is_not_positive_verdict():
    src = bars(400)
    r = diagnose("BTCUSDT", src, src[340].timestamp+BAR_MS,
                 src[350].timestamp+BAR_MS, windows=(200,))
    assert r["verdict"] == "DIAGNOSTIC_ONLY_NOT_OOS"
    assert r["windows"]["200"]["decision_funnel"]["decision_prefixes"] == 10


def test_gap_detection_fails_closed(tmp_path):
    p = tmp_path / "BTCUSDT_5m.csv"
    rows = bars(3)
    with p.open("w", encoding="utf-8") as f:
        f.write("timestamp,open,high,low,close,volume\n")
        for c in (rows[0], rows[2]):
            f.write(f"{c.timestamp},100,102,99,100,10\n")
    with pytest.raises(ValueError, match="gap"):
        load_bars(p)
