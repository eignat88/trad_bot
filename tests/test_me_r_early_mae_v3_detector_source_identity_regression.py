from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.research.me_r_long_early_mae_v3 import detector
from app.research.me_r_long_early_mae_v3.identity import source_identity
from app.research.me_r_long_early_mae_v3.protocol import SCANNER_NAME


def test_valid_signal_produces_stable_source_identity(monkeypatch):
    opened = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(minutes=5)
    fetched = closed + timedelta(seconds=2)
    detected = closed + timedelta(seconds=3)

    signal = SimpleNamespace(open_at=opened, close_at=closed)
    context = SimpleNamespace(
        symbol="BTCUSDT",
        signal_candle=signal,
        source_fetch_started_at=closed + timedelta(seconds=1),
        source_fetch_completed_at=fetched,
    )
    candidate = SimpleNamespace(
        signal_candle_open_time=int(opened.timestamp() * 1000),
        detected_at=detected,
        entry_zone_low=99,
        entry_zone_high=101,
        reference_price=100,
        invalidation_price=97.5,
        target_1=103,
        features={},
    )

    # Exercise source payload creation with real temporal validation.
    first = detector._source_payload(context, candidate)
    second = detector._source_payload(context, candidate)

    expected = source_identity("BTCUSDT", SCANNER_NAME, opened)
    assert first["source_setup_id"] == expected
    assert second["source_setup_id"] == expected
    assert first["source_candle_open_at"] == opened
    assert first["source_candle_close_at"] == closed
