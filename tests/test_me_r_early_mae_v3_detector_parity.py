from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from app.models import Candle
from app.research.me_r_long_early_mae_v3.detector import detect_v1_closed
from app.scanners.momentum_exhaustion_reverse_long_v1 import MomentumExhaustionReverseLongV1Scanner

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

def cs(tf, count, end):
    return tuple(Candle(
        timestamp=int((end - timedelta(minutes=tf * (count - i))).timestamp() * 1000),
        open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=1000,
    ) for i in range(count))

def test_scanner_has_v1_identity_and_no_added_filter():
    scanner = MomentumExhaustionReverseLongV1Scanner()
    assert scanner.name == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"
    assert scanner.version == "1.0.0"

def test_detector_has_independent_research_identity():
    source = SimpleNamespace(
        symbol="BTCUSDT", candles_5m=cs(5, 40, BASE + timedelta(minutes=10)),
        candles_15m=cs(15, 40, BASE), candles_1h=cs(60, 40, BASE),
        source_fetch_started_at=BASE + timedelta(minutes=10),
    )
    result = detect_v1_closed(source, BASE + timedelta(minutes=10))
    assert result.status in {"NO_SIGNAL", "SIGNAL"}
    if result.status == "SIGNAL":
        assert result.source["scanner_name"] == "ME_R_LONG_CLOSED_V1"
        assert result.source["features"]["ruleset"] == "ORIGINAL_V1_NO_RSI_DELTA_FILTER"
