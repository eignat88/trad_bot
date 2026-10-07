from types import SimpleNamespace
from unittest.mock import patch

from app.scanners.momentum_exhaustion import MomentumExhaustionScanner


def test_me_short_scanner_persists_exact_trigger_5m_close():
    scanner = MomentumExhaustionScanner()

    candles_5m = []
    for i in range(14):
        candles_5m.append(
            SimpleNamespace(
                open=100.0,
                high=100.2,
                low=99.8,
                close=100.0,
                volume=100.0,
            )
        )

    trigger = SimpleNamespace(
        open=100.10,
        high=101.00,
        low=99.90,
        close=100.00,
        volume=100.0,
    )
    candles_5m.append(trigger)

    ctx = SimpleNamespace(
        candles_15m=[SimpleNamespace()] * 30,
        candles_5m=candles_5m,
        indicators=SimpleNamespace(rsi=70.0, atr=1.0),
        symbol="TESTUSDT",
        evaluated_at=None,
        market_regime="TEST",
    )

    swing_highs = [
        SimpleNamespace(price=99.8),
        SimpleNamespace(price=99.5),
    ]

    with patch(
        "app.scanners.momentum_exhaustion.find_swing_highs",
        return_value=swing_highs,
    ):
        candidate = scanner._scan_short(ctx)

    assert candidate is not None
    assert candidate.direction == "SHORT"

    # Authoritative source is the exact close of the trigger 5m candle.
    assert candidate.features["trigger_5m_close"] == trigger.close

    # It must not silently collapse to the structural reference price.
    assert candidate.reference_price != trigger.close


def test_me_short_quality_feature_builder_contract_unchanged():
    scanner = MomentumExhaustionScanner()

    candles_5m = [
        SimpleNamespace(
            open=100.1,
            high=100.3,
            low=99.9,
            close=100.0,
            volume=100.0,
        )
        for _ in range(15)
    ]

    features = scanner._build_short_features(
        candles_5m=candles_5m,
        prev_high=99.0,
        recent_high=101.0,
        entry=100.0,
        invalidation=101.2,
        target_1=98.0,
        atr=1.0,
        rsi=70.0,
    )

    assert "trigger_5m_close" not in features
    assert set(features) == {
        "exhaustion_magnitude",
        "body_ratio",
        "rsi_confirmation",
        "volume_ratio",
        "rr_ratio",
        "stop_distance_atr",
    }
