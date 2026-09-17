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
    QualityCheckStatus,
    QualityStatus,
    Watermark,
)
from app.analytics.agents.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    AgentInputManifest,
    AgentResult,
    DailyTradingReport,
    AgentType,
    ValidationStatus,
    ActionClass,
    ReportStatus,
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
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    str(run.run_id),
                    run.business_date,
                    run.schedule_timezone,
                    run.analysis_from,
                    run.analysis_to,
                    run.observation_cutoff,
                    run.post_exit_horizon,
                    run.maturity.value,
                    run.status.value,
                    run.pipeline_version,
                    json.dumps(run.source_watermarks),
                    run.started_at,
                    run.finished_at,
                    run.created_at,
                    run.updated_at,
                    run.error_code,
                    run.error_message,
                ),
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
                    maturity = %s,
                    status = %s,
                    observation_cutoff = %s,
                    started_at = %s,
                    finished_at = %s,
                    updated_at = %s,
                    error_code = %s,
                    error_message = %s
                WHERE run_id = %s
                """,
                (
                    run.maturity.value,
                    run.status.value,
                    run.observation_cutoff,
                    run.started_at,
                    run.finished_at,
                    datetime.now(timezone.utc),
                    run.error_code,
                    run.error_message,
                    str(run.run_id),
                ),
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
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                RETURNING stage_run_id
                """,
                (
                    str(stage_run.run_id),
                    stage_run.stage_name,
                    stage_run.attempt,
                    stage_run.status.value,
                    stage_run.input_rows,
                    stage_run.output_rows,
                    stage_run.watermark,
                    json.dumps(stage_run.result_json) if stage_run.result_json else None,
                    stage_run.started_at,
                    stage_run.finished_at,
                    stage_run.error_code,
                    stage_run.error_message,
                ),
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
                    status = %s,
                    input_rows = %s,
                    output_rows = %s,
                    watermark = %s,
                    result_json = %s,
                    started_at = %s,
                    finished_at = %s,
                    error_code = %s,
                    error_message = %s
                WHERE stage_run_id = %s
                """,
                (
                    stage_run.status.value,
                    stage_run.input_rows,
                    stage_run.output_rows,
                    stage_run.watermark,
                    json.dumps(stage_run.result_json) if stage_run.result_json else None,
                    stage_run.started_at,
                    stage_run.finished_at,
                    stage_run.error_code,
                    stage_run.error_message,
                    stage_run.stage_run_id,
                ),
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
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                RETURNING quality_result_id
                """,
                (
                    str(result.run_id),
                    result.stage_name,
                    result.check_name,
                    result.scope_type,
                    result.scope_id,
                    result.severity.value,
                    result.status.value,
                    json.dumps(result.expected_value) if result.expected_value else None,
                    json.dumps(result.actual_value) if result.actual_value else None,
                    result.affected_entity_count,
                    json.dumps(result.affected_entity_ids) if result.affected_entity_ids else None,
                    result.checked_at,
                    json.dumps(result.details) if result.details else None,
                ),
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
                (str(run_id) if not isinstance(run_id, str) else run_id,),
            )
            rows = cursor.fetchall()
            return [self._row_to_quality_result(row) for row in rows]
        except Exception as e:
            logger.error("Failed to get quality results: %s", e)
            raise

    def upsert_quality_result(self, result: DataQualityResult) -> DataQualityResult:
        """Upsert a data quality result (insert or update if exists).
        
        Uses (run_id, check_name) as the logical key to ensure idempotency.
        If a result already exists for the same run_id and check_name, it will be updated.
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.data_quality_result (
                    run_id, stage_name, check_name, scope_type, scope_id,
                    severity, status, expected_value, actual_value,
                    affected_entity_count, affected_entity_ids, checked_at, details
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (run_id, check_name) DO UPDATE SET
                    stage_name = EXCLUDED.stage_name,
                    scope_type = EXCLUDED.scope_type,
                    scope_id = EXCLUDED.scope_id,
                    severity = EXCLUDED.severity,
                    status = EXCLUDED.status,
                    expected_value = EXCLUDED.expected_value,
                    actual_value = EXCLUDED.actual_value,
                    affected_entity_count = EXCLUDED.affected_entity_count,
                    affected_entity_ids = EXCLUDED.affected_entity_ids,
                    checked_at = EXCLUDED.checked_at,
                    details = EXCLUDED.details
                RETURNING quality_result_id
                """,
                (
                    str(result.run_id),
                    result.stage_name,
                    result.check_name,
                    result.scope_type,
                    result.scope_id,
                    result.severity.value,
                    result.status.value,
                    json.dumps(result.expected_value) if result.expected_value else None,
                    json.dumps(result.actual_value) if result.actual_value else None,
                    result.affected_entity_count,
                    json.dumps(result.affected_entity_ids) if result.affected_entity_ids else None,
                    result.checked_at,
                    json.dumps(result.details) if result.details else None,
                ),
            )
            result.quality_result_id = cursor.fetchone()[0]
            self._conn.commit()
            logger.info(
                "Upserted quality result: %s for check: %s",
                result.quality_result_id,
                result.check_name,
            )
            return result
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to upsert quality result: %s", e)
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
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
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
                (
                    candle.exchange,
                    candle.market_type,
                    candle.instrument_id,
                    candle.timeframe,
                    candle.open_time,
                    candle.close_time,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    candle.turnover,
                    candle.is_closed,
                    candle.source,
                    candle.source_received_at,
                    candle.ingested_at,
                    candle.quality_status.value,
                ),
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
        # Handle UUID - pg8000 may return UUID object or string
        run_id = row[0]
        if isinstance(run_id, str):
            run_id = UUID(run_id)
        
        # Handle INTERVAL → timedelta conversion
        # pg8000 returns Python datetime.timedelta for PostgreSQL INTERVAL
        post_exit_horizon = row[6]
        if isinstance(post_exit_horizon, str):
            # Fallback: parse string "4 hours" → timedelta
            parts = post_exit_horizon.split()
            hours = int(parts[0]) if parts else 4
            post_exit_horizon = timedelta(hours=hours)
        elif not isinstance(post_exit_horizon, timedelta):
            post_exit_horizon = timedelta(hours=4)
        
        return AnalysisRun(
            run_id=run_id,
            business_date=row[1],
            schedule_timezone=row[2],
            analysis_from=row[3],
            analysis_to=row[4],
            observation_cutoff=row[5],
            post_exit_horizon=post_exit_horizon,
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
        run_id_raw = row[1]
        if isinstance(run_id_raw, str):
            run_id_raw = UUID(run_id_raw)
        # else: already a UUID from pg8000
        
        return DataQualityResult(
            quality_result_id=row[0],
            run_id=run_id_raw,
            stage_name=row[2],
            check_name=row[3],
            scope_type=row[4],
            scope_id=row[5],
            severity=Severity(row[6]),
            status=QualityCheckStatus(row[7]),
            expected_value=row[8] if isinstance(row[8], (dict, list)) else (json.loads(row[8]) if row[8] else None),
            actual_value=row[9] if isinstance(row[9], (dict, list)) else (json.loads(row[9]) if row[9] else None),
            affected_entity_count=row[10],
            affected_entity_ids=row[11] if isinstance(row[11], (dict, list)) else (json.loads(row[11]) if row[11] else None),
            checked_at=row[12],
            details=row[13] if isinstance(row[13], (dict, list)) else (json.loads(row[13]) if row[13] else None),
        )

    # ============================================================
    # Agent Definition
    # ============================================================

    def create_agent_definition(self, defn: AgentDefinition) -> AgentDefinition:
        """Register a new agent definition.
        
        Uses ON CONFLICT (agent_name) DO UPDATE to upsert — if the agent
        already exists, its contract/prompt versions and description are updated.
        Returns the definition with created_at/updated_at from DB.
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.agent_definition (
                    agent_name, agent_type, contract_version, prompt_version,
                    model, enabled, description
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (agent_name) DO UPDATE SET
                    agent_type = EXCLUDED.agent_type,
                    contract_version = EXCLUDED.contract_version,
                    prompt_version = EXCLUDED.prompt_version,
                    model = EXCLUDED.model,
                    enabled = EXCLUDED.enabled,
                    description = EXCLUDED.description
                RETURNING agent_name, agent_type, contract_version, prompt_version,
                          model, enabled, description, created_at, updated_at
                """,
                (
                    defn.agent_name,
                    defn.agent_type.value,
                    defn.contract_version,
                    defn.prompt_version,
                    defn.model,
                    defn.enabled,
                    defn.description,
                ),
            )
            row = cursor.fetchone()
            self._conn.commit()
            result = AgentDefinition(
                agent_name=row[0],
                agent_type=AgentType(row[1]),
                contract_version=row[2],
                prompt_version=row[3],
                model=row[4],
                enabled=row[5],
                description=row[6],
                created_at=row[7],
                updated_at=row[8],
            )
            logger.info("Created/upserted agent definition: %s", defn.agent_name)
            return result
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to create agent definition: %s", e)
            raise

    def get_agent_definition(self, agent_name: str) -> Optional[AgentDefinition]:
        """Get agent definition by name."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT agent_name, agent_type, contract_version, prompt_version,
                       model, enabled, description, created_at, updated_at
                FROM analytics.agent_definition
                WHERE agent_name = %s
                """,
                (agent_name,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return AgentDefinition(
                agent_name=row[0],
                agent_type=AgentType(row[1]),
                contract_version=row[2],
                prompt_version=row[3],
                model=row[4],
                enabled=row[5],
                description=row[6],
                created_at=row[7],
                updated_at=row[8],
            )
        except Exception as e:
            logger.error("Failed to get agent definition: %s", e)
            raise

    def list_agent_definitions(self, enabled_only: bool = True) -> list[AgentDefinition]:
        """List all agent definitions."""
        try:
            cursor = self._conn.cursor()
            if enabled_only:
                cursor.execute(
                    """
                    SELECT agent_name, agent_type, contract_version, prompt_version,
                           model, enabled, description, created_at, updated_at
                    FROM analytics.agent_definition
                    WHERE enabled = TRUE
                    ORDER BY agent_name
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT agent_name, agent_type, contract_version, prompt_version,
                           model, enabled, description, created_at, updated_at
                    FROM analytics.agent_definition
                    ORDER BY agent_name
                    """
                )
            rows = cursor.fetchall()
            return [
                AgentDefinition(
                    agent_name=r[0],
                    agent_type=AgentType(r[1]),
                    contract_version=r[2],
                    prompt_version=r[3],
                    model=r[4],
                    enabled=r[5],
                    description=r[6],
                    created_at=r[7],
                    updated_at=r[8],
                )
                for r in rows
            ]
        except Exception as e:
            logger.error("Failed to list agent definitions: %s", e)
            raise

    # ============================================================
    # Agent Run
    # ============================================================

    def create_agent_run(self, run: AgentRun) -> AgentRun:
        """Create a new agent run."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.agent_run (
                    agent_run_id, analysis_run_id, agent_name, attempt, model,
                    status, started_at, finished_at, latency_ms,
                    input_tokens, output_tokens, total_tokens,
                    error_class, error_message
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                RETURNING agent_run_id
                """,
                (
                    str(run.agent_run_id),
                    str(run.analysis_run_id),
                    run.agent_name,
                    run.attempt,
                    run.model,
                    run.status.value,
                    run.started_at,
                    run.finished_at,
                    run.latency_ms,
                    run.input_tokens,
                    run.output_tokens,
                    run.total_tokens,
                    run.error_class,
                    run.error_message,
                ),
            )
            run.agent_run_id = cursor.fetchone()[0]
            self._conn.commit()
            logger.info(
                "Created agent run: %s for agent: %s (attempt %d)",
                run.agent_run_id,
                run.agent_name,
                run.attempt,
            )
            return run
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to create agent run: %s", e)
            raise

    def get_agent_run(self, agent_run_id: UUID) -> Optional[AgentRun]:
        """Get agent run by ID."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT agent_run_id, analysis_run_id, agent_name, attempt, model,
                       status, started_at, finished_at, latency_ms,
                       input_tokens, output_tokens, total_tokens,
                       error_class, error_message, created_at
                FROM analytics.agent_run
                WHERE agent_run_id = %s
                """,
                (str(agent_run_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_agent_run(row)
        except Exception as e:
            logger.error("Failed to get agent run: %s", e)
            raise

    def get_agent_runs_for_analysis(self, analysis_run_id: UUID) -> list[AgentRun]:
        """Get all agent runs for an analysis run."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT agent_run_id, analysis_run_id, agent_name, attempt, model,
                       status, started_at, finished_at, latency_ms,
                       input_tokens, output_tokens, total_tokens,
                       error_class, error_message, created_at
                FROM analytics.agent_run
                WHERE analysis_run_id = %s
                ORDER BY agent_name, attempt
                """,
                (str(analysis_run_id),),
            )
            rows = cursor.fetchall()
            return [self._row_to_agent_run(r) for r in rows]
        except Exception as e:
            logger.error("Failed to get agent runs for analysis: %s", e)
            raise

    def update_agent_run_status(
        self,
        agent_run_id: UUID,
        status: AgentRunStatus,
        error_class: Optional[str] = None,
        error_message: Optional[str] = None,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
        latency_ms: Optional[int] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
    ) -> None:
        """Update agent run status and optional fields.
        
        Only non-None parameters are included in the SET clause, allowing
        partial updates of the agent run record.
        """
        try:
            cursor = self._conn.cursor()
            set_parts = ["status = %s"]
            params: list[Any] = [status.value]

            if error_class is not None:
                set_parts.append("error_class = %s")
                params.append(error_class)
            if error_message is not None:
                set_parts.append("error_message = %s")
                params.append(error_message)
            if started_at is not None:
                set_parts.append("started_at = %s")
                params.append(started_at)
            if finished_at is not None:
                set_parts.append("finished_at = %s")
                params.append(finished_at)
            if latency_ms is not None:
                set_parts.append("latency_ms = %s")
                params.append(latency_ms)
            if input_tokens is not None:
                set_parts.append("input_tokens = %s")
                params.append(input_tokens)
            if output_tokens is not None:
                set_parts.append("output_tokens = %s")
                params.append(output_tokens)
            if total_tokens is not None:
                set_parts.append("total_tokens = %s")
                params.append(total_tokens)

            params.append(str(agent_run_id))

            cursor.execute(
                f"UPDATE analytics.agent_run SET {', '.join(set_parts)} WHERE agent_run_id = %s",
                params,
            )
            self._conn.commit()
            logger.info("Updated agent run %s status to %s", agent_run_id, status.value)
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to update agent run status: %s", e)
            raise

    def get_next_attempt(self, analysis_run_id: UUID, agent_name: str) -> int:
        """Get the next attempt number for an agent on a given analysis run.
        
        Returns 1 if no attempts exist yet, otherwise MAX(attempt) + 1.
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT COALESCE(MAX(attempt), 0) + 1
                FROM analytics.agent_run
                WHERE analysis_run_id = %s AND agent_name = %s
                """,
                (str(analysis_run_id), agent_name),
            )
            row = cursor.fetchone()
            return row[0] if row else 1
        except Exception as e:
            logger.error("Failed to get next attempt: %s", e)
            raise

    # ============================================================
    # Agent Input Manifest
    # ============================================================

    def create_input_manifest(self, manifest: AgentInputManifest) -> AgentInputManifest:
        """Create an immutable input manifest. Rejects if manifest contains secrets."""
        from app.analytics.security.redaction import contains_secret_in_object
        if contains_secret_in_object(manifest.manifest_json):
            raise ValueError(
                "SECURITY_POLICY_ERROR: manifest_json contains detected secret — "
                "immutable artifacts must not store secrets"
            )
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.agent_input_manifest (
                    agent_run_id, dataset_version, input_hash, schema_version,
                    analysis_window_from, analysis_window_to,
                    maturity, data_quality_status,
                    limitations, sample_sizes, metrics, segments, cases,
                    evidence_ids, manifest_json
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    str(manifest.agent_run_id),
                    manifest.dataset_version,
                    manifest.input_hash,
                    manifest.schema_version,
                    manifest.analysis_window_from,
                    manifest.analysis_window_to,
                    manifest.maturity,
                    manifest.data_quality_status,
                    json.dumps(manifest.limitations),
                    json.dumps(manifest.sample_sizes),
                    json.dumps(manifest.metrics),
                    json.dumps(manifest.segments),
                    json.dumps(manifest.cases),
                    json.dumps(manifest.evidence_ids),
                    json.dumps(manifest.manifest_json),
                ),
            )
            self._conn.commit()
            logger.info(
                "Created input manifest for agent run: %s",
                manifest.agent_run_id,
            )
            return manifest
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to create input manifest: %s", e)
            raise

    def get_input_manifest(self, agent_run_id: UUID) -> Optional[AgentInputManifest]:
        """Get input manifest by agent_run_id."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT agent_run_id, dataset_version, input_hash, schema_version,
                       analysis_window_from, analysis_window_to,
                       maturity, data_quality_status,
                       limitations, sample_sizes, metrics, segments, cases,
                       evidence_ids, manifest_json, created_at
                FROM analytics.agent_input_manifest
                WHERE agent_run_id = %s
                """,
                (str(agent_run_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return AgentInputManifest(
                agent_run_id=UUID(row[0]) if isinstance(row[0], str) else row[0],
                dataset_version=row[1],
                input_hash=row[2],
                schema_version=row[3],
                analysis_window_from=row[4],
                analysis_window_to=row[5],
                maturity=row[6],
                data_quality_status=row[7],
                limitations=json.loads(row[8]) if row[8] else [],
                sample_sizes=json.loads(row[9]) if row[9] else {},
                metrics=json.loads(row[10]) if row[10] else {},
                segments=json.loads(row[11]) if row[11] else [],
                cases=json.loads(row[12]) if row[12] else [],
                evidence_ids=json.loads(row[13]) if row[13] else [],
                manifest_json=json.loads(row[14]) if row[14] else {},
                created_at=row[15],
            )
        except Exception as e:
            logger.error("Failed to get input manifest: %s", e)
            raise

    # ============================================================
    # Agent Result
    # ============================================================

    def create_agent_result(self, result: AgentResult) -> AgentResult:
        """Create an immutable agent result. Rejects if result contains secrets."""
        from app.analytics.security.redaction import contains_secret_in_object
        if contains_secret_in_object(result.result_json):
            raise ValueError(
                "SECURITY_POLICY_ERROR: result_json contains detected secret — "
                "immutable artifacts must not store secrets"
            )
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.agent_result (
                    agent_run_id, schema_version, result_json,
                    result_hash, validation_status
                ) VALUES (
                    %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    str(result.agent_run_id),
                    result.schema_version,
                    json.dumps(result.result_json),
                    result.result_hash,
                    result.validation_status.value,
                ),
            )
            self._conn.commit()
            logger.info(
                "Created agent result for run: %s",
                result.agent_run_id,
            )
            return result
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to create agent result: %s", e)
            raise

    def get_agent_result(self, agent_run_id: UUID) -> Optional[AgentResult]:
        """Get agent result by agent_run_id."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT agent_run_id, schema_version, result_json,
                       result_hash, validation_status, created_at
                FROM analytics.agent_result
                WHERE agent_run_id = %s
                """,
                (str(agent_run_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return AgentResult(
                agent_run_id=UUID(row[0]) if isinstance(row[0], str) else row[0],
                schema_version=row[1],
                result_json=json.loads(row[2]) if row[2] else {},
                result_hash=row[3],
                validation_status=ValidationStatus(row[4]),
                created_at=row[5],
            )
        except Exception as e:
            logger.error("Failed to get agent result: %s", e)
            raise

    # ============================================================
    # Daily Trading Report
    # ============================================================

    def create_daily_report(self, report: DailyTradingReport) -> DailyTradingReport:
        """Create a daily trading report."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.daily_trading_report (
                    report_id, analysis_run_id, report_version, maturity,
                    executive_summary, findings, hypotheses, proposed_experiments,
                    action_class, limitations, evidence_refs, partial,
                    missing_agents, status, agent_run_ids
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s
                )
                RETURNING report_id
                """,
                (
                    str(report.report_id),
                    str(report.analysis_run_id),
                    report.report_version,
                    report.maturity,
                    report.executive_summary,
                    json.dumps(report.findings),
                    json.dumps(report.hypotheses),
                    json.dumps(report.proposed_experiments),
                    report.action_class.value,
                    json.dumps(report.limitations),
                    json.dumps(report.evidence_refs),
                    report.partial,
                    json.dumps(report.missing_agents),
                    report.status.value,
                    json.dumps(report.agent_run_ids),
                ),
            )
            report.report_id = cursor.fetchone()[0]
            self._conn.commit()
            logger.info(
                "Created daily report: %s (version %d, maturity %s)",
                report.report_id,
                report.report_version,
                report.maturity,
            )
            return report
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to create daily report: %s", e)
            raise

    def get_daily_report(self, report_id: UUID) -> Optional[DailyTradingReport]:
        """Get report by ID."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT report_id, analysis_run_id, report_version, maturity,
                       executive_summary, findings, hypotheses, proposed_experiments,
                       action_class, limitations, evidence_refs, partial,
                       missing_agents, status, agent_run_ids, created_at
                FROM analytics.daily_trading_report
                WHERE report_id = %s
                """,
                (str(report_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_daily_report(row)
        except Exception as e:
            logger.error("Failed to get daily report: %s", e)
            raise

    def get_latest_report(
        self, analysis_run_id: UUID, maturity: str
    ) -> Optional[DailyTradingReport]:
        """Get latest report version for a given analysis run and maturity."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT report_id, analysis_run_id, report_version, maturity,
                       executive_summary, findings, hypotheses, proposed_experiments,
                       action_class, limitations, evidence_refs, partial,
                       missing_agents, status, agent_run_ids, created_at
                FROM analytics.daily_trading_report
                WHERE analysis_run_id = %s AND maturity = %s
                ORDER BY report_version DESC
                LIMIT 1
                """,
                (str(analysis_run_id), maturity),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_daily_report(row)
        except Exception as e:
            logger.error("Failed to get latest report: %s", e)
            raise

    def list_reports_for_run(self, analysis_run_id: UUID) -> list[DailyTradingReport]:
        """List all report versions for an analysis run."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT report_id, analysis_run_id, report_version, maturity,
                       executive_summary, findings, hypotheses, proposed_experiments,
                       action_class, limitations, evidence_refs, partial,
                       missing_agents, status, agent_run_ids, created_at
                FROM analytics.daily_trading_report
                WHERE analysis_run_id = %s
                ORDER BY report_version DESC
                """,
                (str(analysis_run_id),),
            )
            rows = cursor.fetchall()
            return [self._row_to_daily_report(r) for r in rows]
        except Exception as e:
            logger.error("Failed to list reports for run: %s", e)
            raise

    def update_daily_report_status(
        self, report_id: UUID, status: str,
    ) -> None:
        """Update a daily trading report's status (e.g. SUPERSEDED)."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                "UPDATE analytics.daily_trading_report SET status = %s WHERE report_id = %s",
                (status, str(report_id)),
            )
            self._conn.commit()
            logger.info("Updated report %s status to %s", report_id, status)
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to update report status: %s", e)
            raise

    # ============================================================
    # Agent Row Converters (private)
    # ============================================================

    def _row_to_agent_run(self, row: tuple) -> AgentRun:
        """Convert a database row to AgentRun."""
        agent_run_id = row[0]
        if isinstance(agent_run_id, str):
            agent_run_id = UUID(agent_run_id)

        analysis_run_id = row[1]
        if isinstance(analysis_run_id, str):
            analysis_run_id = UUID(analysis_run_id)

        return AgentRun(
            agent_run_id=agent_run_id,
            analysis_run_id=analysis_run_id,
            agent_name=row[2],
            attempt=row[3],
            model=row[4],
            status=AgentRunStatus(row[5]),
            started_at=row[6],
            finished_at=row[7],
            latency_ms=row[8],
            input_tokens=row[9],
            output_tokens=row[10],
            total_tokens=row[11],
            error_class=row[12],
            error_message=row[13],
            created_at=row[14],
        )

    def _row_to_daily_report(self, row: tuple) -> DailyTradingReport:
        """Convert a database row to DailyTradingReport."""
        report_id = row[0]
        if isinstance(report_id, str):
            report_id = UUID(report_id)

        analysis_run_id = row[1]
        if isinstance(analysis_run_id, str):
            analysis_run_id = UUID(analysis_run_id)

        return DailyTradingReport(
            report_id=report_id,
            analysis_run_id=analysis_run_id,
            report_version=row[2],
            maturity=row[3],
            executive_summary=row[4],
            findings=json.loads(row[5]) if row[5] else [],
            hypotheses=json.loads(row[6]) if row[6] else [],
            proposed_experiments=json.loads(row[7]) if row[7] else [],
            action_class=ActionClass(row[8]),
            limitations=json.loads(row[9]) if row[9] else [],
            evidence_refs=json.loads(row[10]) if row[10] else [],
            partial=row[11],
            missing_agents=json.loads(row[12]) if row[12] else [],
            status=ReportStatus(row[13]),
            agent_run_ids=json.loads(row[14]) if row[14] else [],
            created_at=row[15],
        )

    # ============================================================
    # Dataset Publication
    # ============================================================

    def create_dataset_publication(
        self,
        analysis_run_id: UUID,
        dataset_version: str,
        maturity: str,
        quality_status: str,
        analysis_window_from: datetime,
        analysis_window_to: datetime,
        observation_cutoff: datetime,
        canonical_build_json: Optional[dict] = None,
        quality_summary_json: Optional[dict] = None,
    ) -> UUID:
        """Create a new dataset publication. Returns publication_id.

        Also inserts an initial CREATED event into the publication log.
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.dataset_publication (
                    analysis_run_id, dataset_version, maturity,
                    status, quality_status,
                    analysis_window_from, analysis_window_to,
                    observation_cutoff,
                    canonical_build_json, quality_summary_json
                ) VALUES (
                    %s, %s, %s,
                    'BUILDING', %s,
                    %s, %s,
                    %s,
                    %s, %s
                )
                RETURNING publication_id
                """,
                (
                    str(analysis_run_id),
                    dataset_version,
                    maturity,
                    quality_status,
                    analysis_window_from,
                    analysis_window_to,
                    observation_cutoff,
                    json.dumps(canonical_build_json) if canonical_build_json else "{}",
                    json.dumps(quality_summary_json) if quality_summary_json else "{}",
                ),
            )
            row = cursor.fetchone()
            publication_id = row[0]

            # Log the creation event
            cursor.execute(
                """
                INSERT INTO analytics.dataset_publication_log (
                    publication_id, event_type, event_json
                ) VALUES (%s, 'CREATED', %s)
                """,
                (
                    str(publication_id),
                    json.dumps({
                        "dataset_version": dataset_version,
                        "maturity": maturity,
                        "quality_status": quality_status,
                    }),
                ),
            )

            self._conn.commit()
            logger.info(
                "Created dataset publication %s for run %s (maturity=%s)",
                publication_id, analysis_run_id, maturity,
            )
            return publication_id
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to create dataset publication: %s", e)
            raise

    def get_dataset_publication(
        self, analysis_run_id: UUID, maturity: str
    ) -> Optional[dict]:
        """Get dataset publication by analysis_run_id and maturity."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT publication_id, analysis_run_id, dataset_version,
                       maturity, status, quality_status,
                       canonical_schema_version,
                       analysis_window_from, analysis_window_to,
                       observation_cutoff,
                       canonical_build_json, quality_summary_json,
                       published_at, created_at, updated_at
                FROM analytics.dataset_publication
                WHERE analysis_run_id = %s AND maturity = %s
                """,
                (str(analysis_run_id), maturity),
            )
            row = cursor.fetchone()
            if not row:
                return None

            return {
                "publication_id": row[0],
                "analysis_run_id": row[1],
                "dataset_version": row[2],
                "maturity": row[3],
                "status": row[4],
                "quality_status": row[5],
                "canonical_schema_version": row[6],
                "analysis_window_from": row[7],
                "analysis_window_to": row[8],
                "observation_cutoff": row[9],
                "canonical_build_json": row[10],
                "quality_summary_json": row[11],
                "published_at": row[12],
                "created_at": row[13],
                "updated_at": row[14],
            }
        except Exception as e:
            logger.error("Failed to get dataset publication: %s", e)
            raise

    def get_dataset_publication_by_version(
        self, dataset_version: str
    ) -> Optional[dict]:
        """Get dataset publication by version hash."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT publication_id, analysis_run_id, dataset_version,
                       maturity, status, quality_status,
                       canonical_schema_version,
                       analysis_window_from, analysis_window_to,
                       observation_cutoff,
                       canonical_build_json, quality_summary_json,
                       published_at, created_at, updated_at
                FROM analytics.dataset_publication
                WHERE dataset_version = %s
                """,
                (dataset_version,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            return {
                "publication_id": row[0],
                "analysis_run_id": row[1],
                "dataset_version": row[2],
                "maturity": row[3],
                "status": row[4],
                "quality_status": row[5],
                "canonical_schema_version": row[6],
                "analysis_window_from": row[7],
                "analysis_window_to": row[8],
                "observation_cutoff": row[9],
                "canonical_build_json": row[10],
                "quality_summary_json": row[11],
                "published_at": row[12],
                "created_at": row[13],
                "updated_at": row[14],
            }
        except Exception as e:
            logger.error("Failed to get dataset publication by version: %s", e)
            raise

    def update_dataset_publication_status(
        self,
        publication_id: UUID,
        status: str,
        quality_status: Optional[str] = None,
        canonical_build_json: Optional[dict] = None,
        quality_summary_json: Optional[dict] = None,
    ) -> None:
        """Update publication status. Also logs the event.

        Sets ``published_at`` to ``NOW()`` when status transitions to READY.
        """
        try:
            cursor = self._conn.cursor()

            set_parts: list[str] = ["status = %s"]
            params: list[Any] = [status]

            if quality_status is not None:
                set_parts.append("quality_status = %s")
                params.append(quality_status)
            if canonical_build_json is not None:
                set_parts.append("canonical_build_json = %s")
                params.append(json.dumps(canonical_build_json))
            if quality_summary_json is not None:
                set_parts.append("quality_summary_json = %s")
                params.append(json.dumps(quality_summary_json))
            if status == "READY":
                set_parts.append("published_at = NOW()")

            params.append(str(publication_id))

            cursor.execute(
                f"""
                UPDATE analytics.dataset_publication
                SET {', '.join(set_parts)}
                WHERE publication_id = %s
                """,
                params,
            )

            # Determine log event type from the new status
            event_type_map = {
                "READY": "PUBLISHED",
                "FAILED": "FAILED",
                "BUILDING": "STATUS_CHANGE",
            }
            event_type = event_type_map.get(status, "STATUS_CHANGE")

            event_payload: dict[str, Any] = {"new_status": status}
            if quality_status is not None:
                event_payload["quality_status"] = quality_status
            if canonical_build_json is not None:
                event_payload["canonical_build_json"] = canonical_build_json
            if quality_summary_json is not None:
                event_payload["quality_summary_json"] = quality_summary_json

            cursor.execute(
                """
                INSERT INTO analytics.dataset_publication_log (
                    publication_id, event_type, event_json
                ) VALUES (%s, %s, %s)
                """,
                (str(publication_id), event_type, json.dumps(event_payload)),
            )

            self._conn.commit()
            logger.info(
                "Updated dataset publication %s status to %s",
                publication_id, status,
            )
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to update dataset publication status: %s", e)
            raise

    def log_publication_event(
        self,
        publication_id: UUID,
        event_type: str,
        event_json: Optional[dict] = None,
    ) -> None:
        """Log a publication event to the append-only log."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO analytics.dataset_publication_log (
                    publication_id, event_type, event_json
                ) VALUES (%s, %s, %s)
                """,
                (str(publication_id), event_type, json.dumps(event_json) if event_json else "{}"),
            )
            self._conn.commit()
            logger.info(
                "Logged publication event '%s' for publication %s",
                event_type, publication_id,
            )
        except Exception as e:
            self._conn.rollback()
            logger.error("Failed to log publication event: %s", e)
            raise

    def get_stage_run_by_name(
        self, run_id: UUID, stage_name: str
    ) -> Optional[AnalysisStageRun]:
        """Get the latest stage run for a given stage name within an analysis run."""
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT stage_run_id, run_id, stage_name, attempt, status,
                       input_rows, output_rows, watermark, result_json,
                       started_at, finished_at, error_code, error_message
                FROM analytics.analysis_stage_run
                WHERE run_id = %s AND stage_name = %s
                ORDER BY attempt DESC
                LIMIT 1
                """,
                (str(run_id), stage_name),
            )
            row = cursor.fetchone()
            if not row:
                return None

            stage_run_id = row[0]
            if isinstance(stage_run_id, str):
                stage_run_id = UUID(stage_run_id)

            run_id_val = row[1]
            if isinstance(run_id_val, str):
                run_id_val = UUID(run_id_val)

            return AnalysisStageRun(
                stage_run_id=stage_run_id,
                run_id=run_id_val,
                stage_name=row[2],
                attempt=row[3],
                status=StageStatus(row[4]),
                input_rows=row[5],
                output_rows=row[6],
                watermark=row[7],
                result_json=json.loads(row[8]) if row[8] else None,
                started_at=row[9],
                finished_at=row[10],
                error_code=row[11],
                error_message=row[12],
            )
        except Exception as e:
            logger.error("Failed to get stage run by name: %s", e)
            raise

    def get_stale_agent_runs(
        self, stale_timeout_seconds: int = 3600
    ) -> list[dict]:
        """Find agent runs stuck in RUNNING state for longer than timeout.

        Returns a list of dicts with keys: agent_run_id, agent_name, started_at.
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT agent_run_id, agent_name, started_at
                FROM analytics.agent_run
                WHERE status = 'RUNNING'
                  AND started_at < NOW() - make_interval(secs => %s)
                ORDER BY started_at ASC
                """,
                (stale_timeout_seconds,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "agent_run_id": row[0],
                    "agent_name": row[1],
                    "started_at": row[2],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error("Failed to get stale agent runs: %s", e)
            raise
