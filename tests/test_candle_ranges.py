"""Unit tests for candle range planning."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from app.analytics.candle_ranges import CandleRangePlanner
from app.analytics.models import CandleRange, Gap


class TestCandleRangePlanner:
    """Tests for CandleRangePlanner."""

    def setup_method(self):
        """Set up test fixtures."""
        self.planner = CandleRangePlanner()

    def test_calculate_required_ranges_with_signal(self):
        """Test calculating required ranges with signal time."""
        signal_time = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        
        ranges = self.planner.calculate_required_ranges(
            instrument_id=1,
            timeframe="5",
            signal_time=signal_time,
            lookback_hours=24,
        )
        
        assert len(ranges) == 1
        assert ranges[0].instrument_id == 1
        assert ranges[0].timeframe == "5"
        assert ranges[0].from_time == signal_time - timedelta(hours=24)
        assert ranges[0].to_time > datetime.now(timezone.utc)

    def test_calculate_required_ranges_with_exit(self):
        """Test calculating required ranges with exit time."""
        exit_time = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
        
        ranges = self.planner.calculate_required_ranges(
            instrument_id=1,
            timeframe="5",
            exit_time=exit_time,
            post_exit_horizon=timedelta(hours=4),
        )
        
        assert len(ranges) == 1
        assert ranges[0].to_time == exit_time + timedelta(hours=4)

    def test_merge_overlapping_ranges(self):
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
        
        merged = self.planner.merge_ranges(ranges)
        
        assert len(merged) == 1
        assert merged[0].from_time == datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        assert merged[0].to_time == datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    def test_merge_adjacent_ranges(self):
        """Test merging adjacent ranges."""
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
                from_time=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
                to_time=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            ),
        ]
        
        merged = self.planner.merge_ranges(ranges)
        
        assert len(merged) == 1
        assert merged[0].from_time == datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        assert merged[0].to_time == datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    def test_merge_separate_ranges(self):
        """Test that separate ranges are not merged."""
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
                from_time=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
                to_time=datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc),
            ),
        ]
        
        merged = self.planner.merge_ranges(ranges)
        
        assert len(merged) == 2

    def test_extract_gaps(self):
        """Test extracting gaps from coverage."""
        required_ranges = [
            CandleRange(
                instrument_id=1,
                timeframe="5",
                from_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                to_time=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
            )
        ]
        
        existing_coverage = [
            {
                "gap_start": datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc),
                "gap_end": datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc),
            },
            {
                "gap_start": datetime(2026, 9, 12, 10, 30, tzinfo=timezone.utc),
                "gap_end": datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc),
            },
        ]
        
        gaps = self.planner.extract_gaps(
            instrument_id=1,
            timeframe="5",
            required_ranges=required_ranges,
            existing_coverage=existing_coverage,
        )
        
        assert len(gaps) == 2
        assert gaps[0].gap_start == datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc)
        assert gaps[0].gap_end == datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)

    def test_split_gaps_for_api(self):
        """Test splitting gaps for API batches."""
        gaps = [
            Gap(
                instrument_id=1,
                timeframe="5",
                gap_start=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                gap_end=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
            ),
            Gap(
                instrument_id=1,
                timeframe="5",
                gap_start=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
                gap_end=datetime(2026, 9, 12, 16, 0, tzinfo=timezone.utc),
            ),
        ]
        
        batches = self.planner.split_gaps_for_api(gaps, max_batch_size=100)
        
        # 4 hours = 48 candles (5m timeframe), so should be split
        assert len(batches) >= 1

    def test_empty_ranges(self):
        """Test handling empty ranges."""
        merged = self.planner.merge_ranges([])
        assert merged == []

    def test_calculate_ranges_for_trades(self):
        """Test calculating ranges for multiple trades."""
        trades = [
            {
                "instrument_id": 1,
                "signal_time": datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
                "entry_time": datetime(2026, 9, 12, 6, 5, tzinfo=timezone.utc),
                "exit_time": datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
            },
            {
                "instrument_id": 2,
                "signal_time": datetime(2026, 9, 12, 7, 0, tzinfo=timezone.utc),
                "entry_time": datetime(2026, 9, 12, 7, 5, tzinfo=timezone.utc),
                "exit_time": datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc),
            },
        ]
        
        ranges_by_instrument = self.planner.calculate_ranges_for_trades(
            trades=trades,
            timeframe="5",
            post_exit_horizon=timedelta(hours=4),
        )
        
        assert 1 in ranges_by_instrument
        assert 2 in ranges_by_instrument
        assert len(ranges_by_instrument[1]) >= 1
        assert len(ranges_by_instrument[2]) >= 1