from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.research.me_r_long_early_mae_v2.evaluator import MeEarlyMaeV2Evaluator


def test_detected_source_waits_until_expiry_without_fetching_candles():
    detected_at = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    repo = MagicMock()

    repo.load_pending_sources.return_value = [{
        "source_event_id": 1001,
        "source_status": "DETECTED",
        "detected_at": detected_at,
        "source_available_at": detected_at,
        "entry_observable_at": detected_at,
        "instrument_id": 1,
    }]

    evaluator = MeEarlyMaeV2Evaluator(repo)
    stats = evaluator.run_cycle(detected_at + timedelta(minutes=239))

    assert stats["errors"] == 0
    assert stats["sources"] == 1
    assert stats["fills"] == 0

    repo.fetch_candles.assert_not_called()
    repo.mark_source_not_filled.assert_not_called()
    repo.update_source_entry.assert_not_called()



def test_expired_source_with_missing_candles_remains_pending():
    detected_at = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    repo = MagicMock()

    repo.load_pending_sources.return_value = [{
        "source_event_id": 1002,
        "source_status": "DETECTED",
        "detected_at": detected_at,
        "source_available_at": detected_at,
        "entry_observable_at": detected_at,
        "instrument_id": 1,
        "entry_zone_low": 99.8,
        "entry_zone_high": 100.1,
        "invalidation_price": 97.5,
        "target_price": 103.0,
    }]
    repo.fetch_candles.return_value = []

    evaluator = MeEarlyMaeV2Evaluator(repo)
    stats = evaluator.run_cycle(detected_at + timedelta(minutes=245))

    assert stats["errors"] == 0
    assert stats["sources"] == 1
    assert stats["fills"] == 0
    assert stats["incomplete_entry_coverage"] == 1

    repo.fetch_candles.assert_called_once()
    repo.mark_source_not_filled.assert_not_called()
    repo.update_source_entry.assert_not_called()



def test_expired_source_with_full_coverage_and_no_touch_is_not_filled():
    from app.research.me_r_long_early_mae_v2.candles import ClosedCandle

    detected_at = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    repo = MagicMock()
    repo.load_pending_sources.return_value = [{
        "source_event_id": 1003,
        "source_status": "DETECTED",
        "detected_at": detected_at,
        "source_available_at": detected_at,
        "entry_observable_at": detected_at,
        "instrument_id": 1,
        "entry_zone_low": 99.8,
        "entry_zone_high": 100.1,
        "invalidation_price": 97.5,
        "target_price": 103.0,
    }]

    repo.fetch_candles.return_value = [
        ClosedCandle(
            open_at=detected_at + timedelta(minutes=5 * i),
            close_at=detected_at + timedelta(minutes=5 * (i + 1)),
            open=101.0,
            high=101.5,
            low=100.5,
            close=101.0,
        )
        for i in range(48)
    ]

    evaluator = MeEarlyMaeV2Evaluator(repo)
    stats = evaluator.run_cycle(detected_at + timedelta(minutes=245))

    assert stats["errors"] == 0
    assert stats["sources"] == 1
    assert stats["fills"] == 0

    repo.mark_source_not_filled.assert_called_once_with(
        1003, "ENTRY_NOT_FILLED"
    )
    repo.update_source_entry.assert_not_called()



def test_expired_source_with_touch_records_virtual_fill():
    from app.research.me_r_long_early_mae_v2.candles import ClosedCandle

    detected_at = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
    repo = MagicMock()
    repo.load_pending_sources.return_value = [{
        "source_event_id": 1004,
        "source_status": "DETECTED",
        "detected_at": detected_at,
        "source_available_at": detected_at,
        "entry_observable_at": detected_at,
        "instrument_id": 1,
        "entry_zone_low": 99.8,
        "entry_zone_high": 100.1,
        "invalidation_price": 97.5,
        "target_price": 103.0,
    }]

    repo.fetch_candles.return_value = [
        ClosedCandle(
            open_at=detected_at + timedelta(minutes=5 * i),
            close_at=detected_at + timedelta(minutes=5 * (i + 1)),
            open=101.0,
            high=101.5,
            low=99.9 if i == 2 else 100.5,
            close=101.0,
        )
        for i in range(48)
    ]

    evaluator = MeEarlyMaeV2Evaluator(repo)
    stats = evaluator.run_cycle(detected_at + timedelta(minutes=245))

    assert stats["errors"] == 0
    assert stats["sources"] == 1
    assert stats["fills"] == 1

    repo.update_source_entry.assert_called_once()
    args, _ = repo.update_source_entry.call_args

    assert args[0] == 1004
    assert args[1]["entry_filled_at"] == detected_at + timedelta(minutes=15)
    assert args[1]["entry_price"] == 100.1
    assert args[1]["virtual_stop_price"] == 97.5
    assert args[1]["virtual_target_price"] == 103.0

    repo.ensure_variants.assert_called_once_with(1004)
    repo.mark_source_not_filled.assert_not_called()
