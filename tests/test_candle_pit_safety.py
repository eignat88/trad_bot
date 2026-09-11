"""Tests for point-in-time candle safety (cutoff enforcement)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from app.analytics.candle_sync import CandleSync
from app.analytics.models import QualityStatus


class TestParseCandlePITSafety:
    """Tests that _parse_candle respects observation_cutoff."""

    def setup_method(self):
        self.mock_bybit = MagicMock()
        self.mock_repo = MagicMock()
        self.sync = CandleSync(self.mock_bybit, self.mock_repo)

    def test_candle_within_cutoff_persists(self):
        """Test that a candle with close_time <= cutoff is persisted."""
        # Use a timestamp in milliseconds
        open_time_ms = int(datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        raw = [open_time_ms, "100", "105", "99", "103", "1000", "103000"]
        cutoff = datetime(2026, 9, 11, 15, 10, tzinfo=timezone.utc)
        
        candle = self.sync._parse_candle(raw, instrument_id=1, timeframe="5",
                                          observation_cutoff=cutoff)
        
        assert candle is not None
        assert candle.close_time <= cutoff

    def test_candle_after_cutoff_rejected(self):
        """Test that a candle with close_time > cutoff is NOT persisted."""
        # Candle: open 15:05, close 15:10
        open_time_ms = int(datetime(2026, 9, 11, 15, 5, tzinfo=timezone.utc).timestamp() * 1000)
        raw = [open_time_ms, "100", "105", "99", "103", "1000", "103000"]
        cutoff = datetime(2026, 9, 11, 15, 7, 16, tzinfo=timezone.utc)
        
        candle = self.sync._parse_candle(raw, instrument_id=1, timeframe="5",
                                          observation_cutoff=cutoff)
        
        assert candle is None

    def test_candle_no_cutoff_always_persists(self):
        """Test that without cutoff, all candles are persisted."""
        open_time_ms = int(datetime(2026, 9, 11, 15, 5, tzinfo=timezone.utc).timestamp() * 1000)
        raw = [open_time_ms, "100", "105", "99", "103", "1000", "103000"]
        
        candle = self.sync._parse_candle(raw, instrument_id=1, timeframe="5",
                                          observation_cutoff=None)
        
        assert candle is not None


class TestFetchRangeClamping:
    """Tests that fetch ranges are clamped to observation_cutoff."""

    def setup_method(self):
        self.mock_bybit = MagicMock()
        self.mock_repo = MagicMock()
        self.sync = CandleSync(self.mock_bybit, self.mock_repo)

    def test_fetch_range_clamped_to_cutoff(self):
        """Test that to_time is clamped to observation_cutoff."""
        cutoff = datetime(2026, 9, 11, 15, 7, 16, tzinfo=timezone.utc)
        
        # Mock Bybit to return empty
        self.mock_bybit._public_get.return_value = {"retCode": 0, "result": {"list": []}}
        
        inserted, updated, rejected, failed = self.sync.fetch_and_store_candles(
            instrument_id=1,
            symbol="BTCUSDT",
            timeframe="5",
            from_time=datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc),
            to_time=datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc),  # 3 hours
            observation_cutoff=cutoff,
        )
        
        # Verify Bybit was called with clamped end time
        call_args = self.mock_bybit._public_get.call_args
        # The 'end' parameter should be <= cutoff
        end_ms = call_args[1].get("end") or call_args[0][1].get("end")
        if end_ms:
            end_time = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
            assert end_time <= cutoff

    def test_fetch_range_not_clamped_without_cutoff(self):
        """Test that without cutoff, full range is used."""
        self.mock_bybit._public_get.return_value = {"retCode": 0, "result": {"list": []}}
        
        inserted, updated, rejected, failed = self.sync.fetch_and_store_candles(
            instrument_id=1,
            symbol="BTCUSDT",
            timeframe="5",
            from_time=datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc),
            to_time=datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc),
            observation_cutoff=None,
        )
        
        # Should be called (no clamping)
        assert self.mock_bybit._public_get.call_count > 0

    def test_fetch_from_after_cutoff_returns_empty(self):
        """Test that from_time >= to_time (after clamping) returns empty."""
        cutoff = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)
        
        inserted, updated, rejected, failed = self.sync.fetch_and_store_candles(
            instrument_id=1,
            symbol="BTCUSDT",
            timeframe="5",
            from_time=datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc),  # after cutoff
            to_time=datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc),
            observation_cutoff=cutoff,
        )
        
        assert inserted == 0
        assert updated == 0
        assert rejected == 0
        assert failed == []
        # Bybit should NOT be called
        self.mock_bybit._public_get.assert_not_called()


class TestIncompleteCandleFiltering:
    """Tests that incomplete candles from Bybit are filtered."""

    def setup_method(self):
        self.mock_bybit = MagicMock()
        self.mock_repo = MagicMock()
        self.sync = CandleSync(self.mock_bybit, self.mock_repo)

    def test_partial_candle_skipped(self):
        """Test that a candle partially beyond cutoff is skipped."""
        # Candle open 15:00, close 15:05 — but cutoff is 15:03
        open_time_ms = int(datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        raw = [open_time_ms, "100", "105", "99", "103", "1000", "103000"]
        cutoff = datetime(2026, 9, 11, 15, 3, tzinfo=timezone.utc)
        
        candle = self.sync._parse_candle(raw, instrument_id=1, timeframe="5",
                                          observation_cutoff=cutoff)
        
        # Candle close_time is 15:05, cutoff is 15:03 -> rejected
        assert candle is None

    def test_boundary_candle_accepted(self):
        """Test that a candle with close_time == cutoff is accepted."""
        # Candle open 15:00, close 15:05
        open_time_ms = int(datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        raw = [open_time_ms, "100", "105", "99", "103", "1000", "103000"]
        cutoff = datetime(2026, 9, 11, 15, 5, tzinfo=timezone.utc)
        
        candle = self.sync._parse_candle(raw, instrument_id=1, timeframe="5",
                                          observation_cutoff=cutoff)
        
        assert candle is not None
        assert candle.close_time == cutoff

    def test_fetch_and_store_filters_incomplete(self):
        """Test that fetch_and_store filters incomplete candles."""
        cutoff = datetime(2026, 9, 11, 15, 7, 16, tzinfo=timezone.utc)
        
        # Mock Bybit to return two candles:
        # 1) 15:00-15:05 (complete, before cutoff)
        # 2) 15:05-15:10 (incomplete, close_time > cutoff)
        open1 = int(datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
        open2 = int(datetime(2026, 9, 11, 15, 5, tzinfo=timezone.utc).timestamp() * 1000)
        
        self.mock_bybit._public_get.return_value = {
            "retCode": 0,
            "result": {
                "list": [
                    [open1, "100", "105", "99", "103", "1000", "103000"],  # 15:00
                    [open2, "100", "105", "99", "103", "1000", "103000"],  # 15:05
                ]
            }
        }
        self.mock_repo.insert_candles_batch.return_value = (1, 0, 0)
        
        inserted, updated, rejected, failed = self.sync.fetch_and_store_candles(
            instrument_id=1,
            symbol="BTCUSDT",
            timeframe="5",
            from_time=datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc),
            to_time=cutoff,
            observation_cutoff=cutoff,
        )
        
        # Only 1 candle should be inserted (the complete one)
        assert inserted == 1
        
        # Verify only 1 candle was passed to repository
        call_args = self.mock_repo.insert_candles_batch.call_args
        candles = call_args[0][0]
        assert len(candles) == 1
        assert candles[0].close_time <= cutoff