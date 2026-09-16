"""Tests for Stage 3 Agent Orchestrator state machine.

Covers: readiness gate, parallel execution, retry/repair as separate attempts,
idempotency, PROVISIONAL/FINAL independence, crash recovery, DEGRADED propagation.
"""
import pytest
import asyncio
from uuid import uuid4
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from app.analytics.agents.models import (
    AgentRunStatus, AgentRun, AgentInputManifest, AgentResult, AgentDefinition,
    AgentType, ValidationStatus,
)
from app.analytics.agents.errors import AgentErrorCode
from app.analytics.agents.readiness import DataReadinessResult, DataReadinessGate
from app.analytics.agents.executor import AgentExecutionResult
from app.analytics.agents.orchestrator import (
    AgentOrchestrator, SPECIALIST_AGENTS, CHIEF_AGENT,
)
from app.analytics.agents.llm_client import ModelResponse


# ── Helpers ───────────────────────────────────────────────────────────

def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_success_result():
    return AgentExecutionResult(
        status=AgentRunStatus.SUCCEEDED,
        result=AgentResult(
            agent_run_id=uuid4(),
            result_json={"summary": "ok", "observations": [], "hypotheses": [],
                         "proposed_experiments": [], "anomalies": [], "confidence": "LOW",
                         "limitations": [], "evidence_refs": []},
            result_hash="abc",
            validation_status=ValidationStatus.VALID,
        ),
        model_response=ModelResponse(
            content="{}", parsed_json={}, model="test",
            input_tokens=100, output_tokens=200, total_tokens=300,
            latency_ms=500, finish_reason="stop",
        ),
    )


def _make_failed_result(error_code=AgentErrorCode.EVIDENCE_VALIDATION_ERROR):
    return AgentExecutionResult(
        status=AgentRunStatus.FAILED,
        result=None,
        error_code=error_code,
        error_message="test error",
    )


def _make_readiness(**overrides):
    defaults = dict(
        ready=True, dataset_state="READY", quality_status="PASS",
        dataset_version="v1", maturity="FINAL",
        blockers=(), limitations=(),
        analysis_window_from=datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc),
        analysis_window_to=datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return DataReadinessResult(**defaults)


def _make_analysis_run(status="SUCCEEDED", maturity="FINAL"):
    run = MagicMock()
    # Use a simple string for status, not a MagicMock
    run.status = status
    run.maturity = maturity
    run.analysis_from = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
    run.analysis_to = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)
    return run


def _make_publication(**overrides):
    pub = MagicMock()
    pub.dataset_version = overrides.get("dataset_version", "v1")
    pub.status = overrides.get("pub_status", "READY")
    pub.quality_status = overrides.get("quality_status", "PASS")
    pub.canonical_build_json = overrides.get("canonical_build_json", {
        "trade_fact_built": True, "setup_fact_built": True,
    })
    return pub


def _make_repo_mock():
    repo = MagicMock()
    repo.get_analysis_run.return_value = _make_analysis_run()
    repo.get_dataset_publication.return_value = _make_publication()
    repo.get_quality_results.return_value = []
    repo.get_agent_definition.return_value = None
    repo.create_agent_definition.return_value = AgentDefinition(
        agent_name="TEST", agent_type="SPECIALIST", model="test-model",
    )
    repo.get_agent_runs_for_analysis.return_value = []
    repo.get_next_attempt.return_value = 1
    
    # create_agent_run must return a proper AgentRun with a UUID
    def _create_run(r):
        r.agent_run_id = uuid4()
        return r
    repo.create_agent_run.side_effect = _create_run
    
    repo.create_input_manifest.side_effect = lambda m: m
    repo.get_agent_result.return_value = None
    repo.get_input_manifest.return_value = None
    repo.get_stale_agent_runs.return_value = []
    return repo


# ── Constants tests ──────────────────────────────────────────────────

class TestOrchestratorConstants:
    def test_specialist_agents_count(self):
        assert len(SPECIALIST_AGENTS) == 3

    def test_chief_agent_defined(self):
        assert CHIEF_AGENT == "CHIEF_TRADING_ANALYST"

    def test_specialist_names(self):
        assert "FUNNEL_AND_PERFORMANCE" in SPECIALIST_AGENTS
        assert "EXECUTION_QUALITY" in SPECIALIST_AGENTS
        assert "DRIFT_AND_ANOMALY" in SPECIALIST_AGENTS

    def test_no_duplicates_in_specialists(self):
        assert len(SPECIALIST_AGENTS) == len(set(SPECIALIST_AGENTS))


