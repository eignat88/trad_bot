"""Unit tests for candle synchronization."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock, patch
from uuid import uuid4

from app.analytics.candle_sync import CandleSync, normalize_timeframe
from app.analytics.models import Candle, CandleRange, Gap, Watermark, QualityStatus
from app.analytics.repository import AnalyticsRepository
from app.exchange.bybit_client import BybitClient


class TestNormalizeTimeframe:
    """Tests for normalize_timeframe function."""

    def test_direct_numeric(self):
        """Test direct numeric timeframes."""
        assert normalize_timeframe("1") == "1"
        assert normalize_timeframe("5") == "5"
        assert normalize_timeframe("15") == "15"
        assert normalize_timeframe("60") == "60"
        assert normalize_timeframe("240") == "240"

    def test_suffix_minutes(self):
        """Test minute suffix timeframes."""
        assert normalize_timeframe("1m") == "1"
        assert normalize_timeframe("5m") == "5"
        assert normalize_timeframe("15m") == "15"
        assert normalize_timeframe("30m") == "30"

    def test_suffix_hours(self):
        """Test hour suffix timeframes."""
        assert normalize_timeframe("1h") == "60"
        assert normalize_timeframe("2h") == "120"
        assert normalize_timeframe("4h") == "240"

    def test_special_timeframes(self):
        """Test special timeframes."""
        assert normalize_timeframe("D") == "D"
        assert normalize_timeframe("1D") == "D"
        assert normalize_timeframe("day") == "D"
        assert normalize_timeframe("W") == "W"
        assert normalize_timeframe("M") == "M"

    def test_case_insensitive(self):
        """Test case insensitivity."""
        assert normalize_timeframe("5M") == "5"
        assert normalize_timeframe("1H") == "60"
        assert normalize_timeframe("1d") == "D"

    def test_invalid_timeframe(self):
        """Test invalid timeframe raises ValueError."""
        with pytest.raises(ValueError, match="Unrecognized timeframe"):
            normalize_timeframe("abc")
        
        with pytest.raises(ValueError, match="Unrecognized timeframe"):
            normalize_timeframe("5x")
        
        with pytest.raises(ValueError, match="Unrecognized timeframe"):
            normalize_timeframe("")


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

    def test_parse_candle_quality_status(self):
        """Test that parsed candle has QualityStatus.VALIDATED, not string."""
        raw_candle = [1694505600000, "100.0", "105.0", "99.0", "103.0", "1000.0", "103000.0"]
        
        candle = self.candle_sync._parse_candle(
            raw_candle, instrument_id=1, timeframe="5"
        )
        
        assert candle is not None
        assert candle.quality_status == QualityStatus.VALIDATED
        assert isinstance(candle.quality_status, QualityStatus)

    def test_parse_candle_inserted_to_postgresql(self):
        """Test that a parsed candle can be inserted to PostgreSQL without rejection."""
        # Mock Bybit response with a valid candle
        self.mock_bybit._public_get.return_value = {
            "retCode": 0,
            "result": {
                "list": [
                    [1694505600000, "100.0", "105.0", "99.0", "103.0", "1000.0", "103000.0"],
                ]
            }
        }
        
        self.mock_repo.insert_candles_batch.return_value = (1, 0, 0)
        
        inserted, updated, rejected, failed = self.candle_sync.fetch_and_store_candles(
            instrument_id=1,
            symbol="BTCUSDT",
            timeframe="5",
            from_time=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc),
            to_time=datetime(2026, 9, 12, 6, 10, tzinfo=timezone.utc),
        )
        
        assert inserted > 0
        assert rejected == 0
        assert failed == []
        
        # Verify the candle passed to repository has correct quality_status
        call_args = self.mock_repo.insert_candles_batch.call_args
        candles = call_args[0][0]
        assert len(candles) == 1
        assert candles[0].quality_status == QualityStatus.VALIDATED

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


class TestCoalesceGaps:
    """Tests for gap coalescing."""

    def test_adjacent_gaps_merge(self):
        """Test that adjacent per-candle gaps merge into one contiguous range."""
        t0 = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        gaps = [
            Gap(instrument_id=1, timeframe="5", gap_start=t0, gap_end=t0 + timedelta(minutes=5)),
            Gap(instrument_id=1, timeframe="5", gap_start=t0 + timedelta(minutes=5), gap_end=t0 + timedelta(minutes=10)),
            Gap(instrument_id=1, timeframe="5", gap_start=t0 + timedelta(minutes=10), gap_end=t0 + timedelta(minutes=15)),
        ]
        
        coalesced = CandleSync._coalesce_gaps(gaps)
        
        assert len(coalesced) == 1
        assert coalesced[0].gap_start == t0
        assert coalesced[0].gap_end == t0 + timedelta(minutes=15)

    def test_separated_gaps_stay_separate(self):
        """Test that non-adjacent gaps remain separate."""
        t0 = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        gaps = [
            Gap(instrument_id=1, timeframe="5", gap_start=t0, gap_end=t0 + timedelta(minutes=5)),
            Gap(instrument_id=1, timeframe="5", gap_start=t0 + timedelta(minutes=10), gap_end=t0 + timedelta(minutes=15)),
        ]
        
        coalesced = CandleSync._coalesce_gaps(gaps)
        
        assert len(coalesced) == 2

    def test_empty_8h_5m_range_one_batch(self):
        """Test that an empty 8-hour 5m range produces few coalesced gaps."""
        t0 = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        t_end = t0 + timedelta(hours=8)
        
        # Simulate per-candle gaps (96 missing 5m candles)
        gaps = []
        current = t0
        while current < t_end:
            next_slot = current + timedelta(minutes=5)
            gaps.append(Gap(instrument_id=1, timeframe="5", gap_start=current, gap_end=next_slot))
            current = next_slot
        
        assert len(gaps) == 96  # One per candle slot
        
        coalesced = CandleSync._coalesce_gaps(gaps)
        
        # Should be merged into 1 gap
        assert len(coalesced) == 1
        assert coalesced[0].gap_start == t0
        assert coalesced[0].gap_end == t_end

    def test_partial_coverage_yields_only_missing(self):
        """Test that existing partial coverage yields only missing contiguous ranges."""
        t0 = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        
        # Two gaps with a covered region in between
        gaps = [
            Gap(instrument_id=1, timeframe="5", gap_start=t0, gap_end=t0 + timedelta(minutes=10)),
            Gap(instrument_id=1, timeframe="5", gap_start=t0 + timedelta(minutes=20), gap_end=t0 + timedelta(minutes=30)),
        ]
        
        coalesced = CandleSync._coalesce_gaps(gaps)
        
        assert len(coalesced) == 2
        assert coalesced[0].gap_end == t0 + timedelta(minutes=10)
        assert coalesced[1].gap_start == t0 + timedelta(minutes=20)

    def test_empty_gaps_list(self):
        """Test empty gaps list."""
        coalesced = CandleSync._coalesce_gaps([])
        assert coalesced == []

    def test_single_gap_unchanged(self):
        """Test single gap is returned unchanged."""
        t0 = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)
        gaps = [Gap(instrument_id=1, timeframe="5", gap_start=t0, gap_end=t0 + timedelta(hours=1))]
        
        coalesced = CandleSync._coalesce_gaps(gaps)
        
        assert len(coalesced) == 1
        assert coalesced[0].gap_start == t0
        assert coalesced[0].gap_end == t0 + timedelta(hours=1)