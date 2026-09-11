from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from app.analytics.models import Candle, CandleRange, Gap, Watermark
from app.analytics.repository import AnalyticsRepository
from app.exchange.bybit_client import BybitClient

logger = logging.getLogger(__name__)


class CandleSync:
    """Synchronizes candle data from Bybit to PostgreSQL."""

    def __init__(self, bybit_client: BybitClient, repository: AnalyticsRepository):
        """Initialize with Bybit client and repository."""
        self._bybit = bybit_client
        self._repo = repository

    def fetch_and_store_candles(
        self,
        instrument_id: int,
        symbol: str,
        timeframe: str,
        from_time: datetime,
        to_time: datetime,
        workers: int = 2,
        retry_count: int = 3,
    ) -> tuple[int, int, int]:
        """Fetch candles from Bybit and store them.
        
        Returns:
            Tuple of (inserted, updated, rejected) counts.
        """
        logger.info(
            "Fetching candles for %s %s from %s to %s",
            symbol,
            timeframe,
            from_time,
            to_time,
        )
        
        all_candles = []
        current_from = from_time
        
        while current_from < to_time:
            # Calculate batch end (max 200 candles per request)
            batch_end = min(
                current_from + timedelta(hours=8),  # 8 hours per batch
                to_time,
            )
            
            try:
                # Fetch candles from Bybit
                candles = self._fetch_candles_batch(
                    symbol=symbol,
                    timeframe=timeframe,
                    start_time=current_from,
                    end_time=batch_end,
                    retry_count=retry_count,
                )
                
                # Convert to Candle objects
                for raw_candle in candles:
                    try:
                        candle = self._parse_candle(
                            raw_candle, instrument_id, timeframe
                        )
                        if candle:
                            all_candles.append(candle)
                    except Exception as e:
                        logger.warning("Failed to parse candle: %s", e)
                
                current_from = batch_end
                
                # Rate limiting
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(
                    "Failed to fetch candles batch from %s to %s: %s",
                    current_from,
                    batch_end,
                    e,
                )
                current_from = batch_end
        
        # Store all candles
        if all_candles:
            inserted, updated, rejected = self._repo.insert_candles_batch(all_candles)
            logger.info(
                "Stored candles for %s %s: inserted=%d, updated=%d, rejected=%d",
                symbol,
                timeframe,
                inserted,
                updated,
                rejected,
            )
            return inserted, updated, rejected
        
        return 0, 0, 0

    def _fetch_candles_batch(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        retry_count: int = 3,
    ) -> list[dict[str, Any]]:
        """Fetch a batch of candles from Bybit with retry logic."""
        for attempt in range(1, retry_count + 1):
            try:
                # Convert timeframe to Bybit format
                bybit_timeframe = self._timeframe_to_bybit(timeframe)
                
                # Calculate limit based on time range
                minutes_diff = (end_time - start_time).total_seconds() / 60
                limit = min(200, int(minutes_diff / int(timeframe)) + 1)
                
                # Fetch from Bybit
                payload = self._bybit._public_get(
                    "/v5/market/kline",
                    category="linear",
                    symbol=symbol,
                    interval=bybit_timeframe,
                    start=int(start_time.timestamp() * 1000),
                    end=int(end_time.timestamp() * 1000),
                    limit=limit,
                )
                
                if payload.get("retCode") != 0:
                    raise ValueError(f"Bybit API error: {payload.get('retMsg')}")
                
                return payload.get("result", {}).get("list", [])
                
            except Exception as e:
                if attempt < retry_count:
                    logger.warning(
                        "Bybit API attempt %d/%d failed: %s. Retrying...",
                        attempt,
                        retry_count,
                        e,
                    )
                    time.sleep(1 * attempt)  # Exponential backoff
                else:
                    logger.error(
                        "Bybit API failed after %d attempts: %s",
                        retry_count,
                        e,
                    )
                    raise

    def _parse_candle(
        self,
        raw_candle: list[Any],
        instrument_id: int,
        timeframe: str,
    ) -> Optional[Candle]:
        """Parse a raw Bybit candle into a Candle object."""
        try:
            # Bybit format: [timestamp, open, high, low, close, volume, turnover]
            if len(raw_candle) < 7:
                return None
            
            open_time = datetime.fromtimestamp(
                int(raw_candle[0]) / 1000, tz=timezone.utc
            )
            
            # Calculate close_time based on timeframe
            timeframe_minutes = self._timeframe_to_minutes(timeframe)
            close_time = open_time + timedelta(minutes=timeframe_minutes)
            
            candle = Candle(
                exchange="bybit",
                market_type="linear",
                instrument_id=instrument_id,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                open=float(raw_candle[1]),
                high=float(raw_candle[2]),
                low=float(raw_candle[3]),
                close=float(raw_candle[4]),
                volume=float(raw_candle[5]),
                turnover=float(raw_candle[6]) if raw_candle[6] else None,
                is_closed=True,
                source="bybit_api",
                source_received_at=datetime.now(timezone.utc),
                ingested_at=datetime.now(timezone.utc),
                quality_status="validated",
            )
            
            return candle
            
        except Exception as e:
            logger.warning("Failed to parse raw candle: %s", e)
            return None

    def _timeframe_to_bybit(self, timeframe: str) -> str:
        """Convert internal timeframe to Bybit format."""
        mapping = {
            "1": "1",
            "3": "3",
            "5": "5",
            "15": "15",
            "30": "30",
            "60": "60",
            "120": "120",
            "240": "240",
            "360": "360",
            "720": "720",
            "D": "D",
            "W": "W",
            "M": "M",
        }
        return mapping.get(timeframe, "5")

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

    def reconcile_candles(
        self,
        instrument_id: int,
        symbol: str,
        timeframe: str,
        required_ranges: list[CandleRange],
        watermark: Optional[Watermark] = None,
    ) -> tuple[list[Gap], int, int, int]:
        """Reconcile candle data for required ranges.
        
        Returns:
            Tuple of (gaps, inserted, updated, rejected).
        """
        logger.info(
            "Reconciling candles for %s %s with %d ranges",
            symbol,
            timeframe,
            len(required_ranges),
        )
        
        # Merge overlapping ranges
        merged_ranges = self._merge_ranges(required_ranges)
        
        # Check existing coverage
        gaps = []
        total_inserted = 0
        total_updated = 0
        total_rejected = 0
        
        for range_ in merged_ranges:
            # Check coverage for this range
            existing_gaps = self._repo.get_candle_coverage(
                instrument_id, timeframe, range_.from_time, range_.to_time
            )
            
            if not existing_gaps:
                # Range is fully covered
                continue
            
            # Add gaps to list
            for gap in existing_gaps:
                gaps.append(
                    Gap(
                        instrument_id=instrument_id,
                        timeframe=timeframe,
                        gap_start=gap["gap_start"],
                        gap_end=gap["gap_end"],
                    )
                )
            
            # Fetch and store candles for gaps
            for gap in existing_gaps:
                inserted, updated, rejected = self.fetch_and_store_candles(
                    instrument_id=instrument_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    from_time=gap["gap_start"],
                    to_time=gap["gap_end"],
                )
                total_inserted += inserted
                total_updated += updated
                total_rejected += rejected
        
        logger.info(
            "Reconciliation complete for %s %s: gaps=%d, inserted=%d, updated=%d, rejected=%d",
            symbol,
            timeframe,
            len(gaps),
            total_inserted,
            total_updated,
            total_rejected,
        )
        
        return gaps, total_inserted, total_updated, total_rejected

    def _merge_ranges(self, ranges: list[CandleRange]) -> list[CandleRange]:
        """Merge overlapping or adjacent ranges."""
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

    def check_post_exit_coverage(
        self,
        instrument_id: int,
        timeframe: str,
        exit_time: datetime,
        post_exit_horizon: timedelta,
    ) -> bool:
        """Check if post-exit coverage is complete."""
        from_time = exit_time
        to_time = exit_time + post_exit_horizon
        
        gaps = self._repo.get_candle_coverage(
            instrument_id, timeframe, from_time, to_time
        )
        
        return len(gaps) == 0