from datetime import datetime, timedelta, timezone

from app.research.me_r_long_early_mae_v3.candles import ClosedCandle
from app.research.me_r_long_early_mae_v3.entry_policy import decide_next_open


T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def entry_candle():
    opened = T + timedelta(minutes=5)
    return ClosedCandle(
        opened,
        opened + timedelta(minutes=5),
        100, 101, 99, 100,
    )


def decide(asof, candles=None):
    return decide_next_open(
        candles=[entry_candle()] if candles is None else candles,
        entry_eligible_at=T,
        detected_at=T,
        activation_at=T,
        asof=asof,
    )


def test_restart_after_expiry_preserves_original_fill():
    normal = decide(T + timedelta(minutes=10))
    restarted = decide(T + timedelta(minutes=241))

    assert normal.state == "ENTRY_FILLED"
    assert restarted.state == "ENTRY_FILLED"
    assert restarted.entry == normal.entry


def test_restart_one_day_later_preserves_original_fill():
    restarted = decide(T + timedelta(days=1))

    assert restarted.state == "ENTRY_FILLED"
    assert restarted.entry.entry_at == T + timedelta(minutes=5)
    assert restarted.entry.entry_price == 100


def test_restart_missing_entry_candle_remains_incomplete():
    restarted = decide(
        T + timedelta(days=1),
        candles=[],
    )

    assert restarted.state == "INCOMPLETE_COVERAGE"
    assert restarted.entry is None


def test_entry_candle_not_closed_asof_remains_pending():
    early = decide(T + timedelta(minutes=6))

    assert early.state == "ENTRY_PENDING"
    assert early.entry is None
