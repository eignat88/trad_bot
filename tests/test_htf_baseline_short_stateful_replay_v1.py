"""#176 causal HTF adapter and bounded stateful replay contracts."""
from types import SimpleNamespace

import pytest

from app.models import Candle
from app.research.htf_baseline_short_causal_level_adapter_v1 import (
    causal_resistance_snapshot, causal_contact,
)
from tools.research.htf_baseline_short_stateful_replay_v1 import replay

STEP = 300000
HOUR = 3600000
BASE = 1_800_000_000_000 // HOUR * HOUR


def bar(t, high=101., low=99., close=100.):
    return Candle(t, 100., high, low, close, 10.)


def test_selector_never_uses_unclosed_htf_candle():
    five = [bar(BASE + i*STEP) for i in range(200)]
    asof = BASE + 200*STEP
    htf = [bar(BASE - (40-i)*HOUR) for i in range(35)]
    htf.append(bar(asof//HOUR*HOUR))
    with pytest.raises(ValueError, match="unclosed"):
        causal_resistance_snapshot(htf, five, asof)


def test_selector_rejects_future_execution_bar():
    five = [bar(BASE + i*STEP) for i in range(200)]
    htf = [bar(BASE - (40-i)*HOUR) for i in range(35)]
    with pytest.raises(ValueError, match="future"):
        causal_resistance_snapshot(htf, five, BASE + 199*STEP)


def test_selector_no_level_on_flat_history():
    five = [bar(BASE+i*STEP) for i in range(200)]
    htf = [bar(BASE-(40-i)*HOUR) for i in range(35)]
    assert causal_resistance_snapshot(htf, five, BASE+200*STEP) is None


def test_contact_requires_available_level():
    from app.research.htf_baseline_short_stateful_episode_v1 import LevelSnapshot
    c = bar(BASE)
    level = LevelSnapshot("L", 100, BASE+HOUR)
    assert not causal_contact(c, level, 1.0)


def test_incremental_replay_is_read_only_and_no_future_confirmation():
    five = [bar(BASE+i*STEP) for i in range(420)]
    start = BASE+300*STEP
    end = BASE+400*STEP
    r = replay("BTCUSDT", five, start, end)
    assert r["counters"]["decisions"] == 100
    assert r["counters"].get("new_confirmations", 0) == 0
    assert not r["emissions"]
    assert r["verdict"] == "HISTORICAL_DIAGNOSTIC_NOT_OOS"
