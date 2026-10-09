"""ME clean OOS temporal provenance validation."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.scanners.models import SetupCandidate
from app.shadow.me_r_long_close_location_oos_runner import (
    MERLongCLoOosAck,
    MERLongCLoOosObserver,
)


def candidate(
    candle_open="2026-10-09T13:30:00+00:00",
    source="2026-10-09T13:30:00+00:00",
    decision="2026-10-09T13:35:10+00:00",
):
    open_time = datetime.fromisoformat(candle_open)
    decision_time = datetime.fromisoformat(decision)

    return SetupCandidate(
        scanner_name="ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1",
        scanner_version="1.2.0",
        symbol="BTCUSDT",
        direction="LONG",
        detected_at=decision_time,
        features={
            "oos_clean_observer_version": "1.2.0",
            "oos_signal_open": 101.0,
            "oos_signal_high": 103.0,
            "oos_signal_low": 99.0,
            "oos_signal_close": 100.0,
            "oos_signal_volume": 1200.0,
            "oos_signal_entry_price": 100.0,
            "oos_signal_candle_open_ms": int(open_time.timestamp() * 1000),
            "close_location_source_timestamp": source,
            "close_location": 0.25,
            "close_location_threshold": 0.70,
            "close_location_passed": False,
        },
    )


@pytest.mark.parametrize(
    "candle_open,source,decision",
    [
        # Decision before the 5m candle has closed.
        ("2026-10-09T13:30:00+00:00",
         "2026-10-09T13:30:00+00:00",
         "2026-10-09T13:34:59+00:00"),

        # Source timestamp after decision.
        ("2026-10-09T13:30:00+00:00",
         "2026-10-09T13:36:00+00:00",
         "2026-10-09T13:35:10+00:00"),

        # Source candle timestamp disagrees with actual candle open.
        ("2026-10-09T13:30:00+00:00",
         "2026-10-09T13:25:00+00:00",
         "2026-10-09T13:35:10+00:00"),
    ],
)
def test_inconsistent_provenance_is_rejected(
    candle_open, source, decision
):
    repo = MagicMock()
    observer = MERLongCLoOosObserver(repo)

    ack = observer.observe(candidate(candle_open, source, decision))

    assert ack == MERLongCLoOosAck.INVALID_SOURCE
    repo.save_signal.assert_not_called()


def test_valid_closed_candle_reaches_repository():
    repo = MagicMock()
    observer = MERLongCLoOosObserver(repo)

    observer.observe(candidate())

    repo.save_signal.assert_called_once()

    saved = repo.save_signal.call_args.kwargs

    assert saved["signal_candle_open_time"] == datetime(
        2026, 10, 9, 13, 30, tzinfo=timezone.utc
    )
    assert saved["signal_time"] == datetime(
        2026, 10, 9, 13, 30, tzinfo=timezone.utc
    )
