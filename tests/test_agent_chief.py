"""Tests for Stage 3 PR4 Chief Trading Analyst and Daily Reporting."""
import asyncio
import json
import pytest
from uuid import uuid4
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

from app.analytics.agents.models import (
    AgentDefinition, AgentType, AgentRunStatus, AgentInputManifest,
    AgentResult, DailyTradingReport, ActionClass, ReportStatus,
    ValidationStatus, ConfidenceLevel,
)
from app.analytics.agents.executor import AgentExecutor, AgentExecutionResult
from app.analytics.agents.errors import AgentErrorCode
from app.analytics.agents.registry import SPECIALIST_REGISTRY
from app.analytics.agents.llm_client import ModelResponse
from app.analytics.agents.readiness import DataReadinessResult
from app.analytics.agents.chief_policy import ChiefEligibility, check_chief_eligibility
from app.analytics.agents.evidence import EvidenceCatalog, build_metric_id


# ── Helpers ───────────────────────────────────────────────────────────

def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_success_result_json():
    """Golden Chief output — healthy system."""
    return {
        "executive_summary": "All three analytical domains show stable behavior within normal parameters.",
        "findings": [
            {
                "section": "WHAT_HAPPENED",
                "observation_code": "STABLE_FUNNEL",
                "statement": "Signal funnel conversion rates stable across 24h/7d/30d windows.",
                "evidence_refs": ["metric:scanner:ME:SHORT:24h:win_rate"],
                "sample_size": 18,
                "confidence": "MEDIUM",
            }
        ],
        "hypotheses": [
            {
                "hypothesis": "Market conditions remain consistent with recent regime",
                "falsifiable_experiment": "Check regime change indicators in 7d window",
                "required_data": ["market.candle 7d"],
                "validation_criterion": "Regime distribution stable within 20%",
            }
        ],
        "proposed_experiments": [
            {
                "experiment": "Monitor regime indicators for 24h",
                "required_data": ["metric_snapshot 24h regime"],
                "validation_criterion": "No significant regime shift",
            }
        ],
        "action_class": "MONITOR",
        "limitations": ["Single day analysis"],
        "evidence_refs": ["metric:scanner:ME:SHORT:24h:win_rate"],
        "partial": False,
        "missing_agents": [],
    }


def _make_partially_missing_specialists():
    """Specialists with one missing."""
    from app.analytics.agents.executor import AgentExecutionResult
    from app.analytics.agents.models import AgentRunStatus, AgentResult, ValidationStatus
    from app.analytics.agents.llm_client import ModelResponse

    success = AgentExecutionResult(
        status=AgentRunStatus.SUCCEEDED,
        result=AgentResult(
            agent_run_id=uuid4(),
            result_json={"summary": "ok", "observations": [], "hypotheses": [],
                         "proposed_experiments": [], "anomalies": [], "confidence": "MEDIUM",
                         "limitations": [], "evidence_refs": ["metric:test:24h:win_rate"]},
            result_hash="abc",
            validation_status=ValidationStatus.VALID,
        ),
        model_response=ModelResponse(
            content="{}", parsed_json={}, model="test",
            input_tokens=100, output_tokens=200, total_tokens=300,
            latency_ms=500, finish_reason="stop",
        ),
    )
    failed = AgentExecutionResult(
        status=AgentRunStatus.FAILED,
        result=None,
        error_code=AgentErrorCode.MODEL_TIMEOUT,
        error_message="timeout",
    )
    return {
        "FUNNEL_AND_PERFORMANCE": success,
        "EXECUTION_QUALITY": failed,
        "DRIFT_AND_ANOMALY": success,
    }


def _make_readiness(**overrides):
    defaults = dict(
        ready=True, dataset_state="READY", quality_status="PASS",
        dataset_version="v1", maturity="FINAL",
        blockers=(), limitations=["Single day analysis"],
        analysis_window_from=datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc),
        analysis_window_to=datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return DataReadinessResult(**defaults)


# ── Chief Input Assembler Tests ───────────────────────────────────────

