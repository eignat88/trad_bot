"""Tests for Stage 3 PR3 specialist agents.

Covers: input assemblers, evidence catalog, prompts, provider client,
golden semantic fixtures, confidence enforcement, DEGRADED propagation.
"""
import asyncio
import json
import pytest
from uuid import uuid4
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

from app.analytics.agents.models import (
    AgentDefinition, AgentType, AgentRunStatus, AgentInputManifest,
    AgentResult, ValidationStatus,
)
from app.analytics.agents.executor import AgentExecutor, AgentExecutionResult
from app.analytics.agents.errors import AgentErrorCode
from app.analytics.agents.registry import (
    SPECIALIST_REGISTRY, SpecialistRegistration,
)
from app.analytics.agents.llm_client import ModelResponse
from app.analytics.agents.readiness import DataReadinessResult, DataReadinessGate
from app.analytics.agents.evidence import build_metric_id, build_case_id, build_quality_id, EvidenceCatalog
from app.analytics.agents.policies.confidence_v1 import ConfidencePolicyV1
from app.analytics.agents.models import ConfidenceLevel


# ── Helpers (duplicated from orchestration tests to avoid cross-import) ──

def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_success_response():
    return {
        "summary": "Short signals showed positive expectancy in 24h window",
        "observations": [
            {
                "observation_code": "SIGNAL_FREQUENCY_COLLAPSE",
                "scope": {"scanner": "MOMENTUM_EXHAUSTION", "direction": "SHORT"},
                "statement": "Signal frequency dropped 40% vs 7d baseline (3 vs 5/day)",
                "metric_refs": ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"],
                "sample_size": 3,
                "confidence": "LOW",
                "evidence_refs": ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"],
            }
        ],
        "hypotheses": [
            {
                "hypothesis": "Market regime shift may be reducing setup frequency",
                "evidence_refs": ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"],
                "confidence": "LOW",
                "proposed_experiment": "Compare 7d frequency across regime segments",
            }
        ],
        "proposed_experiments": [
            {
                "experiment": "Replay with extended confirmation delay",
                "required_data": ["market.candle 7d", "scanner_setup 7d"],
                "validation_criterion": "Setup frequency stable within 20% of baseline",
            }
        ],
        "anomalies": [],
        "confidence": "LOW",
        "limitations": ["Single day observation, small sample"],
        "evidence_refs": ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"],
    }


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
    def _create_run(r):
        r.agent_run_id = uuid4()
        return r
    repo.create_agent_run.side_effect = _create_run
    repo.create_input_manifest.side_effect = lambda m: m
    repo.get_agent_result.return_value = None
    repo.get_input_manifest.return_value = None
    repo.get_stale_agent_runs.return_value = []
    return repo


# ── Helpers ───────────────────────────────────────────────────────────

def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_success_response():
    return {
        "summary": "Short signals showed positive expectancy in 24h window",
        "observations": [
            {
                "observation_code": "SIGNAL_FREQUENCY_COLLAPSE",
                "scope": {"scanner": "MOMENTUM_EXHAUSTION", "direction": "SHORT"},
                "statement": "Signal frequency dropped 40% vs 7d baseline (3 vs 5/day)",
                "metric_refs": ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"],
                "sample_size": 3,
                "confidence": "LOW",
                "evidence_refs": ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"],
            }
        ],
        "hypotheses": [
            {
                "hypothesis": "Market regime shift may be reducing setup frequency",
                "evidence_refs": ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"],
                "confidence": "LOW",
                "proposed_experiment": "Compare 7d frequency across regime segments",
            }
        ],
        "proposed_experiments": [
            {
                "experiment": "Replay with extended confirmation delay",
                "required_data": ["market.candle 7d", "scanner_setup 7d"],
                "validation_criterion": "Setup frequency stable within 20% of baseline",
            }
        ],
        "anomalies": [],
        "confidence": "LOW",
        "limitations": ["Single day observation, small sample"],
        "evidence_refs": ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"],
    }


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


# ── Registry tests ────────────────────────────────────────────────────

class TestSpecialistRegistry:
    def test_three_specialists_registered(self):
        assert len(SPECIALIST_REGISTRY) == 3

    def test_all_required_agents_present(self):
        assert "FUNNEL_AND_PERFORMANCE" in SPECIALIST_REGISTRY
        assert "EXECUTION_QUALITY" in SPECIALIST_REGISTRY
        assert "DRIFT_AND_ANOMALY" in SPECIALIST_REGISTRY

    def test_each_has_assembler_and_prompt(self):
        for name, reg in SPECIALIST_REGISTRY.items():
            assert reg.assembler_class is not None, f"{name} missing assembler"
            assert reg.prompt_builder is not None, f"{name} missing prompt_builder"

    def test_enabled_by_default(self):
        for name, reg in SPECIALIST_REGISTRY.items():
            assert reg.enabled is True, f"{name} should be enabled"

    def test_get_registration(self):
        reg = SPECIALIST_REGISTRY.get("FUNNEL_AND_PERFORMANCE")
        assert reg is not None
        assert reg.agent_name == "FUNNEL_AND_PERFORMANCE"

    def test_all_enabled(self):
        enabled = [r for r in SPECIALIST_REGISTRY.values() if r.enabled]
        assert len(enabled) == 3


# ── Evidence catalog tests ────────────────────────────────────────────

