from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.scanners.context_builder import _build_indicators
from app.scanners.me_r_long_close_location_oos_validation import (
    MERLongCloseLocationOOSValidationV1Scanner,
)


@dataclass
class Candle:
    timestamp: datetime
    open: float = 100.0
    high: float = 102.0
    low: float = 98.0
    close: float = 100.0
    volume: float = 1000.0


@dataclass
class Context:
    evaluated_at: datetime
    candles_5m: tuple
    candles_15m: tuple
    candles_1h: tuple
    indicators: object
    market_regime: str = "UNKNOWN"


def make_candles(minutes, count, asof):
    end = asof.replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % minutes)

    start = end - timedelta(minutes=minutes * (count - 1))

    return tuple(
        Candle(
            timestamp=start + timedelta(minutes=i * minutes),
            close=100.0 + i * 0.2,
        )
        for i in range(count)
    )


def test_frozen_v1_receives_closed_htf_context():
    asof = datetime(2026, 10, 9, 13, 32, tzinfo=timezone.utc)

    candles_5m = make_candles(5, 40, asof)
    candles_15m = make_candles(15, 40, asof)
    candles_1h = make_candles(60, 40, asof)

    contaminated = MagicMock()
    contaminated.rsi = 99.0
    contaminated.atr = 99999.0

    ctx = Context(
        evaluated_at=asof,
        candles_5m=candles_5m,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        indicators=contaminated,
    )

    scanner = MERLongCloseLocationOOSValidationV1Scanner()
    base = MagicMock()
    base.scan.return_value = []
    scanner._base_scanner = base

    result = scanner.scan(ctx)

    assert result == []
    base.scan.assert_called_once()

    clean = base.scan.call_args.args[0]

    for series, interval in (
        (clean.candles_5m, 5),
        (clean.candles_15m, 15),
        (clean.candles_1h, 60),
    ):
        violations = [
            (
                c.timestamp,
                c.timestamp + timedelta(minutes=interval),
            )
            for c in series
            if c.timestamp + timedelta(minutes=interval) > asof
        ]
        assert not violations, (
            f"{interval}m unclosed candles passed to frozen V1: "
            f"{violations}"
        )

    expected = _build_indicators(list(clean.candles_1h))

    assert clean.indicators.rsi == pytest.approx(expected.rsi)
    assert clean.indicators.atr == pytest.approx(expected.atr)

    assert clean.indicators.rsi != contaminated.rsi
    assert clean.indicators.atr != contaminated.atr

    assert ctx.indicators is contaminated
    assert ctx.candles_1h is candles_1h
    assert scanner.version == "1.2.0"
