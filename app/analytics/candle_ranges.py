from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from app.analytics.models import CandleRange, Gap

logger = logging.getLogger(__name__)


class CandleRangePlanner:
    """Plans candle data ranges for analytics."""

    def __init__(self):
        """Initialize the range planner."""
        pass

    def calculate_required_ranges(
        self,
        instrument_id: int,
        timeframe: str,
        signal_time: Optional[datetime] = None,
        entry_time: Optional[datetime] = None,
        exit_time: Optional[datetime] = None,
        post_exit_horizon: timedelta = timedelta(hours=4),
        lookback_hours: int = 24,
    ) -> list[CandleRange]:
        """Calculate required candle ranges for a trade.
        
        Args:
            instrument_id: Instrument ID
            timeframe: Candle timeframe
            signal_time: When signal was generated
            entry_time: When trade was entered
            exit_time: When trade was exited
            post_exit_horizon: Required post-exit data window
            lookback_hours: Hours of lookback before signal
            
        Returns:
            List of required candle ranges
        """
        ranges = []
        
        # Determine start time
        if signal_time:
            start_time = signal_time - timedelta(hours=lookback_hours)
        elif entry_time:
            start_time = entry_time - timedelta(hours=lookback_hours)
        else:
            # Default to 24 hours ago
            start_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        
        # Determine end time
        if exit_time:
            end_time = exit_time + post_exit_horizon
        else:
            # If no exit, use current time + post_exit_horizon
            end_time = datetime.now(timezone.utc) + post_exit_horizon
        
        # Create range
        range_ = CandleRange(
            instrument_id=instrument_id,
            timeframe=timeframe,
            from_time=start_time,
            to_time=end_time,
        )
        ranges.append(range_)
        
        logger.info(
            "Calculated required range for instrument %d: %s to %s",
            instrument_id,
            start_time,
            end_time,
        )
        
        return ranges

    def calculate_ranges_for_trades(
        self,
        trades: list[dict[str, Any]],
        timeframe: str,
        post_exit_horizon: timedelta = timedelta(hours=4),
        lookback_hours: int = 24,
    ) -> dict[int, list[CandleRange]]:
        """Calculate required ranges for multiple trades.
        
        Args:
            trades: List of trade dictionaries with instrument_id, signal_time, entry_time, exit_time
            timeframe: Candle timeframe
            post_exit_horizon: Required post-exit data window
            lookback_hours: Hours of lookback before signal
            
        Returns:
            Dictionary mapping instrument_id to list of ranges
        """
        ranges_by_instrument: dict[int, list[CandleRange]] = {}
        
        for trade in trades:
            instrument_id = trade.get("instrument_id")
            if not instrument_id:
                continue
            
            signal_time = trade.get("signal_time")
            entry_time = trade.get("entry_time")
            exit_time = trade.get("exit_time")
            
            ranges = self.calculate_required_ranges(
                instrument_id=instrument_id,
                timeframe=timeframe,
                signal_time=signal_time,
                entry_time=entry_time,
                exit_time=exit_time,
                post_exit_horizon=post_exit_horizon,
                lookback_hours=lookback_hours,
            )
            
            if instrument_id not in ranges_by_instrument:
                ranges_by_instrument[instrument_id] = []
            
            ranges_by_instrument[instrument_id].extend(ranges)
        
        # Merge ranges for each instrument
        for instrument_id in ranges_by_instrument:
            ranges_by_instrument[instrument_id] = self.merge_ranges(
                ranges_by_instrument[instrument_id]
            )
        
        logger.info(
            "Calculated ranges for %d instruments across %d trades",
            len(ranges_by_instrument),
            len(trades),
        )
        
        return ranges_by_instrument

    def merge_ranges(self, ranges: list[CandleRange]) -> list[CandleRange]:
        """Merge overlapping or adjacent ranges.
        
        Args:
            ranges: List of ranges to merge
            
        Returns:
            List of merged ranges
        """
        if not ranges:
            return []
        
        # Sort by start time
        sorted_ranges = sorted(ranges, key=lambda r: r.from_time)
        
        merged = [sorted_ranges[0]]
        
        for current in sorted_ranges[1:]:
            last_merged = merged[-1]
            
            # Check if ranges overlap or are adjacent
            if current.from_time <= last_merged.to_time:
                # Merge ranges
                merged[-1] = CandleRange(
                    instrument_id=last_merged.instrument_id,
                    timeframe=last_merged.timeframe,
                    from_time=last_merged.from_time,
                    to_time=max(last_merged.to_time, current.to_time),
                )
            else:
                merged.append(current)
        
        return merged

    def extract_gaps(
        self,
        instrument_id: int,
        timeframe: str,
        required_ranges: list[CandleRange],
        existing_coverage: list[dict[str, Any]],
    ) -> list[Gap]:
        """Extract gaps in candle coverage.
        
        Args:
            instrument_id: Instrument ID
            timeframe: Candle timeframe
            required_ranges: Required time ranges
            existing_coverage: Existing coverage from database
            
        Returns:
            List of gaps
        """
        gaps = []
        
        for range_ in required_ranges:
            # Find gaps within this range
            for gap_info in existing_coverage:
                gap_start = gap_info.get("gap_start")
                gap_end = gap_info.get("gap_end")
                
                if not gap_start or not gap_end:
                    continue
                
                # Ensure gap is within the required range
                effective_start = max(gap_start, range_.from_time)
                effective_end = min(gap_end, range_.to_time)
                
                if effective_start < effective_end:
                    gaps.append(
                        Gap(
                            instrument_id=instrument_id,
                            timeframe=timeframe,
                            gap_start=effective_start,
                            gap_end=effective_end,
                        )
                    )
        
        # Merge adjacent gaps
        merged_gaps = self._merge_gaps(gaps)
        
        logger.info(
            "Extracted %d gaps for instrument %d %s",
            len(merged_gaps),
            instrument_id,
            timeframe,
        )
        
        return merged_gaps

    def _merge_gaps(self, gaps: list[Gap]) -> list[Gap]:
        """Merge adjacent gaps."""
        if not gaps:
            return []
        
        # Sort by start time
        sorted_gaps = sorted(gaps, key=lambda g: g.gap_start)
        
        merged = [sorted_gaps[0]]
        
        for current in sorted_gaps[1:]:
            last_merged = merged[-1]
            
            # Check if gaps are adjacent or overlapping
            if current.gap_start <= last_merged.gap_end:
                # Merge gaps
                merged[-1] = Gap(
                    instrument_id=last_merged.instrument_id,
                    timeframe=last_merged.timeframe,
                    gap_start=last_merged.gap_start,
                    gap_end=max(last_merged.gap_end, current.gap_end),
                )
            else:
                merged.append(current)
        
        return merged

    def split_gaps_for_api(
        self,
        gaps: list[Gap],
        max_batch_size: int = 200,
    ) -> list[list[Gap]]:
        """Split gaps into batches for Bybit API.
        
        Args:
            gaps: List of gaps to split
            max_batch_size: Maximum candles per batch
            
        Returns:
            List of gap batches
        """
        batches = []
        current_batch = []
        current_batch_candles = 0
        
        for gap in gaps:
            # Estimate number of candles in gap
            gap_candles = self._estimate_candle_count(gap)
            
            # If adding this gap would exceed batch size, start new batch
            if current_batch_candles + gap_candles > max_batch_size and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_batch_candles = 0
            
            current_batch.append(gap)
            current_batch_candles += gap_candles
        
        # Add final batch
        if current_batch:
            batches.append(current_batch)
        
        logger.info(
            "Split %d gaps into %d batches",
            len(gaps),
            len(batches),
        )
        
        return batches

    def _estimate_candle_count(self, gap: Gap) -> int:
        """Estimate number of candles in a gap."""
        timeframe_minutes = self._timeframe_to_minutes(gap.timeframe)
        gap_minutes = (gap.gap_end - gap.gap_start).total_seconds() / 60
        return int(gap_minutes / timeframe_minutes) + 1

    def _timeframe_to_minutes(self, timeframe: str) -> int:
        """Convert timeframe to minutes."""
        mapping = {
            "1": 1,
            "3": 3,
            "5": 5,
            "15": 15,
            "30": 30,
            "60": 60,
            "120": 120,
            "240": 240,
            "360": 360,
            "720": 720,
            "D": 1440,
            "W": 10080,
            "M": 43200,
        }
        return mapping.get(timeframe, 5)