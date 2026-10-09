from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.scanners.models import SetupCandidate
from app.shadow.me_r_long_close_location_oos_runner import MERLongCLoOosObserver


def test_clean_observer_persists_real_candle_and_timing():
    repo = MagicMock()
    decision = datetime(2026, 10, 9, 13, 35, 10, tzinfo=timezone.utc)
    candle_open_ms = int(
        datetime(2026, 10, 9, 13, 30, tzinfo=timezone.utc).timestamp() * 1000
    )

    candidate = SetupCandidate(
        scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=decision,
        reference_price=105.0,
        entry_zone_low=99.8,
        entry_zone_high=100.1,
        invalidation_price=97.5,
        features={
            "oos_clean_observer_version": "1.2.0",
            "oos_signal_open": 101.0,
            "oos_signal_high": 103.0,
            "oos_signal_low": 99.0,
            "oos_signal_close": 100.0,
            "oos_signal_volume": 1200.0,
            "oos_signal_entry_price": 100.0,
            "oos_signal_candle_open_ms": candle_open_ms,
            "close_location": 0.25,
            "close_location_threshold": 0.70,
            "close_location_passed": False,
        },
    )

    MERLongCLoOosObserver(repo).observe(candidate)

    repo.save_signal.assert_called_once()
    saved = repo.save_signal.call_args.kwargs

    assert saved["open"] == 101.0
    assert saved["high"] == 103.0
    assert saved["low"] == 99.0
    assert saved["close"] == 100.0
    assert saved["volume"] == 1200.0

    assert saved["signal_price"] == 100.0
    assert saved["signal_price"] != candidate.reference_price
    assert saved["decision_time"] == decision
    assert saved["signal_candle_open_time"] == datetime(
        2026, 10, 9, 13, 30, tzinfo=timezone.utc
    )
    assert saved["filter_passed"] is False
    assert saved["signal_version"] == "1.2.0"


def test_non_clean_candidate_is_not_persisted():
    repo = MagicMock()
    candidate = SetupCandidate(
        scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
        symbol="BTCUSDT",
        features={"close_location": 0.75},
    )

    MERLongCLoOosObserver(repo).observe(candidate)

    repo.save_signal.assert_not_called()


@pytest.mark.parametrize(
    "missing_field",
    [
        "oos_signal_open",
        "oos_signal_high",
        "oos_signal_low",
        "oos_signal_close",
        "oos_signal_volume",
        "oos_signal_entry_price",
        "oos_signal_candle_open_ms",
    ],
)
def test_missing_clean_field_is_not_persisted(missing_field):
    repo = MagicMock()

    features = {
        "oos_clean_observer_version": "1.2.0",
        "oos_signal_open": 101.0,
        "oos_signal_high": 103.0,
        "oos_signal_low": 99.0,
        "oos_signal_close": 100.0,
        "oos_signal_volume": 1200.0,
        "oos_signal_entry_price": 100.0,
        "oos_signal_candle_open_ms": 1791552600000,
    }
    features.pop(missing_field)

    candidate = SetupCandidate(
        scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        features=features,
    )

    MERLongCLoOosObserver(repo).observe(candidate)

    repo.save_signal.assert_not_called()