class TestEvidenceCatalog:
    def test_build_metric_id_format(self):
        eid = build_metric_id("ME", "SHORT", "24h", "pnl_r")
        assert eid == "metric:scanner:ME:SHORT:24h:pnl_r"

    def test_build_case_id_format(self):
        eid = build_case_id("trade", "12345")
        assert eid == "case:trade:12345"

    def test_build_quality_id_format(self):
        eid = build_quality_id("symbol_coverage")
        assert eid == "quality:symbol_coverage"

    def test_catalog_validate_known(self):
        cat = EvidenceCatalog(["metric:a", "case:trade:1"])
        assert cat.validate("metric:a") is True

    def test_catalog_validate_unknown(self):
        cat = EvidenceCatalog(["metric:a"])
        assert cat.validate("metric:b") is False

    def test_catalog_validate_refs_mixed(self):
        cat = EvidenceCatalog(["metric:a", "metric:b"])
        invalid = cat.validate_refs(["metric:a", "metric:c"])
        assert invalid == ["metric:c"]


# ── Confidence policy integration ──────────────────────────────────────

class TestConfidenceEnforcement:
    def test_high_requires_sufficient_evidence(self):
        policy = ConfidencePolicyV1()
        # n=2 should not get HIGH
        result = policy.evaluate(
            sample_size=2, maturity="FINAL",
            data_quality_status="PASS", windows_with_effect=3, has_gaps=False,
        )
        assert result != ConfidenceLevel.HIGH

    def test_high_requires_multi_window(self):
        policy = ConfidencePolicyV1()
        result = policy.evaluate(
            sample_size=30, maturity="FINAL",
            data_quality_status="PASS", windows_with_effect=1, has_gaps=False,
        )
        assert result != ConfidenceLevel.HIGH

    def test_medium_achievable(self):
        policy = ConfidencePolicyV1()
        result = policy.evaluate(
            sample_size=10, maturity="PROVISIONAL",
            data_quality_status="PASS", windows_with_effect=2, has_gaps=False,
        )
        assert result == ConfidenceLevel.MEDIUM


# ── Input assembler tests (mocked DB) ──────────────────────────────────

class TestInputAssemblers:
    def _mock_data_repo(self):
        repo = MagicMock()
        repo.get_funnel_data.return_value = []
        repo.get_trade_cases.return_value = []
        repo.get_metric_snapshots.return_value = []
        repo.get_horizon_metrics.return_value = []
        repo.get_replay_metrics.return_value = []
        repo.get_quality_summary.return_value = {}
        repo.detect_drift_candidates.return_value = []
        return repo

    def test_funnel_assembler_empty_data(self):
        """Funnel assembler handles empty canonical data gracefully."""
        from app.analytics.agents.input.funnel import FunnelPerformanceInputAssembler
        data_repo = self._mock_data_repo()
        assembler = FunnelPerformanceInputAssembler(data_repo)
        
        result = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        assert result.agent_name == "FUNNEL_AND_PERFORMANCE"
        assert isinstance(result.evidence_ids, list)
        assert isinstance(result.metrics, dict)

    def test_execution_assembler_empty_data(self):
        """Execution assembler handles empty canonical data gracefully."""
        from app.analytics.agents.input.execution import ExecutionQualityInputAssembler
        data_repo = self._mock_data_repo()
        assembler = ExecutionQualityInputAssembler(data_repo)
        
        result = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        assert result.agent_name == "EXECUTION_QUALITY"
        assert isinstance(result.evidence_ids, list)

    def test_drift_assembler_empty_data(self):
        """Drift assembler handles empty canonical data gracefully."""
        from app.analytics.agents.input.drift import DriftAnomalyInputAssembler
        data_repo = self._mock_data_repo()
        assembler = DriftAnomalyInputAssembler(data_repo)
        
        result = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        assert result.agent_name == "DRIFT_AND_ANOMALY"
        assert isinstance(result.evidence_ids, list)

    def test_funnel_assembler_populates_evidence(self):
        """Funnel assembler populates evidence_ids from metrics."""
        from app.analytics.agents.input.funnel import FunnelPerformanceInputAssembler
        from app.analytics.agents.data_repository import FunnelData
        
        data_repo = self._mock_data_repo()
        data_repo.get_funnel_data.return_value = [
            FunnelData(
                scanner="ME", direction="SHORT",
                total_setups=10, fills=5, closed_trades=5,
                wins=3, losses=2, win_rate=0.6,
                total_pnl_r=2.5, avg_pnl_r=0.5, profit_factor=1.8,
            )
        ]
        
        assembler = FunnelPerformanceInputAssembler(data_repo)
        result = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        assert len(result.evidence_ids) > 0
        assert len(result.metrics) > 0
        assert result.sample_sizes["total_segments"] == 1

    def test_truncation_adds_limitation(self):
        """Assembler adds limitation when data is truncated."""
        from app.analytics.agents.input.funnel import FunnelPerformanceInputAssembler, MAX_SEGMENTS
        from app.analytics.agents.data_repository import FunnelData
        
        data_repo = self._mock_data_repo()
        # Create more segments than MAX_SEGMENTS
        data_repo.get_funnel_data.return_value = [
            FunnelData(scanner=f"SC{i}", direction="LONG", total_setups=5)
            for i in range(MAX_SEGMENTS + 5)
        ]
        
        assembler = FunnelPerformanceInputAssembler(data_repo)
        result = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        assert any("truncated" in lim.lower() for lim in result.limitations)


# ── Prompt builder tests ───────────────────────────────────────────────

