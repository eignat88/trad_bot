from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from uuid import UUID

from app.analytics.models import (
    AnalysisRun,
    DataQualityResult,
    Severity,
    QualityCheckStatus,
)
from app.analytics.candle_sync import normalize_timeframe, align_to_grid
from app.analytics.repository import AnalyticsRepository

logger = logging.getLogger(__name__)


class DataQualityGate:
    """Data quality gate for analytics pipeline."""

    def __init__(self, repository: AnalyticsRepository):
        """Initialize with repository."""
        self._repo = repository

    def run_quality_checks(
        self,
        run: AnalysisRun,
        stage_name: str = "quality_gate",
    ) -> tuple[bool, list[DataQualityResult]]:
        """Run all quality checks for an analysis run.
        
        Returns:
            Tuple of (passed, results)
        """
        logger.info("Running quality checks for run: %s", run.run_id)
        
        results = []
        
        # 1. PostgreSQL availability
        results.append(self._check_postgresql_availability(run.run_id, stage_name))
        
        # 2. Source timestamps freshness
        results.append(self._check_source_timestamps(run, stage_name))
        
        # 3. Duplicate candles
        results.append(self._check_duplicate_candles(run, stage_name))
        
        # 4. OHLC validation
        results.append(self._check_ohlc_validation(run, stage_name))
        
        # 5. Closed candles intervals
        results.append(self._check_closed_candle_intervals(run, stage_name))
        
        # 6. Post-exit coverage
        results.append(self._check_post_exit_coverage(run, stage_name))
        
        # 7. Lifecycle timestamps
        results.append(self._check_lifecycle_timestamps(run, stage_name))
        
        # 8. Data freshness
        results.append(self._check_data_freshness(run, stage_name))
        
        # Determine if all BLOCKING checks passed
        passed = all(
            result.status == QualityCheckStatus.PASS
            for result in results
            if result.severity == Severity.BLOCKING
        )
        
        # Store results
        for result in results:
            self._repo.create_quality_result(result)
        
        logger.info(
            "Quality checks completed for run %s: passed=%d/%d",
            run.run_id,
            sum(1 for r in results if r.status == QualityCheckStatus.PASS),
            len(results),
        )
        
        return passed, results

    def _check_postgresql_availability(
        self, run_id: UUID, stage_name: str
    ) -> DataQualityResult:
        """Check if PostgreSQL is available."""
        try:
            # Try to execute a simple query
            cursor = self._repo._conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            
            return DataQualityResult(
                run_id=run_id,
                stage_name=stage_name,
                check_name="postgresql_availability",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
                details={"message": "PostgreSQL is available"},
            )
        except Exception as e:
            # Rollback the aborted transaction
            self._repo._conn.rollback()
            return DataQualityResult(
                run_id=run_id,
                stage_name=stage_name,
                check_name="postgresql_availability",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
                actual_value={"error": str(e)},
                details={"message": "PostgreSQL is not available"},
            )

    def _check_source_timestamps(
        self, run: AnalysisRun, stage_name: str
    ) -> DataQualityResult:
        """Check if source timestamps are not later than observation cutoff."""
        try:
            cursor = self._repo._conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM market.candle
                WHERE open_time > %s
                AND ingested_at >= %s
                """,
                (run.observation_cutoff, run.started_at or run.created_at),
            )
            future_candle_count = cursor.fetchone()[0]
            
            if future_candle_count > 0:
                return DataQualityResult(
                    run_id=run.run_id,
                    stage_name=stage_name,
                    check_name="source_timestamps",
                    severity=Severity.BLOCKING,
                    status=QualityCheckStatus.FAIL,
                    actual_value={"future_candle_count": future_candle_count},
                    details={"message": f"Found {future_candle_count} candles with timestamps after observation cutoff"},
                )
            
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="source_timestamps",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
                details={"message": "Source timestamps are within acceptable range"},
            )
        except Exception as e:
            # Rollback the aborted transaction
            self._repo._conn.rollback()
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="source_timestamps",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
                actual_value={"error": str(e)},
                details={"message": "Source timestamps check failed"},
            )

    def _check_duplicate_candles(
        self, run: AnalysisRun, stage_name: str
    ) -> DataQualityResult:
        """Check for duplicate candles."""
        try:
            cursor = self._repo._conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT exchange, market_type, instrument_id, timeframe, open_time,
                           COUNT(*) as cnt
                    FROM market.candle
                    WHERE ingested_at >= %s
                    GROUP BY exchange, market_type, instrument_id, timeframe, open_time
                    HAVING COUNT(*) > 1
                ) duplicates
                """,
                (run.started_at or run.created_at,),
            )
            duplicate_count = cursor.fetchone()[0]
            
            if duplicate_count > 0:
                return DataQualityResult(
                    run_id=run.run_id,
                    stage_name=stage_name,
                    check_name="duplicate_candles",
                    severity=Severity.BLOCKING,
                    status=QualityCheckStatus.FAIL,
                    actual_value={"duplicate_count": duplicate_count},
                    details={"message": f"Found {duplicate_count} duplicate candle groups"},
                )
            
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="duplicate_candles",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
                details={"message": "No duplicate candles found"},
            )
        except Exception as e:
            # Rollback the aborted transaction
            self._repo._conn.rollback()
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="duplicate_candles",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
                actual_value={"error": str(e)},
                details={"message": "Duplicate candles check failed"},
            )

    def _check_ohlc_validation(
        self, run: AnalysisRun, stage_name: str
    ) -> DataQualityResult:
        """Check OHLC validation rules."""
        try:
            cursor = self._repo._conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM market.candle
                WHERE ingested_at >= %s
                AND (
                    high < open OR high < close OR high < low OR
                    low > open OR low > close OR
                    open <= 0 OR high <= 0 OR low <= 0 OR close <= 0 OR
                    volume < 0 OR
                    (turnover IS NOT NULL AND turnover < 0)
                )
                """,
                (run.started_at or run.created_at,),
            )
            invalid_count = cursor.fetchone()[0]
            
            if invalid_count > 0:
                return DataQualityResult(
                    run_id=run.run_id,
                    stage_name=stage_name,
                    check_name="ohlc_validation",
                    severity=Severity.BLOCKING,
                    status=QualityCheckStatus.FAIL,
                    actual_value={"invalid_candle_count": invalid_count},
                    details={"message": f"Found {invalid_count} candles with invalid OHLC"},
                )
            
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="ohlc_validation",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
                details={"message": "All candles have valid OHLC"},
            )
        except Exception as e:
            # Rollback the aborted transaction
            self._repo._conn.rollback()
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="ohlc_validation",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
                actual_value={"error": str(e)},
                details={"message": "OHLC validation check failed"},
            )

    def _check_closed_candle_intervals(
        self, run: AnalysisRun, stage_name: str
    ) -> DataQualityResult:
        """Check that closed candles have expected intervals."""
        try:
            cursor = self._repo._conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM market.candle
                WHERE ingested_at >= %s
                AND is_closed = TRUE
                AND (
                    -- Check that close_time matches expected interval
                    (timeframe = '1' AND close_time != open_time + INTERVAL '1 minute') OR
                    (timeframe = '3' AND close_time != open_time + INTERVAL '3 minutes') OR
                    (timeframe = '5' AND close_time != open_time + INTERVAL '5 minutes') OR
                    (timeframe = '15' AND close_time != open_time + INTERVAL '15 minutes') OR
                    (timeframe = '30' AND close_time != open_time + INTERVAL '30 minutes') OR
                    (timeframe = '60' AND close_time != open_time + INTERVAL '1 hour') OR
                    (timeframe = '120' AND close_time != open_time + INTERVAL '2 hours') OR
                    (timeframe = '240' AND close_time != open_time + INTERVAL '4 hours') OR
                    (timeframe = '360' AND close_time != open_time + INTERVAL '6 hours') OR
                    (timeframe = '720' AND close_time != open_time + INTERVAL '12 hours') OR
                    (timeframe = 'D' AND close_time != open_time + INTERVAL '1 day') OR
                    (timeframe = 'W' AND close_time != open_time + INTERVAL '7 days') OR
                    (timeframe = 'M' AND close_time != open_time + INTERVAL '1 month')
                )
                """,
                (run.started_at or run.created_at,),
            )
            invalid_interval_count = cursor.fetchone()[0]
            
            if invalid_interval_count > 0:
                return DataQualityResult(
                    run_id=run.run_id,
                    stage_name=stage_name,
                    check_name="closed_candle_intervals",
                    severity=Severity.BLOCKING,
                    status=QualityCheckStatus.FAIL,
                    actual_value={"invalid_interval_count": invalid_interval_count},
                    details={"message": f"Found {invalid_interval_count} candles with incorrect intervals"},
                )
            
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="closed_candle_intervals",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
                details={"message": "Closed candle intervals are correct"},
            )
        except Exception as e:
            # Rollback the aborted transaction
            self._repo._conn.rollback()
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="closed_candle_intervals",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
                actual_value={"error": str(e)},
                details={"message": "Closed candle intervals check failed"},
            )

    def _check_post_exit_coverage(
        self, run: AnalysisRun, stage_name: str
    ) -> DataQualityResult:
        """Check post-exit coverage for all trades."""
        try:
            # Get all closed trades that need post-exit coverage
            # Using production schema: symbol, closed_at, entry_timeframe
            cursor = self._repo._conn.cursor()
            cursor.execute(
                """
                SELECT 
                    pt.symbol,
                    pt.closed_at,
                    pt.entry_timeframe,
                    COUNT(*) as trade_count
                FROM dds.paper_trade pt
                WHERE pt.closed_at IS NOT NULL
                AND pt.closed_at >= %s
                AND pt.closed_at < %s
                GROUP BY pt.symbol, pt.closed_at, pt.entry_timeframe
                """,
                (run.analysis_from, run.analysis_to),
            )
            trades = cursor.fetchall()
            
            if not trades:
                return DataQualityResult(
                    run_id=run.run_id,
                    stage_name=stage_name,
                    check_name="post_exit_coverage",
                    severity=Severity.WARNING if run.maturity.value == "PROVISIONAL" else Severity.BLOCKING,
                    status=QualityCheckStatus.PASS,
                    details={"message": "No trades found requiring post-exit coverage"},
                )
            
            # Use post_exit_horizon directly as timedelta
            post_exit_horizon = run.post_exit_horizon if run.post_exit_horizon else timedelta(hours=4)
            
            missing_coverage = []
            for trade in trades:
                symbol, closed_at, entry_timeframe, trade_count = trade
                
                # Normalize production timeframe (e.g. "5m" -> "5")
                try:
                    normalized_timeframe = normalize_timeframe(entry_timeframe)
                except ValueError:
                    logger.warning(
                        "Skipping trade for %s: unrecognizable timeframe %r",
                        symbol, entry_timeframe,
                    )
                    continue
                
                post_exit_end = closed_at + post_exit_horizon
                
                # Check if we have candles covering the post-exit period
                # Note: market.candle uses instrument_id, not symbol
                # We need to look up instrument_id from dds.instrument
                cursor.execute(
                    """
                    SELECT i.instrument_id 
                    FROM dds.instrument i 
                    WHERE i.symbol = %s 
                    LIMIT 1
                    """,
                    (symbol,),
                )
                instrument_row = cursor.fetchone()
                if not instrument_row:
                    logger.warning("Instrument not found for symbol: %s", symbol)
                    continue
                
                instrument_id = instrument_row[0]
                
                # Align range to candle grid boundaries
                aligned_from = align_to_grid(closed_at, normalized_timeframe)
                aligned_to = align_to_grid(post_exit_end, normalized_timeframe)
                
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM market.candle
                    WHERE instrument_id = %s
                    AND timeframe = %s
                    AND open_time >= %s
                    AND open_time < %s
                    AND is_closed = TRUE
                    """,
                    (instrument_id, normalized_timeframe, aligned_from, aligned_to),
                )
                candle_count = cursor.fetchone()[0]
                
                # Calculate expected candles based on aligned range
                expected_candles = self._calculate_expected_candles(
                    normalized_timeframe, aligned_from, aligned_to
                )
                
                if candle_count < expected_candles:
                    missing_coverage.append({
                        "symbol": symbol,
                        "closed_at": closed_at.isoformat(),
                        "entry_timeframe": entry_timeframe,
                        "normalized_timeframe": normalized_timeframe,
                        "expected_candles": expected_candles,
                        "actual_candles": candle_count,
                        "coverage_ratio": candle_count / expected_candles if expected_candles > 0 else 0,
                    })
            
            if missing_coverage:
                severity = Severity.WARNING if run.maturity.value == "PROVISIONAL" else Severity.BLOCKING
                return DataQualityResult(
                    run_id=run.run_id,
                    stage_name=stage_name,
                    check_name="post_exit_coverage",
                    severity=severity,
                    status=QualityCheckStatus.FAIL,
                    affected_entity_count=len(missing_coverage),
                    actual_value={"missing_coverage": missing_coverage},
                    details={"message": f"Found {len(missing_coverage)} trades with incomplete post-exit coverage"},
                )
            
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="post_exit_coverage",
                severity=Severity.WARNING if run.maturity.value == "PROVISIONAL" else Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
                details={"message": "Post-exit coverage is sufficient for all trades"},
            )
        except Exception as e:
            # Rollback the aborted transaction
            self._repo._conn.rollback()
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="post_exit_coverage",
                severity=Severity.WARNING if run.maturity.value == "PROVISIONAL" else Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
                actual_value={"error": str(e)},
                details={"message": "Post-exit coverage check failed"},
            )
    
    def _calculate_expected_candles(self, timeframe: str, start_time: datetime, end_time: datetime) -> int:
        """Calculate expected number of candles for a time range.
        
        Accepts normalized timeframe strings ("5", "60", "D", etc.).
        """
        timeframe_minutes = self._normalized_timeframe_to_minutes(timeframe)
        total_minutes = (end_time - start_time).total_seconds() / 60
        return int(total_minutes / timeframe_minutes)
    
    @staticmethod
    def _normalized_timeframe_to_minutes(timeframe: str) -> int:
        """Convert a normalized timeframe string to minutes.
        
        Only accepts canonical forms: "1", "5", "15", "60", "D", etc.
        For production values like "5m", use normalize_timeframe() first.
        """
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

    def _check_lifecycle_timestamps(
        self, run: AnalysisRun, stage_name: str
    ) -> DataQualityResult:
        """Check lifecycle timestamps."""
        try:
            # Verify that started_at is before finished_at
            if run.started_at and run.finished_at:
                if run.started_at >= run.finished_at:
                    return DataQualityResult(
                        run_id=run.run_id,
                        stage_name=stage_name,
                        check_name="lifecycle_timestamps",
                        severity=Severity.BLOCKING,
                        status=QualityCheckStatus.FAIL,
                        actual_value={
                            "started_at": run.started_at.isoformat(),
                            "finished_at": run.finished_at.isoformat(),
                        },
                        details={"message": "started_at must be before finished_at"},
                    )
            
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="lifecycle_timestamps",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.PASS,
                details={"message": "Lifecycle timestamps are valid"},
            )
        except Exception as e:
            # Rollback the aborted transaction
            self._repo._conn.rollback()
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="lifecycle_timestamps",
                severity=Severity.BLOCKING,
                status=QualityCheckStatus.FAIL,
                actual_value={"error": str(e)},
                details={"message": "Lifecycle timestamps check failed"},
            )

    def _check_data_freshness(
        self, run: AnalysisRun, stage_name: str
    ) -> DataQualityResult:
        """Check data freshness against SLA."""
        try:
            # Get latest candle timestamp
            cursor = self._repo._conn.cursor()
            cursor.execute(
                """
                SELECT MAX(open_time) as latest_candle
                FROM market.candle
                WHERE is_closed = TRUE
                """
            )
            result = cursor.fetchone()
            latest_candle = result[0] if result else None
            
            if not latest_candle:
                return DataQualityResult(
                    run_id=run.run_id,
                    stage_name=stage_name,
                    check_name="data_freshness",
                    severity=Severity.WARNING,
                    status=QualityCheckStatus.SKIPPED,
                    details={"message": "No candles found to check freshness"},
                )
            
            # Calculate freshness: how old is the latest candle?
            now = datetime.now(timezone.utc)
            freshness_minutes = (now - latest_candle).total_seconds() / 60
            
            # Define SLA thresholds (in minutes)
            sla_thresholds = {
                "1": 5,      # 1m candles should be within 5 minutes
                "5": 15,     # 5m candles should be within 15 minutes
                "15": 45,    # 15m candles should be within 45 minutes
                "60": 180,   # 1h candles should be within 3 hours
                "D": 1440,   # Daily candles should be within 24 hours
            }
            
            # Get the most common timeframe
            cursor.execute(
                """
                SELECT timeframe, COUNT(*) as cnt
                FROM market.candle
                WHERE open_time >= NOW() - INTERVAL '24 hours'
                GROUP BY timeframe
                ORDER BY cnt DESC
                LIMIT 1
                """
            )
            result = cursor.fetchone()
            primary_timeframe = result[0] if result else "5"
            
            sla_minutes = sla_thresholds.get(primary_timeframe, 60)
            
            if freshness_minutes > sla_minutes:
                return DataQualityResult(
                    run_id=run.run_id,
                    stage_name=stage_name,
                    check_name="data_freshness",
                    severity=Severity.WARNING,
                    status=QualityCheckStatus.FAIL,
                    actual_value={
                        "latest_candle": latest_candle.isoformat(),
                        "freshness_minutes": freshness_minutes,
                        "sla_minutes": sla_minutes,
                        "primary_timeframe": primary_timeframe,
                    },
                    details={"message": f"Data freshness ({freshness_minutes:.1f} min) exceeds SLA ({sla_minutes} min)"},
                )
            
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="data_freshness",
                severity=Severity.WARNING,
                status=QualityCheckStatus.PASS,
                actual_value={
                    "latest_candle": latest_candle.isoformat(),
                    "freshness_minutes": freshness_minutes,
                    "sla_minutes": sla_minutes,
                },
                details={"message": f"Data freshness ({freshness_minutes:.1f} min) is within SLA ({sla_minutes} min)"},
            )
        except Exception as e:
            # Rollback the aborted transaction
            self._repo._conn.rollback()
            return DataQualityResult(
                run_id=run.run_id,
                stage_name=stage_name,
                check_name="data_freshness",
                severity=Severity.WARNING,
                status=QualityCheckStatus.FAIL,
                actual_value={"error": str(e)},
                details={"message": "Data freshness check failed"},
            )

    def has_blocking_failures(self, results: list[DataQualityResult]) -> bool:
        """Check if any BLOCKING checks failed."""
        return any(
            result.severity == Severity.BLOCKING
            and result.status == QualityCheckStatus.FAIL
            for result in results
        )

    def get_blocking_failures(self, results: list[DataQualityResult]) -> list[DataQualityResult]:
        """Get all BLOCKING failures."""
        return [
            result
            for result in results
            if result.severity == Severity.BLOCKING
            and result.status == QualityCheckStatus.FAIL
        ]

    def get_summary(self, results: list[DataQualityResult]) -> dict[str, Any]:
        """Get summary of quality check results."""
        total = len(results)
        passed = sum(1 for r in results if r.status == QualityCheckStatus.PASS)
        failed = sum(1 for r in results if r.status == QualityCheckStatus.FAIL)
        skipped = sum(1 for r in results if r.status == QualityCheckStatus.SKIPPED)
        
        blocking_failed = sum(
            1
            for r in results
            if r.severity == Severity.BLOCKING and r.status == QualityCheckStatus.FAIL
        )
        
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "blocking_failed": blocking_failed,
            "passed_all_blocking": blocking_failed == 0,
        }