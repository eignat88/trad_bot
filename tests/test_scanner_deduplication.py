from dataclasses import replace
from datetime import datetime, timezone

from app.models import Candle
from app.db.repository import ScannerRepository
from app.scanners.deduplication import DeduplicationEngine
from app.scanners.models import (
    IndicatorSnapshot,
    MarketContext,
    MarketLevels,
    SetupCandidate,
    SetupState,
)
from app.scanners.orchestrator import ScannerOrchestrator
from app.scanners.risk_geometry import INVALID_RISK_GEOMETRY, validate_risk_geometry


def candidate(**overrides):
    base = SetupCandidate(
        scanner_name="TREND_PULLBACK",
        symbol="BTCUSDT",
        direction="LONG",
        entry_timeframe="5m",
        signal_candle_open_time=1_777_294_700_000,
        reference_price=79_203.10,
    )
    return replace(base, **overrides)


def test_deduplication_uses_strategy_side_ltf_and_candle_time():
    engine = DeduplicationEngine()
    first = candidate()
    repriced_same_candle = candidate(reference_price=79_219.80)

    assert engine.filter_new([first]) == [first]
    assert engine.filter_new([repriced_same_candle]) == []
    next_candle = candidate(signal_candle_open_time=first.signal_candle_open_time + 300_000)
    assert engine.filter_new([next_candle]) == [next_candle]


def test_orchestrator_attaches_entry_candle_open_time():
    candle = Candle(1_777_294_700_000, 100, 102, 99, 101, 10)
    ctx = MarketContext(
        symbol="BTCUSDT",
        candles_5m=(candle,),
        candles_15m=(),
        candles_1h=(),
        candles_4h=(),
        indicators=IndicatorSnapshot(),
        market_regime=None,
        levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )

    result = ScannerOrchestrator._attach_signal_candle(
        candidate(signal_candle_open_time=0), ctx
    )

    assert result.signal_candle_open_time == candle.timestamp


def test_orchestrator_attaches_market_regime_from_context():
    candle = Candle(1_777_294_700_000, 100, 102, 99, 101, 10)
    ctx = MarketContext(
        symbol="BTCUSDT", candles_5m=(candle,), candles_15m=(),
        candles_1h=(), candles_4h=(), indicators=IndicatorSnapshot(),
        market_regime="TREND_UP", levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )

    result = ScannerOrchestrator._attach_context(
        candidate(signal_candle_open_time=0, market_regime=None), ctx
    )

    assert result.signal_candle_open_time == candle.timestamp
    assert result.market_regime == "TREND_UP"


def test_risk_geometry_accepts_valid_long_and_short_setups():
    valid_long = candidate(
        direction="LONG",
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=99,
        target_1=103,
    )
    valid_short = candidate(
        direction="SHORT",
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=102,
        target_1=98,
    )

    assert validate_risk_geometry(valid_long) == (True, None)
    assert validate_risk_geometry(valid_short) == (True, None)


def test_risk_geometry_rejects_stop_inside_or_wrong_side_of_entry_zone():
    bad_long = candidate(
        direction="LONG",
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=100.5,
        target_1=103,
    )
    bad_short = candidate(
        direction="SHORT",
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=100.5,
        target_1=98,
    )

    assert validate_risk_geometry(bad_long) == (False, INVALID_RISK_GEOMETRY)
    assert validate_risk_geometry(bad_short) == (False, INVALID_RISK_GEOMETRY)


def test_risk_geometry_rejects_target_on_wrong_side_of_entry_zone():
    bad_long = candidate(
        direction="LONG",
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=99,
        target_1=100.5,
    )
    bad_short = candidate(
        direction="SHORT",
        entry_zone_low=100,
        entry_zone_high=101,
        invalidation_price=102,
        target_1=100.5,
    )

    assert validate_risk_geometry(bad_long) == (False, INVALID_RISK_GEOMETRY)
    assert validate_risk_geometry(bad_short) == (False, INVALID_RISK_GEOMETRY)


def test_orchestrator_filters_invalid_risk_geometry_before_returning_setups():
    class FakeScanner:
        def scan(self, ctx):
            return [candidate(
                direction="LONG",
                entry_zone_low=100,
                entry_zone_high=101,
                invalidation_price=100.5,
                target_1=103,
                reference_price=101,
                features={"stop_distance_ok": True, "trend_alignment": True},
            )]

    candle = Candle(1_777_294_700_000, 100, 102, 99, 101, 10)
    ctx = MarketContext(
        symbol="BTCUSDT", candles_5m=(candle,), candles_15m=(),
        candles_1h=(), candles_4h=(), indicators=IndicatorSnapshot(),
        market_regime="TREND_UP", levels=MarketLevels(),
        evaluated_at=datetime.now(timezone.utc),
    )
    orchestrator = ScannerOrchestrator(enabled_scanners=[])
    orchestrator.scanners = {"TREND_PULLBACK": FakeScanner()}

    candidates, stats = orchestrator.scan_all_with_stats(ctx)

    assert candidates == []
    assert stats["TREND_PULLBACK"]["candidates_found"] == 1
    assert stats["TREND_PULLBACK"]["setups_saved"] == 0


def test_repository_rejects_setup_without_candle_timestamp(tmp_path):
    repository = ScannerRepository(
        jsonl_path=str(tmp_path / "setups.jsonl"), backend="jsonl",
    )

    import pytest
    with pytest.raises(ValueError, match="signal_candle_open_time"):
        repository.save_setup(candidate(signal_candle_open_time=0))


def test_jsonl_repository_upserts_same_signal_candle(tmp_path):
    path = tmp_path / "setups.jsonl"
    repository = ScannerRepository(jsonl_path=str(path), backend="jsonl")
    first = candidate(state=SetupState.READY_TO_TRADE)
    repository.save_setup(first)
    repository.save_setup(candidate(
        reference_price=79_219.80, state=SetupState.READY_TO_TRADE
    ))

    records = repository.get_active_setups()
    assert len(records) == 1
    assert records[0]["setup_id"] == str(first.setup_id)
    assert records[0]["reference_price"] == 79_219.80
    assert records[0]["status"] == "READY_TO_TRADE"