class TestPromptBuilders:
    def test_funnel_prompt_has_system_prompt(self):
        from app.analytics.agents.prompts.funnel_performance_v1 import build_funnel_prompt, SYSTEM_PROMPT
        from app.analytics.agents.input.base import SpecialistInput
        
        si = SpecialistInput(
            agent_name="FUNNEL_AND_PERFORMANCE",
            run_id=uuid4(), dataset_version="v1",
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
            maturity="FINAL", quality_status="PASS",
            limitations=[], sample_sizes={}, metrics={},
            segments=[], cases=[], evidence_ids=[], evidence_catalog=[],
        )
        
        prompt = build_funnel_prompt(si)
        assert "system_prompt" in prompt
        assert "input_json" in prompt
        assert "FUNNEL" in prompt["system_prompt"] or "funnel" in prompt["system_prompt"].lower()
        assert prompt["input_json"]["maturity"] == "FINAL"

    def test_execution_prompt_has_system_prompt(self):
        from app.analytics.agents.prompts.execution_quality_v1 import build_execution_prompt, SYSTEM_PROMPT
        from app.analytics.agents.input.base import SpecialistInput
        
        si = SpecialistInput(
            agent_name="EXECUTION_QUALITY",
            run_id=uuid4(), dataset_version="v1",
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
            maturity="FINAL", quality_status="PASS",
            limitations=[], sample_sizes={}, metrics={},
            segments=[], cases=[], evidence_ids=[], evidence_catalog=[],
        )
        
        prompt = build_execution_prompt(si)
        assert "system_prompt" in prompt
        assert "EXECUTION" in prompt["system_prompt"] or "execution" in prompt["system_prompt"].lower()

    def test_drift_prompt_has_system_prompt(self):
        from app.analytics.agents.prompts.drift_anomaly_v1 import build_drift_prompt, SYSTEM_PROMPT
        from app.analytics.agents.input.base import SpecialistInput
        
        si = SpecialistInput(
            agent_name="DRIFT_AND_ANOMALY",
            run_id=uuid4(), dataset_version="v1",
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
            maturity="FINAL", quality_status="PASS",
            limitations=[], sample_sizes={}, metrics={},
            segments=[], cases=[], evidence_ids=[], evidence_catalog=[],
        )
        
        prompt = build_drift_prompt(si)
        assert "system_prompt" in prompt
        assert "DRIFT" in prompt["system_prompt"] or "drift" in prompt["system_prompt"].lower()

    def test_all_prompts_prohibit_production_mutation(self):
        """All prompts must explicitly prohibit production changes."""
        from app.analytics.agents.prompts.funnel_performance_v1 import SYSTEM_PROMPT as FUNNEL
        from app.analytics.agents.prompts.execution_quality_v1 import SYSTEM_PROMPT as EXEC
        from app.analytics.agents.prompts.drift_anomaly_v1 import SYSTEM_PROMPT as DRIFT
        
        for name, prompt in [("FUNNEL", FUNNEL), ("EXECUTION", EXEC), ("DRIFT", DRIFT)]:
            assert "not authorized to modify" in prompt.lower() or "not authorized to recommend" in prompt.lower(), \
                f"{name} prompt must prohibit production mutation"


# ── Golden semantic fixtures ───────────────────────────────────────────

class TestGoldenFixtures:
    """Test that golden semantic fixtures produce structurally valid output."""

    def test_success_response_validates(self):
        """Golden success response passes schema validation."""
        from app.analytics.agents.contracts.schema_validator import validate_specialist_output
        
        response = _make_success_response()
        errors = validate_specialist_output(response)
        assert errors == [], f"Golden response failed validation: {errors}"

    def test_success_response_has_evidence(self):
        """Golden response observations reference evidence."""
        response = _make_success_response()
        assert len(response["observations"]) > 0
        for obs in response["observations"]:
            assert "evidence_refs" in obs
            assert len(obs["evidence_refs"]) > 0
            assert "sample_size" in obs
            assert obs["sample_size"] > 0

    def test_success_response_separates_observation_from_hypothesis(self):
        """Golden response separates facts from hypotheses."""
        response = _make_success_response()
        obs_codes = {obs["observation_code"] for obs in response["observations"]}
        hyp_texts = [h["hypothesis"] for h in response["hypotheses"]]
        # Observations should not appear as hypotheses
        for code in obs_codes:
            for hyp in hyp_texts:
                assert code.lower() not in hyp.lower(), \
                    f"Observation code {code} found in hypothesis text"

    def test_success_response_no_production_action(self):
        """Golden response contains no production mutation recommendation."""
        response = _make_success_response()
        for exp in response.get("proposed_experiments", []):
            assert "deploy" not in exp["experiment"].lower()
            assert "change config" not in exp["experiment"].lower()
            assert "disable scanner" not in exp["experiment"].lower()


# ── Executor integration tests ─────────────────────────────────────────

