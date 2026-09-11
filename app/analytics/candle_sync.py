from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from app.analytics.models import Candle, CandleRange, Gap, Watermark, QualityStatus
from app.analytics.repository import AnalyticsRepository
from app.exchange.bybit_client import BybitClient

logger = logging.getLogger(__name__)


def normalize_timeframe(raw: str) -> str:
    """Normalize a timeframe string to the internal/Bybit canonical form.
    
    Handles production paper_trade values ("5m", "15m", "1h", "4h", "1D")
    and internal values ("5", "15", "60", etc.).
    
    Returns the canonical string used by market.candle and Bybit API.
    Raises ValueError for unrecognized formats.
    """
    raw = raw.strip().lower()
    
    # Direct numeric (already normalized)
    direct = {
        "1": "1", "3": "3", "5": "5", "15": "15", "30": "30",
        "60": "60", "120": "120", "240": "240", "360": "360", "720": "720",
    }
    if raw in direct:
        return direct[raw]
    
    # Suffix forms: "5m" -> "5", "1h" -> "60", "4h" -> "240"
    # Must check before special single-letter forms
    if raw.endswith("m") and raw[:-1].isdigit():
        minutes = int(raw[:-1])
        return str(minutes)
    
    if raw.endswith("h") and raw[:-1].isdigit():
        hours = int(raw[:-1])
        return str(hours * 60)
    
    # Special timeframes (after suffix checks)
    if raw in ("d", "1d", "day"):
        return "D"
    if raw in ("w", "1w", "week"):
        return "W"
    if raw in ("m", "month"):
        return "M"
    
    raise ValueError(f"Unrecognized timeframe: {raw!r}")


def _timeframe_to_minutes_canonical(timeframe: str) -> int:
    """Convert a normalized timeframe string to minutes.
    
    Only accepts canonical forms: "1", "5", "15", "60", "D", etc.
    """
    mapping = {
        "1": 1, "3": 3, "5": 5, "15": 15, "30": 30,
        "60": 60, "120": 120, "240": 240, "360": 360, "720": 720,
        "D": 1440, "W": 10080, "M": 43200,
    }
    return mapping.get(timeframe, 5)