class TestChiefInputAssembler:
    def test_assembles_full_specialists(self):
        from app.analytics.agents.input.chief import ChiefInputAssembler

        success_result = AgentExecutionResult(
            status=AgentRunStatus.SUCCEEDED,
            result=AgentResult(
                agent_run_id=uuid4(),
                result_json={"summary": "ok", "observations": [], "hypotheses": [],
                             "proposed_experiments": [], "anomalies": [], "confidence": "MEDIUM",
                             "limitations": [], "evidence_refs": ["metric:test:24h:pnl_r"]},
                result_hash="hash1", validation_status=ValidationStatus.VALID,
            ),
            model_response=ModelResponse(content="{}", parsed_json={}, model="test",
                                          input_tokens=100, output_tokens=200, total_tokens=300,
                                          latency_ms=500, finish_reason="stop"),
        )
        specialist_results = {
            "FUNNEL_AND_PERFORMANCE": success_result,
            "EXECUTION_QUALITY": success_result,
            "DRIFT_AND_ANOMALY": success_result,
        }
        eligibility = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SUCCEEDED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })

        assembler = ChiefInputAssembler()
        inp = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=["test"],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
            specialist_results=specialist_results,
            chief_eligibility=eligibility,
        )

        assert inp.maturity == "FINAL"
        assert len(inp.specialists) == 3
        assert inp.missing_agents == []
        assert len(inp.aggregate_evidence_refs) > 0

    def test_assembles_partial_specialists(self):
        from app.analytics.agents.input.chief import ChiefInputAssembler

        specialist_results = _make_partially_missing_specialists()
        eligibility = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "FAILED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })

        assembler = ChiefInputAssembler()
        inp = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
            specialist_results=specialist_results,
            chief_eligibility=eligibility,
        )

        assert len(inp.missing_agents) == 1
        assert "EXECUTION_QUALITY" in inp.missing_agents

    def test_unavailable_specialist_in_output(self):
        from app.analytics.agents.input.chief import ChiefInputAssembler

        specialist_results = _make_partially_missing_specialists()
        eligibility = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "FAILED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })

        assembler = ChiefInputAssembler()
        inp = assembler.assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
            specialist_results=specialist_results,
            chief_eligibility=eligibility,
        )

        exec_spec = next(s for s in inp.specialists if s["agent_name"] == "EXECUTION_QUALITY")
        assert exec_spec["status"] in ("FAILED", "UNAVAILABLE")
        assert exec_spec["output"] is None


# ── Chief Prompt Tests ────────────────────────────────────────────────

class TestChiefPrompt:
    def test_prompt_prohibits_production_mutation(self):
        from app.analytics.agents.prompts.chief_trading_analyst_v1 import SYSTEM_PROMPT
        assert "not authorized to modify" in SYSTEM_PROMPT.lower()
        assert "not authorized to recommend" in SYSTEM_PROMPT.lower()

    def test_prompt_prohibits_sql(self):
        from app.analytics.agents.prompts.chief_trading_analyst_v1 import SYSTEM_PROMPT
        assert "SQL" in SYSTEM_PROMPT

    def test_prompt_prohibits_chain_of_thought(self):
        from app.analytics.agents.prompts.chief_trading_analyst_v1 import SYSTEM_PROMPT
        assert "chain-of-thought" in SYSTEM_PROMPT.lower() or "chain of thought" in SYSTEM_PROMPT.lower()

    def test_build_chief_prompt_returns_correct_shape(self):
        from app.analytics.agents.input.chief import ChiefInputAssembler
        from app.analytics.agents.prompts.chief_trading_analyst_v1 import build_chief_prompt

        inp = ChiefInputAssembler().assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
            specialist_results={}, chief_eligibility=ChiefEligibility(
                eligible=True, partial=False, available_agents=(),
                missing_agents=(), blockers=(),
            ),
        )
        prompt = build_chief_prompt(inp)
        assert "system_prompt" in prompt
        assert "input_json" in prompt
        assert prompt["input_json"]["maturity"] == "FINAL"
        assert "specialist_results" in prompt["input_json"]


# ── Action Policy Tests ───────────────────────────────────────────────