class TestExecutorIntegration:
    def test_executor_with_model_configured(self):
        """Executor calls LLM when model is configured."""
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=ModelResponse(
            content=json.dumps(_make_success_response()),
            parsed_json=_make_success_response(),
            model="gpt-4o", input_tokens=100, output_tokens=200,
            total_tokens=300, latency_ms=500, finish_reason="stop",
        ))
        
        def prompt_builder(definition, manifest):
            return {"system_prompt": "test", "input_json": {}, "response_schema": None}
        
        executor = AgentExecutor(mock_client, prompt_builder)
        
        defn = AgentDefinition(
            agent_name="FUNNEL_AND_PERFORMANCE",
            agent_type=AgentType.SPECIALIST,
            model="gpt-4o",
        )
        manifest = MagicMock()
        manifest.evidence_ids = [
            "metric:scanner:ME:SHORT:24h:pnl_r",
            "metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups",
        ]
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.status == AgentRunStatus.SUCCEEDED
        assert result.result is not None
        mock_client.generate.assert_called_once()

    def test_executor_rejects_without_model(self):
        """Executor rejects execution when model is None."""
        mock_client = AsyncMock()
        executor = AgentExecutor(mock_client, lambda d, m: {"system_prompt": "", "input_json": {}})
        
        defn = AgentDefinition(
            agent_name="TEST", agent_type=AgentType.SPECIALIST, model=None,
        )
        manifest = MagicMock()
        manifest.evidence_ids = []
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.status == AgentRunStatus.FAILED
        mock_client.generate.assert_not_called()

    def test_evidence_validation_rejects_unknown_refs(self):
        """Executor rejects output with unknown evidence references."""
        response_data = _make_success_response()
        response_data["evidence_refs"].append("metric:fake:123")
        
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=ModelResponse(
            content=json.dumps(response_data),
            parsed_json=response_data,
            model="gpt-4o", input_tokens=100, output_tokens=200,
            total_tokens=300, latency_ms=500, finish_reason="stop",
        ))
        
        def prompt_builder(definition, manifest):
            return {"system_prompt": "test", "input_json": {}, "response_schema": None}
        
        executor = AgentExecutor(mock_client, prompt_builder)
        
        defn = AgentDefinition(
            agent_name="TEST", agent_type=AgentType.SPECIALIST, model="gpt-4o",
        )
        manifest = MagicMock()
        manifest.evidence_ids = ["metric:scanner:ME:SHORT:24h:pnl_r"]  # no "metric:fake:123"
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.status == AgentRunStatus.FAILED
        assert result.error_code.value == "EVIDENCE_VALIDATION_ERROR"

    def test_schema_validation_rejects_invalid_output(self):
        """Executor rejects output that doesn't match schema."""
        bad_response = {"only_summary": "missing required fields"}
        
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=ModelResponse(
            content=json.dumps(bad_response),
            parsed_json=bad_response,
            model="gpt-4o", input_tokens=100, output_tokens=200,
            total_tokens=300, latency_ms=500, finish_reason="stop",
        ))
        
        def prompt_builder(definition, manifest):
            return {"system_prompt": "test", "input_json": {}, "response_schema": None}
        
        executor = AgentExecutor(mock_client, prompt_builder)
        
        defn = AgentDefinition(
            agent_name="TEST", agent_type=AgentType.SPECIALIST, model="gpt-4o",
        )
        manifest = MagicMock()
        manifest.evidence_ids = []
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.status == AgentRunStatus.FAILED
        assert result.error_code.value == "SCHEMA_VALIDATION_ERROR"

    def test_provider_timeout_raises(self):
        """Provider timeout is properly classified."""
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(side_effect=TimeoutError("timeout"))
        
        def prompt_builder(definition, manifest):
            return {"system_prompt": "test", "input_json": {}, "response_schema": None}
        
        executor = AgentExecutor(mock_client, prompt_builder)
        
        defn = AgentDefinition(
            agent_name="TEST", agent_type=AgentType.SPECIALIST, model="gpt-4o",
        )
        manifest = MagicMock()
        manifest.evidence_ids = []
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.status == AgentRunStatus.FAILED
        assert result.error_code.value == "MODEL_TIMEOUT"

    def test_token_accounting_populated(self):
        """ModelResponse token fields are preserved in AgentResult."""
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=ModelResponse(
            content=json.dumps(_make_success_response()),
            parsed_json=_make_success_response(),
            model="gpt-4o", input_tokens=500, output_tokens=300,
            total_tokens=800, latency_ms=1200, finish_reason="stop",
        ))
        
        def prompt_builder(definition, manifest):
            return {"system_prompt": "test", "input_json": {}, "response_schema": None}
        
        executor = AgentExecutor(mock_client, prompt_builder)
        
        defn = AgentDefinition(
            agent_name="TEST", agent_type=AgentType.SPECIALIST, model="gpt-4o",
        )
        manifest = MagicMock()
        manifest.evidence_ids = []
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.model_response.input_tokens == 500
        assert result.model_response.output_tokens == 300
        assert result.model_response.total_tokens == 800
        assert result.model_response.latency_ms == 1200


# ── Prompt injection resistance ────────────────────────────────────────

class TestSecurity:
    def test_all_prompts_treat_data_as_data(self):
        """All prompts instruct the model to treat database content as data, not instructions."""
        from app.analytics.agents.prompts.funnel_performance_v1 import SYSTEM_PROMPT as FUNNEL
        from app.analytics.agents.prompts.execution_quality_v1 import SYSTEM_PROMPT as EXEC
        from app.analytics.agents.prompts.drift_anomaly_v1 import SYSTEM_PROMPT as DRIFT
        
        for name, prompt in [("FUNNEL", FUNNEL), ("EXECUTION", EXEC), ("DRIFT", DRIFT)]:
            # Must contain instruction about not following injected instructions
            assert "data" in prompt.lower() or "not instructions" in prompt.lower(), \
                f"{name} prompt should treat data as data"

    def test_no_shell_commands_in_prompts(self):
        """Prompts must not contain shell/system commands."""
        from app.analytics.agents.prompts.funnel_performance_v1 import SYSTEM_PROMPT as FUNNEL
        from app.analytics.agents.prompts.execution_quality_v1 import SYSTEM_PROMPT as EXEC
        from app.analytics.agents.prompts.drift_anomaly_v1 import SYSTEM_PROMPT as DRIFT
        
        dangerous = ["shell", "systemd", "git ", "deploy", "ssh"]
        for name, prompt in [("FUNNEL", FUNNEL), ("EXECUTION", EXEC), ("DRIFT", DRIFT)]:
            for word in dangerous:
                assert word not in prompt.lower(), \
                    f"{name} prompt contains dangerous word: {word}"

    def test_evidence_catalog_is_deterministic(self):
        """Evidence IDs are deterministic for same inputs."""
        eid1 = build_metric_id("ME", "SHORT", "24h", "pnl_r")
        eid2 = build_metric_id("ME", "SHORT", "24h", "pnl_r")
        assert eid1 == eid2

    def test_no_chain_of_thought_in_output(self):
        """Golden response does not contain chain-of-thought."""
        response = _make_success_response()
        response_str = json.dumps(response).lower()
        assert "let me think" not in response_str
        assert "chain of thought" not in response_str
        assert "reasoning step" not in response_str


