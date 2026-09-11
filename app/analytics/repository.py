from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, date, timedelta
from typing import Any, Optional
from uuid import UUID

from app.analytics.models import (
    AnalysisRun,
    AnalysisStageRun,
    DataQualityResult,
    Candle,
    Maturity,
    RunStatus,
    StageStatus,
    Severity,
    QualityStatus,
    Watermark,
)

logger = logging.getLogger(__name__)


class AnalyticsRepository:
    """Repository for analytics pipeline data."""

    def __init__(self, conn: Any):
        """Initialize with database connection."""
        self._conn = conn

    def create_analysis_run(self, run: AnalysisRun) -> AnalysisRun:
        """Create a new analysis run."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.analysis_run (
                    run_id, business_date, schedule_timezone, analysis_from, analysis_to,
                    observation_cutoff, post_exit_horizon, maturity, status, pipeline_version,
                    source_watermarks, started_at, finished_at, created_at, updated_at,
                    error_code, error_message
                ) VALUES (
                    %(run_id)s, %(business_date)s, %(schedule_timezone)s, %(analysis_from)s, %(analysis_to)s,
                    %(observation_cutoff)s, %(post_exit_horizon)s, %(maturity)s, %(status)s, %(pipeline_version)s,
                    %(source_watermarks)s, %(started_at)s, %(finished_at)s, %(created_at)s, %(updated_at)s,
                    %(error_code)s, %(error_message)s
                )
                """,
                {
                    "run_id": str(run.run_id),
                    "business_date": run.business_date,
                    "schedule_timezone": run.schedule_timezone,
                    "analysis_from": run.analysis_from,
                    "analysis_to": run.analysis_to,
                    "observation_cutoff": run.observation_cutoff,
                    "post_exit_horizon": run.post_exit_horizon,
                    "maturity": run.maturity.value,
                    "status": run.status.value,
                    "pipeline_version": run.pipeline_version,
                    "source_watermarks": json.dumps(run.source_watermarks),
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "created_at": run.created_at,
                    "updated_at": run.updated_at,
                    "error_code": run.error_code,
                    "error_message": run.error_message,
                },
            )
            self._conn.commit()
            logger.info("Created analysis run: %s for date: %s", run.run_id, run.business_date)
            return run
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to create analysis run: %s", e)
            raise

    def get_analysis_run(self, run_id: UUID) -> Optional[AnalysisRun]:
        """Get an analysis run by ID."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT * FROM analytics.analysis_run WHERE run_id = %s",
                (str(run_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            return self._row_to_analysis_run(row)
        except Exception as e:
            logger.error("Failed to get analysis run: %s", e)
            raise

    def get_analysis_run_by_date(
        self, business_date: date, pipeline_version: str = "1.0.0"
    ) -> Optional[AnalysisRun]:
        """Get an analysis run by business date and pipeline version."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT * FROM analytics.analysis_run 
                WHERE business_date = %s AND pipeline_version = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (business_date, pipeline_version),
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            return self._row_to_analysis_run(row)
        except Exception as e:
            logger.error("Failed to get analysis run by date: %s", e)
            raise

    def update_analysis_run(self, run: AnalysisRun) -> AnalysisRun:
        """Update an existing analysis run."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                UPDATE analytics.analysis_run SET
                    maturity = %(maturity)s,
                    status = %(status)s,
                    started_at = %(started_at)s,
                    finished_at = %(finished_at)s,
                    updated_at = %(updated_at)s,
                    error_code = %(error_code)s,
                    error_message = %(error_message)s
                WHERE run_id = %(run_id)s
                """,
                {
                    "run_id": str(run.run_id),
                    "maturity": run.maturity.value,
                    "status": run.status.value,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "updated_at": datetime.now(timezone.utc),
                    "error_code": run.error_code,
                    "error_message": run.error_message,
                },
            )
            self._conn.commit()
            logger.info("Updated analysis run: %s", run.run_id)
            return run
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to update analysis run: %s", e)
            raise

    def create_stage_run(self, stage_run: AnalysisStageRun) -> AnalysisStageRun:
        """Create a new stage run."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.analysis_stage_run (
                    run_id, stage_name, attempt, status, input_rows, output_rows,
                    watermark, result_json, started_at, finished_at, error_code, error_message
                ) VALUES (
                    %(run_id)s, %(stage_name)s, %(attempt)s, %(status)s, %(input_rows)s, %(output_rows)s,
                    %(watermark)s, %(result_json)s, %(started_at)s, %(finished_at)s, %(error_code)s, %(error_message)s
                )
                RETURNING stage_run_id
                """,
                {
                    "run_id": str(stage_run.run_id),
                    "stage_name": stage_run.stage_name,
                    "attempt": stage_run.attempt,
                    "status": stage_run.status.value,
                    "input_rows": stage_run.input_rows,
                    "output_rows": stage_run.output_rows,
                    "watermark": stage_run.watermark,
                    "result_json": json.dumps(stage_run.result_json) if stage_run.result_json else None,
                    "started_at": stage_run.started_at,
                    "finished_at": stage_run.finished_at,
                    "error_code": stage_run.error_code,
                    "error_message": stage_run.error_message,
                },
            )
            stage_run.stage_run_id = cursor.fetchone()[0]
            self._conn.commit()
            logger.info(
                "Created stage run: %s for stage: %s",
                stage_run.stage_run_id,
                stage_run.stage_name,
            )
            return stage_run
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to create stage run: %s", e)
            raise

    def update_stage_run(self, stage_run: AnalysisStageRun) -> AnalysisStageRun:
        """Update an existing stage run."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                UPDATE analytics.analysis_stage_run SET
                    status = %(status)s,
                    input_rows = %(input_rows)s,
                    output_rows = %(output_rows)s,
                    watermark = %(watermark)s,
                    result_json = %(result_json)s,
                    started_at = %(started_at)s,
                    finished_at = %(finished_at)s,
                    error_code = %(error_code)s,
                    error_message = %(error_message)s
                WHERE stage_run_id = %(stage_run_id)s
                """,
                {
                    "stage_run_id": stage_run.stage_run_id,
                    "status": stage_run.status.value,
                    "input_rows": stage_run.input_rows,
                    "output_rows": stage_run.output_rows,
                    "watermark": stage_run.watermark,
                    "result_json": json.dumps(stage_run.result_json) if stage_run.result_json else None,
                    "started_at": stage_run.started_at,
                    "finished_at": stage_run.finished_at,
                    "error_code": stage_run.error_code,
                    "error_message": stage_run.error_message,
                },
            )
            self._conn.commit()
            logger.info("Updated stage run: %s", stage_run.stage_run_id)
            return stage_run
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to update stage run: %s", e)
            raise

    def create_quality_result(self, result: DataQualityResult) -> DataQualityResult:
        """Create a new data quality result."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.data_quality_result (
                    run_id, stage_name, check_name, scope_type, scope_id,
                    severity, status, expected_value, actual_value,
                    affected_entity_count, affected_entity_ids, checked_at, details
                ) VALUES (
                    %(run_id)s, %(stage_name)s, %(check_name)s, %(scope_type)s, %(scope_id)s,
                    %(severity)s, %(status)s, %(expected_value)s, %(actual_value)s,
                    %(affected_entity_count)s, %(affected_entity_ids)s, %(checked_at)s, %(details)s
                )
                RETURNING quality_result_id
                """,
                {
                    "run_id": str(result.run_id),
                    "stage_name": result.stage_name,
                    "check_name": result.check_name,
                    "scope_type": result.scope_type,
                    "scope_id": result.scope_id,
                    "severity": result.severity.value,
                    "status": result.status.value,
                    "expected_value": json.dumps(result.expected_value) if result.expected_value else None,
                    "actual_value": json.dumps(result.actual_value) if result.actual_value else None,
                    "affected_entity_count": result.affected_entity_count,
                    "affected_entity_ids": json.dumps(result.affected_entity_ids) if result.affected_entity_ids else None,
                    "checked_at": result.checked_at,
                    "details": json.dumps(result.details) if result.details else None,
                },
            )
            result.quality_result_id = cursor.fetchone()[0]
            self._conn.commit()
            logger.info(
                "Created quality result: %s for check: %s",
                result.quality_result_id,
                result.check_name,
            )
            return result
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to create quality result: %s", e)
            raise

    def get_quality_results(self, run_id: UUID) -> list[DataQualityResult]:
        """Get all quality results for an analysis run."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT * FROM analytics.data_quality_result WHERE run_id = %s",
                (str(run_id),),
            )
            rows = cursor.fetchall()
            return [self._row_to_quality_result(row) for row in rows]
        except Exception as e:
            logger.error("Failed to get quality results: %s", e)
            raise

    def insert_candle(self, candle: Candle) -> bool:
        """Insert or update a candle (UPSERT)."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO market.candle (
                    exchange, market_type, instrument_id, timeframe, open_time,
                    close_time, open, high, low, close, volume, turnover,
                    is_closed, source, source_received_at, ingested_at, quality_status
                ) VALUES (
                    %(exchange)s, %(market_type)s, %(instrument_id)s, %(timeframe)s, %(open_time)s,
                    %(close_time)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s, %(turnover)s,
                    %(is_closed)s, %(source)s, %(source_received_at)s, %(ingested_at)s, %(quality_status)s
                )
                ON CONFLICT (exchange, market_type, instrument_id, timeframe, open_time)
                DO UPDATE SET
                    close_time = EXCLUDED.close_time,
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    turnover = EXCLUDED.turnover,
                    is_closed = EXCLUDED.is_closed,
                    source = EXCLUDED.source,
                    source_received_at = EXCLUDED.source_received_at,
                    ingested_at = EXCLUDED.ingested_at,
                    quality_status = EXCLUDED.quality_status
                """,
                {
                    "exchange": candle.exchange,
                    "market_type": candle.market_type,
                    "instrument_id": candle.instrument_id,
                    "timeframe": candle.timeframe,
                    "open_time": candle.open_time,
                    "close_time": candle.close_time,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "turnover": candle.turnover,
                    "is_closed": candle.is_closed,
                    "source": candle.source,
                    "source_received_at": candle.source_received_at,
                    "ingested_at": candle.ingested_at,
                    "quality_status": candle.quality_status.value,
                },
            )
            self._conn.commit()
            return True
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to insert candle: %s", e)
            return False

    def insert_candles_batch(self, candles: list[Candle]) -> tuple[int, int, int]:
        """Insert or update a batch of candles. Returns (inserted, updated, rejected)."""
        inserted = 0
        updated = 0
        rejected = 0
        
        for candle in candles:
            try:
                # Check if candle already exists
                cursor = self._conn.cursor()
                cursor.execute(
                    """
                    SELECT 1 FROM market.candle 
                    WHERE exchange = %s AND market_type = %s AND instrument_id = %s 
                    AND timeframe = %s AND open_time = %s
                    """,
                    (candle.exchange, candle.market_type, candle.instrument_id, candle.timeframe, candle.open_time),
                )
                exists = cursor.fetchone() is not None
                
                if self.insert_candle(candle):
                    if exists:
                        updated += 1
                    else:
                        inserted += 1
                else:
                    rejected += 1
            except Exception as e:
                logger.warning("Failed to insert candle: %s", e)
                rejected += 1
        
        logger.info(
            "Candle batch insert: inserted=%d, updated=%d, rejected=%d",
            inserted,
            updated,
            rejected,
        )
        return inserted, updated, rejected

    def get_candle_coverage(
        self, instrument_id: int, timeframe: str, from_time: datetime, to_time: datetime
    ) -> list[dict[str, Any]]:
        """Get candle coverage gaps for a time range."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT * FROM market.check_candle_coverage(%s, %s, %s, %s)",
                (instrument_id, timeframe, from_time, to_time),
            )
            return [
                {
                    "gap_start": row[0],
                    "gap_end": row[1],
                    "gap_duration": row[2],
                }
                for row in cursor.fetchall()
            ]
        except Exception as e:
            logger.error("Failed to get candle coverage: %s", e)
            raise

    def get_watermark(self, instrument_id: int, timeframe: str) -> Optional[Watermark]:
        """Get the latest candle timestamp for an instrument."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT MAX(open_time) as latest_candle_time
                FROM market.candle
                WHERE instrument_id = %s AND timeframe = %s AND is_closed = TRUE
                """,
                (instrument_id, timeframe),
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                return None
            
            return Watermark(
                instrument_id=instrument_id,
                timeframe=timeframe,
                latest_candle_time=row[0],
                updated_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.error("Failed to get watermark: %s", e)
            raise

    def get_existing_final_run(
        self, business_date: date, pipeline_version: str = "1.0.0"
    ) -> Optional[AnalysisRun]:
        """Check if a FINAL run already exists for a business date."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT * FROM analytics.analysis_run 
                WHERE business_date = %s AND pipeline_version = %s AND maturity = 'FINAL'
                LIMIT 1
                """,
                (business_date, pipeline_version),
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            return self._row_to_analysis_run(row)
        except Exception as e:
            logger.error("Failed to get existing final run: %s", e)
            raise

    def acquire_advisory_lock(self, lock_id: int = 1) -> bool:
        """Acquire a PostgreSQL advisory lock."""
        try:
            cursor = self._conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
            result = cursor.fetchone()
            return result[0] if result else False
        except Exception as e:
            logger.error("Failed to acquire advisory lock: %s", e)
            return False

    def release_advisory_lock(self, lock_id: int = 1) -> bool:
        """Release a PostgreSQL advisory lock."""
        try:
            cursor = self._conn.cursor()
            cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
            result = cursor.fetchone()
            return result[0] if result else False
        except Exception as e:
            logger.error("Failed to release advisory lock: %s", e)
            return False

    def _row_to_analysis_run(self, row: tuple) -> AnalysisRun:
        """Convert a database row to AnalysisRun."""
        return AnalysisRun(
            run_id=UUID(row[0]),
            business_date=row[1],
            schedule_timezone=row[2],
            analysis_from=row[3],
            analysis_to=row[4],
            observation_cutoff=row[5],
            post_exit_horizon=row[6],
            maturity=Maturity(row[7]),
            status=RunStatus(row[8]),
            pipeline_version=row[9],
            source_watermarks=json.loads(row[10]) if row[10] else {},
            started_at=row[11],
            finished_at=row[12],
            created_at=row[13],
            updated_at=row[14],
            error_code=row[15],
            error_message=row[16],
        )

    def _row_to_quality_result(self, row: tuple) -> DataQualityResult:
        """Convert a database row to DataQualityResult."""
        return DataQualityResult(
            quality_result_id=row[0],
            run_id=UUID(row[1]),
            stage_name=row[2],
            check_name=row[3],
            scope_type=row[4],
            scope_id=row[5],
            severity=Severity(row[6]),
            status=StageStatus(row[7]),
            expected_value=json.loads(row[8]) if row[8] else None,
            actual_value=json.loads(row[9]) if row[9] else None,
            affected_entity_count=row[10],
            affected_entity_ids=json.loads(row[11]) if row[11] else None,
            checked_at=row[12],
            details=json.loads(row[13]) if row[13] else None,
        )