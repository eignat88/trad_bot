from datetime import datetime, timezone

from app.models import Candle
from app.scanners.expectancy_filter import (
    DEFAULT_MIN_SAMPLES,
    ExpectancyFilter,
    ExpectancyRecord,
    filter_candidates,
)
from app.scanners.models import (
    IndicatorSnapshot,
    MarketContext,
    MarketLevels,
    SetupCandidate,
)
from app.scanners.orchestrator import ScannerOrchestrator


def candidate(**overrides):
    base = SetupCandidate(
        scanner_name="TREND_PULLBACK",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        signal_candle_open_time=1_777_294_700_000,
        reference_price=79_203.10,
        entry_zone_low=79_000,
        entry_zone_high=79_300,
        invalidation_price=78_500,
        target_1=80_000,
        score=30,
    )
    return SetupCandidate(**{**base.__dict__, **overrides})


def test_filter_allows_no_history_combinations():
    f = ExpectancyFilter()
    assert f.is_profitable("UNKNOWN_SCANNER", "LONG") is True
    assert f.reason_for("UNKNOWN_SCANNER", "LONG") == "NO_HISTORY"


def test_filter_allows_insufficient_samples():
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=5, avg_r_after_costs=-0.5, win_rate=0.0,
        ),
    })
    assert f.is_profitable("TREND_PULLBACK", "LONG") is True
    assert f.reason_for("TREND_PULLBACK", "LONG").startswith("INSUFFICIENT_SAMPLES")


def test_filter_rejects_negative_expectancy():
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=20, avg_r_after_costs=-0.2, win_rate=0.1,
        ),
    })
    assert f.is_profitable("TREND_PULLBACK", "LONG", min_avg_r=0.0) is False
    assert f.is_profitable("TREND_PULLBACK", "LONG", min_avg_r=-0.5) is True


def test_filter_allows_positive_expectancy():
    f = ExpectancyFilter(records={
        ("SUPPORT_RESISTANCE_REACTION", "LONG"): ExpectancyRecord(
            scanner_name="SUPPORT_RESISTANCE_REACTION", direction="LONG",
            samples=15, avg_r_after_costs=0.3, win_rate=0.4,
        ),
    })
    assert f.is_profitable("SUPPORT_RESISTANCE_REACTION", "LONG") is True


def test_filter_candidates_returns_accepted_and_rejected():
    f = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=20, avg_r_after_costs=-0.2, win_rate=0.1,
        ),
        ("BREAKOUT_RETEST", "SHORT"): ExpectancyRecord(
            scanner_name="BREAKOUT_RETEST", direction="SHORT",
            samples=20, avg_r_after_costs=-0.1, win_rate=0.1,
        ),
    })
    candidates = [
        candidate(scanner_name="TREND_PULLBACK", direction="LONG"),
        candidate(scanner_name="BREAKOUT_RETEST", direction="SHORT"),
        candidate(scanner_name="VOLATILITY_COMPRESSION", direction="SHORT"),  # no history
    ]
    accepted, rejected = filter_candidates(candidates, f)
    assert rejected == 2
    assert len(accepted) == 1
    assert accepted[0].scanner_name == "VOLATILITY_COMPRESSION"


def test_orchestrator_applies_expectancy_filter():
    """Expectancy filter is applied in scan_all_with_stats."""

    def fake_candidate(**overrides):
        base = candidate(
            entry_zone_low=95, entry_zone_high=100,
            invalidation_price=90, target_1=110,
            reference_price=100,
            features={"trend_alignment": True, "htf_context": True},
        )
        return SetupCandidate(**{**base.__dict__, **overrides})

    class FakeScanner:
        def scan(self, ctx):
            return [fake_candidate()]

    candle = Candle(1_777_294_700_000, 100, 102, 99, 101, 10)
    ctx = MarketContext(
        symbol="BTCUSDT", candles_5m=(candle,), candles_15m=(),
        candles_1h=(), candles_4h=(), indicators=IndicatorSnapshot(),
        market_regime="TREND_UP", levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )
    orchestrator = ScannerOrchestrator(enabled_scanners=[])
    orchestrator.scanners = {"TREND_PULLBACK": FakeScanner()}

    # With negative expectancy: should be filtered out
    negative_filter = ExpectancyFilter(records={
        ("TREND_PULLBACK", "LONG"): ExpectancyRecord(
            scanner_name="TREND_PULLBACK", direction="LONG",
            samples=20, avg_r_after_costs=-0.2, win_rate=0.1,
        ),
    })
    candidates, stats = orchestrator.scan_all_with_stats(
        ctx, expectancy_filter=negative_filter,
    )
    assert candidates == []
    assert stats["TREND_PULLBACK"]["setups_saved"] == 0

    # Without filter: should pass (fresh orchestrator + different candle)
    candle2 = Candle(1_777_295_000_000, 100, 102, 99, 101, 10)
    ctx2 = MarketContext(
        symbol="BTCUSDT", candles_5m=(candle2,), candles_15m=(),
        candles_1h=(), candles_4h=(), indicators=IndicatorSnapshot(),
        market_regime="TREND_UP", levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )
    orchestrator2 = ScannerOrchestrator(enabled_scanners=[])
    orchestrator2.scanners = {"TREND_PULLBACK": FakeScanner()}
    candidates2, stats2 = orchestrator2.scan_all_with_stats(ctx2)
    assert len(candidates2) == 1
    assert stats2["TREND_PULLBACK"]["setups_saved"] == 1