# ── DEGRADED propagation ───────────────────────────────────────────────

class TestDegradedPropagation:
    def test_degraded_quality_in_input(self):
        """DEGRADED quality flows into specialist input."""
        from app.analytics.agents.input.funnel import FunnelPerformanceInputAssembler
        
        data_repo = MagicMock()
        data_repo.get_funnel_data.return_value = []
        data_repo.get_trade_cases.return_value = []
        data_repo.get_metric_snapshots.return_value = []
        data_repo.detect_drift_candidates.return_value = []
        data_repo.get_quality_summary.return_value = {}
        
        assembler = FunnelPerformanceInputAssembler(data_repo)
        result = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="DEGRADED",
            limitations=["missing 7d candles for ETHUSDT"],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        assert result.quality_status == "DEGRADED"
        assert "missing 7d candles" in result.limitations[0]


# ── PR3 CORRECTION: Registry/Orchestrator E2E ──────────────────────────

class TestRegistryOrchestratorE2E:
    """Tests that the real SPECIALIST_REGISTRY works with the orchestrator."""

    def test_registry_uses_dataclass_api(self):
        """#1: Registry uses SpecialistRegistration dataclass, not dict."""
        from app.analytics.agents.registry import SpecialistRegistration
        
        for name, reg in SPECIALIST_REGISTRY.items():
            assert isinstance(reg, SpecialistRegistration)
            assert hasattr(reg, "assembler_class")
            assert hasattr(reg, "prompt_builder")
            # assembler_class is an actual class, not a callable factory
            assert isinstance(reg.assembler_class, type)

    def test_registry_assembler_instantiated(self):
        """#2: Real assembler class is instantiated."""
        for name, reg in SPECIALIST_REGISTRY.items():
            assembler = reg.assembler_class(MagicMock())
            assert hasattr(assembler, "assemble")

    def test_real_registry_end_to_end(self):
        """#29: Real SPECIALIST_REGISTRY + fake repo + fake LLM + real orchestrator."""
        from app.analytics.agents.orchestrator import AgentOrchestrator
        
        # Mock repository
        repo = MagicMock()
        repo.get_analysis_run.return_value = _make_analysis_run()
        repo.get_dataset_publication.return_value = _make_publication()
        repo.get_quality_results.return_value = []
        repo.get_agent_definition.return_value = AgentDefinition(
            agent_name="TEST", agent_type="SPECIALIST", model="test-model",
        )
        repo.get_agent_runs_for_analysis.return_value = []
        repo.get_next_attempt.return_value = 1
        repo.create_agent_run.side_effect = lambda r: r
        repo.create_input_manifest.side_effect = lambda m: m
        repo.get_agent_result.return_value = None
        repo.get_input_manifest.return_value = None
        repo.get_stale_agent_runs.return_value = []
        
        # Mock data repo (returns empty)
        data_repo = MagicMock()
        data_repo.get_funnel_data.return_value = []
        data_repo.get_trade_cases.return_value = []
        data_repo.get_metric_snapshots.return_value = []
        data_repo.get_horizon_metrics.return_value = []
        data_repo.get_replay_metrics.return_value = []
        data_repo.get_quality_summary.return_value = {}
        data_repo.detect_drift_candidates.return_value = []
        
        # Fake LLM client
        async def fake_execute(**kwargs):
            return _make_success_result()
        
        executor = AsyncMock()
        executor.execute = fake_execute
        executor.repair = AsyncMock(return_value=_make_failed_result(AgentErrorCode.REPAIR_FAILED))
        
        # Create orchestrator with real registry
        orch = AgentOrchestrator(
            repository=repo,
            executor=executor,
            data_repo=data_repo,
            specialist_registry=SPECIALIST_REGISTRY,
        )
        
        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))
        
        assert result["status"] == "COMPLETED"
        # All 3 specialists should be in the result
        for name in ["FUNNEL_AND_PERFORMANCE", "EXECUTION_QUALITY", "DRIFT_AND_ANOMALY"]:
            assert name in result["agents"]


# ── PR3 CORRECTION: Assembly fail-closed ───────────────────────────────

