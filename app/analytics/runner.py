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
                
                # Stage 2: Python quality gate
                self._execute_stage(run, "quality_gate", self._stage_quality_gate)
                
                # Stage 3: Canonical build (REAL SQL functions)
                self._execute_stage(run, "canonical_build", self._stage_canonical_build)
                
                # Stage 4: SQL quality gate (fail-closed)
                self._execute_stage(run, "sql_quality_gate", self._stage_sql_quality_gate)
                
                # Stage 5: Dataset publication
                self._execute_stage(run, "dataset_publication", self._stage_dataset_publication)
                
                # Stage 6: Retention cleanup
                self._execute_stage(run, "retention", self._stage_retention)
                
                # Stage 7: Stage 3 agent orchestration (isolated from canonical)
                # Stage 3 failure must NOT fail the analysis_run —
                # the canonical dataset publication is already READY.
                try:
                    self._stage_agent_orchestration(run)
                except Exception as e:
                    logger.error("Stage 3 agent orchestration failed: %s", e)
                    # Do NOT mark analysis_run as FAILED — canonical data is still valid
                
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
            business_date = self._get_previous_business_date()
        
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
        
        # Safety guard: FINAL must not execute before analysis_to
        now_utc = datetime.now(timezone.utc)
        if now_utc < provisional_run.analysis_to:
            raise ValueError(
                f"FINAL_NOT_READY: analysis_to={provisional_run.analysis_to.isoformat()}, "
                f"current_time={now_utc.isoformat()}. "
                f"Cannot finalize before the analysis window is complete."
            )
        
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
                
                # Stage 3: Canonical rebuild (REAL SQL functions)
                self._execute_stage(run, "canonical_build", self._stage_canonical_build)
                
                # Stage 4: SQL quality gate (fail-closed)
                self._execute_stage(run, "sql_quality_gate", self._stage_sql_quality_gate)
                
                # Stage 5: Dataset publication (FINAL)
                self._execute_stage(run, "dataset_publication", self._stage_dataset_publication)
                
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

    def _get_previous_business_date(self) -> date:
        """Get previous business date in Europe/Sofia timezone.
        
        Used by finalize command: the previous day's PROVISIONAL run
        should be finalized.
        """
        return self._get_business_date() - timedelta(days=1)

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
            logger.exception("Stage %s failed: %s", stage_name, e)
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
        
        from app.analytics.candle_sync import normalize_timeframe, align_to_grid, CandleRange
        from app.analytics.candle_ranges import CandleRangePlanner
        
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
            
            # Build required range with grid-aligned boundaries
            trade_end = closed_at + run.post_exit_horizon
            aligned_from = align_to_grid(entered_at, timeframe)
            aligned_to = align_to_grid(trade_end, timeframe)
            
            range_ = CandleRange(
                instrument_id=instrument_id,
                timeframe=timeframe,
                from_time=aligned_from,
                to_time=aligned_to,
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
                observation_cutoff=run.observation_cutoff,
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

    def _stage_dataset_publication(
        self, run: AnalysisRun, stage_run: AnalysisStageRun
    ) -> dict[str, Any]:
        """Stage: Dataset publication.

        Builds the publication manifest, computes dataset_version,
        creates analytics.dataset_publication with status=BUILDING,
        performs final validation, then marks publication READY.

        The runner's job ends at publication READY — the external
        Stage 3 orchestrator picks it up from there.
        """
        from app.analytics.agents.dataset_version import (
            compute_dataset_version,
            build_publication_manifest,
        )

        logger.info("Running dataset publication for run %s", run.run_id)

        # Derive quality status from the sql_quality_gate stage result.
        # Look up the previous stage run to get quality_status.
        quality_status = "PASS"  # default
        try:
            prev_stage = self._repo.get_stage_run_by_name(
                run.run_id, "sql_quality_gate"
            )
            if prev_stage and prev_stage.result_json:
                quality_status = prev_stage.result_json.get("quality_status", "PASS")
        except Exception:
            logger.warning("Could not read sql_quality_gate result, defaulting to PASS")

        # Build publication manifest
        manifest = build_publication_manifest(
            analysis_run_id=str(run.run_id),
            analysis_window_from=run.analysis_from,
            analysis_window_to=run.analysis_to,
            observation_cutoff=run.observation_cutoff,
            maturity=run.maturity.value,
        )

        # Compute deterministic dataset_version
        dataset_version = compute_dataset_version(**manifest)

        # Create publication with status=BUILDING
        publication_id = self._repo.create_dataset_publication(
            analysis_run_id=run.run_id,
            dataset_version=dataset_version,
            maturity=run.maturity.value,
            quality_status=quality_status,
            analysis_window_from=run.analysis_from,
            analysis_window_to=run.analysis_to,
            observation_cutoff=run.observation_cutoff,
            canonical_build_json={
                "trade_fact_built": True,
                "setup_fact_built": True,
                "events_built": True,
            },
            quality_summary_json={
                "quality_status": quality_status,
            },
        )

        # Final validation: verify canonical objects exist
        cursor = self._repo._conn.cursor()
        cursor.execute(
            "SELECT analytics.count_trade_facts(%s)", (str(run.run_id),)
        )
        trade_count = cursor.fetchone()[0]

        cursor.execute(
            "SELECT analytics.count_setup_facts(%s)", (str(run.run_id),)
        )
        setup_count = cursor.fetchone()[0]

        cursor.execute(
            "SELECT analytics.count_trade_events(%s)", (str(run.run_id),)
        )
        event_count = cursor.fetchone()[0]

        # Mark publication READY
        self._repo.update_dataset_publication_status(
            publication_id,
            status="READY",
            canonical_build_json={
                "trade_fact_built": True,
                "setup_fact_built": True,
                "events_built": True,
                "trade_fact_rows": trade_count,
                "setup_fact_rows": setup_count,
                "trade_event_rows": event_count,
            },
            quality_summary_json={
                "quality_status": quality_status,
            },
        )

        result = {
            "output_rows": trade_count + setup_count + event_count,
            "publication_id": str(publication_id),
            "dataset_version": dataset_version,
            "quality_status": quality_status,
            "trade_fact_rows": trade_count,
            "setup_fact_rows": setup_count,
            "trade_event_rows": event_count,
            "message": (
                f"Dataset publication READY: version={dataset_version[:12]}..., "
                f"quality={quality_status}, "
                f"trade_fact={trade_count}, setup_fact={setup_count}"
            ),
        }

        logger.info(
            "Dataset publication %s READY (version=%s, quality=%s)",
            publication_id, dataset_version[:12], quality_status,
        )
        return result

    def _stage_agent_orchestration(self, run: AnalysisRun) -> dict[str, Any]:
        """Stage 7: Stage 3 agent orchestration (isolated from canonical).

        Invokes the AgentOrchestrator to run specialist agents against
        the published dataset.  This stage is wrapped in a try/except
        in the caller — failure here must NOT mark the analysis_run as
        FAILED because the canonical dataset publication is already READY.

        The orchestrator is constructed with the analytics repository and
        specialist registry.  All LLM/provider errors are caught and
        logged, not propagated.
        """
        import asyncio
        from app.analytics.agents.orchestrator import AgentOrchestrator
        from app.analytics.agents.executor import AgentExecutor
        from app.analytics.agents.registry import SPECIALIST_REGISTRY

        logger.info("Running Stage 3 agent orchestration for run %s", run.run_id)

        # Build orchestrator dependencies
        repo = self._repo
        specialist_registry = SPECIALIST_REGISTRY

        orchestrator = AgentOrchestrator(
            repository=repo,
            executor=None,  # Executor is created per-agent inside orchestrator
            data_repo=repo,
            specialist_registry=specialist_registry,
        )

        # Determine maturity string for orchestrator
        maturity = run.maturity.value if run.maturity else "PROVISIONAL"

        # Run orchestrator (async) — isolated from canonical pipeline
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already in an async context — use a new thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        orchestrator.run(
                            analysis_run_id=run.run_id,
                            maturity=maturity,
                        ),
                    ).result(timeout=300)
            else:
                result = loop.run_until_complete(
                    orchestrator.run(
                        analysis_run_id=run.run_id,
                        maturity=maturity,
                    )
                )
        except Exception as e:
            logger.error("Stage 3 agent orchestration failed: %s", e)
            raise

        logger.info(
            "Stage 3 agent orchestration completed for run %s: status=%s",
            run.run_id, result.get("status"),
        )
        return result

    def _stage_retention(
        self, run: AnalysisRun, stage_run: AnalysisStageRun
    ) -> dict[str, Any]:
        """Stage 6: Retention cleanup."""
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
        
        from app.analytics.candle_sync import normalize_timeframe, align_to_grid, CandleRange
        from app.analytics.candle_ranges import CandleRangePlanner
        
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
        ranges_by_key: dict[tuple[int, str], list[CandleRange]] = {}
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
            
            # Align to candle grid boundaries
            aligned_from = align_to_grid(closed_at, timeframe)
            aligned_to = align_to_grid(effective_end, timeframe)
            
            range_ = CandleRange(
                instrument_id=instrument_id,
                timeframe=timeframe,
                from_time=aligned_from,
                to_time=aligned_to,
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
                observation_cutoff=run.observation_cutoff,
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

    def _stage_canonical_build(
        self, run: AnalysisRun, stage_run: AnalysisStageRun
    ) -> dict[str, Any]:
        """Canonical build stage: REAL SQL function execution.

        Calls analytics.build_events, analytics.build_trade_fact,
        analytics.build_setup_fact, and downstream metric functions
        to populate canonical analytics tables for this run.

        trade_fact_built / setup_fact_built are TRUE only when the SQL
        function actually executed — no more optimistic True.
        """
        logger.info("Running canonical build for run %s", run.run_id)

        cursor = self._repo._conn.cursor()
        results: dict[str, Any] = {}

        # 1. Build events (migration 016 wrapper)
        cursor.execute("SELECT analytics.build_events(%s)", (str(run.run_id),))
        results["events_count"] = cursor.fetchone()[0]
        results["events_executed"] = True
        logger.info("  Events: %d rows", results["events_count"])

        # 2. Build trade_fact (migration 017 wrapper)
        cursor.execute("SELECT analytics.build_trade_fact(%s)", (str(run.run_id),))
        results["trade_fact_count"] = cursor.fetchone()[0]
        results["trade_fact_built"] = True  # function executed successfully
        logger.info("  Trade fact: %d rows", results["trade_fact_count"])

        # 3. Build setup_fact (migration 018 wrapper)
        cursor.execute("SELECT analytics.build_setup_fact(%s)", (str(run.run_id),))
        results["setup_fact_count"] = cursor.fetchone()[0]
        results["setup_fact_built"] = True  # function executed successfully
        logger.info("  Setup fact: %d rows", results["setup_fact_count"])

        # 4. Build horizon metrics (migration 020)
        cursor.execute("SELECT analytics.build_horizon_metrics(%s)", (str(run.run_id),))
        results["horizon_metrics_count"] = cursor.fetchone()[0]
        results["horizon_metrics_built"] = True
        logger.info("  Horizon metrics: %d rows", results["horizon_metrics_count"])

        # 5. Build replay metrics (migration 021)
        cursor.execute("SELECT analytics.build_all_replay_metrics(%s)", (str(run.run_id),))
        results["replay_metrics"] = cursor.fetchone()[0]
        results["replay_metrics_built"] = True
        logger.info("  Replay metrics: %s", results["replay_metrics"])

        # 6. Build metric snapshots (migration 022)
        cursor.execute("SELECT analytics.build_metric_snapshots(%s)", (str(run.run_id),))
        results["metric_snapshots"] = cursor.fetchone()[0]
        results["metric_snapshots_built"] = True
        logger.info("  Metric snapshots: %s", results["metric_snapshots"])

        # 7. Verify canonical objects exist (count helpers)
        cursor.execute("SELECT analytics.count_trade_facts(%s)", (str(run.run_id),))
        results["trade_fact_rows"] = cursor.fetchone()[0]

        cursor.execute("SELECT analytics.count_setup_facts(%s)", (str(run.run_id),))
        results["setup_fact_rows"] = cursor.fetchone()[0]

        cursor.execute("SELECT analytics.count_trade_events(%s)", (str(run.run_id),))
        results["trade_event_rows"] = cursor.fetchone()[0]

        self._repo._conn.commit()

        # After commit: "built" flags are TRUE because functions executed.
        # 0 rows is OK — it means "executed, 0 legitimate trades".
        results["output_rows"] = sum([
            results["events_count"],
            results["trade_fact_count"],
            results["setup_fact_count"],
            results["horizon_metrics_count"],
        ])

        results["message"] = (
            f"Canonical build complete: events={results['events_count']}, "
            f"trade_fact={results['trade_fact_rows']}, "
            f"setup_fact={results['setup_fact_rows']}"
        )

        return results

    def _stage_sql_quality_gate(
        self, run: AnalysisRun, stage_run: AnalysisStageRun
    ) -> dict[str, Any]:
        """SQL quality gate: fail-closed.

        Uses analytics.quality_gate(run_id) which checks:
        - duplicate trade grain, orphan setup/trade, fill events,
          exit events, DCA consistency, MFE/MAE sign, config hash,
          candle coverage, PIT violations.

        FAIL-CLOSED: if the function doesn't exist or returns no rows,
        the gate is FAILED, not silently skipped.
        """
        logger.info("Running SQL quality gate for run %s", run.run_id)

        cursor = self._repo._conn.cursor()
        try:
            cursor.execute("SELECT * FROM analytics.quality_gate(%s)", (str(run.run_id),))
            row = cursor.fetchone()
        except Exception as e:
            self._repo._conn.rollback()
            # Missing function = FAILED, not PASS
            raise RuntimeError(
                f"SQL quality gate function not available: {e}"
            ) from e

        if row is None:
            raise RuntimeError(
                "SQL quality gate returned no results — "
                "quality_gate(%s) returned NULL/empty".format(str(run.run_id))
            )

        passed, blocking_count, degraded_count, total_checks = row
        self._repo._conn.commit()

        # Determine quality status
        if blocking_count > 0:
            quality_status = "FAIL"
        elif degraded_count > 0:
            quality_status = "DEGRADED"
        else:
            quality_status = "PASS"

        if not passed:
            raise RuntimeError(
                f"SQL quality gate FAILED: {blocking_count} BLOCKING "
                f"out of {total_checks} checks"
            )

        result = {
            "passed": bool(passed),
            "quality_status": quality_status,
            "blocking_count": int(blocking_count),
            "degraded_count": int(degraded_count),
            "total_checks": int(total_checks),
            "message": (
                f"SQL quality gate: {quality_status} "
                f"({blocking_count} blocking, {degraded_count} degraded, "
                f"{total_checks} total)"
            ),
        }

        logger.info(
            "SQL quality gate: quality_status=%s, blocking=%d, degraded=%d, total=%d",
            quality_status, blocking_count, degraded_count, total_checks,
        )
        return result