class TestActionPolicyV1:
    def test_valid_no_action(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="NO_ACTION", findings=[], hypotheses=[],
            proposed_experiments=[], missing_agents=[],
            data_quality_status="PASS", limitations=[], anomalies=[],
        )
        assert result.valid is True

    def test_no_action_with_anomaly_invalid(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="NO_ACTION", findings=[], hypotheses=[],
            proposed_experiments=[], missing_agents=[],
            data_quality_status="PASS", limitations=[],
            anomalies=[{"code": "STRUCTURAL_ISSUE", "severity": "HIGH", "entities": []}],
        )
        assert result.valid is False
        assert len(result.violations) > 0

    def test_no_action_with_missing_agents_invalid(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="NO_ACTION", findings=[], hypotheses=[],
            proposed_experiments=[], missing_agents=["EXECUTION_QUALITY"],
            data_quality_status="PASS", limitations=[],
        )
        assert result.valid is False

    def test_production_incident_requires_evidence(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="PRODUCTION_INCIDENT", findings=[], hypotheses=[],
            proposed_experiments=[], missing_agents=[],
            data_quality_status="PASS", limitations=[],
        )
        assert result.valid is False
        assert "incident evidence" in result.violations[0].lower()

    def test_production_incident_with_evidence_valid(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="PRODUCTION_INCIDENT",
            findings=[{"section": "WHY", "observation_code": "X", "statement": "lifecycle corruption detected",
                       "evidence_refs": [], "sample_size": 1, "confidence": "HIGH"}],
            hypotheses=[], proposed_experiments=[], missing_agents=[],
            data_quality_status="PASS", limitations=[],
        )
        assert result.valid is True

    def test_backtest_requires_experiment(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="BACKTEST", findings=[], hypotheses=[],
            proposed_experiments=[], missing_agents=[],
            data_quality_status="PASS", limitations=[],
        )
        assert result.valid is False
        assert "proposed_experiment" in result.violations[0].lower()

    def test_backtest_with_experiment_valid(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="BACKTEST", findings=[], hypotheses=[],
            proposed_experiments=[{"experiment": "test", "required_data": [], "validation_criterion": "ok"}],
            missing_agents=[], data_quality_status="PASS", limitations=[],
        )
        assert result.valid is True

    def test_data_fix_requires_quality_evidence(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="DATA_FIX", findings=[], hypotheses=[],
            proposed_experiments=[], missing_agents=[],
            data_quality_status="PASS", limitations=["random limitation"],
        )
        assert result.valid is False

    def test_data_fix_with_quality_evidence_valid(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="DATA_FIX", findings=[], hypotheses=[],
            proposed_experiments=[], missing_agents=[],
            data_quality_status="DEGRADED", limitations=[],
        )
        assert result.valid is True

    def test_invalid_action_class(self):
        from app.analytics.agents.policies.action_v1 import ActionPolicyV1
        result = ActionPolicyV1.validate(
            action_class="CHANGE_CONFIG", findings=[], hypotheses=[],
            proposed_experiments=[], missing_agents=[],
            data_quality_status="PASS", limitations=[],
        )
        assert result.valid is False
        assert "Invalid" in result.violations[0]


# ── Confidence Ceiling Tests ──────────────────────────────────────────