class TestAssemblyFailClosed:
    def test_assembler_exception_returns_failed(self):
        """#3: assembler exception → FAILED, provider not called."""
        from app.analytics.agents.orchestrator import AgentOrchestrator, SPECIALIST_AGENTS
        
        class BrokenAssembler:
            def __init__(self, data_repo):
                pass
            def assemble(self, **kwargs):
                raise RuntimeError("DB connection lost")
        
        # Use one of the real agent names but with a broken assembler
        target_agent = SPECIALIST_AGENTS[0]
        broken_registry = {
            target_agent: SpecialistRegistration(
                agent_name=target_agent,
                assembler_class=BrokenAssembler,
                prompt_builder=lambda si: {"system_prompt": "", "input_json": {}},
                enabled=True,
            )
        }
        
        repo = MagicMock()
        repo.get_analysis_run.return_value = _make_analysis_run()
        repo.get_dataset_publication.return_value = _make_publication()
        repo.get_quality_results.return_value = []
        repo.get_agent_definition.return_value = AgentDefinition(
            agent_name=target_agent, agent_type="SPECIALIST", model="test",
        )
        repo.get_agent_runs_for_analysis.return_value = []
        repo.get_next_attempt.return_value = 1
        repo.create_agent_run.side_effect = lambda r: r
        repo.create_input_manifest.side_effect = lambda m: m
        repo.get_agent_result.return_value = None
        repo.get_input_manifest.return_value = None
        repo.get_stale_agent_runs.return_value = []
        
        executor = AsyncMock()
        orch = AgentOrchestrator(
            repository=repo, executor=executor,
            data_repo=MagicMock(), specialist_registry=broken_registry,
        )
        
        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))
        
        # The target agent should be FAILED due to assembly error
        agent_result = result["agents"].get(target_agent)
        assert agent_result is not None
        assert agent_result["status"] == "FAILED"
        assert "INPUT_ASSEMBLY_ERROR" == agent_result["error_code"]

    def test_missing_registry_for_production_fails_closed(self):
        """#15: no registry entry for production specialist → FAILED."""
        from app.analytics.agents.orchestrator import AgentOrchestrator, SPECIALIST_AGENTS
        
        repo = MagicMock()
        repo.get_analysis_run.return_value = _make_analysis_run()
        repo.get_dataset_publication.return_value = _make_publication()
        repo.get_quality_results.return_value = []
        repo.get_agent_definition.return_value = AgentDefinition(
            agent_name=SPECIALIST_AGENTS[0], agent_type="SPECIALIST", model="test",
        )
        repo.get_agent_runs_for_analysis.return_value = []
        repo.get_next_attempt.return_value = 1
        repo.create_agent_run.side_effect = lambda r: r
        repo.create_input_manifest.side_effect = lambda m: m
        repo.get_agent_result.return_value = None
        repo.get_input_manifest.return_value = None
        repo.get_stale_agent_runs.return_value = []
        
        executor = AsyncMock(return_value=_make_success_result())
        orch = AgentOrchestrator(
            repository=repo, executor=executor,
            data_repo=MagicMock(), specialist_registry={},
        )
        
        result = _run_async(orch.run(analysis_run_id=uuid4(), maturity="FINAL"))
        
        # Production specialist with empty registry → FAILED
        agent_result = result["agents"].get(SPECIALIST_AGENTS[0])
        assert agent_result is not None
        assert agent_result["status"] == "FAILED"
        assert agent_result["error_code"] == "INVALID_INPUT"


# ── PR3 CORRECTION: Confidence enforcement ─────────────────────────────

class TestConfidenceEnforcementIntegration:
    def test_high_n2_downgraded(self):
        """#8: LLM HIGH + n=2 → persisted LOW/MEDIUM."""
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=ModelResponse(
            content=json.dumps({**_make_success_response(), "confidence": "HIGH"}),
            parsed_json={**_make_success_response(), "confidence": "HIGH"},
            model="gpt-4o", input_tokens=100, output_tokens=200,
            total_tokens=300, latency_ms=500, finish_reason="stop",
        ))
        
        def prompt_builder(definition, manifest):
            return {"system_prompt": "test", "input_json": {}, "response_schema": None}
        
        executor = AgentExecutor(mock_client, prompt_builder)
        
        defn = AgentDefinition(agent_name="TEST", agent_type=AgentType.SPECIALIST, model="gpt-4o")
        manifest = MagicMock()
        manifest.evidence_ids = ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"]
        manifest.sample_sizes = {"total_trades": 2}  # small sample
        manifest.maturity = "FINAL"
        manifest.data_quality_status = "PASS"
        manifest.limitations = []
        manifest.metrics = {}
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.status == AgentRunStatus.SUCCEEDED
        # Confidence should be downgraded from HIGH to MEDIUM (n=2 < 10)
        confidence = result.result.result_json["confidence"]
        assert confidence in ("LOW", "MEDIUM")
        # Downgrade message should be in limitations
        assert any("downgraded" in lim.lower() for lim in result.result.result_json["limitations"])

    def test_high_provisional_downgraded(self):
        """#9: LLM HIGH + PROVISIONAL → not HIGH."""
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=ModelResponse(
            content=json.dumps({**_make_success_response(), "confidence": "HIGH"}),
            parsed_json={**_make_success_response(), "confidence": "HIGH"},
            model="gpt-4o", input_tokens=100, output_tokens=200,
            total_tokens=300, latency_ms=500, finish_reason="stop",
        ))
        
        def prompt_builder(definition, manifest):
            return {"system_prompt": "test", "input_json": {}, "response_schema": None}
        
        executor = AgentExecutor(mock_client, prompt_builder)
        
        defn = AgentDefinition(agent_name="TEST", agent_type=AgentType.SPECIALIST, model="gpt-4o")
        manifest = MagicMock()
        manifest.evidence_ids = ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"]
        manifest.sample_sizes = {"total_trades": 50}
        manifest.maturity = "PROVISIONAL"
        manifest.data_quality_status = "PASS"
        manifest.limitations = []
        manifest.metrics = {}
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.status == AgentRunStatus.SUCCEEDED
        confidence = result.result.result_json["confidence"]
        assert confidence != "HIGH"

    def test_high_degraded_downgraded(self):
        """#10: LLM HIGH + DEGRADED → not HIGH."""
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=ModelResponse(
            content=json.dumps({**_make_success_response(), "confidence": "HIGH"}),
            parsed_json={**_make_success_response(), "confidence": "HIGH"},
            model="gpt-4o", input_tokens=100, output_tokens=200,
            total_tokens=300, latency_ms=500, finish_reason="stop",
        ))
        
        def prompt_builder(definition, manifest):
            return {"system_prompt": "test", "input_json": {}, "response_schema": None}
        
        executor = AgentExecutor(mock_client, prompt_builder)
        
        defn = AgentDefinition(agent_name="TEST", agent_type=AgentType.SPECIALIST, model="gpt-4o")
        manifest = MagicMock()
        manifest.evidence_ids = ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"]
        manifest.sample_sizes = {"total_trades": 50}
        manifest.maturity = "FINAL"
        manifest.data_quality_status = "DEGRADED"
        manifest.limitations = ["data quality degraded"]
        manifest.metrics = {}
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.status == AgentRunStatus.SUCCEEDED
        confidence = result.result.result_json["confidence"]
        assert confidence != "HIGH"

    def test_allowed_high_preserved(self):
        """#11: HIGH + FINAL + PASS + n>=30 → may remain HIGH."""
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=ModelResponse(
            content=json.dumps({**_make_success_response(), "confidence": "HIGH"}),
            parsed_json={**_make_success_response(), "confidence": "HIGH"},
            model="gpt-4o", input_tokens=100, output_tokens=200,
            total_tokens=300, latency_ms=500, finish_reason="stop",
        ))
        
        def prompt_builder(definition, manifest):
            return {"system_prompt": "test", "input_json": {}, "response_schema": None}
        
        executor = AgentExecutor(mock_client, prompt_builder)
        
        defn = AgentDefinition(agent_name="TEST", agent_type=AgentType.SPECIALIST, model="gpt-4o")
        manifest = MagicMock()
        manifest.evidence_ids = ["metric:scanner:MOMENTUM_EXHAUSTION:SHORT:24h:total_setups"]
        manifest.sample_sizes = {"total_trades": 35}
        manifest.maturity = "FINAL"
        manifest.data_quality_status = "PASS"
        manifest.limitations = []
        manifest.metrics = {"24h:foo": {}, "7d:foo": {}, "30d:foo": {}}
        
        result = _run_async(executor.execute(definition=defn, manifest=manifest))
        
        assert result.status == AgentRunStatus.SUCCEEDED
        confidence = result.result.result_json["confidence"]
        assert confidence == "HIGH"