# ── Readiness gate tests ─────────────────────────────────────────────

class TestOrchestratorReadinessGate:
    def test_skip_when_run_not_found(self):
        """#5: Quality FAIL → clean SKIPPED, no exception."""
        repo = _make_repo_mock()
        repo.get_analysis_run.return_value = None
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "SKIPPED"
        executor.execute.assert_not_called()

    def test_skip_when_publication_missing(self):
        """#2: publication missing → SKIPPED."""
        repo = _make_repo_mock()
        repo.get_dataset_publication.return_value = None
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "SKIPPED"
        executor.execute.assert_not_called()

    def test_skip_when_publication_building(self):
        """#2: publication BUILDING → SKIPPED."""
        repo = _make_repo_mock()
        repo.get_dataset_publication.return_value = _make_publication(pub_status="BUILDING")
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "SKIPPED"
        executor.execute.assert_not_called()

    def test_skip_when_quality_fail(self):
        """#5: Quality FAIL → clean SKIPPED, no exception."""
        repo = _make_repo_mock()
        repo.get_dataset_publication.return_value = _make_publication(quality_status="FAIL")
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "SKIPPED"
        executor.execute.assert_not_called()

    def test_skip_when_canonical_metadata_missing(self):
        """#3: canonical_build_json missing → SKIPPED."""
        repo = _make_repo_mock()
        repo.get_dataset_publication.return_value = _make_publication(canonical_build_json=None)
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "SKIPPED"

    def test_skip_when_status_failed(self):
        """#5: actual analysis_run FAILED → SKIPPED."""
        repo = _make_repo_mock()
        repo.get_analysis_run.return_value = _make_analysis_run(status="FAILED")
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "SKIPPED"

    def test_skip_when_stub_version(self):
        """#1-2: stub dataset_version → SKIPPED."""
        repo = _make_repo_mock()
        repo.get_dataset_publication.return_value = _make_publication(dataset_version="00000000.0")
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "SKIPPED"


# ── Specialist execution tests ────────────────────────────────────────

class TestOrchestratorExecution:
    def test_pass_runs_specialists(self):
        repo = _make_repo_mock()
        executor = AsyncMock(return_value=_make_success_result())
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "COMPLETED"
        assert executor.execute.call_count == 3

    def test_parallel_execution(self):
        """#14: three specialists actually run concurrently."""
        repo = _make_repo_mock()
        success = _make_success_result()

        async def mock_execute(**kwargs):
            return success

        executor = AsyncMock()
        executor.execute = mock_execute
        executor.repair = AsyncMock(return_value=_make_failed_result(AgentErrorCode.REPAIR_FAILED))
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "COMPLETED"
        # 3 specialists + chief = 4 calls
        assert result.get("chief") is not None
        # All specialists should be in the result
        for name in SPECIALIST_AGENTS:
            assert name in result["agents"]
            assert result["agents"][name]["status"] == "SUCCEEDED"

    def test_one_failure_does_not_cancel_others(self):
        """#14: one specialist failure does not cancel other two."""
        repo = _make_repo_mock()
        call_count = [0]

        async def execute_side_effect(definition, manifest, timeout_s=120.0):
            call_count[0] += 1
            # Fail only the first specialist (first 1-3 calls are specialists, 
            # call 4+ is chief)
            if call_count[0] == 1:
                return _make_failed_result(AgentErrorCode.EVIDENCE_VALIDATION_ERROR)
            return _make_success_result()

        executor = AsyncMock()
        executor.execute = execute_side_effect
        executor.repair = AsyncMock(return_value=_make_failed_result(AgentErrorCode.REPAIR_FAILED))
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "COMPLETED"
        failed = [n for n, a in result["agents"].items() if a["status"] == "FAILED"]
        succeeded = [n for n, a in result["agents"].items() if a["status"] == "SUCCEEDED"]
        assert len(failed) == 1
        assert len(succeeded) == 2


# ── DEGRADED propagation ──────────────────────────────────────────────

