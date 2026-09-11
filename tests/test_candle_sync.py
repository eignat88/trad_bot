"""Unit tests for candle synchronization."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4

from app.analytics.candle_sync import CandleSync
from app.analytics.models import Candle, CandleRange, Gap, Watermark
from app.analytics.repository import AnalyticsRepository
from app.exchange.bybit_client import BybitClient


class TestCandleSync:
    """Tests for CandleSync."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_repo = Mock(spec=AnalyticsRepository)
        self.mock_bybit = Mock(spec=BybitClient)
        self.candle_sync = CandleSync(self.mock_bybit, self.mock_repo)

    def test_fetch_and_store_candles(self):
        """Test fetching and storing candles."""
        # Mock Bybit response
        self.mock_bybit._public_get.return_value = {
            "retCode": 0,
            "result": {
                "list": [
                    [1694505600000, "100.0", "105.0", "99.0", "103.0", "1000.0", "103000.0"],
                    [1694505900000, "103.0", "108.0", "102.0", "107.0", "1200.0", "128400.0"],
                ]
            }
        }
        
        # Mock repository
        self.mock_repo.insert_candles_batch.return_value = (2, 0, 0)
        
        inserted, updated, rejected, failed = self.candle_sync.fetch_and_store_candles(
            instrument_id=1,
            symbol="BTCUSDT",
            timeframe="5",
            from_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            to_time=datetime(2026, 9, 12, 6, 10, tzinfo=timezone.utc),
        )
        
        assert inserted == 2
        assert updated == 0
        assert rejected == 0
        assert failed == []
        self.mock_repo.insert_candles_batch.assert_called_once()

    def test_parse_candle(self):
        """Test parsing raw Bybit candle."""
        raw_candle = [1694505600000, "100.0", "105.0", "99.0", "103.0", "1000.0", "103000.0"]
        
        candle = self.candle_sync._parse_candle(
            raw_candle, instrument_id=1, timeframe="5"
        )
        
        assert candle is not None
        assert candle.instrument_id == 1
        assert candle.timeframe == "5"
        assert candle.open == 100.0
        assert candle.high == 105.0
        assert candle.low == 99.0
        assert candle.close == 103.0
        assert candle.volume == 1000.0
        assert candle.turnover == 103000.0
        assert candle.is_closed is True

    def test_parse_candle_invalid(self):
        """Test parsing invalid raw candle."""
        # Too few values
        raw_candle = [1694505600000, "100.0", "105.0"]
        
        candle = self.candle_sync._parse_candle(
            raw_candle, instrument_id=1, timeframe="5"
        )
        
        assert candle is None

    def test_merge_ranges(self):
        """Test merging overlapping ranges."""
        ranges = [
            CandleRange(
                instrument_id=1,
                timeframe="5",
                from_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                to_time=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
            ),
            CandleRange(
                instrument_id=1,
                timeframe="5",
                from_time=datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc),
                to_time=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            ),
        ]
        
        merged = self.candle_sync._merge_ranges(ranges)
        
        assert len(merged) == 1
        assert merged[0].from_time == datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        assert merged[0].to_time == datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    def test_timeframe_to_bybit(self):
        """Test timeframe conversion to Bybit format."""
        assert self.candle_sync._timeframe_to_bybit("1") == "1"
        assert self.candle_sync._timeframe_to_bybit("5") == "5"
        assert self.candle_sync._timeframe_to_bybit("15") == "15"
        assert self.candle_sync._timeframe_to_bybit("60") == "60"
        assert self.candle_sync._timeframe_to_bybit("D") == "D"
        assert self.candle_sync._timeframe_to_bybit("W") == "W"
        assert self.candle_sync._timeframe_to_bybit("M") == "M"

    def test_timeframe_to_minutes(self):
        """Test timeframe conversion to minutes."""
        assert self.candle_sync._timeframe_to_minutes("1") == 1
        assert self.candle_sync._timeframe_to_minutes("5") == 5
        assert self.candle_sync._timeframe_to_minutes("15") == 15
        assert self.candle_sync._timeframe_to_minutes("60") == 60
        assert self.candle_sync._timeframe_to_minutes("D") == 1440
        assert self.candle_sync._timeframe_to_minutes("W") == 10080
        assert self.candle_sync._timeframe_to_minutes("M") == 43200

    def test_reconcile_candles(self):
        """Test candle reconciliation."""
        # Mock repository
        self.mock_repo.get_candle_coverage.return_value = [
            {
                "gap_start": datetime(2026, 9, 12, 7, 0, tzinfo=timezone.utc),
                "gap_end": datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc),
            }
        ]
        
        # Mock fetch_and_store_candles to return success
        self.candle_sync.fetch_and_store_candles = MagicMock(return_value=(12, 0, 0, []))
        
        required_ranges = [
            CandleRange(
                instrument_id=1,
                timeframe="5",
                from_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                to_time=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
            )
        ]
        
        gaps, inserted, updated, rejected, failed = self.candle_sync.reconcile_candles(
            instrument_id=1,
            symbol="BTCUSDT",
            timeframe="5",
            required_ranges=required_ranges,
        )
        
        assert len(gaps) == 1
        assert inserted == 12
        assert updated == 0
        assert rejected == 0
        assert failed == []

    def test_check_post_exit_coverage(self):
        """Test post-exit coverage check."""
        # Mock repository
        self.mock_repo.get_candle_coverage.return_value = []
        
        coverage_complete = self.candle_sync.check_post_exit_coverage(
            instrument_id=1,
            timeframe="5",
            exit_time=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
            post_exit_horizon=timedelta(hours=4),
        )
        
        assert coverage_complete is True

    def test_check_post_exit_coverage_incomplete(self):
        """Test incomplete post-exit coverage."""
        # Mock repository
        self.mock_repo.get_candle_coverage.return_value = [
            {
                "gap_start": datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc),
                "gap_end": datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            }
        ]
        
        coverage_complete = self.candle_sync.check_post_exit_coverage(
            instrument_id=1,
            timeframe="5",
            exit_time=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
            post_exit_horizon=timedelta(hours=4),
        )
        
        assert coverage_complete is False