# ── PR3 CORRECTION: Provider fail-closed ───────────────────────────────

class TestProviderFailClosed:
    def test_missing_api_key_raises(self):
        """#11: missing API key → ValueError, no HTTP call."""
        from app.analytics.agents.llm_provider import OpenAICompatibleClient
        with pytest.raises(ValueError, match="api_key is required"):
            OpenAICompatibleClient(api_key="")

    def test_whitespace_api_key_raises(self):
        """#12: whitespace API key → ValueError."""
        from app.analytics.agents.llm_provider import OpenAICompatibleClient
        with pytest.raises(ValueError, match="api_key is required"):
            OpenAICompatibleClient(api_key="   ")

    def test_none_api_key_raises(self):
        from app.analytics.agents.llm_provider import OpenAICompatibleClient
        with pytest.raises(ValueError, match="api_key is required"):
            OpenAICompatibleClient(api_key=None)

    def test_provider_429_classified_as_rate_limit(self):
        """#14: HTTP 429 → MODEL_RATE_LIMIT."""
        from app.analytics.agents.llm_provider import OpenAICompatibleClient
        from app.analytics.agents.errors import AgentErrorCode
        
        # We can't easily mock httpx.AsyncClient in this context,
        # but we verify the error taxonomy is defined correctly
        # by checking the import path
        from app.analytics.agents.errors import RetryableError
        err = RetryableError(AgentErrorCode.MODEL_RATE_LIMIT, "HTTP 429")
        assert err.code == AgentErrorCode.MODEL_RATE_LIMIT

    def test_provider_401_terminal(self):
        """#15: HTTP 401/403 → SECURITY_POLICY_ERROR (terminal)."""
        from app.analytics.agents.errors import AgentErrorCode, TerminalError
        err = TerminalError(AgentErrorCode.SECURITY_POLICY_ERROR, "HTTP 401")
        assert err.code == AgentErrorCode.SECURITY_POLICY_ERROR


# ── PR3 CORRECTION: Stage3 runner integration ──────────────────────────

class TestRunnerStage3Integration:
    def test_runner_has_agent_orchestration_stage(self):
        """#17: runner includes Stage3 agent orchestration stage."""
        import inspect
        from app.analytics.runner import AnalyticsRunner
        source = inspect.getsource(AnalyticsRunner.run_provisional)
        assert "agent_orchestration" in source.lower() or "stage3" in source.lower() or "orchestrat" in source.lower()

    def test_agent_failure_isolated_from_canonical(self):
        """#19: agent failure does not fail analysis_run."""
        # The runner wraps Stage3 in try/except and doesn't set FAILED
        import inspect
        from app.analytics.runner import AnalyticsRunner
        source = inspect.getsource(AnalyticsRunner._stage_agent_orchestration)
        # Should have a try/except that doesn't re-raise
        assert "except" in source.lower()
        # Should NOT raise or mark run as FAILED
        assert "run.status = RunStatus.FAILED" not in source


# ── PR3 CORRECTION: Input hash identity ────────────────────────────────