class TestChiefConfidenceCeiling:
    def test_high_allowed(self):
        from app.analytics.agents.orchestrator import AgentOrchestrator
        from app.analytics.agents.models import ConfidenceLevel

        inp = MagicMock()
        inp.maturity = "FINAL"
        inp.data_quality_status = "PASS"
        inp.limitations = []
        inp.specialists = [
            {"status": "SUCCEEDED", "output": {"confidence": "MEDIUM"}},
            {"status": "SUCCEEDED", "output": {"confidence": "HIGH"}},
            {"status": "SUCCEEDED", "output": {"confidence": "MEDIUM"}},
        ]
        eligibility = ChiefEligibility(
            eligible=True, partial=False,
            available_agents=("a", "b", "c"), missing_agents=(), blockers=(),
        )

        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        ceiling = orch._compute_chief_confidence_ceiling(inp, eligibility)
        assert ceiling == ConfidenceLevel.HIGH

    def test_partial_forces_low(self):
        from app.analytics.agents.orchestrator import AgentOrchestrator
        from app.analytics.agents.models import ConfidenceLevel

        inp = MagicMock()
        inp.maturity = "FINAL"
        inp.data_quality_status = "PASS"
        inp.limitations = []
        inp.specialists = [{"status": "SUCCEEDED", "output": {"confidence": "HIGH"}}]
        eligibility = ChiefEligibility(
            eligible=True, partial=True,
            available_agents=("a",), missing_agents=("b",), blockers=(),
        )

        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        ceiling = orch._compute_chief_confidence_ceiling(inp, eligibility)
        assert ceiling == ConfidenceLevel.LOW

    def test_degraded_forces_low(self):
        from app.analytics.agents.orchestrator import AgentOrchestrator
        from app.analytics.agents.models import ConfidenceLevel

        inp = MagicMock()
        inp.maturity = "FINAL"
        inp.data_quality_status = "DEGRADED"
        inp.limitations = []
        inp.specialists = [{"status": "SUCCEEDED", "output": {"confidence": "HIGH"}}]
        eligibility = ChiefEligibility(
            eligible=True, partial=False,
            available_agents=("a",), missing_agents=(), blockers=(),
        )

        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        ceiling = orch._compute_chief_confidence_ceiling(inp, eligibility)
        assert ceiling == ConfidenceLevel.LOW

    def test_provisional_forces_low(self):
        from app.analytics.agents.orchestrator import AgentOrchestrator
        from app.analytics.agents.models import ConfidenceLevel

        inp = MagicMock()
        inp.maturity = "PROVISIONAL"
        inp.data_quality_status = "PASS"
        inp.limitations = []
        inp.specialists = [{"status": "SUCCEEDED", "output": {"confidence": "HIGH"}}]
        eligibility = ChiefEligibility(
            eligible=True, partial=False,
            available_agents=("a",), missing_agents=(), blockers=(),
        )

        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        ceiling = orch._compute_chief_confidence_ceiling(inp, eligibility)
        assert ceiling == ConfidenceLevel.LOW

    def test_one_specialist_low_ceiling(self):
        from app.analytics.agents.orchestrator import AgentOrchestrator
        from app.analytics.agents.models import ConfidenceLevel

        inp = MagicMock()
        inp.maturity = "FINAL"
        inp.data_quality_status = "PASS"
        inp.limitations = []
        inp.specialists = [{"status": "SUCCEEDED", "output": {"confidence": "MEDIUM"}}]
        eligibility = ChiefEligibility(
            eligible=True, partial=True,
            available_agents=("a",), missing_agents=("b", "c"), blockers=(),
        )

        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        ceiling = orch._compute_chief_confidence_ceiling(inp, eligibility)
        assert ceiling == ConfidenceLevel.LOW


# ── Report Renderer Tests ─────────────────────────────────────────────

