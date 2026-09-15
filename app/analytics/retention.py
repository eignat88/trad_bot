from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from app.analytics.repository import AnalyticsRepository

logger = logging.getLogger(__name__)


class CandleRetention:
    """Manages candle data retention policy."""

    def __init__(self, repository: AnalyticsRepository, retention_days: int = 180):
        """Initialize with repository and retention period."""
        self._repo = repository
        self._retention_days = retention_days

    def run_retention(
        self,
        dry_run: bool = False,
        batch_size: int = 1000,
    ) -> dict[str, Any]:
        """Run candle retention cleanup.
        
        Args:
            dry_run: If True, only report what would be deleted
            batch_size: Number of candles to delete per batch
            
        Returns:
            Dictionary with retention statistics
        """
        logger.info(
            "Running candle retention (dry_run=%s, retention_days=%d)",
            dry_run,
            self._retention_days,
        )
        
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        
        stats = {
            "cutoff_date": cutoff_date.isoformat(),
            "retention_days": self._retention_days,
            "dry_run": dry_run,
            "total_deleted": 0,
            "total_skipped": 0,
            "batches_processed": 0,
            "errors": [],
        }
        
        try:
            # Get count of candles to delete
            cursor = self._repo._conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM market.candle
                WHERE open_time < %s
                """,
                (cutoff_date,),
            )
            total_count = cursor.fetchone()[0]
            
            if total_count == 0:
                logger.info("No candles to delete")
                return stats
            
            logger.info("Found %d candles older than %s", total_count, cutoff_date)
            
            if dry_run:
                stats["total_deleted"] = total_count
                return stats
            
            # Delete in batches
            deleted_total = 0
            while deleted_total < total_count:
                cursor.execute(
                    """
                    DELETE FROM market.candle
                    WHERE ctid IN (
                        SELECT ctid FROM market.candle
                        WHERE open_time < %s
                        LIMIT %s
                    )
                    """,
                    (cutoff_date, batch_size),
                )
                deleted = cursor.rowcount
                deleted_total += deleted
                stats["batches_processed"] += 1
                
                logger.info(
                    "Deleted batch %d: %d candles (total: %d/%d)",
                    stats["batches_processed"],
                    deleted,
                    deleted_total,
                    total_count,
                )
                
                if deleted == 0:
                    break
            
            self._repo._conn.commit()
            stats["total_deleted"] = deleted_total
            
            logger.info(
                "Retention completed: deleted %d candles",
                deleted_total,
            )
            
        except Exception as e:
            self._repo._conn.rollback()
            stats["errors"].append(str(e))
            logger.error("Retention failed: %s", e)
            raise
        
        return stats

    def get_retention_stats(self) -> dict[str, Any]:
        """Get current retention statistics."""
        try:
            cursor = self._repo._conn.cursor()
            
            # Get total candles
            cursor.execute("SELECT COUNT(*) FROM market.candle")
            total_candles = cursor.fetchone()[0]
            
            # Get oldest candle
            cursor.execute("SELECT MIN(open_time) FROM market.candle")
            oldest_candle = cursor.fetchone()[0]
            
            # Get newest candle
            cursor.execute("SELECT MAX(open_time) FROM market.candle")
            newest_candle = cursor.fetchone()[0]
            
            # Get candles by age
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
            cursor.execute(
                """
                SELECT COUNT(*) FROM market.candle
                WHERE open_time < %s
                """,
                (cutoff_date,),
            )
            candles_to_delete = cursor.fetchone()[0]
            
            return {
                "total_candles": total_candles,
                "oldest_candle": oldest_candle.isoformat() if oldest_candle else None,
                "newest_candle": newest_candle.isoformat() if newest_candle else None,
                "retention_days": self._retention_days,
                "cutoff_date": cutoff_date.isoformat(),
                "candles_to_delete": candles_to_delete,
            }
        except Exception as e:
            logger.error("Failed to get retention stats: %s", e)
            raise

    def check_retention_needed(self) -> bool:
        """Check if retention cleanup is needed."""
        try:
            stats = self.get_retention_stats()
            return stats["candles_to_delete"] > 0
        except Exception as e:
            logger.error("Failed to check retention needed: %s", e)
            return False

    def estimate_cleanup_time(self, batch_size: int = 1000) -> dict[str, Any]:
        """Estimate cleanup time based on current data."""
        try:
            stats = self.get_retention_stats()
            total_to_delete = stats["candles_to_delete"]
            
            if total_to_delete == 0:
                return {
                    "estimated_batches": 0,
                    "estimated_seconds": 0,
                    "estimated_minutes": 0,
                }
            
            estimated_batches = (total_to_delete + batch_size - 1) // batch_size
            # Assume ~1 second per batch for deletion
            estimated_seconds = estimated_batches * 1
            
            return {
                "estimated_batches": estimated_batches,
                "estimated_seconds": estimated_seconds,
                "estimated_minutes": estimated_seconds / 60,
                "total_candles_to_delete": total_to_delete,
                "batch_size": batch_size,
            }
        except Exception as e:
            logger.error("Failed to estimate cleanup time: %s", e)
            raise