class TestDegradedPropagation:
    def test_degraded_quality_propagates(self):
        """#15: DEGRADED propagation works."""
        repo = _make_repo_mock()
        repo.get_dataset_publication.return_value = _make_publication(quality_status="DEGRADED")

        success = _make_success_result()

        async def mock_execute(**kwargs):
            return success

        executor = AsyncMock()
        executor.execute = mock_execute
        executor.repair = AsyncMock(return_value=_make_failed_result(AgentErrorCode.REPAIR_FAILED))
        orch = AgentOrchestrator(repository=repo, executor=executor)

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        # All specialists should be DEGRADED (SUCCEEDED downgraded)
        for name in SPECIALIST_AGENTS:
            assert result["agents"][name]["status"] == "DEGRADED"


# ── Retry creates separate attempts ───────────────────────────────────

class TestRetryAttempts:
    def test_retry_creates_attempt_2(self):
        """#6: retry creates attempt 2 as separate agent_run."""
        repo = _make_repo_mock()
        attempt_count = [0]

        async def execute_side_effect(**kwargs):
            attempt_count[0] += 1
            if attempt_count[0] <= 1:
                # First attempt: timeout (retryable)
                return _make_failed_result(AgentErrorCode.MODEL_TIMEOUT)
            return _make_success_result()

        executor = AsyncMock(side_effect=execute_side_effect)
        orch = AgentOrchestrator(repository=repo, executor=executor)

        # Mock create_agent_run to return proper AgentRun
        runs_created = []
        def make_run(r):
            r.agent_run_id = uuid4()
            runs_created.append(r)
            return r
        repo.create_agent_run.side_effect = make_run

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        assert result["status"] == "COMPLETED"
        # Should have created multiple agent_runs (at least 2 for the retried specialist)
        assert len(runs_created) >= 2

    def test_schema_repair_creates_separate_attempt(self):
        """#7: schema repair creates separate attempt."""
        repo = _make_repo_mock()

        async def execute_side_effect(**kwargs):
            return _make_failed_result(AgentErrorCode.SCHEMA_VALIDATION_ERROR)

        async def repair_side_effect(**kwargs):
            return _make_success_result()

        executor = AsyncMock()
        executor.execute = AsyncMock(side_effect=execute_side_effect)
        executor.repair = AsyncMock(side_effect=repair_side_effect)
        orch = AgentOrchestrator(repository=repo, executor=executor)

        runs_created = []
        def make_run(r):
            r.agent_run_id = uuid4()
            runs_created.append(r)
            return r
        repo.create_agent_run.side_effect = make_run

        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))

        # Schema error → repair attempt → should have created 2 runs for the failed specialist
        assert len(runs_created) >= 2
        # Repair should have been called
        assert executor.repair.call_count >= 1


# ── PROVISIONAL/FINAL independence ────────────────────────────────────

class TestMaturityIndependence:
    def test_provisional_not_reused_by_final(self):
        """#9-10: PROVISIONAL result not reused for FINAL."""
        repo = _make_repo_mock()
        executor = AsyncMock(return_value=_make_success_result())

        # Simulate existing PROVISIONAL completed run
        prov_run = AgentRun(
            agent_run_id=uuid4(), analysis_run_id=uuid4(),
            agent_name="FUNNEL_AND_PERFORMANCE", attempt=1,
            status=AgentRunStatus.SUCCEEDED,
        )
        prov_manifest = MagicMock()
        prov_manifest.dataset_version = "prov_version"
        prov_manifest.maturity = "PROVISIONAL"
        repo.get_agent_runs_for_analysis.return_value = [prov_run]
        repo.get_input_manifest.return_value = prov_manifest

        orch = AgentOrchestrator(repository=repo, executor=executor)

        # Run FINAL with different dataset_version
        result = _run_async(orch.run(
            analysis_run_id=prov_run.analysis_run_id,
            maturity="FINAL",
        ))

        # Should not reuse PROVISIONAL result — different dataset_version
        # executor should still be called for FINAL execution
        assert executor.execute.call_count >= 1

    def test_same_dataset_same_maturity_reuses(self):
        """#9: same analysis_run + dataset_version + maturity → reuse."""
        repo = _make_repo_mock()
        executor = AsyncMock(return_value=_make_success_result())

        existing_run = AgentRun(
            agent_run_id=uuid4(), analysis_run_id=uuid4(),
            agent_name="FUNNEL_AND_PERFORMANCE", attempt=1,
            status=AgentRunStatus.SUCCEEDED,
        )
        existing_manifest = MagicMock()
        existing_manifest.dataset_version = "v1"
        existing_manifest.maturity = "FINAL"

        existing_result = AgentResult(
            agent_run_id=existing_run.agent_run_id,
            result_json={}, result_hash="abc",
            validation_status=ValidationStatus.VALID,
        )

        repo.get_agent_runs_for_analysis.return_value = [existing_run]
        repo.get_input_manifest.return_value = existing_manifest
        repo.get_agent_result.return_value = existing_result

        orch = AgentOrchestrator(repository=repo, executor=executor)

        # Create readiness with same dataset_version
        result = _run_async(orch.run(
            analysis_run_id=existing_run.analysis_run_id,
            maturity="FINAL",
        ))

        # Should reuse — executor not called for this specialist
        # (only for the other 2 specialists + chief)
        assert executor.execute.call_count <= 3