class TestReportRenderer:
    def test_renderer_produces_output(self):
        from app.analytics.agents.reporting.renderer import render_daily_report
        from uuid import uuid4

        report = render_daily_report(
            report_id=uuid4(), analysis_run_id=uuid4(),
            business_date="2026-09-15", maturity="FINAL",
            executive_summary="All stable.",
            findings=[], hypotheses=[], proposed_experiments=[],
            action_class="MONITOR", limitations=[], evidence_refs=[],
            partial=False, missing_agents=[],
        )

        assert "DAILY TRADING ANALYTICS REPORT" in report
        assert "FINAL" in report
        assert "END OF REPORT" in report

    def test_renderer_partial_label(self):
        from app.analytics.agents.reporting.renderer import render_daily_report
        from uuid import uuid4

        report = render_daily_report(
            report_id=uuid4(), analysis_run_id=uuid4(),
            business_date="2026-09-15", maturity="PROVISIONAL",
            executive_summary="Preliminary.", findings=[], hypotheses=[],
            proposed_experiments=[], action_class="NO_ACTION",
            limitations=[], evidence_refs=[], partial=True,
            missing_agents=["EXECUTION_QUALITY"],
        )

        assert "PROVISIONAL" in report
        assert "PARTIAL" in report
        assert "EXECUTION_QUALITY" in report

    def test_renderer_deterministic(self):
        from app.analytics.agents.reporting.renderer import render_daily_report
        from uuid import uuid4

        rid = uuid4()
        aid = uuid4()
        kwargs = dict(
            report_id=rid, analysis_run_id=aid,
            business_date="2026-09-15", maturity="FINAL",
            executive_summary="Test.", findings=[], hypotheses=[],
            proposed_experiments=[], action_class="NO_ACTION",
            limitations=[], evidence_refs=[], partial=False, missing_agents=[],
        )
        r1 = render_daily_report(**kwargs)
        r2 = render_daily_report(**kwargs)
        assert r1 == r2

    def test_renderer_includes_findings(self):
        from app.analytics.agents.reporting.renderer import render_daily_report
        from uuid import uuid4

        report = render_daily_report(
            report_id=uuid4(), analysis_run_id=uuid4(),
            business_date="2026-09-15", maturity="FINAL",
            executive_summary="Summary.",
            findings=[{
                "section": "WHAT_HAPPENED",
                "observation_code": "TEST",
                "statement": "Something happened.",
                "evidence_refs": ["metric:test:24h:pnl_r"],
                "sample_size": 10,
                "confidence": "MEDIUM",
            }],
            hypotheses=[], proposed_experiments=[],
            action_class="MONITOR", limitations=[], evidence_refs=[],
            partial=False, missing_agents=[],
        )

        assert "WHAT_HAPPENED" in report
        assert "Something happened." in report
        assert "KEY FINDINGS" in report

    def test_renderer_includes_missing_agents(self):
        from app.analytics.agents.reporting.renderer import render_daily_report
        from uuid import uuid4

        report = render_daily_report(
            report_id=uuid4(), analysis_run_id=uuid4(),
            business_date="2026-09-15", maturity="FINAL",
            executive_summary="Partial.",
            findings=[], hypotheses=[], proposed_experiments=[],
            action_class="MONITOR", limitations=[], evidence_refs=[],
            partial=True, missing_agents=["DRIFT_AND_ANOMALY"],
        )

        assert "UNAVAILABLE" in report
        assert "DRIFT_AND_ANOMALY" in report


# ── Chief Orchestrator Integration Tests ──────────────────────────────

