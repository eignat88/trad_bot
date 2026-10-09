"""ME clean observer invalid-source contract."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.scanners.models import SetupCandidate
from app.shadow.me_r_long_close_location_oos_runner import (
    MERLongCLoOosAck,
    MERLongCLoOosObserver,
)


def make_candidate(**overrides):
    features = {
        "oos_clean_observer_version": "1.2.0",
        "oos_signal_open": 101.0,
        "oos_signal_high": 103.0,
        "oos_signal_low": 99.0,
        "oos_signal_close": 100.0,
        "oos_signal_volume": 1200.0,
        "oos_signal_entry_price": 100.0,
        "oos_signal_candle_open_ms": 1791552600000,
        "close_location_source_timestamp": "2026-10-09T13:30:00+00:00",
        "close_location": 0.25,
        "close_location_threshold": 0.70,
        "close_location_passed": False,
    }
    features.update(overrides)

    return SetupCandidate(
        scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=datetime(
            2026, 10, 9, 13, 35, tzinfo=timezone.utc
        ),
        features=features,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("oos_signal_open", float("nan")),
        ("oos_signal_high", float("inf")),
        ("oos_signal_low", float("-inf")),
        ("oos_signal_close", "not-a-number"),
        ("oos_signal_volume", float("nan")),
        ("oos_signal_entry_price", float("inf")),
        ("oos_signal_candle_open_ms", "invalid"),
        ("close_location_source_timestamp", "bad-timestamp"),
    ],
)
def test_invalid_source_returns_ack_not_exception(field, value):
    repo = MagicMock()
    observer = MERLongCLoOosObserver(repo)

    ack = observer.observe(make_candidate(**{field: value}))

    assert ack == MERLongCLoOosAck.INVALID_SOURCE
    assert observer.get_stats()["invalid_source"] == 1
    repo.save_signal.assert_not_called()


def test_wrong_scanner_version_is_not_persisted():
    repo = MagicMock()
    observer = MERLongCLoOosObserver(repo)

    candidate = replace(
        make_candidate(),
        scanner_version="1.0.0",
    )

    ack = observer.observe(candidate)

    assert ack == MERLongCLoOosAck.SKIPPED_VERSION
    repo.save_signal.assert_not_called()
