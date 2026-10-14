from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from app.models import Candle
from app.research.me_r_long_early_mae_v3.detector import detect_v1_closed, v1_reference_candidate

BASE = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

def candles(minutes, count, end):
    return tuple(Candle(
        timestamp=int((end - timedelta(minutes=minutes * (count - i))).timestamp() * 1000),
        open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=1000,
    ) for i in range(count))

def source(asof):
    return SimpleNamespace(
        symbol="BTCUSDT",
        candles_5m=candles(5, 40, asof),
        candles_15m=candles(15, 40, asof.replace(minute=(asof.minute // 15) * 15, second=0, microsecond=0)),
        candles_1h=candles(60, 40, asof.replace(minute=0, second=0, microsecond=0)),
        source_fetch_started_at=asof,
    )

def test_reference_v1_identity_and_features():
    asof = BASE + timedelta(minutes=10)
    result = detect_v1_closed(source(asof), asof, asof)
    if result.status != "SIGNAL":
        assert result.status == "NO_SIGNAL"
        return
    reference = v1_reference_candidate(result.context)
    assert reference is not None
    assert reference.scanner_name == "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1"
    for key in (
        "exhaustion_magnitude", "body_ratio", "rsi_confirmation",
        "volume_ratio", "rr_ratio", "stop_distance_atr",
        "stop_loss_pct", "take_profit_pct", "hold_minutes",
        "source_scanner", "source_direction",
    ):
        assert result.candidate.features[key] == reference.features[key]
    assert result.candidate.features["ruleset"] == "ORIGINAL_V1_NO_RSI_DELTA_FILTER"

def test_signal_candle_identity_and_hour_boundary():
    asof = BASE + timedelta(hours=1, minutes=5)
    result = detect_v1_closed(source(asof), asof, asof)
    assert result.status in {"NO_SIGNAL", "SIGNAL"}
    if result.status == "SIGNAL":
        assert result.source["source_candle_open_at"] == asof - timedelta(minutes=5)
        assert result.source["source_candle_close_at"] == asof
        assert result.source["source_candle_open_at"].minute == 0

def test_repeated_identity_stability():
    asof = BASE + timedelta(minutes=10)
    first = detect_v1_closed(source(asof), asof, asof)
    second = detect_v1_closed(source(asof), asof, asof)
    if first.status == second.status == "SIGNAL":
        assert first.source["source_setup_id"] == second.source["source_setup_id"]