class TestChiefOrchestratorIntegration:
    def test_chief_eligibility_full(self):
        """All 3 specialists → full report."""
        eligibility = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SUCCEEDED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        assert eligibility.eligible is True
        assert eligibility.partial is False

    def test_chief_eligibility_partial(self):
        """1 failed → partial."""
        eligibility = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "FAILED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        assert eligibility.eligible is True
        assert eligibility.partial is True

    def test_chief_eligibility_not_eligible(self):
        """2+ failed → not eligible."""
        eligibility = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "FAILED",
            "EXECUTION_QUALITY": "FAILED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        assert eligibility.eligible is False

    def test_chief_evidence_from_specialists(self):
        """Chief evidence is union of specialist evidence."""
        from app.analytics.agents.input.chief import ChiefInputAssembler

        success = AgentExecutionResult(
            status=AgentRunStatus.SUCCEEDED,
            result=AgentResult(
                agent_run_id=uuid4(),
                result_json={"summary": "", "observations": [], "hypotheses": [],
                             "proposed_experiments": [], "anomalies": [], "confidence": "LOW",
                             "limitations": [], "evidence_refs": ["metric:a:24h:x", "metric:b:24h:y"]},
                result_hash="h1", validation_status=ValidationStatus.VALID,
            ),
            model_response=ModelResponse(content="{}", parsed_json={}, model="t",
                                          input_tokens=100, output_tokens=200, total_tokens=300,
                                          latency_ms=500, finish_reason="stop"),
        )
        results = {
            "FUNNEL_AND_PERFORMANCE": success,
            "EXECUTION_QUALITY": success,
            "DRIFT_AND_ANOMALY": success,
        }
        eligibility = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SUCCEEDED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })

        inp = ChiefInputAssembler().assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
            specialist_results=results,
            chief_eligibility=eligibility,
        )

        # Evidence should be deduplicated from all 3 specialists
        assert len(inp.aggregate_evidence_refs) == 2  # "metric:a:24h:x" and "metric:b:24h:y"

    def test_chief_specialist_result_hashes_in_manifest(self):
        """Chief input includes specialist result hashes for forensic audit."""
        from app.analytics.agents.input.chief import ChiefInputAssembler

        success = AgentExecutionResult(
            status=AgentRunStatus.SUCCEEDED,
            result=AgentResult(
                agent_run_id=uuid4(),
                result_json={"summary": "", "observations": [], "hypotheses": [],
                             "proposed_experiments": [], "anomalies": [], "confidence": "LOW",
                             "limitations": [], "evidence_refs": []},
                result_hash="hash_abc123",
                validation_status=ValidationStatus.VALID,
            ),
            model_response=ModelResponse(content="{}", parsed_json={}, model="t",
                                          input_tokens=100, output_tokens=200, total_tokens=300,
                                          latency_ms=500, finish_reason="stop"),
        )
        results = {
            "FUNNEL_AND_PERFORMANCE": success,
            "EXECUTION_QUALITY": success,
            "DRIFT_AND_ANOMALY": success,
        }
        eligibility = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SUCCEEDED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })

        inp = ChiefInputAssembler().assemble(
            run_id=uuid4(), dataset_version="v1", maturity="FINAL",
            quality_status="PASS", limitations=[],
            analysis_window_from="2026-09-15T06:00:00Z",
            analysis_window_to="2026-09-16T06:00:00Z",
            specialist_results=results,
            chief_eligibility=eligibility,
        )

        assert len(inp.specialist_result_hashes) == 3
        assert all(h == "hash_abc123" for h in inp.specialist_result_hashes.values())

    def test_chief_validation_rejects_unknown_evidence(self):
        """Chief output with unknown evidence is rejected."""
        from app.analytics.agents.orchestrator import AgentOrchestrator
        from app.analytics.agents.input.chief import ChiefInput

        orch = AgentOrchestrator.__new__(AgentOrchestrator)

        chief_input = MagicMock()
        chief_input.aggregate_evidence_refs = ["metric:known:24h:x"]
        chief_input.limitations = []
        chief_input.data_quality_status = "PASS"

        result = AgentExecutionResult(
            status=AgentRunStatus.SUCCEEDED,
            result=AgentResult(
                agent_run_id=uuid4(),
                result_json={
                    "executive_summary": "test", "findings": [], "hypotheses": [],
                    "proposed_experiments": [], "action_class": "NO_ACTION",
                    "limitations": [], "evidence_refs": ["metric:fake:unknown"],
                    "partial": False, "missing_agents": [],
                },
                result_hash="x", validation_status=ValidationStatus.VALID,
            ),
            model_response=ModelResponse(content="{}", parsed_json={}, model="t",
                                          input_tokens=100, output_tokens=200, total_tokens=300,
                                          latency_ms=500, finish_reason="stop"),
        )
        eligibility = ChiefEligibility(
            eligible=True, partial=False,
            available_agents=(), missing_agents=(), blockers=(),
        )

        error = orch._validate_chief_output(result, chief_input, eligibility)
        assert error is not None
        assert "unknown evidence" in error.lower()


# ── Daily Report Supersession Test ────────────────────────────────────

class TestReportSupersession:
    def test_report_status_lifecycle(self):
        report = DailyTradingReport(
            report_id=uuid4(), analysis_run_id=uuid4(),
            executive_summary="test", maturity="PROVISIONAL",
        )
        assert report.status == ReportStatus.DRAFT
        assert report.report_version == 1

    def test_action_class_values(self):
        assert len(ActionClass) == 6
        assert ActionClass.NO_ACTION.value == "NO_ACTION"
        assert ActionClass.PRODUCTION_INCIDENT.value == "PRODUCTION_INCIDENT"