def align_to_grid(dt: datetime, timeframe: str) -> datetime:
    """Align a timestamp DOWN to the canonical candle open-time boundary.
    
    Alignment is always performed in UTC because candles are stored
    with UTC open_times.
    
    For 5m: 11:12:15 UTC -> 11:10:00 UTC
    For 15m: 11:22:00 UTC -> 11:15:00 UTC
    For 1h: 11:22:00 UTC -> 11:00:00 UTC
    For D: Sep 15 03:00 UTC -> Sep 15 00:00 UTC
    """
    minutes = _timeframe_to_minutes_canonical(timeframe)
    
    # Convert to UTC for alignment
    dt_utc = dt.astimezone(timezone.utc)
    
    # For daily/weekly/monthly, align to midnight UTC
    if minutes >= 1440:
        return dt_utc.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    
    # For minute/hour timeframes, align down to the nearest boundary in UTC
    total_minutes = dt_utc.hour * 60 + dt_utc.minute
    aligned_minutes = (total_minutes // minutes) * minutes
    
    aligned_hour = aligned_minutes // 60
    aligned_minute = aligned_minutes % 60
    
    return dt_utc.replace(
        hour=aligned_hour,
        minute=aligned_minute,
        second=0,
        microsecond=0,
        tzinfo=timezone.utc,
    )


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
        observation_cutoff: Optional[datetime] = None,
        workers: int = 2,
        retry_count: int = 3,
    ) -> tuple[int, int, int, list[dict[str, Any]]]:
        """Fetch candles from Bybit and store them.
        
        observation_cutoff: If provided, no candle with close_time > cutoff
        will be stored as closed.  Fetch ranges are also clamped to this limit.
        
        Returns:
            Tuple of (inserted, updated, rejected, failed_ranges).
            failed_ranges contains dicts with from_time/to_time/error for
            any batch that could not be fetched after all retries.
        """
        # Clamp to_time to observation_cutoff for point-in-time safety
        if observation_cutoff is not None and to_time > observation_cutoff:
            to_time = observation_cutoff
        
        if from_time >= to_time:
            return 0, 0, 0, []
        
        logger.info(
            "Fetching candles for %s %s from %s to %s (cutoff=%s)",
            symbol,
            timeframe,
            from_time,
            to_time,
            observation_cutoff,
        )
        
        all_candles = []
        failed_ranges: list[dict[str, Any]] = []
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
                            raw_candle, instrument_id, timeframe,
                            observation_cutoff=observation_cutoff,
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
                failed_ranges.append({
                    "from_time": current_from,
                    "to_time": batch_end,
                    "error": str(e),
                })
                current_from = batch_end
        
        # Store all candles
        if all_candles:
            inserted, updated, rejected = self._repo.insert_candles_batch(all_candles)
            logger.info(
                "Stored candles for %s %s: inserted=%d, updated=%d, rejected=%d, failed_batches=%d",
                symbol,
                timeframe,
                inserted,
                updated,
                rejected,
                len(failed_ranges),
            )
            return inserted, updated, rejected, failed_ranges
        
        if failed_ranges:
            logger.warning(
                "No candles fetched for %s %s: %d failed batches",
                symbol,
                timeframe,
                len(failed_ranges),
            )
        
        return 0, 0, 0, failed_ranges

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
        observation_cutoff: Optional[datetime] = None,
    ) -> Optional[Candle]:
        """Parse a raw Bybit candle into a Candle object.
        
        Only persists candles whose close_time <= observation_cutoff.
        This prevents storing incomplete/current candles as closed.
        """
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
            
            # Point-in-time safety: skip candles whose close_time
            # is after the observation cutoff (still forming / incomplete)
            if observation_cutoff is not None and close_time > observation_cutoff:
                logger.debug(
                    "Skipping candle %s: close_time %s > observation_cutoff %s",
                    open_time, close_time, observation_cutoff,
                )
                return None
            
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
                quality_status=QualityStatus.VALIDATED,
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
        observation_cutoff: Optional[datetime] = None,
    ) -> tuple[list[Gap], int, int, int, list[dict[str, Any]]]:
        """Reconcile candle data for required ranges.
        
        Returns:
            Tuple of (gaps, inserted, updated, rejected, failed_ranges).
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
        all_raw_gaps: list[Gap] = []
        total_inserted = 0
        total_updated = 0
        total_rejected = 0
        all_failed_ranges: list[dict[str, Any]] = []
        
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
                all_raw_gaps.append(
                    Gap(
                        instrument_id=instrument_id,
                        timeframe=timeframe,
                        gap_start=gap["gap_start"],
                        gap_end=gap["gap_end"],
                    )
                )
        
        # Coalesce consecutive/per-candle gaps into contiguous ranges
        gaps = self._coalesce_gaps(all_raw_gaps)
        
        # Fetch and store candles for coalesced gaps
        for gap in gaps:
            inserted, updated, rejected, failed = self.fetch_and_store_candles(
                instrument_id=instrument_id,
                symbol=symbol,
                timeframe=timeframe,
                from_time=gap.gap_start,
                to_time=gap.gap_end,
                observation_cutoff=observation_cutoff,
            )
            total_inserted += inserted
            total_updated += updated
            total_rejected += rejected
            all_failed_ranges.extend(failed)
        
        logger.info(
            "Reconciliation complete for %s %s: raw_gaps=%d coalesced=%d inserted=%d updated=%d rejected=%d failed=%d",
            symbol,
            timeframe,
            len(all_raw_gaps),
            len(gaps),
            total_inserted,
            total_updated,
            total_rejected,
            len(all_failed_ranges),
        )
        
        return gaps, total_inserted, total_updated, total_rejected, all_failed_ranges

    @staticmethod
    def _coalesce_gaps(gaps: list[Gap]) -> list[Gap]:
        """Coalesce adjacent/overlapping gaps into contiguous ranges.
        
        This is a safety net: even if the SQL function returns per-candle gaps,
        this merges them before Bybit API calls.
        """
        if not gaps:
            return []
        
        sorted_gaps = sorted(gaps, key=lambda g: g.gap_start)
        coalesced: list[Gap] = [sorted_gaps[0]]
        
        for current in sorted_gaps[1:]:
            last = coalesced[-1]
            # Merge if current gap starts at or before the last gap ends
            if current.gap_start <= last.gap_end:
                coalesced[-1] = Gap(
                    instrument_id=last.instrument_id,
                    timeframe=last.timeframe,
                    gap_start=last.gap_start,
                    gap_end=max(last.gap_end, current.gap_end),
                )
            else:
                coalesced.append(current)
        
        return coalesced

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