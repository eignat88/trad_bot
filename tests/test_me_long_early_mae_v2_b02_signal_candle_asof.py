from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch



class _repo:
    def __init__(self):
        self.inserted = []

    def ensure_instrument(self, symbol):
        return 1

    def insert_source_with_variants(self, source):
        self.inserted.append(source)
        return 1


def _candidate(offset_minutes):
    open_at = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
    detected_at = open_at + timedelta(minutes=offset_minutes)

    return SimpleNamespace(
        scanner_name='momentum_exhaustion_reverse_long_v1'.upper(),
        scanner_version='1.0.0',
        direction='long'.upper(),
        detected_at=detected_at,
        signal_candle_open_time=int(open_at.timestamp() * 1000),
        entry_timeframe='5m',
        target_1=110.0,
        setup_id='test-setup',
        symbol='testusdt',
        entry_zone_low=99.0,
        entry_zone_high=100.0,
        reference_price=100.0,
        invalidation_price=98.0,
        features={},
    )


def test_signal_candle_asof_boundaries():
    from app.research.me_r_long_early_mae_v2 import capture

    repo = _repo()
    adapter = getattr(capture, 'meearlymaev2sourceadapter'.replace(
        'meearlymaev2sourceadapter',
        'MeEarlyMaeV2SourceAdapter'
    ))(repo)

    with patch.object(capture._protocol, 'validate_activation_freeze'), patch.object(
        capture, 'require_activation_at',
        return_value=datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc),
    ):
        assert adapter.capture(_candidate(4)) is None
        assert adapter.capture(_candidate(5)) == 1
        assert adapter.capture(_candidate(6)) == 1

    assert len(repo.inserted) == 2
