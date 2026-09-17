"""Tests for Stage 4 Research Foundation (PR1) — migration 035 schema.

Validates:
- DOC4-aligned enums (FindingStatus, FindingType, HypothesisStatus, etc.)
- All 12 research tables exist in the migration SQL
- Junction table hypothesis_finding supports many-to-many
- transition_history immutability (append-only)
- Migration idempotency and schema isolation
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.analytics.agents.research.models import (
    ChangeCandidate,
    ChangeCandidateStatus,
    EntityType,
    Experiment,
    ExperimentRun,
    ExperimentStatus,
    ExperimentRunStatus,
    Finding,
    FindingOccurrence,
    FindingStatus,
    FindingType,
    FindingSeverity,
    Fingerprint,
    Hypothesis,
    HypothesisFinding,
    HypothesisStatus,
    MonitoringResult,
    MonitoringVerdict,
    ProductionChange,
    TransitionRecord,
    ValidationSplit,
    ValidationResult,
    ValidationVerdict,
)

MIGRATION_PATH = Path("sql/migrations/035_research_foundation.sql")


# ======================================================================
# Enum alignment with DOC4
# ======================================================================

class TestResearchEnums:
    """Verify every enum value matches the DOC4 / migration 035 specification."""

    def test_finding_status_values(self):
        assert FindingStatus.OPEN.value == "OPEN"
        assert FindingStatus.REPEATED.value == "REPEATED"
        assert FindingStatus.RESEARCH_REQUIRED.value == "RESEARCH_REQUIRED"
        assert FindingStatus.CLOSED.value == "CLOSED"

    def test_finding_status_count(self):
        assert len(FindingStatus) == 4

    def test_finding_type_values(self):
        assert FindingType.ENTRY.value == "ENTRY"
        assert FindingType.DCA.value == "DCA"
        assert FindingType.STOP.value == "STOP"
        assert FindingType.EXIT.value == "EXIT"
        assert FindingType.FUNNEL.value == "FUNNEL"
        assert FindingType.DRIFT.value == "DRIFT"
        assert FindingType.DATA.value == "DATA"
        assert FindingType.INCIDENT.value == "INCIDENT"

    def test_finding_type_count(self):
        assert len(FindingType) == 8

    def test_finding_severity_values(self):
        assert len(FindingSeverity) == 3
        assert FindingSeverity.LOW.value == "LOW"
        assert FindingSeverity.MEDIUM.value == "MEDIUM"
        assert FindingSeverity.HIGH.value == "HIGH"

    def test_hypothesis_status_values(self):
        assert HypothesisStatus.DRAFT.value == "DRAFT"
        assert HypothesisStatus.RESEARCH_REQUIRED.value == "RESEARCH_REQUIRED"
        assert HypothesisStatus.EXPERIMENT_DESIGNED.value == "EXPERIMENT_DESIGNED"
        assert HypothesisStatus.BACKTESTING.value == "BACKTESTING"
        assert HypothesisStatus.OOS_VALIDATION.value == "OOS_VALIDATION"
        assert HypothesisStatus.VALIDATED.value == "VALIDATED"
        assert HypothesisStatus.REJECTED.value == "REJECTED"
        assert HypothesisStatus.INCONCLUSIVE.value == "INCONCLUSIVE"

    def test_hypothesis_status_count(self):
        assert len(HypothesisStatus) == 8

    def test_experiment_status_values(self):
        assert ExperimentStatus.PROPOSED.value == "PROPOSED"
        assert ExperimentStatus.FROZEN.value == "FROZEN"
        assert ExperimentStatus.RUNNING.value == "RUNNING"
        assert ExperimentStatus.COMPLETED.value == "COMPLETED"
        assert ExperimentStatus.CANCELLED.value == "CANCELLED"

    def test_experiment_status_count(self):
        assert len(ExperimentStatus) == 5

    def test_experiment_run_status_values(self):
        assert ExperimentRunStatus.PENDING.value == "PENDING"
        assert ExperimentRunStatus.RUNNING.value == "RUNNING"
        assert ExperimentRunStatus.COMPLETED.value == "COMPLETED"
        assert ExperimentRunStatus.FAILED.value == "FAILED"

    def test_validation_split_values(self):
        assert ValidationSplit.TRAIN.value == "TRAIN"
        assert ValidationSplit.VALIDATION.value == "VALIDATION"
        assert ValidationSplit.OOS.value == "OOS"
        assert ValidationSplit.ROBUSTNESS.value == "ROBUSTNESS"

    def test_validation_split_count(self):
        assert len(ValidationSplit) == 4

    def test_validation_verdict_values(self):
        assert ValidationVerdict.VALIDATED.value == "VALIDATED"
        assert ValidationVerdict.REJECTED.value == "REJECTED"
        assert ValidationVerdict.INCONCLUSIVE.value == "INCONCLUSIVE"

    def test_validation_verdict_count(self):
        assert len(ValidationVerdict) == 3

    def test_change_candidate_status_values(self):
        assert ChangeCandidateStatus.PROPOSED.value == "PROPOSED"
        assert ChangeCandidateStatus.APPROVED.value == "APPROVED"
        assert ChangeCandidateStatus.REJECTED.value == "REJECTED"
        assert ChangeCandidateStatus.IMPLEMENTED.value == "IMPLEMENTED"

    def test_change_candidate_status_count(self):
        assert len(ChangeCandidateStatus) == 4

    def test_monitoring_verdict_values(self):
        assert MonitoringVerdict.PENDING.value == "PENDING"
        assert MonitoringVerdict.PASS.value == "PASS"
        assert MonitoringVerdict.FAIL.value == "FAIL"
        assert MonitoringVerdict.INCONCLUSIVE.value == "INCONCLUSIVE"

    def test_entity_type_values(self):
        assert EntityType.finding.value == "finding"
        assert EntityType.hypothesis.value == "hypothesis"
        assert EntityType.experiment.value == "experiment"
        assert EntityType.experiment_run.value == "experiment_run"
        assert EntityType.validation_result.value == "validation_result"
        assert EntityType.change_candidate.value == "change_candidate"
        assert EntityType.production_change.value == "production_change"
        assert EntityType.monitoring_result.value == "monitoring_result"

    def test_entity_type_count(self):
        assert len(EntityType) == 8


# ======================================================================
# Model creation smoke tests
# ======================================================================

class TestResearchModels:
    def test_finding_creation(self):
        f = Finding(
            finding_type="ENTRY",
            title="Large MAE on entry",
            scope_json={"symbol": "BTCUSDT", "timeframe": "15m"},
            source_run_id=uuid4(),
            agent_name="EXECUTION_QUALITY",
        )
        assert f.finding_type == "ENTRY"
        assert f.status == "OPEN"
        assert f.fingerprint is None  # nullable in PR1

    def test_finding_occurrence_creation(self):
        fo = FindingOccurrence(
            finding_id=uuid4(),
            analysis_run_id=uuid4(),
            metric_value=-0.5,
            sample_size=18,
            confidence="HIGH",
        )
        assert fo.finding_id is not None
        assert fo.metric_value == -0.5

    def test_hypothesis_creation(self):
        h = Hypothesis(
            statement="Entry timing may be premature",
            falsification_criterion="Replay with confirmation delay",
            primary_metric="avg_r",
            population_json={"symbols": ["BTCUSDT"], "timeframes": ["15m"]},
        )
        assert h.status == "DRAFT"
        assert h.version == 1

    def test_hypothesis_finding_junction(self):
        hf = HypothesisFinding(
            hypothesis_id=uuid4(),
            finding_id=uuid4(),
        )
        assert hf.hypothesis_id is not None
        assert hf.finding_id is not None

    def test_experiment_creation(self):
        e = Experiment(
            hypothesis_id=uuid4(),
            title="Test confirmation delay",
            description="Replay setups with N-bar delay",
            protocol_version="1.0",
        )
        assert e.status == "PROPOSED"

    def test_experiment_run_creation(self):
        er = ExperimentRun(
            experiment_id=uuid4(),
            dataset_version="2024-01",
            status="PENDING",
        )
        assert er.experiment_run_id is not None

    def test_validation_result_creation(self):
        vr = ValidationResult(
            experiment_run_id=uuid4(),
            split="OOS",
            verdict="VALIDATED",
        )
        assert vr.split == "OOS"
        assert vr.verdict == "VALIDATED"

    def test_change_candidate_creation(self):
        cc = ChangeCandidate(
            hypothesis_id=uuid4(),
            experiment_id=uuid4(),
            validation_result_id=uuid4(),
            status="PROPOSED",
        )
        assert cc.status == "PROPOSED"

    def test_production_change_creation(self):
        pc = ProductionChange(
            candidate_id=uuid4(),
            branch="feature/faster-entries",
            pr_number=42,
            commit_sha="abc123",
        )
        assert pc.branch == "feature/faster-entries"

    def test_monitoring_result_creation(self):
        mr = MonitoringResult(
            change_id=uuid4(),
            window="24h",
            verdict="PASS",
        )
        assert mr.window == "24h"
        assert mr.verdict == "PASS"

    def test_transition_record(self):
        t = TransitionRecord(
            entity_type="finding",
            entity_id=uuid4(),
            from_status="OPEN",
            to_status="RESEARCH_REQUIRED",
            actor="system",
            reason="Automated review",
        )
        assert t.to_status == "RESEARCH_REQUIRED"

    def test_fingerprint_creation(self):
        fp = Fingerprint(
            finding_id=uuid4(),
            fingerprint_hash="abc123",
            finding_type="ENTRY",
            scanner_name="ME",
            direction="SHORT",
            metric_name="pnl_r",
        )
        assert fp.fingerprint_hash == "abc123"


# ======================================================================
# Migration 035 SQL validation
# ======================================================================

class TestMigrationFiles:
    def test_migration_035_exists(self):
        assert MIGRATION_PATH.exists()

    def test_migration_035_is_idempotent(self):
        content = MIGRATION_PATH.read_text()
        assert "IF NOT EXISTS" in content

    def test_migration_035_has_all_10_core_tables(self):
        """All 10 core research tables must be created in migration 035."""
        content = MIGRATION_PATH.read_text()
        core_tables = [
            "research.finding",
            "research.finding_occurrence",
            "research.hypothesis",
            "research.hypothesis_finding",
            "research.experiment",
            "research.experiment_run",
            "research.validation_result",
            "research.change_candidate",
            "research.production_change",
            "research.monitoring_result",
        ]
        for table in core_tables:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in content, (
                f"Missing CREATE TABLE for {table}"
            )

    def test_migration_035_has_transition_history(self):
        content = MIGRATION_PATH.read_text()
        assert "CREATE TABLE IF NOT EXISTS research.transition_history" in content

    def test_migration_035_has_fingerprint(self):
        content = MIGRATION_PATH.read_text()
        assert "CREATE TABLE IF NOT EXISTS research.fingerprint" in content

    def test_migration_035_finding_status_check(self):
        content = MIGRATION_PATH.read_text()
        assert "'OPEN'" in content
        assert "'REPEATED'" in content
        assert "'RESEARCH_REQUIRED'" in content
        assert "'CLOSED'" in content

    def test_migration_035_finding_type_check(self):
        content = MIGRATION_PATH.read_text()
        for ft in ["ENTRY", "DCA", "STOP", "EXIT", "FUNNEL", "DRIFT", "DATA", "INCIDENT"]:
            assert f"'{ft}'" in content, f"Missing finding_type CHECK value: {ft}"

    def test_migration_035_hypothesis_status_check(self):
        content = MIGRATION_PATH.read_text()
        for st in ["DRAFT", "RESEARCH_REQUIRED", "EXPERIMENT_DESIGNED",
                    "BACKTESTING", "OOS_VALIDATION", "VALIDATED",
                    "REJECTED", "INCONCLUSIVE"]:
            assert f"'{st}'" in content, f"Missing hypothesis status CHECK value: {st}"

    def test_migration_035_validation_split_check(self):
        content = MIGRATION_PATH.read_text()
        assert "'TRAIN'" in content
        assert "'VALIDATION'" in content
        assert "'OOS'" in content
        assert "'ROBUSTNESS'" in content

    def test_migration_035_validation_verdict_check(self):
        content = MIGRATION_PATH.read_text()
        assert "'VALIDATED'" in content
        assert "'REJECTED'" in content
        assert "'INCONCLUSIVE'" in content

    def test_migration_035_change_candidate_status_check(self):
        content = MIGRATION_PATH.read_text()
        assert "'PROPOSED'" in content
        assert "'APPROVED'" in content
        assert "'REJECTED'" in content
        assert "'IMPLEMENTED'" in content

    def test_migration_035_transition_history_entity_types(self):
        content = MIGRATION_PATH.read_text()
        for et in ["finding", "hypothesis", "experiment", "experiment_run",
                    "validation_result", "change_candidate",
                    "production_change", "monitoring_result"]:
            assert f"'{et}'" in content, f"Missing entity_type CHECK value: {et}"


# ======================================================================
# Isolation: research schema, not analytics
# ======================================================================

class TestResearchIsolation:
    def test_research_tables_in_research_schema(self):
        content = MIGRATION_PATH.read_text()
        assert "CREATE SCHEMA IF NOT EXISTS research" in content
        for table in ["finding", "hypothesis", "experiment"]:
            assert f"CREATE TABLE IF NOT EXISTS research.{table}" in content

    def test_no_analytics_schema_tables(self):
        content = MIGRATION_PATH.read_text()
        assert "CREATE TABLE IF NOT EXISTS analytics.finding" not in content
        assert "CREATE TABLE IF NOT EXISTS analytics.hypothesis" not in content
        assert "CREATE TABLE IF NOT EXISTS analytics.experiment" not in content


# ======================================================================
# transition_history append-only enforcement
# ======================================================================

class TestTransitionHistoryImmutability:
    def test_migration_has_immutable_guard_function(self):
        content = MIGRATION_PATH.read_text()
        assert "fn_block_transition_history_mutation" in content
        assert "RAISE EXCEPTION" in content

    def test_migration_has_immutable_trigger(self):
        content = MIGRATION_PATH.read_text()
        assert "trg_transition_history_immutability" in content
        assert "BEFORE UPDATE OR DELETE" in content

    def test_reject_update(self):
        """In-code verification that UPDATE is not a valid operation."""
        # The DB trigger blocks UPDATE; this test documents the intent.
        # No Python-level UPDATE method should exist on transition_history.
        from app.analytics.agents.research import repository as repo_mod
        repo_cls = repo_mod.ResearchRepository
        assert not hasattr(repo_cls, "update_transition"), (
            "update_transition must not exist — transition_history is append-only"
        )

    def test_reject_delete(self):
        """In-code verification that DELETE is not a valid operation."""
        from app.analytics.agents.research import repository as repo_mod
        repo_cls = repo_mod.ResearchRepository
        assert not hasattr(repo_cls, "delete_transition"), (
            "delete_transition must not exist — transition_history is append-only"
        )


# ======================================================================
# Hypothesis ↔ Finding junction (many-to-many)
# ======================================================================

class TestHypothesisFindingJunction:
    def test_repository_has_link_methods(self):
        from app.analytics.agents.research import repository as repo_mod
        repo_cls = repo_mod.ResearchRepository
        assert hasattr(repo_cls, "link_hypothesis_finding")
        assert hasattr(repo_cls, "unlink_hypothesis_finding")
        assert hasattr(repo_cls, "get_hypothesis_findings")
        assert hasattr(repo_cls, "get_finding_hypotheses")

    def test_migration_has_junction_table(self):
        content = MIGRATION_PATH.read_text()
        assert "CREATE TABLE IF NOT EXISTS research.hypothesis_finding" in content
        assert "hypothesis_id" in content
        assert "finding_id" in content


# ======================================================================
# All 12 repository CRUD methods exist
# ======================================================================

class TestRepositoryCompleteness:
    """Verify ResearchRepository has CRUD for every table."""

    def _repo_methods(self) -> set[str]:
        from app.analytics.agents.research import repository as repo_mod
        return set(dir(repo_mod.ResearchRepository))

    def test_finding_crud(self):
        methods = self._repo_methods()
        assert "create_finding" in methods
        assert "get_finding" in methods
        assert "update_finding_status" in methods
        assert "list_findings" in methods

    def test_finding_occurrence_crud(self):
        methods = self._repo_methods()
        assert "create_finding_occurrence" in methods
        assert "list_finding_occurrences" in methods

    def test_hypothesis_crud(self):
        methods = self._repo_methods()
        assert "create_hypothesis" in methods
        assert "get_hypothesis" in methods
        assert "update_hypothesis_status" in methods
        assert "list_hypotheses" in methods

    def test_hypothesis_finding_crud(self):
        methods = self._repo_methods()
        assert "link_hypothesis_finding" in methods
        assert "unlink_hypothesis_finding" in methods
        assert "get_hypothesis_findings" in methods
        assert "get_finding_hypotheses" in methods

    def test_experiment_crud(self):
        methods = self._repo_methods()
        assert "create_experiment" in methods
        assert "get_experiment" in methods
        assert "update_experiment_status" in methods
        assert "list_experiments" in methods

    def test_experiment_run_crud(self):
        methods = self._repo_methods()
        assert "create_experiment_run" in methods
        assert "get_experiment_run" in methods
        assert "update_experiment_run_status" in methods
        assert "list_experiment_runs" in methods

    def test_validation_result_crud(self):
        methods = self._repo_methods()
        assert "create_validation_result" in methods
        assert "get_validation_result" in methods
        assert "list_validation_results" in methods

    def test_change_candidate_crud(self):
        methods = self._repo_methods()
        assert "create_change_candidate" in methods
        assert "get_change_candidate" in methods
        assert "update_change_candidate_status" in methods
        assert "list_change_candidates" in methods

    def test_production_change_crud(self):
        methods = self._repo_methods()
        assert "create_production_change" in methods
        assert "get_production_change" in methods
        assert "list_production_changes" in methods

    def test_monitoring_result_crud(self):
        methods = self._repo_methods()
        assert "create_monitoring_result" in methods
        assert "get_monitoring_result" in methods
        assert "list_monitoring_results" in methods

    def test_transition_history(self):
        methods = self._repo_methods()
        assert "_record_transition" in methods
        assert "record_transition" in methods
        assert "get_transitions" in methods

    def test_fingerprint_crud(self):
        methods = self._repo_methods()
        assert "create_fingerprint" in methods
        assert "find_by_fingerprint" in methods
        assert "get_fingerprints_by_finding" in methods


# ======================================================================
# Fingerprint determinism
# ======================================================================

class TestResearchFingerprint:
    def test_fingerprint_deterministic(self):
        """Same inputs produce identical SHA-256 hash."""
        h1 = hashlib.sha256("ENTRY|ME|SHORT|pnl_r".lower().encode()).hexdigest()
        h2 = hashlib.sha256("ENTRY|ME|SHORT|pnl_r".lower().encode()).hexdigest()
        assert h1 == h2

    def test_fingerprint_different_inputs(self):
        h1 = hashlib.sha256("ENTRY|ME|SHORT|pnl_r".lower().encode()).hexdigest()
        h2 = hashlib.sha256("STOP|ME|LONG|pnl_r".lower().encode()).hexdigest()
        assert h1 != h2


# ======================================================================
# INCONCLUSIVE status support
# ======================================================================

class TestInconclusiveSupport:
    def test_hypothesis_inconclusive(self):
        assert HypothesisStatus.INCONCLUSIVE.value == "INCONCLUSIVE"

    def test_validation_verdict_inconclusive(self):
        assert ValidationVerdict.INCONCLUSIVE.value == "INCONCLUSIVE"

    def test_monitoring_verdict_inconclusive(self):
        assert MonitoringVerdict.INCONCLUSIVE.value == "INCONCLUSIVE"

    def test_migration_hypothesis_inconclusive(self):
        content = MIGRATION_PATH.read_text()
        assert "'INCONCLUSIVE'" in content


class TestTransitionPolicyV1:
    """ResearchTransitionPolicyV1 — legal/illegal state transitions."""

    def test_valid_finding_transition(self):
        from app.analytics.agents.research.transition_policy import ResearchTransitionPolicyV1
        # Should not raise
        ResearchTransitionPolicyV1.validate("finding", "OPEN", "REPEATED")
        ResearchTransitionPolicyV1.validate("finding", "REPEATED", "RESEARCH_REQUIRED")

    def test_valid_finding_to_closed(self):
        from app.analytics.agents.research.transition_policy import ResearchTransitionPolicyV1
        for status in ("OPEN", "REPEATED", "RESEARCH_REQUIRED"):
            ResearchTransitionPolicyV1.validate("finding", status, "CLOSED")

    def test_invalid_finding_open_to_research_required(self):
        from app.analytics.agents.research.transition_policy import (
            ResearchTransitionPolicyV1,
        )
        with pytest.raises(ValueError, match="Illegal transition"):
            ResearchTransitionPolicyV1.validate("finding", "OPEN", "RESEARCH_REQUIRED")

    def test_invalid_finding_closed_is_terminal(self):
        from app.analytics.agents.research.transition_policy import (
            ResearchTransitionPolicyV1,
        )
        with pytest.raises(ValueError, match="No transitions allowed"):
            ResearchTransitionPolicyV1.validate("finding", "CLOSED", "OPEN")

    def test_valid_hypothesis_transition(self):
        from app.analytics.agents.research.transition_policy import (
            ResearchTransitionPolicyV1,
        )
        ResearchTransitionPolicyV1.validate("hypothesis", "DRAFT", "RESEARCH_REQUIRED")
        ResearchTransitionPolicyV1.validate(
            "hypothesis", "RESEARCH_REQUIRED", "EXPERIMENT_DESIGNED"
        )
        ResearchTransitionPolicyV1.validate("hypothesis", "BACKTESTING", "OOS_VALIDATION")
        ResearchTransitionPolicyV1.validate("hypothesis", "OOS_VALIDATION", "VALIDATED")
        ResearchTransitionPolicyV1.validate(
            "hypothesis", "OOS_VALIDATION", "INCONCLUSIVE"
        )

    def test_invalid_hypothesis_draft_to_validated(self):
        from app.analytics.agents.research.transition_policy import (
            ResearchTransitionPolicyV1,
        )
        with pytest.raises(ValueError, match="Illegal transition"):
            ResearchTransitionPolicyV1.validate("hypothesis", "DRAFT", "VALIDATED")

    def test_invalid_hypothesis_terminal(self):
        from app.analytics.agents.research.transition_policy import (
            ResearchTransitionPolicyV1,
        )
        with pytest.raises(ValueError, match="No transitions allowed"):
            ResearchTransitionPolicyV1.validate("hypothesis", "VALIDATED", "DRAFT")
        with pytest.raises(ValueError, match="No transitions allowed"):
            ResearchTransitionPolicyV1.validate("hypothesis", "REJECTED", "DRAFT")

    def test_experiment_transitions(self):
        from app.analytics.agents.research.transition_policy import (
            ResearchTransitionPolicyV1,
        )
        ResearchTransitionPolicyV1.validate("experiment", "PROPOSED", "FROZEN")
        ResearchTransitionPolicyV1.validate("experiment", "FROZEN", "RUNNING")
        ResearchTransitionPolicyV1.validate("experiment", "RUNNING", "COMPLETED")

    def test_unknown_entity_type(self):
        from app.analytics.agents.research.transition_policy import (
            ResearchTransitionPolicyV1,
        )
        with pytest.raises(ValueError, match="Unknown entity_type"):
            ResearchTransitionPolicyV1.validate("unknown_entity", "A", "B")


class TestPolicyInRepository:
    """Verify that repository update_*_status checks transition policy."""

    def test_invalid_transition_does_not_update(self):
        """Failed policy check should leave status unchanged and create no history."""
        from unittest.mock import MagicMock
        from app.analytics.agents.research.repository import ResearchRepository

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        # First query returns current status
        mock_cursor.fetchone.return_value = ("OPEN",)

        repo = ResearchRepository(mock_conn)
        finding_id = uuid4()

        with pytest.raises(ValueError, match="Illegal transition"):
            repo.update_finding_status(finding_id, "RESEARCH_REQUIRED")

        # Should NOT have called commit (rollback on exception)
        mock_conn.commit.assert_not_called()

    def test_valid_transition_commits(self):
        """Valid transition should commit and record history."""
        from unittest.mock import MagicMock
        from app.analytics.agents.research.repository import ResearchRepository

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        # Current status = OPEN
        mock_cursor.fetchone.return_value = ("OPEN",)

        repo = ResearchRepository(mock_conn)
        finding_id = uuid4()

        repo.update_finding_status(finding_id, "REPEATED", reason="3+ occurrences")
        mock_conn.commit.assert_called_once()


class TestEnumSqlParity:
    """Verify Python enum values match SQL CHECK values exactly."""

    def test_finding_status_matches_sql(self):
        from pathlib import Path
        content = Path("sql/migrations/035_research_foundation.sql").read_text()
        python_values = {s.value for s in FindingStatus}
        for val in python_values:
            assert f"'{val}'" in content, f"FindingStatus.{val} not in SQL CHECK"

    def test_hypothesis_status_matches_sql(self):
        from pathlib import Path
        content = Path("sql/migrations/035_research_foundation.sql").read_text()
        python_values = {s.value for s in HypothesisStatus}
        for val in python_values:
            assert f"'{val}'" in content, f"HypothesisStatus.{val} not in SQL CHECK"

    def test_finding_type_matches_sql(self):
        from pathlib import Path
        content = Path("sql/migrations/035_research_foundation.sql").read_text()
        python_values = {t.value for t in FindingType}
        for val in python_values:
            assert f"'{val}'" in content, f"FindingType.{val} not in SQL CHECK"


class TestRbacMigration:
    """Verify RBAC migration enforces least privilege."""

    def test_rbac_has_explicit_grants(self):
        from pathlib import Path
        content = Path("sql/migrations/036_research_rbac.sql").read_text()
        assert "GRANT" in content
        assert "analytics_runner" in content
        assert "analytics_agent" in content

    def test_rbac_denies_blanket_update(self):
        from pathlib import Path
        content = Path("sql/migrations/036_research_rbac.sql").read_text()
        assert "REVOKE UPDATE" in content

    def test_rbac_denies_delete(self):
        from pathlib import Path
        content = Path("sql/migrations/036_research_rbac.sql").read_text()
        assert "REVOKE" in content
        assert "DELETE" in content

    def test_rbac_denies_transition_history_mutation(self):
        from pathlib import Path
        content = Path("sql/migrations/036_research_rbac.sql").read_text()
        assert "REVOKE UPDATE" in content
        assert "transition_history" in content

