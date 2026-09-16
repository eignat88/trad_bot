"""Tests for Stage 3 Agent Foundation models."""
import pytest
from datetime import datetime, timezone
from uuid import uuid4

from app.analytics.agents.models import (
    AgentDefinition, AgentRun, AgentInputManifest, AgentResult, DailyTradingReport,
    AgentType, AgentRunStatus, ValidationStatus, ActionClass, ReportStatus,
    ConfidenceLevel, DataQualityStatus,
)


# ---------------------------------------------------------------------------
# Enum value & count tests
# ---------------------------------------------------------------------------

class TestAgentEnums:
    def test_agent_type_values(self):
        assert AgentType.SPECIALIST.value == "SPECIALIST"
        assert AgentType.CHIEF.value == "CHIEF"

    def test_agent_run_status_values(self):
        # All 6 statuses: PENDING, RUNNING, SUCCEEDED, FAILED, SKIPPED, TIMED_OUT
        assert len(AgentRunStatus) == 6

    def test_action_class_values(self):
        # All 6 action classes
        assert len(ActionClass) == 6

    def test_report_status_values(self):
        # DRAFT, VALIDATED, PUBLISHED, SUPERSEDED
        assert len(ReportStatus) == 4

    def test_confidence_level_values(self):
        assert len(ConfidenceLevel) == 3


# ---------------------------------------------------------------------------
# AgentDefinition
# ---------------------------------------------------------------------------

class TestAgentDefinition:
    def test_valid_definition(self):
        d = AgentDefinition(agent_name="funnel_performance", agent_type=AgentType.SPECIALIST)
        assert d.agent_name == "funnel_performance"
        assert d.enabled is True

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="agent_name is required"):
            AgentDefinition(agent_name="", agent_type=AgentType.SPECIALIST)


# ---------------------------------------------------------------------------
# AgentRun
# ---------------------------------------------------------------------------

class TestAgentRun:
    def test_valid_run(self):
        run = AgentRun(analysis_run_id=uuid4(), agent_name="funnel_performance")
        assert run.status == AgentRunStatus.PENDING
        assert run.attempt == 1

    def test_empty_agent_name_raises(self):
        with pytest.raises(ValueError, match="agent_name is required"):
            AgentRun(agent_name="")

    def test_error_message_with_secrets_raises(self):
        with pytest.raises(ValueError, match="error_message must not contain secrets"):
            AgentRun(agent_name="test", error_message="Connection password=xxx failed")

    def test_error_message_with_token_raises(self):
        with pytest.raises(ValueError):
            AgentRun(agent_name="test", error_message="Invalid token in header")

    def test_error_message_without_secrets_ok(self):
        run = AgentRun(agent_name="test", error_message="Timeout after 30s")
        assert run.error_message == "Timeout after 30s"


# ---------------------------------------------------------------------------
# AgentInputManifest
# ---------------------------------------------------------------------------

class TestAgentInputManifest:
    def test_valid_manifest(self):
        m = AgentInputManifest(
            agent_run_id=uuid4(),
            dataset_version="20260915.F.abc123",
            input_hash="sha256hash",
        )
        assert m.schema_version == "v1"
        assert m.evidence_ids == []

    def test_immutability_convention(self):
        """Manifest is immutable by convention (DB triggers enforce at DB level)."""
        m = AgentInputManifest(
            agent_run_id=uuid4(),
            dataset_version="v1",
            input_hash="h",
        )
        # Python-level: can technically set attributes, but DB blocks mutations
        assert m.dataset_version == "v1"


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------

class TestAgentResult:
    def test_valid_result(self):
        r = AgentResult(
            agent_run_id=uuid4(),
            result_json={"summary": "test"},
            result_hash="abc",
        )
        assert r.validation_status == ValidationStatus.PENDING


# ---------------------------------------------------------------------------
# DailyTradingReport
# ---------------------------------------------------------------------------

class TestDailyTradingReport:
    def test_valid_report(self):
        report = DailyTradingReport(
            analysis_run_id=uuid4(),
            executive_summary="Market was mixed",
        )
        assert report.action_class == ActionClass.NO_ACTION
        assert report.partial is False
        assert report.status == ReportStatus.DRAFT

    def test_report_with_missing_agents(self):
        report = DailyTradingReport(
            analysis_run_id=uuid4(),
            partial=True,
            missing_agents=["drift_anomaly"],
            executive_summary="Partial report",
        )
        assert report.partial is True
        assert "drift_anomaly" in report.missing_agents
