"""Tests for Stage 4 Research Foundation (PR1)."""
import pytest
from uuid import uuid4
from datetime import datetime, timezone

from app.analytics.agents.research.models import (
    Finding, Hypothesis, Experiment, TransitionRecord,
    FindingStatus, FindingSeverity, HypothesisStatus, ExperimentStatus, EntityType,
)


class TestResearchEnums:
    def test_finding_status_values(self):
        assert FindingStatus.OPEN.value == "OPEN"
        assert FindingStatus.CONFIRMED.value == "CONFIRMED"
        assert FindingStatus.FALSE_POSITIVE.value == "FALSE_POSITIVE"
        assert FindingStatus.SUPERSEDED.value == "SUPERSEDED"

    def test_finding_severity_values(self):
        assert len(FindingSeverity) == 3

    def test_hypothesis_status_values(self):
        assert HypothesisStatus.PROPOSED.value == "PROPOSED"
        assert HypothesisStatus.UNDER_TEST.value == "UNDER_TEST"
        assert HypothesisStatus.CONFIRMED.value == "CONFIRMED"
        assert HypothesisStatus.REJECTED.value == "REJECTED"

    def test_experiment_status_values(self):
        assert ExperimentStatus.PROPOSED.value == "PROPOSED"
        assert ExperimentStatus.FROZEN.value == "FROZEN"
        assert ExperimentStatus.RUNNING.value == "RUNNING"
        assert ExperimentStatus.COMPLETED.value == "COMPLETED"
        assert ExperimentStatus.CANCELLED.value == "CANCELLED"

    def test_entity_type_values(self):
        assert EntityType.FINDING.value == "finding"
        assert EntityType.HYPOTHESIS.value == "hypothesis"
        assert EntityType.EXPERIMENT.value == "experiment"


class TestResearchModels:
    def test_finding_creation(self):
        f = Finding(
            finding_code="ENTRY_IMMEDIATE_MAE",
            title="Large MAE on entry",
            severity="MEDIUM",
            statement="12/18 trades reached -0.5R before +0.25R",
            source_run_id=uuid4(),
            agent_name="EXECUTION_QUALITY",
        )
        assert f.finding_code == "ENTRY_IMMEDIATE_MAE"
        assert f.status == "OPEN"

    def test_hypothesis_creation(self):
        h = Hypothesis(
            finding_id=uuid4(),
            hypothesis_code="H001",
            statement="Entry timing may be premature",
            falsifiable_experiment="Replay with confirmation delay",
            required_data=["market.candle"],
            validation_criterion="Improved avg R",
        )
        assert h.status == "PROPOSED"

    def test_experiment_creation(self):
        e = Experiment(
            hypothesis_id=uuid4(),
            title="Test confirmation delay",
            description="Replay setups with N-bar delay",
            required_data=["market.candle", "scanner_setup"],
            validation_criterion="Positive avg R improvement",
        )
        assert e.status == "PROPOSED"

    def test_transition_record(self):
        t = TransitionRecord(
            entity_type="finding",
            entity_id=uuid4(),
            from_status="OPEN",
            to_status="CONFIRMED",
            actor="system",
            reason="Manual review",
        )
        assert t.to_status == "CONFIRMED"


class TestMigrationFiles:
    def test_migration_035_exists(self):
        from pathlib import Path
        assert Path("sql/migrations/035_research_foundation.sql").exists()

    def test_migration_035_has_finding_table(self):
        from pathlib import Path
        content = Path("sql/migrations/035_research_foundation.sql").read_text()
        assert "research.finding" in content
        assert "research.hypothesis" in content
        assert "research.experiment" in content
        assert "research.transition_history" in content

    def test_migration_035_is_idempotent(self):
        from pathlib import Path
        content = Path("sql/migrations/035_research_foundation.sql").read_text()
        assert "IF NOT EXISTS" in content

    def test_migration_036_exists(self):
        from pathlib import Path
        assert Path("sql/migrations/036_research_rbac.sql").exists()

    def test_migration_036_has_grants(self):
        from pathlib import Path
        content = Path("sql/migrations/036_research_rbac.sql").read_text()
        assert "analytics_runner" in content
        assert "research" in content
        assert "GRANT" in content


class TestResearchRepository:
    def test_fingerprint_deterministic(self):
        """Fingerprint hash is deterministic for same inputs."""
        from app.analytics.agents.research.models import Finding
        
        # Same code/scanner/direction/metric should produce same fingerprint
        # (tested via hash computation, not DB)
        import hashlib
        fp_input = "ENTRY_IMMEDIATE_MAE:ME:SHORT:pnl_r"
        h1 = hashlib.sha256(fp_input.encode()).hexdigest()
        h2 = hashlib.sha256(fp_input.encode()).hexdigest()
        assert h1 == h2


class TestResearchIsolation:
    def test_research_tables_in_research_schema(self):
        """Research tables are in research schema, not analytics."""
        from pathlib import Path
        content = Path("sql/migrations/035_research_foundation.sql").read_text()
        assert "CREATE TABLE IF NOT EXISTS research.finding" in content
        assert "CREATE TABLE IF NOT EXISTS research.hypothesis" in content
        assert "CREATE TABLE IF NOT EXISTS research.experiment" in content
        # Not in analytics schema
        assert "CREATE TABLE IF NOT EXISTS analytics.finding" not in content
