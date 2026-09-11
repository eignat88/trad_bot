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
            
            # Update status to RUNNING
            run.status = RunStatus.RUNNING
            run.started_at = datetime.now(timezone.utc)
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
            # Update run
            run = provisional_run
            run.status = RunStatus.RUNNING
            run.started_at = datetime.now(timezone.utc)
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
        
        # Post-exit horizon
        post_exit_horizon = f"{self._settings.analytics_post_exit_hours} hours"
        
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
        """Stage 1: Candle reconciliation."""
        logger.info("Running candle reconciliation")
        
        # This is a placeholder - actual implementation would:
        # 1. Get required candle ranges from trades
        # 2. Reconcile with existing data
        # 3. Fetch missing candles from Bybit
        
        return {
            "output_rows": 0,
            "message": "Candle reconciliation completed",
        }

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
        """Stage 1: Post-exit candle backfill for FINAL."""
        logger.info("Running post-exit backfill")
        
        # This is a placeholder - actual implementation would:
        # 1. Get trades that need post-exit data
        # 2. Check coverage for each trade
        # 3. Fetch missing post-exit candles
        
        return {
            "output_rows": 0,
            "message": "Post-exit backfill completed",
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