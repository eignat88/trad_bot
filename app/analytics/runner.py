from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone, timedelta, date
from typing import Any, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from app.analytics.models import (
    AnalysisRun,
    AnalysisStageRun,
    Maturity,
    RunStatus,
    StageStatus,
)
from app.analytics.repository import AnalyticsRepository
from app.analytics.candle_sync import CandleSync
from app.analytics.quality import DataQualityGate
from app.analytics.retention import CandleRetention
from app.config import Settings

logger = logging.getLogger(__name__)


class AnalyticsRunner:
    """Daily analytics pipeline runner."""

    def __init__(
        self,
        settings: Settings,
        repository: AnalyticsRepository,
        candle_sync: CandleSync,
        quality_gate: DataQualityGate,
        retention: CandleRetention,
    ):
        """Initialize with dependencies."""
        self._settings = settings
        self._repo = repository
        self._candle_sync = candle_sync
        self._quality_gate = quality_gate
        self._retention = retention

    def run_provisional(self, business_date: Optional[date] = None) -> AnalysisRun:
        """Run PROVISIONAL analytics for a business date.
        
        Args:
            business_date: Date to analyze (default: today in Europe/Sofia)
            
        Returns:
            AnalysisRun with PROVISIONAL maturity
        """
        if business_date is None:
            business_date = self._get_business_date()
        
        logger.info(
            "Starting PROVISIONAL analytics for date: %s",
            business_date,
        )
        
        # Acquire advisory lock
        lock_id = self._calculate_lock_id(business_date)
        if not self._repo.acquire_advisory_lock(lock_id):
            raise RuntimeError(f"Could not acquire advisory lock {lock_id}")
        
        try:
            # Create or get analysis run
            run = self._create_or_get_run(business_date)
            
            if run.maturity == Maturity.FINAL:
                logger.info("FINAL run already exists for date: %s", business_date)
                return run
            
            # Reset run for fresh execution (clears stale terminal-state fields)
            self._prepare_run_for_execution(run, Maturity.PROVISIONAL)
            self._repo.update_analysis_run(run)
            
            # Execute stages
            try:
                # Stage 1: Candle reconciliation
                self._execute_stage(run, "candle_reconciliation", self._stage_candle_reconciliation)
                
                # Stage 2: Data quality gate
                self._execute_stage(run, "quality_gate", self._stage_quality_gate)
                
                # Stage 3: Retention cleanup
                self._execute_stage(run, "retention", self._stage_retention)
                
                # Mark as SUCCEEDED
                run.status = RunStatus.SUCCEEDED
                run.maturity = Maturity.PROVISIONAL
                
            except Exception as e:
                logger.error("Stage failed: %s", e)
                run.status = RunStatus.FAILED
                run.maturity = Maturity.FAILED
                run.error_code = "STAGE_FAILED"
                run.error_message = str(e)
            
            # Update run
            run.finished_at = datetime.now(timezone.utc)
            self._repo.update_analysis_run(run)
            
            logger.info(
                "PROVISIONAL analytics completed for date: %s, status: %s",
                business_date,
                run.status.value,
            )
            
            return run
            
        finally:
            # Release advisory lock
            self._repo.release_advisory_lock(lock_id)

    def run_final(self, business_date: Optional[date] = None) -> AnalysisRun:
        """Run FINAL analytics for a business date.
        
        Args:
            business_date: Date to analyze (default: today in Europe/Sofia)
            
        Returns:
            AnalysisRun with FINAL maturity
        """
        if business_date is None:
            business_date = self._get_business_date()
        
        logger.info(
            "Starting FINAL analytics for date: %s",
            business_date,
        )
        
        # Find existing PROVISIONAL run
        provisional_run = self._repo.get_analysis_run_by_date(business_date)
        if not provisional_run:
            raise ValueError(f"No PROVISIONAL run found for date: {business_date}")
        
        if provisional_run.maturity == Maturity.FINAL:
            logger.info("FINAL run already exists for date: %s", business_date)
            return provisional_run
        
        # Acquire advisory lock
        lock_id = self._calculate_lock_id(business_date)
        if not self._repo.acquire_advisory_lock(lock_id):
            raise RuntimeError(f"Could not acquire advisory lock {lock_id}")
        
        try:
            # Reset run for fresh execution (clears stale terminal-state fields)
            run = self._prepare_run_for_execution(provisional_run, provisional_run.maturity)
            self._repo.update_analysis_run(run)
            
            # Execute stages
            try:
                # Stage 1: Post-exit candle backfill
                self._execute_stage(run, "post_exit_backfill", self._stage_post_exit_backfill)
                
                # Stage 2: Final quality gate
                self._execute_stage(run, "final_quality_gate", self._stage_final_quality_gate)
                
                # Mark as SUCCEEDED
                run.status = RunStatus.SUCCEEDED
                run.maturity = Maturity.FINAL
                
            except Exception as e:
                logger.error("FINAL stage failed: %s", e)
                run.status = RunStatus.FAILED
                run.maturity = Maturity.FAILED
                run.error_code = "FINAL_STAGE_FAILED"
                run.error_message = str(e)
            
            # Update run
            run.finished_at = datetime.now(timezone.utc)
            self._repo.update_analysis_run(run)
            
            logger.info(
                "FINAL analytics completed for date: %s, status: %s",
                business_date,
                run.status.value,
            )
            
            return run
            
        finally:
            # Release advisory lock
            self._repo.release_advisory_lock(lock_id)

    def _get_business_date(self) -> date:
        """Get current business date in Europe/Sofia timezone."""
        # Use zoneinfo for proper timezone handling (Python 3.9+)
        tz_sofia = ZoneInfo("Europe/Sofia")
        now_sofia = datetime.now(tz_sofia)
        return now_sofia.date()

    @staticmethod
    def _prepare_run_for_execution(run: AnalysisRun, maturity: Maturity) -> AnalysisRun:
        """Reset an existing run to a clean RUNNING state for execution.
        
        Preserves run_id and stage history. Resets all terminal-state fields
        so a retry does not carry stale finished_at, error_code, or error_message.
        Refreshes observation_cutoff to current execution time.
        """
        run.status = RunStatus.RUNNING
        run.maturity = maturity
        run.started_at = datetime.now(timezone.utc)
        run.observation_cutoff = datetime.now(timezone.utc)
        run.finished_at = None
        run.error_code = None
        run.error_message = None
        return run

    def _calculate_lock_id(self, business_date: date) -> int:
        """Calculate advisory lock ID for a business date."""
        # Use a hash of the business date to create a unique lock ID
        date_str = business_date.isoformat()
        return hash(date_str) % (2**31)

    def _create_or_get_run(self, business_date: date) -> AnalysisRun:
        """Create or get existing analysis run."""
        existing_run = self._repo.get_analysis_run_by_date(business_date)
        if existing_run:
            return existing_run
        
        # Calculate analysis window using zoneinfo
        tz_sofia = ZoneInfo("Europe/Sofia")
        now_sofia = datetime.now(tz_sofia)
        
        # Analysis from: start of business day
        analysis_from = datetime.combine(
            business_date, datetime.min.time()
        ).replace(tzinfo=tz_sofia)
        
        # Analysis to: end of business day + post-exit horizon
        analysis_to = analysis_from + timedelta(days=1) + timedelta(
            hours=self._settings.analytics_post_exit_hours
        )
        
        # Observation cutoff: current time
        observation_cutoff = now_sofia
        
        # Post-exit horizon as timedelta
        post_exit_horizon = timedelta(hours=self._settings.analytics_post_exit_hours)
        
        run = AnalysisRun(
            business_date=business_date,
            schedule_timezone="Europe/Sofia",
            analysis_from=analysis_from.astimezone(timezone.utc),
            analysis_to=analysis_to.astimezone(timezone.utc),
            observation_cutoff=observation_cutoff.astimezone(timezone.utc),
            post_exit_horizon=post_exit_horizon,
            maturity=Maturity.PROVISIONAL,
            status=RunStatus.CREATED,
            pipeline_version="1.0.0",
            source_watermarks={},
        )
        
        return self._repo.create_analysis_run(run)

    def _execute_stage(
        self,
        run: AnalysisRun,
        stage_name: str,
        stage_func,
    ) -> AnalysisStageRun:
        """Execute a pipeline stage."""
        logger.info("Executing stage: %s", stage_name)
        
        # Get next attempt number
        attempt = self._get_next_attempt(run.run_id, stage_name)
        
        # Create stage run
        stage_run = AnalysisStageRun(
            run_id=run.run_id,
            stage_name=stage_name,
            attempt=attempt,
            status=StageStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        stage_run = self._repo.create_stage_run(stage_run)
        
        try:
            # Execute stage function
            result = stage_func(run, stage_run)
            
            # Update stage run
            stage_run.status = StageStatus.SUCCEEDED
            stage_run.finished_at = datetime.now(timezone.utc)
            stage_run.output_rows = result.get("output_rows") if result else None
            stage_run.result_json = result
            
            self._repo.update_stage_run(stage_run)
            
            logger.info("Stage %s completed successfully", stage_name)
            
        except Exception as e:
            logger.error("Stage %s failed: %s", stage_name, e)
            stage_run.status = StageStatus.FAILED
            stage_run.finished_at = datetime.now(timezone.utc)
            stage_run.error_code = "STAGE_FAILED"
            stage_run.error_message = str(e)
            
            self._repo.update_stage_run(stage_run)
            raise
        
        return stage_run

    def _get_next_attempt(self, run_id: UUID, stage_name: str) -> int:
        """Get next attempt number for a stage."""
        try:
            cursor = self._repo._conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) + 1
                FROM analytics.analysis_stage_run
                WHERE run_id = %s AND stage_name = %s
                """,
                (str(run_id), stage_name),
            )
            return cursor.fetchone()[0]
        except Exception as e:
            logger.error("Failed to get next attempt: %s", e)
            return 1

    def _stage_candle_reconciliation(
        self, run: AnalysisRun, stage_run: AnalysisStageRun
    ) -> dict[str, Any]:
        """Stage 1: Candle reconciliation.
        
        Fetches closed candles from Bybit for all relevant trades
        within the analysis window.
        """
        logger.info("Running candle reconciliation for run %s", run.run_id)
        
        from app.analytics.candle_sync import normalize_timeframe, CandleRange
        
        # 1. Query relevant trades from dds.paper_trade
        cursor = self._repo._conn.cursor()
        cursor.execute(
            """
            SELECT 
                pt.trade_id, pt.symbol, pt.entered_at, pt.closed_at, pt.entry_timeframe
            FROM dds.paper_trade pt
            WHERE pt.closed_at IS NOT NULL
              AND pt.entered_at < %s
            ORDER BY pt.entered_at
            """,
            (run.analysis_to,),
        )
        trades = cursor.fetchall()
        
        if not trades:
            logger.info("No relevant trades found for candle reconciliation")
            return {
                "output_rows": 0,
                "no_required_ranges": True,
                "trades_considered": 0,
                "instruments_considered": 0,
                "ranges_requested": 0,
                "gaps_detected": 0,
                "inserted": 0,
                "updated": 0,
                "rejected": 0,
                "failed_ranges": [],
                "message": "No trades found — reconciliation succeeded with zero rows",
            }
        
        # 2. Resolve symbol → instrument_id and build required ranges
        # Group by (instrument_id, symbol, timeframe)
        instrument_map: dict[str, int] = {}  # symbol → instrument_id
        ranges_by_key: dict[tuple[int, str], list[CandleRange]] = {}
        
        for trade_id, symbol, entered_at, closed_at, raw_timeframe in trades:
            # Normalize timeframe
            try:
                timeframe = normalize_timeframe(raw_timeframe)
            except ValueError:
                logger.warning("Skipping trade %s: unrecognizable timeframe %r", trade_id, raw_timeframe)
                continue
            
            # Resolve symbol → instrument_id
            if symbol not in instrument_map:
                cursor.execute(
                    "SELECT instrument_id FROM dds.instrument WHERE symbol = %s LIMIT 1",
                    (symbol,),
                )
                row = cursor.fetchone()
                if not row:
                    logger.warning("Skipping trade %s: instrument not found for symbol %s", trade_id, symbol)
                    continue
                instrument_map[symbol] = row[0]
            
            instrument_id = instrument_map[symbol]
            key = (instrument_id, symbol, timeframe)
            
            # Build required range: entered_at → closed_at + post_exit_horizon
            trade_end = closed_at + run.post_exit_horizon
            range_ = CandleRange(
                instrument_id=instrument_id,
                timeframe=timeframe,
                from_time=entered_at,
                to_time=trade_end,
            )
            
            if key not in ranges_by_key:
                ranges_by_key[key] = []
            ranges_by_key[key].append(range_)
        
        # 3. For each unique (instrument_id, symbol, timeframe), merge and reconcile
        total_inserted = 0
        total_updated = 0
        total_rejected = 0
        total_gaps = 0
        all_failed_ranges: list[dict[str, Any]] = []
        
        for (instrument_id, symbol, timeframe), ranges in ranges_by_key.items():
            merged_ranges = self._candle_sync._merge_ranges(ranges)
            
            gaps, inserted, updated, rejected, failed = self._candle_sync.reconcile_candles(
                instrument_id=instrument_id,
                symbol=symbol,
                timeframe=timeframe,
                required_ranges=merged_ranges,
            )
            
            total_inserted += inserted
            total_updated += updated
            total_rejected += rejected
            total_gaps += len(gaps)
            all_failed_ranges.extend(failed)
        
        # 4. Check for blocking failures
        if all_failed_ranges and total_inserted == 0 and total_updated == 0:
            raise RuntimeError(
                f"Candle fetch failed for all {len(all_failed_ranges)} ranges "
                f"with no data stored"
            )
        
        total_rows = total_inserted + total_updated
        
        result = {
            "output_rows": total_rows,
            "trades_considered": len(trades),
            "instruments_considered": len(instrument_map),
            "ranges_requested": sum(len(r) for r in ranges_by_key.values()),
            "gaps_detected": total_gaps,
            "inserted": total_inserted,
            "updated": total_updated,
            "rejected": total_rejected,
            "failed_ranges": all_failed_ranges,
        }
        
        if all_failed_ranges:
            result["message"] = (
                f"Reconciliation completed with {len(all_failed_ranges)} failed fetch batches: "
                f"inserted={total_inserted}, updated={total_updated}"
            )
        else:
            result["message"] = (
                f"Reconciliation complete: inserted={total_inserted}, updated={total_updated}"
            )
        
        return result

    def _stage_quality_gate(
        self, run: AnalysisRun, stage_run: AnalysisStageRun
    ) -> dict[str, Any]:
        """Stage 2: Data quality gate."""
        logger.info("Running data quality gate")
        
        passed, results = self._quality_gate.run_quality_checks(run)
        
        if not passed:
            raise RuntimeError("Data quality gate failed")
        
        return {
            "output_rows": len(results),
            "message": "Data quality gate passed",
        }

    def _stage_retention(
        self, run: AnalysisRun, stage_run: AnalysisStageRun
    ) -> dict[str, Any]:
        """Stage 3: Retention cleanup."""
        logger.info("Running retention cleanup")
        
        stats = self._retention.run_retention(dry_run=False)
        
        return {
            "output_rows": stats.get("total_deleted", 0),
            "message": f"Retention completed: deleted {stats.get('total_deleted', 0)} candles",
        }

    def _stage_post_exit_backfill(
        self, run: AnalysisRun, stage_run: AnalysisStageRun
    ) -> dict[str, Any]:
        """Stage 1: Post-exit candle backfill for FINAL.
        
        Ensures all post-exit windows are fully covered.
        FINAL must not succeed if BLOCKING post-exit coverage is missing.
        """
        logger.info("Running post-exit backfill for FINAL run %s", run.run_id)
        
        from app.analytics.candle_sync import normalize_timeframe, CandleRange
        
        # 1. Query trades that need post-exit data
        cursor = self._repo._conn.cursor()
        cursor.execute(
            """
            SELECT 
                pt.trade_id, pt.symbol, pt.closed_at, pt.entry_timeframe
            FROM dds.paper_trade pt
            WHERE pt.closed_at IS NOT NULL
              AND pt.closed_at < %s
            ORDER BY pt.closed_at
            """,
            (run.observation_cutoff,),
        )
        trades = cursor.fetchall()
        
        if not trades:
            logger.info("No trades requiring post-exit backfill")
            return {
                "output_rows": 0,
                "no_required_ranges": True,
                "trades_considered": 0,
                "instruments_considered": 0,
                "ranges_requested": 0,
                "gaps_detected": 0,
                "inserted": 0,
                "updated": 0,
                "rejected": 0,
                "failed_ranges": [],
                "message": "No trades requiring post-exit backfill",
            }
        
        # 2. Resolve symbols and build post-exit ranges
        instrument_map: dict[str, int] = {}
        ranges_by_key: dict[tuple[int, str], list[CandleRange]] = []
        failed_symbols: list[str] = []
        
        for trade_id, symbol, closed_at, raw_timeframe in trades:
            try:
                timeframe = normalize_timeframe(raw_timeframe)
            except ValueError:
                logger.warning("Skipping trade %s: unrecognizable timeframe %r", trade_id, raw_timeframe)
                continue
            
            if symbol not in instrument_map:
                cursor.execute(
                    "SELECT instrument_id FROM dds.instrument WHERE symbol = %s LIMIT 1",
                    (symbol,),
                )
                row = cursor.fetchone()
                if not row:
                    logger.warning("Skipping trade %s: instrument not found for %s", trade_id, symbol)
                    failed_symbols.append(symbol)
                    continue
                instrument_map[symbol] = row[0]
            
            instrument_id = instrument_map[symbol]
            key = (instrument_id, symbol, timeframe)
            
            post_exit_end = closed_at + run.post_exit_horizon
            
            # Only backfill up to observation_cutoff for safety
            effective_end = min(post_exit_end, run.observation_cutoff)
            
            range_ = CandleRange(
                instrument_id=instrument_id,
                timeframe=timeframe,
                from_time=closed_at,
                to_time=effective_end,
            )
            
            if key not in ranges_by_key:
                ranges_by_key[key] = []
            ranges_by_key[key].append(range_)
        
        # 3. Reconcile each group
        total_inserted = 0
        total_updated = 0
        total_rejected = 0
        total_gaps = 0
        all_failed_ranges: list[dict[str, Any]] = []
        
        for (instrument_id, symbol, timeframe), ranges in ranges_by_key.items():
            merged_ranges = self._candle_sync._merge_ranges(ranges)
            
            gaps, inserted, updated, rejected, failed = self._candle_sync.reconcile_candles(
                instrument_id=instrument_id,
                symbol=symbol,
                timeframe=timeframe,
                required_ranges=merged_ranges,
            )
            
            total_inserted += inserted
            total_updated += updated
            total_rejected += rejected
            total_gaps += len(gaps)
            all_failed_ranges.extend(failed)
        
        # 4. FINAL must not succeed if fetch failures occurred
        if all_failed_ranges:
            raise RuntimeError(
                f"Post-exit backfill failed for {len(all_failed_ranges)} ranges: "
                f"incomplete post-exit coverage blocks FINAL"
            )
        
        total_rows = total_inserted + total_updated
        
        return {
            "output_rows": total_rows,
            "trades_considered": len(trades),
            "instruments_considered": len(instrument_map),
            "ranges_requested": sum(len(r) for r in ranges_by_key.values()),
            "gaps_detected": total_gaps,
            "inserted": total_inserted,
            "updated": total_updated,
            "rejected": total_rejected,
            "failed_ranges": all_failed_ranges,
            "failed_symbols": failed_symbols,
            "message": f"Post-exit backfill complete: inserted={total_inserted}, updated={total_updated}",
        }

    def _stage_final_quality_gate(
        self, run: AnalysisRun, stage_run: AnalysisStageRun
    ) -> dict[str, Any]:
        """Stage 2: Final quality gate for FINAL."""
        logger.info("Running final quality gate")
        
        passed, results = self._quality_gate.run_quality_checks(run)
        
        if not passed:
            raise RuntimeError("Final quality gate failed")
        
        # Check that all BLOCKING checks passed
        blocking_failures = self._quality_gate.get_blocking_failures(results)
        if blocking_failures:
            raise RuntimeError(
                f"Final quality gate has {len(blocking_failures)} BLOCKING failures"
            )
        
        return {
            "output_rows": len(results),
            "message": "Final quality gate passed",
        }