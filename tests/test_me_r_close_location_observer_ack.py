"""ME clean observer ACK and accounting contract."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.scanners.models import SetupCandidate
from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosSaveResult,
    MERLongCLoOosSaveStatus,
)
from app.shadow.me_r_long_close_location_oos_runner import (
    MERLongCLoOosAck,
    MERLongCLoOosObserver,
    observe_oos_candidate,
)


def candidate(version="1.2.0", missing=None):
    features = {
        "oos_clean_observer_version": version,
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
    if missing:
        features.pop(missing)

    return SetupCandidate(
        scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
        scanner_version=version,
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=datetime(2026, 10, 9, 13, 35, tzinfo=timezone.utc),
        features=features,
    )


@pytest.mark.parametrize(
    "save_status,expected",
    [
        (MERLongCLoOosSaveStatus.INSERTED, MERLongCLoOosAck.INSERTED),
        (MERLongCLoOosSaveStatus.DUPLICATE, MERLongCLoOosAck.DUPLICATE),
        (MERLongCLoOosSaveStatus.ERROR, MERLongCLoOosAck.ERROR),
    ],
)
def test_persistence_ack_and_counters(save_status, expected):
    repo = MagicMock()
    repo.save_signal.return_value = MERLongCLoOosSaveResult(
        save_status,
        signal_id=42 if save_status == MERLongCLoOosSaveStatus.INSERTED else None,
    )

    observer = MERLongCLoOosObserver(repo)
    assert observer.observe(candidate()) == expected

    stats = observer.get_stats()
    assert stats["inserted"] == int(expected == MERLongCLoOosAck.INSERTED)
    assert stats["duplicates"] == int(expected == MERLongCLoOosAck.DUPLICATE)
    assert stats["errors"] == int(expected == MERLongCLoOosAck.ERROR)
    repo.save_signal.assert_called_once()


def test_missing_field_returns_invalid_source():
    repo = MagicMock()
    observer = MERLongCLoOosObserver(repo)

    assert observer.observe(candidate(missing="oos_signal_high")) == (
        MERLongCLoOosAck.INVALID_SOURCE
    )
    assert observer.get_stats()["invalid_source"] == 1
    repo.save_signal.assert_not_called()


def test_nonclean_version_returns_skipped():
    repo = MagicMock()
    observer = MERLongCLoOosObserver(repo)

    assert observer.observe(candidate(version="1.0.0")) == (
        MERLongCLoOosAck.SKIPPED_VERSION
    )
    assert observer.get_stats()["skipped_version"] == 1
    repo.save_signal.assert_not_called()


def test_convenience_function_exposes_ack():
    repo = MagicMock()
    repo.save_signal.return_value = MERLongCLoOosSaveResult(
        MERLongCLoOosSaveStatus.INSERTED, signal_id=42
    )

    assert observe_oos_candidate(repo, candidate()) == (
        MERLongCLoOosAck.INSERTED
    )