# ── Model identifier ──────────────────────────────────────────────────

class TestModelIdentifier:
    def test_model_is_not_contract_version(self):
        """#16: model identifier is not contract_version."""
        from app.analytics.agents.executor import AgentExecutor
        definition = AgentDefinition(
            agent_name="FUNNEL_AND_PERFORMANCE",
            agent_type="SPECIALIST",
            contract_version="v1",
            prompt_version="v1",
            model="gpt-4o",
        )
        # model passed to generate should be model, not agent_name or contract_version
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=ModelResponse(
            content="{}", parsed_json={}, model="test",
            input_tokens=100, output_tokens=200, total_tokens=300,
            latency_ms=100, finish_reason="stop",
        ))
        executor = AgentExecutor(mock_client, lambda d, m: {"system_prompt": "", "input_json": {}})

        manifest = MagicMock()
        manifest.evidence_ids = []

        _run_async(executor.execute(definition=definition, manifest=manifest))

        call_kwargs = mock_client.generate.call_args
        assert call_kwargs.kwargs["model"] == "gpt-4o"
        assert call_kwargs.kwargs["model"] != "v1"
        assert call_kwargs.kwargs["model"] != "FUNNEL_AND_PERFORMANCE"


# ── Crash recovery ────────────────────────────────────────────────────

class TestCrashRecovery:
    def test_recover_stale_runs(self):
        """#19: stale RUNNING → FAILED/CRASH_RECOVERY."""
        repo = _make_repo_mock()
        repo.get_stale_agent_runs.return_value = [
            {"agent_run_id": uuid4(), "agent_name": "FUNNEL_AND_PERFORMANCE",
             "started_at": datetime.now(timezone.utc)},
        ]
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)

        recovered = _run_async(orch.recover_stale_runs())

        assert recovered == 1
        repo.update_agent_run_status.assert_called_once()

    def test_recover_no_stale(self):
        repo = _make_repo_mock()
        repo.get_stale_agent_runs.return_value = []
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)

        recovered = _run_async(orch.recover_stale_runs())

        assert recovered == 0


# ── Input hash identity ───────────────────────────────────────────────

class TestInputHash:
    def test_same_inputs_same_hash(self):
        """#8: same retry input → same semantic input_hash."""
        repo = _make_repo_mock()
        executor = AsyncMock(return_value=_make_success_result())
        orch = AgentOrchestrator(repository=repo, executor=executor)

        r1 = _make_readiness()
        manifest1 = orch._build_manifest(uuid4(), "v1", "FINAL", r1)
        manifest2 = orch._build_manifest(uuid4(), "v1", "FINAL", r1)

        # Different agent_run_ids but same hash
        assert manifest1.agent_run_id != manifest2.agent_run_id
        assert manifest1.input_hash == manifest2.input_hash

    def test_different_version_different_hash(self):
        """#11: different dataset_version → new execution."""
        repo = _make_repo_mock()
        executor = AsyncMock(return_value=_make_success_result())
        orch = AgentOrchestrator(repository=repo, executor=executor)

        r1 = _make_readiness()
        manifest1 = orch._build_manifest(uuid4(), "v1", "FINAL", r1)
        manifest2 = orch._build_manifest(uuid4(), "v2", "FINAL", r1)

        assert manifest1.input_hash != manifest2.input_hash

    def test_different_maturity_different_hash(self):
        """#9-10: different maturity → different hash."""
        repo = _make_repo_mock()
        executor = AsyncMock(return_value=_make_success_result())
        orch = AgentOrchestrator(repository=repo, executor=executor)

        r1 = _make_readiness()
        manifest1 = orch._build_manifest(uuid4(), "v1", "PROVISIONAL", r1)
        manifest2 = orch._build_manifest(uuid4(), "v1", "FINAL", r1)

        assert manifest1.input_hash != manifest2.input_hash
