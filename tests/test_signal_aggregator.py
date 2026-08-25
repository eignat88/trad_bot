from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.scanners.models import SetupCandidate, SetupState
from app.scanners.signal_aggregator import SignalAggregator


NOW = datetime(2026, 8, 25, 14, 20, tzinfo=timezone.utc)


def signal(scanner: str, direction: str, score: float, age_seconds: int = 0):
    return SetupCandidate(
        scanner_name=scanner,
        symbol="SOLUSDT",
        direction=direction,
        signal_candle_open_time=1_777_294_700_000 + age_seconds,
        detected_at=NOW - timedelta(seconds=age_seconds),
        score=score,
    )


def test_agreed_signals_are_ready_to_trade():
    aggregator = SignalAggregator(conflict_window_seconds=600)

    result = aggregator.resolve([
        signal("TREND_PULLBACK", "LONG", 25),
        signal("VOLATILITY_COMPRESSION", "LONG", 30),
    ], NOW)[0]

    assert result.status == SetupState.READY_TO_TRADE
    assert result.long_score == 55
    assert result.short_score == 0
    assert len(result.trade_candidates) == 2
    assert all(c.state == SetupState.READY_TO_TRADE for c in result.signals)


def test_opposite_signal_blocks_entire_symbol_and_updates_older_signal():
    aggregator = SignalAggregator(conflict_window_seconds=600)
    old_long = signal("TREND_PULLBACK", "LONG", 50, age_seconds=40)
    aggregator.resolve([old_long], NOW - timedelta(seconds=40))

    result = aggregator.resolve([
        signal("VOLATILITY_COMPRESSION", "SHORT", 25)
    ], NOW)[0]

    assert result.status == SetupState.CONFLICT
    assert result.long_score == 50
    assert result.short_score == 25
    assert result.trade_candidates == ()
    assert {c.state for c in result.signals} == {SetupState.CONFLICT}


def test_expired_opposite_signal_no_longer_causes_conflict():
    aggregator = SignalAggregator(conflict_window_seconds=300)
    old_short = signal("VOLATILITY_COMPRESSION", "SHORT", 25, age_seconds=301)
    aggregator.resolve([old_short], NOW - timedelta(seconds=301))

    current_long = replace(
        signal("TREND_PULLBACK", "LONG", 25),
        signal_candle_open_time=1_777_295_000_000,
    )
    result = aggregator.resolve([current_long], NOW)[0]

    assert result.status == SetupState.READY_TO_TRADE
    assert tuple(c.direction for c in result.signals) == ("LONG",)