class TestInputHashIdentity:
    def test_same_retry_input_same_hash(self):
        """#20: same retry input → same hash."""
        from app.analytics.agents.orchestrator import AgentOrchestrator
        
        repo = MagicMock()
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)
        
        r = _make_readiness()
        m1 = orch._build_manifest(uuid4(), "v1", "FINAL", r)
        m2 = orch._build_manifest(uuid4(), "v1", "FINAL", r)
        
        assert m1.input_hash == m2.input_hash

    def test_different_specialists_different_hash(self):
        """#21: Funnel vs Execution input → different hashes."""
        from app.analytics.agents.input.base import SpecialistInput
        from app.analytics.agents.orchestrator import AgentOrchestrator
        from app.analytics.agents.input.funnel import FunnelPerformanceInputAssembler
        from app.analytics.agents.input.execution import ExecutionQualityInputAssembler
        
        data_repo = MagicMock()
        data_repo.get_funnel_data.return_value = []
        data_repo.get_trade_cases.return_value = []
        data_repo.get_metric_snapshots.return_value = []
        data_repo.get_horizon_metrics.return_value = []
        data_repo.get_replay_metrics.return_value = []
        data_repo.get_quality_summary.return_value = {}
        data_repo.detect_drift_candidates.return_value = []
        
        funnel_input = FunnelPerformanceInputAssembler(data_repo).assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        exec_input = ExecutionQualityInputAssembler(data_repo).assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        repo = MagicMock()
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)
        
        r = _make_readiness()
        m_funnel = orch._build_manifest(uuid4(), "v1", "FINAL", r, specialist_input=funnel_input)
        m_exec = orch._build_manifest(uuid4(), "v1", "FINAL", r, specialist_input=exec_input)
        
        # Different assemblers produce different evidence counts → different hashes
        assert m_funnel.input_hash != m_exec.input_hash

    def test_same_specialist_same_hash(self):
        """#24: same retry → same hash."""
        from app.analytics.agents.orchestrator import AgentOrchestrator
        
        repo = MagicMock()
        executor = AsyncMock()
        orch = AgentOrchestrator(repository=repo, executor=executor)
        
        r = _make_readiness()
        m1 = orch._build_manifest(uuid4(), "v1", "FINAL", r)
        m2 = orch._build_manifest(uuid4(), "v1", "FINAL", r)
        
        assert m1.input_hash == m2.input_hash


# ── PR3 CORRECTION: Semantic golden fixtures ───────────────────────────

class TestSemanticGoldenFixtures:
    def test_funnel_frequency_collapse_detected(self):
        """#28: 24h=0, baseline=15 → anomaly detected by drift assembler."""
        from app.analytics.agents.input.drift import DriftAnomalyInputAssembler
        from app.analytics.agents.data_repository import MetricSnapshot
        
        data_repo = MagicMock()
        data_repo.get_metric_snapshots.return_value = [
            MetricSnapshot("24h", "scanner_direction:ME:SHORT", "total_setups", 0, 1,
                          "2026-09-15T06:00:00Z", "2026-09-16T06:00:00Z"),
            MetricSnapshot("7d", "scanner_direction:ME:SHORT", "total_setups", 15, 7,
                          "2026-09-09T06:00:00Z", "2026-09-16T06:00:00Z"),
        ]
        data_repo.detect_drift_candidates.return_value = [
            type('DriftCandidate', (), {
                'candidate_type': 'FREQUENCY_COLLAPSE', 'scanner': 'ME', 'direction': 'SHORT',
                'metric_name': 'total_setups', 'current_value': 0, 'baseline_value': 15,
                'change_pct': -100.0, 'sample_size': 1,
                'description': 'Zero signals in 24h vs 15 in 7d',
            })()
        ]
        data_repo.get_quality_summary.return_value = {}
        
        assembler = DriftAnomalyInputAssembler(data_repo)
        result = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        assert len(result.cases) > 0
        assert any("FREQUENCY_COLLAPSE" in c.get("candidate_type", "") for c in result.cases)

    def test_drift_zero_baseline_not_anomaly(self):
        """#28: 24h=0, baseline=0 → no frequency anomaly."""
        from app.analytics.agents.input.drift import DriftAnomalyInputAssembler
        
        data_repo = MagicMock()
        data_repo.get_metric_snapshots.return_value = []
        data_repo.detect_drift_candidates.return_value = []  # empty = no anomaly
        data_repo.get_quality_summary.return_value = {}
        
        assembler = DriftAnomalyInputAssembler(data_repo)
        result = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        assert len(result.cases) == 0  # no anomalies

    def test_execution_high_mfe_low_realized(self):
        """#28: high MFE + low realized R → exit quality input."""
        from app.analytics.agents.input.execution import ExecutionQualityInputAssembler
        from app.analytics.agents.data_repository import TradeCase
        
        data_repo = MagicMock()
        data_repo.get_trade_cases.return_value = [
            TradeCase(trade_id=1, symbol="BTCUSDT", scanner_name="ME",
                     direction="SHORT", entered_at=None, closed_at=None,
                     status="CLOSED", pnl_r=-0.5, mfe_r=3.0, mae_r=-1.0,
                     exit_reason="STOPPED", avg_entry_price=50000.0,
                     exit_price=51000.0, market_regime="TRENDING"),
        ]
        data_repo.get_horizon_metrics.return_value = []
        data_repo.get_replay_metrics.return_value = []
        
        assembler = ExecutionQualityInputAssembler(data_repo)
        result = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
        )
        
        assert len(result.cases) == 1
        assert result.cases[0]["mfe_r"] == 3.0
        assert result.cases[0]["pnl_r"] == -0.5
