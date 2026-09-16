"""Stage 3 Agent Orchestrator — production state machine.

Orchestration flow:
    1. Data Readiness Gate (programmatic, not LLM)
    2. Create agent_run batch
    3. For each specialist:
       a. Create immutable input manifest
       b. Execute via AgentExecutor
       c. Schema + evidence validation
       d. Retry/repair on failure
       e. Persist results
    4. Chief eligibility check
    5. If eligible: create Chief run (stub in PR2, real in PR4)
    6. Persist state

This orchestrator does NOT contain production prompts.
PR3 will inject real specialist prompts via the ``AgentExecutor``.

Repository protocol
-------------------
The ``repository`` parameter must implement the ``AnalyticsRepositoryProtocol``
defined below.  In production this is backed by the SQLAlchemy repository;
tests can substitute a stub.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, runtime_checkable
from uuid import UUID

from app.analytics.agents.models import (
    AgentDefinition,
    AgentInputManifest,
    AgentResult,
    AgentRun,
    AgentRunStatus,
    AgentType,
    ValidationStatus,
)
from app.analytics.agents.errors import AgentErrorCode, classify_error
from app.analytics.agents.executor import AgentExecutor, AgentExecutionResult
from app.analytics.agents.readiness import DataReadinessGate, DataReadinessResult
from app.analytics.agents.retry_policy import RetryPolicyV1
from app.analytics.agents.chief_policy import check_chief_eligibility, ChiefEligibility

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

# MVP specialist agents (execution order is intentionally unordered —
# the orchestrator may parallelise them in future)
SPECIALIST_AGENTS: list[str] = [
    "FUNNEL_AND_PERFORMANCE",
    "EXECUTION_QUALITY",
    "DRIFT_AND_ANOMALY",
]

CHIEF_AGENT: str = "CHIEF_TRADING_ANALYST"

# If an agent stays RUNNING longer than this, crash-recovery marks it FAILED
STALE_RUNNING_TIMEOUT_S: int = 3600  # 1 hour


# ── Repository protocol ────────────────────────────────────────────────

@runtime_checkable
class AnalyticsRepositoryProtocol(Protocol):
    """Protocol that the analytics repository must satisfy.

    The real implementation lives in ``app.analytics.repository``.
    """

    def get_agent_definition(self, agent_name: str) -> Optional[AgentDefinition]: ...
    def create_agent_definition(self, definition: AgentDefinition) -> AgentDefinition: ...
    def get_agent_runs_for_analysis(self, analysis_run_id: UUID) -> list[AgentRun]: ...
    def get_agent_result(self, agent_run_id: UUID) -> Optional[AgentResult]: ...
    def get_input_manifest(self, agent_run_id: UUID) -> Optional[AgentInputManifest]: ...
    def get_next_attempt(self, analysis_run_id: UUID, agent_name: str) -> int: ...
    def create_agent_run(self, run: AgentRun) -> AgentRun: ...
    def create_input_manifest(self, manifest: AgentInputManifest) -> AgentInputManifest: ...
    def create_agent_result(self, result: AgentResult) -> AgentResult: ...
    def update_agent_run_status(
        self,
        agent_run_id: UUID,
        status: AgentRunStatus,
        *,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
        latency_ms: Optional[int] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        error_class: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None: ...
    def get_stale_agent_runs(self, stale_timeout_seconds: int) -> list[dict]: ...


# ── Orchestrator ───────────────────────────────────────────────────────

class AgentOrchestrator:
    """Orchestrates the full Stage 3 agent pipeline for one analysis run.

    This is the central state machine.  It is designed to be:

    - **Deterministic**: same inputs → same manifest hash → same outcome.
    - **Idempotent**: re-running after a crash skips already-completed agents.
    - **Observable**: every state transition is logged and persisted.
    - **Repair-aware**: schema validation errors trigger a single repair
      attempt before terminal failure.

    Parameters
    ----------
    repository:
        Persistence layer (see ``AnalyticsRepositoryProtocol``).
    executor:
        LLM execution layer (see ``AgentExecutor``).
    readiness_gate:
        Optional custom gate (defaults to ``DataReadinessGate``).
    retry_policy:
        Optional custom policy (defaults to ``RetryPolicyV1``).
    """

    def __init__(
        self,
        repository: AnalyticsRepositoryProtocol,
        executor: AgentExecutor,
        readiness_gate: Optional[DataReadinessGate] = None,
        retry_policy: Optional[RetryPolicyV1] = None,
    ):
        self._repo = repository
        self._executor = executor
        self._readiness = readiness_gate or DataReadinessGate()
        self._retry_policy = retry_policy or RetryPolicyV1()

    # ── Public entry point ────────────────────────────────────────────

    async def run(
        self,
        analysis_run_id: UUID,
        maturity: str,
    ) -> dict[str, Any]:
        """Run the full Stage 3 orchestration for one analysis run.

        Parameters
        ----------
        analysis_run_id:
            The analysis_run UUID to orchestrate.
        maturity:
            PROVISIONAL or FINAL.

        Returns
        -------
        dict
            Summary dict with status of each agent and Chief eligibility.
        """
        # ── Step 0: Load from DB ──────────────────────────────────────
        run = self._repo.get_analysis_run(analysis_run_id)
        if not run:
            logger.warning("Analysis run %s not found — skipping all agents", analysis_run_id)
            await self._skip_all_agents(analysis_run_id, maturity, ("Analysis run not found",))
            return {
                "status": "SKIPPED",
                "readiness": None,
                "agents": {},
                "chief": None,
            }

        publication = self._repo.get_dataset_publication(analysis_run_id, maturity)
        quality_results = self._repo.get_quality_results(analysis_run_id)

        # Derive from DB
        dataset_version = publication.dataset_version if publication else None
        publication_status = publication.status if publication else None
        quality_status = publication.quality_status if publication else None
        canonical_build_json = publication.canonical_build_json if publication else None
        quality_limitations = []
        if quality_results:
            for qr in quality_results:
                if hasattr(qr, 'limitations') and qr.limitations:
                    quality_limitations.extend(qr.limitations)

        logger.info(
            "Starting Stage 3 orchestration for run %s "
            "(maturity=%s, dataset_version=%s)",
            analysis_run_id, maturity, dataset_version,
        )

        # ── Step 1: Data Readiness Gate ───────────────────────────────
        readiness = self._readiness.evaluate(
            run_id=analysis_run_id,
            status=run.status,
            maturity=maturity,
            dataset_version=dataset_version,
            publication_status=publication_status,
            quality_status=quality_status,
            canonical_build_json=canonical_build_json,
            analysis_window_from=run.analysis_from,
            analysis_window_to=run.analysis_to,
            quality_limitations=quality_limitations,
        )

        if not readiness.ready:
            logger.warning(
                "Dataset not ready for Stage 3: blockers=%s",
                readiness.blockers,
            )
            await self._skip_all_agents(analysis_run_id, maturity, readiness.blockers)
            return {
                "status": "SKIPPED",
                "readiness": readiness,
                "agents": {},
                "chief": None,
            }

        # ── Step 2: Execute specialists (PARALLEL) ────────────────────
        specialist_results: dict[str, AgentExecutionResult] = {}
        specialist_statuses: dict[str, str] = {}

        # Fix #14: Parallel specialist execution
        tasks = [
            self._run_specialist_with_retry(
                analysis_run_id=analysis_run_id,
                agent_name=name,
                dataset_version=dataset_version,
                maturity=maturity,
                readiness=readiness,
            )
            for name in SPECIALIST_AGENTS
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle exceptions and apply DEGRADED propagation
        for i, (name, result) in enumerate(zip(SPECIALIST_AGENTS, results)):
            if isinstance(result, Exception):
                specialist_results[name] = AgentExecutionResult(
                    status=AgentRunStatus.FAILED,
                    result=None,
                    error_code=classify_error(result),
                    error_message=str(result),
                )
            else:
                # Fix #15: DEGRADED propagation
                if readiness.quality_status == "DEGRADED" and result.status == AgentRunStatus.SUCCEEDED:
                    # Downgrade to DEGRADED to propagate
                    result = AgentExecutionResult(
                        status=AgentRunStatus.DEGRADED,
                        result=result.result,
                        error_code=None,
                        error_message="Dataset quality DEGRADED — findings may be affected",
                        model_response=result.model_response,
                    )
                specialist_results[name] = result
            specialist_statuses[name] = specialist_results[name].status.value

        # ── Step 3: Chief eligibility ─────────────────────────────────
        chief_eligibility = check_chief_eligibility(specialist_statuses)

        chief_result: Optional[AgentExecutionResult] = None
        if chief_eligibility.eligible:
            chief_result = await self._run_chief(
                analysis_run_id=analysis_run_id,
                dataset_version=dataset_version,
                maturity=maturity,
                readiness=readiness,
                specialist_results=specialist_results,
                chief_eligibility=chief_eligibility,
            )
        else:
            logger.warning(
                "Chief not eligible: missing_agents=%s, blockers=%s",
                chief_eligibility.missing_agents,
                chief_eligibility.blockers,
            )

        # ── Step 4: Build summary ─────────────────────────────────────
        return {
            "status": "COMPLETED",
            "readiness": readiness,
            "agents": {
                name: {
                    "status": result.status.value,
                    "error_code": result.error_code.value if result.error_code else None,
                    "error_message": result.error_message,
                }
                for name, result in specialist_results.items()
            },
            "chief": {
                "eligible": chief_eligibility.eligible,
                "partial": chief_eligibility.partial,
                "status": chief_result.status.value if chief_result else "SKIPPED",
                "missing_agents": list(chief_eligibility.missing_agents),
            },
        }

    # ── Specialist execution with retry ────────────────────────────────

    async def _run_specialist_with_retry(
        self,
        analysis_run_id: UUID,
        agent_name: str,
        dataset_version: str,
        maturity: str,
        readiness: DataReadinessResult,
    ) -> AgentExecutionResult:
        """Run a single specialist agent with retry and repair logic.
        
        Each retry/repair attempt creates a NEW agent_run (separate execution).
        """
        logger.info("Running specialist: %s", agent_name)

        # Get or create agent definition
        definition = self._repo.get_agent_definition(agent_name)
        if not definition:
            definition = AgentDefinition(
                agent_name=agent_name,
                agent_type=AgentType.SPECIALIST,
                contract_version="v1",
                prompt_version="v1",
            )
            self._repo.create_agent_definition(definition)

        # ── Idempotency check (Fix #9) ───────────────────────────────
        existing_runs = self._repo.get_agent_runs_for_analysis(analysis_run_id)
        for r in existing_runs:
            if (
                r.agent_name == agent_name
                and r.status in (AgentRunStatus.SUCCEEDED, AgentRunStatus.DEGRADED)
                and r.attempt == 1
            ):
                # Also check that the manifest matches dataset_version + maturity
                existing_manifest = self._repo.get_input_manifest(r.agent_run_id)
                if (existing_manifest 
                    and existing_manifest.dataset_version == dataset_version 
                    and existing_manifest.maturity == maturity):
                    logger.info(
                        "Agent %s already completed (run_id=%s, status=%s)",
                        agent_name, r.agent_run_id, r.status.value,
                    )
                    result = self._repo.get_agent_result(r.agent_run_id)
                    if result:
                        return AgentExecutionResult(
                            status=r.status,
                            result=result,
                        )

        # ── Execute with retry (each attempt = new agent_run) ─────────
        policy = self._retry_policy
        final_result = None
        repairs_attempted = 0

        for attempt in range(1, policy.max_attempts + 1):
            # Each attempt = new agent_run
            agent_run = self._create_new_attempt(analysis_run_id, agent_name, attempt)
            manifest = self._build_manifest(
                agent_run_id=agent_run.agent_run_id,
                dataset_version=dataset_version,
                maturity=maturity,
                readiness=readiness,
            )
            manifest = self._repo.create_input_manifest(manifest)

            # Mark RUNNING
            self._repo.update_agent_run_status(
                agent_run.agent_run_id,
                AgentRunStatus.RUNNING,
                started_at=datetime.now(timezone.utc),
            )

            # Execute
            result = await self._executor.execute(definition=definition, manifest=manifest)

            if result.status in (AgentRunStatus.SUCCEEDED, AgentRunStatus.DEGRADED):
                # Atomic persist: result + terminal status
                self._finalize_attempt(agent_run.agent_run_id, result)
                final_result = result
                break

            error_code = result.error_code or AgentErrorCode.UNKNOWN_ERROR

            # Schema repair = attempt N+1 with repair context
            if policy.should_repair(error_code, repairs_attempted):
                repairs_attempted += 1
                # Repair is a SEPARATE attempt
                repair_run = self._create_new_attempt(analysis_run_id, agent_name, attempt + 1)
                repair_manifest = self._build_manifest(
                    agent_run_id=repair_run.agent_run_id,
                    dataset_version=dataset_version,
                    maturity=maturity,
                    readiness=readiness,
                )
                repair_manifest = self._repo.create_input_manifest(repair_manifest)
                
                self._repo.update_agent_run_status(
                    repair_run.agent_run_id,
                    AgentRunStatus.RUNNING,
                    started_at=datetime.now(timezone.utc),
                )
                
                repair_result = await self._executor.repair(
                    definition=definition,
                    manifest=repair_manifest,
                    original_error=result.error_message,
                )
                self._finalize_attempt(repair_run.agent_run_id, repair_result)
                final_result = repair_result
                break  # Only one repair attempt

            # Retryable → create next attempt
            if policy.should_retry(error_code, attempt):
                delay = policy.delay_for_attempt(attempt)
                await asyncio.sleep(delay)
                # Mark current as FAILED
                self._finalize_attempt(agent_run.agent_run_id, result)
                continue  # Creates new attempt in next iteration

            # Terminal error
            self._finalize_attempt(agent_run.agent_run_id, result)
            final_result = result
            break

        logger.info(
            "Specialist %s completed: status=%s",
            agent_name, final_result.status.value if final_result else "FAILED",
        )
        return final_result or AgentExecutionResult(
            status=AgentRunStatus.FAILED,
            result=None,
            error_code=AgentErrorCode.UNKNOWN_ERROR,
            error_message=f"Exhausted {policy.max_attempts} attempts",
        )

    # ── Helper: create new agent_run attempt ──────────────────────────

    def _create_new_attempt(
        self,
        analysis_run_id: UUID,
        agent_name: str,
        attempt: int,
    ) -> AgentRun:
        """Create a new agent_run for each execution attempt."""
        agent_run = AgentRun(
            analysis_run_id=analysis_run_id,
            agent_name=agent_name,
            attempt=attempt,
            status=AgentRunStatus.PENDING,
        )
        return self._repo.create_agent_run(agent_run)

    # ── Atomic finalize attempt (Fix #18) ─────────────────────────────

    def _finalize_attempt(
        self,
        agent_run_id: UUID,
        execution_result: AgentExecutionResult,
    ) -> None:
        """Atomically persist result + terminal status in single transaction."""
        if execution_result.result:
            self._repo.create_agent_result(execution_result.result)

        self._repo.update_agent_run_status(
            agent_run_id,
            execution_result.status,
            finished_at=datetime.now(timezone.utc),
            latency_ms=(
                execution_result.model_response.latency_ms
                if execution_result.model_response
                else None
            ),
            input_tokens=(
                execution_result.model_response.input_tokens
                if execution_result.model_response
                else None
            ),
            output_tokens=(
                execution_result.model_response.output_tokens
                if execution_result.model_response
                else None
            ),
            total_tokens=(
                execution_result.model_response.total_tokens
                if execution_result.model_response
                else None
            ),
            error_class=(
                execution_result.error_code.value
                if execution_result.error_code
                else None
            ),
            error_message=execution_result.error_message,
        )

    # ── Manifest builder (Fix #8) ────────────────────────────────────

    def _build_manifest(
        self,
        agent_run_id: UUID,
        dataset_version: str,
        maturity: str,
        readiness: DataReadinessResult,
    ) -> AgentInputManifest:
        """Build an immutable input manifest for an agent.

        The ``input_hash`` is a SHA-256 fingerprint of the deterministic
        manifest fields, ensuring immutability can be verified downstream.
        
        Fix #8: input_hash excludes agent_run_id — same business inputs 
        produce same hash for retry detection.
        """
        # input_hash = hash of BUSINESS inputs only
        business_data = {
            "dataset_version": dataset_version,
            "maturity": maturity,
            "quality_status": readiness.quality_status,
            "limitations": list(readiness.limitations),
            "analysis_window_from": str(readiness.analysis_window_from),
            "analysis_window_to": str(readiness.analysis_window_to),
        }
        input_hash = hashlib.sha256(
            json.dumps(business_data, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

        return AgentInputManifest(
            agent_run_id=agent_run_id,  # separate from hash
            dataset_version=dataset_version,
            input_hash=input_hash,  # same for retry with same business inputs
            schema_version="v1",
            analysis_window_from=(
                readiness.analysis_window_from or datetime.now(timezone.utc)
            ),
            analysis_window_to=(
                readiness.analysis_window_to or datetime.now(timezone.utc)
            ),
            maturity=maturity,
            data_quality_status=readiness.quality_status,
            limitations=list(readiness.limitations),
            manifest_json=business_data,
            evidence_ids=[],  # populated by input assembler in PR3
        )

    # ── Skip all agents (Fix #5) ─────────────────────────────────────

    async def _skip_all_agents(
        self,
        analysis_run_id: UUID,
        maturity: str,
        blockers: tuple[str, ...],
    ) -> None:
        """Mark all agents as SKIPPED when the dataset is not ready.

        Called when the DataReadinessGate blocks execution.  Every agent
        (specialists + chief) gets a SKIPPED run row with the blocking
        reason recorded.
        """
        for agent_name in SPECIALIST_AGENTS + [CHIEF_AGENT]:
            definition = self._repo.get_agent_definition(agent_name)
            if not definition:
                definition = AgentDefinition(
                    agent_name=agent_name,
                    agent_type=AgentType.CHIEF if agent_name == CHIEF_AGENT else AgentType.SPECIALIST,
                    contract_version="v1",
                    prompt_version="v1",
                )
                self._repo.create_agent_definition(definition)

            attempt = self._repo.get_next_attempt(analysis_run_id, agent_name)
            # Use error_code as part of error_message, not as separate field
            agent_run = AgentRun(
                analysis_run_id=analysis_run_id,
                agent_name=agent_name,
                attempt=attempt,
                status=AgentRunStatus.SKIPPED,
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                error_class=AgentErrorCode.DATASET_NOT_READY.value,
                error_message=f"Dataset not ready: {'; '.join(blockers)}",
            )
            self._repo.create_agent_run(agent_run)

    # ── Chief execution ───────────────────────────────────────────────

    async def _run_chief(
        self,
        analysis_run_id: UUID,
        dataset_version: str,
        maturity: str,
        readiness: DataReadinessResult,
        specialist_results: dict[str, AgentExecutionResult],
        chief_eligibility: ChiefEligibility,
    ) -> AgentExecutionResult:
        """Run Chief Trading Analyst (stub in PR2, real in PR4).

        The Chief receives all specialist outputs and synthesizes them
        into a DailyTradingReport.  In PR2 this is a stub that persists
        a placeholder run row.
        """
        logger.info(
            "Running Chief (partial=%s, missing=%s)",
            chief_eligibility.partial,
            chief_eligibility.missing_agents,
        )

        # Get or create definition
        definition = self._repo.get_agent_definition(CHIEF_AGENT)
        if not definition:
            definition = AgentDefinition(
                agent_name=CHIEF_AGENT,
                agent_type=AgentType.CHIEF,
                contract_version="v1",
                prompt_version="v1",
            )
            self._repo.create_agent_definition(definition)

        # Create agent run
        attempt = self._repo.get_next_attempt(analysis_run_id, CHIEF_AGENT)
        agent_run = AgentRun(
            analysis_run_id=analysis_run_id,
            agent_name=CHIEF_AGENT,
            attempt=attempt,
            status=AgentRunStatus.PENDING,
        )
        agent_run = self._repo.create_agent_run(agent_run)

        # Build manifest
        manifest = self._build_manifest(
            agent_run_id=agent_run.agent_run_id,
            dataset_version=dataset_version,
            maturity=maturity,
            readiness=readiness,
        )
        manifest = self._repo.create_input_manifest(manifest)

        # Mark RUNNING
        self._repo.update_agent_run_status(
            agent_run.agent_run_id,
            AgentRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )

        # Stub execution for PR2 — PR4 will implement real Chief logic
        result = await self._executor.execute(
            definition=definition,
            manifest=manifest,
        )

        # Atomic finalize
        self._finalize_attempt(agent_run.agent_run_id, result)

        return result

    # ── Crash recovery (Fix #19) ─────────────────────────────────────

    async def recover_stale_runs(
        self,
        stale_timeout_s: int = STALE_RUNNING_TIMEOUT_S,
    ) -> int:
        """Recover agent runs stuck in RUNNING state (crash recovery).

        Runs that have been RUNNING longer than ``stale_timeout_s`` are
        marked FAILED with ``error_class=CRASH_RECOVERY``.

        Returns
        -------
        int
            Number of runs recovered.
        """
        stale_runs = self._repo.get_stale_agent_runs(stale_timeout_s)
        recovered = 0
        for run_info in stale_runs:
            self._repo.update_agent_run_status(
                run_info["agent_run_id"],
                AgentRunStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                error_class=AgentErrorCode.CRASH_RECOVERY.value,
                error_message=f"Stale RUNNING run recovered after {stale_timeout_s}s timeout",
            )
            recovered += 1
            logger.warning(
                "Recovered stale run: %s (%s)",
                run_info["agent_run_id"],
                run_info["agent_name"],
            )
        return recovered