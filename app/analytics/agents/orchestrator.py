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
from dataclasses import dataclass
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
from app.analytics.agents.evidence import EvidenceCatalog

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
        executor: AgentExecutorProtocol,
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
        dataset_version: str,
        maturity: str,
        quality_status: str,
        quality_limitations: Optional[list[str]] = None,
        canonical_build_json: Optional[dict] = None,
        analysis_window_from: Optional[datetime] = None,
        analysis_window_to: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Run the full Stage 3 orchestration for one analysis run.

        Parameters
        ----------
        analysis_run_id:
            The analysis_run UUID to orchestrate.
        dataset_version:
            Version string from the canonical build.
        maturity:
            PROVISIONAL or FINAL.
        quality_status:
            Quality gate outcome (PASS, DEGRADED, FAIL).
        quality_limitations:
            Optional limitation strings to forward to agents.
        canonical_build_json:
            Optional build summary with ``trade_fact_built`` / ``setup_fact_built``.
        analysis_window_from / analysis_window_to:
            Optional time bounds.

        Returns
        -------
        dict
            Summary dict with status of each agent and Chief eligibility.
        """
        logger.info(
            "Starting Stage 3 orchestration for run %s "
            "(maturity=%s, dataset_version=%s)",
            analysis_run_id, maturity, dataset_version,
        )

        # ── Step 1: Data Readiness Gate ───────────────────────────────
        readiness = self._readiness.evaluate(
            run_id=analysis_run_id,
            status="SUCCEEDED",
            maturity=maturity,
            dataset_version=dataset_version,
            quality_status=quality_status,
            canonical_build_json=canonical_build_json,
            analysis_window_from=analysis_window_from,
            analysis_window_to=analysis_window_to,
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

        # ── Step 2: Execute specialists ───────────────────────────────
        specialist_results: dict[str, AgentExecutionResult] = {}
        specialist_statuses: dict[str, str] = {}

        for agent_name in SPECIALIST_AGENTS:
            result = await self._run_specialist(
                analysis_run_id=analysis_run_id,
                agent_name=agent_name,
                dataset_version=dataset_version,
                maturity=maturity,
                readiness=readiness,
            )
            specialist_results[agent_name] = result
            specialist_statuses[agent_name] = result.status.value

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

    # ── Specialist execution ──────────────────────────────────────────

    async def _run_specialist(
        self,
        analysis_run_id: UUID,
        agent_name: str,
        dataset_version: str,
        maturity: str,
        readiness: DataReadinessResult,
    ) -> AgentExecutionResult:
        """Run a single specialist agent with retry and repair logic."""
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

        # ── Idempotency check ─────────────────────────────────────────
        existing_runs = self._repo.get_agent_runs_for_analysis(analysis_run_id)
        for r in existing_runs:
            if (
                r.agent_name == agent_name
                and r.status in (AgentRunStatus.SUCCEEDED, AgentRunStatus.DEGRADED)
                and r.attempt == 1
            ):
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

        # ── Create agent run ──────────────────────────────────────────
        attempt = self._repo.get_next_attempt(analysis_run_id, agent_name)
        agent_run = AgentRun(
            analysis_run_id=analysis_run_id,
            agent_name=agent_name,
            attempt=attempt,
            status=AgentRunStatus.PENDING,
        )
        agent_run = self._repo.create_agent_run(agent_run)

        # ── Create immutable input manifest ───────────────────────────
        manifest = self._build_manifest(
            agent_run_id=agent_run.agent_run_id,
            dataset_version=dataset_version,
            maturity=maturity,
            readiness=readiness,
        )
        manifest = self._repo.create_input_manifest(manifest)

        # ── Mark RUNNING ──────────────────────────────────────────────
        self._repo.update_agent_run_status(
            agent_run.agent_run_id,
            AgentRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )

        # ── Execute with retry ────────────────────────────────────────
        execution_result = await self._execute_with_retry(
            definition=definition,
            manifest=manifest,
            agent_run_id=agent_run.agent_run_id,
        )

        # ── Persist result ────────────────────────────────────────────
        if execution_result.result:
            self._repo.create_agent_result(execution_result.result)

        # ── Mark terminal status ──────────────────────────────────────
        self._repo.update_agent_run_status(
            agent_run.agent_run_id,
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

        logger.info(
            "Specialist %s completed: status=%s",
            agent_name, execution_result.status.value,
        )
        return execution_result

    # ── Retry / repair loop ───────────────────────────────────────────

    async def _execute_with_retry(
        self,
        definition: AgentDefinition,
        manifest: AgentInputManifest,
        agent_run_id: UUID,
    ) -> AgentExecutionResult:
        """Execute with retry and repair logic.

        Retry flow:
            1. Execute → SUCCESS/DEGRADED → return
            2. Execute → SCHEMA_VALIDATION_ERROR → repair once
            3. Execute → MODEL_TIMEOUT/RATE_LIMIT/PROVIDER_ERROR → retry with backoff
            4. Execute → terminal error → return
            5. Exhausted attempts → return FAILED
        """
        policy = self._retry_policy
        repairs_attempted = 0

        for attempt in range(1, policy.max_attempts + 1):
            result = await self._executor.execute(
                definition=definition,
                manifest=manifest,
            )

            # ── Success or degraded — done ────────────────────────────
            if result.status in (AgentRunStatus.SUCCEEDED, AgentRunStatus.DEGRADED):
                return result

            error_code = result.error_code or AgentErrorCode.UNKNOWN_ERROR

            # ── Schema validation error → repair attempt ──────────────
            if policy.should_repair(error_code, repairs_attempted) and result.error_message:
                logger.info("Attempting schema repair for %s", definition.agent_name)
                repairs_attempted += 1

                repair_result = await self._executor.repair(
                    definition=definition,
                    manifest=manifest,
                    original_error=result.error_message,
                )

                if repair_result.status in (AgentRunStatus.SUCCEEDED, AgentRunStatus.DEGRADED):
                    return repair_result

                # Repair also failed → terminal
                return AgentExecutionResult(
                    status=AgentRunStatus.FAILED,
                    result=None,
                    error_code=AgentErrorCode.REPAIR_FAILED,
                    error_message=f"Repair failed: {repair_result.error_message}",
                )

            # ── Retryable error → delay and retry ─────────────────────
            if policy.should_retry(error_code, attempt):
                delay = policy.delay_for_attempt(attempt)
                logger.info(
                    "Retrying %s (attempt %d/%d) after %.1fs: %s",
                    definition.agent_name,
                    attempt + 1,
                    policy.max_attempts,
                    delay,
                    error_code.value,
                )
                await asyncio.sleep(delay)
                continue

            # ── Terminal error — done ─────────────────────────────────
            return result

        # Exhausted retries
        return AgentExecutionResult(
            status=AgentRunStatus.FAILED,
            result=None,
            error_code=AgentErrorCode.MODEL_TIMEOUT,
            error_message=f"Exhausted {policy.max_attempts} attempts",
        )

    # ── Manifest builder ──────────────────────────────────────────────

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
        """
        manifest_data = {
            "agent_run_id": str(agent_run_id),
            "dataset_version": dataset_version,
            "maturity": maturity,
            "quality_status": readiness.quality_status,
            "limitations": list(readiness.limitations),
        }

        input_hash = hashlib.sha256(
            json.dumps(manifest_data, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

        return AgentInputManifest(
            agent_run_id=agent_run_id,
            dataset_version=dataset_version,
            input_hash=input_hash,
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
            manifest_json=manifest_data,
            evidence_ids=[],  # populated by input assembler in PR3
        )

    # ── Skip all agents ───────────────────────────────────────────────

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
            agent_run = AgentRun(
                analysis_run_id=analysis_run_id,
                agent_name=agent_name,
                attempt=attempt,
                status=AgentRunStatus.SKIPPED,
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                error_code="DATASET_NOT_READY",
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

        # Persist result
        if result.result:
            self._repo.create_agent_result(result.result)

        # Mark terminal status
        self._repo.update_agent_run_status(
            agent_run.agent_run_id,
            result.status,
            finished_at=datetime.now(timezone.utc),
            latency_ms=(
                result.model_response.latency_ms
                if result.model_response
                else None
            ),
            input_tokens=(
                result.model_response.input_tokens
                if result.model_response
                else None
            ),
            output_tokens=(
                result.model_response.output_tokens
                if result.model_response
                else None
            ),
            total_tokens=(
                result.model_response.total_tokens
                if result.model_response
                else None
            ),
            error_class=result.error_code.value if result.error_code else None,
            error_message=result.error_message,
        )

        return result

    # ── Crash recovery ────────────────────────────────────────────────

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

        Note
        ----
        Full implementation requires a repository method to query stale
        runs.  This is a PR2 stub — PR5 will complete it.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - stale_timeout_s
        # TODO: query for agent_runs with status=RUNNING and started_at < cutoff
        # and mark them as FAILED with error_class=CRASH_RECOVERY
        recovered = 0
        logger.info("Stale run recovery: checked, recovered=%d", recovered)
        return recovered
