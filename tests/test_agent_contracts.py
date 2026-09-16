"""Tests for Stage 3 Agent JSON Schema contracts."""
import json
import pytest
from pathlib import Path
from uuid import uuid4

from app.analytics.agents.contracts.schema_validator import (
    validate_input, validate_specialist_output,
    validate_chief_input, validate_chief_output,
)


# ---------------------------------------------------------------------------
# Valid fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_agent_input():
    return {
        "run_id": str(uuid4()),
        "dataset_version": "20260915.F.abc123",
        "analysis_window": {
            "from": "2026-09-15T06:00:00Z",
            "to": "2026-09-16T06:00:00Z",
        },
        "maturity": "FINAL",
        "data_quality": {"status": "PASS", "limitations": []},
        "sample_sizes": {"total_trades": 25},
        "metrics": {
            "pnl_r": {"value": 3.5, "units": "R", "metric_version": "1.0", "population": 25}
        },
        "segments": [],
        "cases": [],
        "evidence_catalog": ["metric:scanner:ME:SHORT:24h:pnl_r"],
    }


@pytest.fixture
def valid_specialist_output():
    return {
        "summary": "Short signals showed positive expectancy in 24h window",
        "observations": [
            {
                "observation_code": "SHORT_POSITIVE_EXPECTANCY",
                "scope": {"scanner": "ME", "direction": "SHORT"},
                "statement": "Short signals returned positive avg R in 24h",
                "metric_refs": ["metric:scanner:ME:SHORT:24h:pnl_r"],
                "sample_size": 17,
                "confidence": "MEDIUM",
                "evidence_refs": ["metric:scanner:ME:SHORT:24h:pnl_r"],
            }
        ],
        "hypotheses": [
            {
                "hypothesis": "Market regime shift favored shorts",
                "evidence_refs": ["metric:scanner:ME:SHORT:24h:pnl_r"],
                "confidence": "LOW",
                "proposed_experiment": "Compare 7d window for persistence",
            }
        ],
        "proposed_experiments": [
            {
                "experiment": "Replay short signals on 7d window",
                "required_data": ["market.candle 7d"],
                "validation_criterion": "Positive avg R maintained",
            }
        ],
        "anomalies": [],
        "confidence": "MEDIUM",
        "limitations": ["Single day observation"],
        "evidence_refs": ["metric:scanner:ME:SHORT:24h:pnl_r"],
    }


# ---------------------------------------------------------------------------
# Agent Input Schema Tests
# ---------------------------------------------------------------------------

class TestAgentInputSchema:
    def test_valid_input(self, valid_agent_input):
        errors = validate_input(valid_agent_input)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_missing_run_id(self, valid_agent_input):
        del valid_agent_input["run_id"]
        errors = validate_input(valid_agent_input)
        assert len(errors) > 0
        assert any("run_id" in e for e in errors)

    def test_extra_field_rejected(self, valid_agent_input):
        valid_agent_input["unknown_field"] = "test"
        errors = validate_input(valid_agent_input)
        assert len(errors) > 0

    def test_wrong_maturity(self, valid_agent_input):
        valid_agent_input["maturity"] = "INVALID"
        errors = validate_input(valid_agent_input)
        assert len(errors) > 0

    def test_wrong_type(self, valid_agent_input):
        valid_agent_input["sample_sizes"] = "not_a_dict"
        errors = validate_input(valid_agent_input)
        assert len(errors) > 0


# ---------------------------------------------------------------------------
# Specialist Output Schema Tests
# ---------------------------------------------------------------------------

class TestSpecialistOutputSchema:
    def test_valid_output(self, valid_specialist_output):
        errors = validate_specialist_output(valid_specialist_output)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_missing_summary(self, valid_specialist_output):
        del valid_specialist_output["summary"]
        errors = validate_specialist_output(valid_specialist_output)
        assert len(errors) > 0

    def test_observation_missing_sample_size(self, valid_specialist_output):
        del valid_specialist_output["observations"][0]["sample_size"]
        errors = validate_specialist_output(valid_specialist_output)
        assert len(errors) > 0

    def test_wrong_confidence_level(self, valid_specialist_output):
        valid_specialist_output["confidence"] = "VERY_HIGH"
        errors = validate_specialist_output(valid_specialist_output)
        assert len(errors) > 0

    def test_extra_field_rejected(self, valid_specialist_output):
        valid_specialist_output["hallucinated_field"] = True
        errors = validate_specialist_output(valid_specialist_output)
        assert len(errors) > 0


# ---------------------------------------------------------------------------
# Chief Input Schema Tests
# ---------------------------------------------------------------------------

class TestChiefInputSchema:
    def test_valid_chief_input(self):
        data = {
            "run_id": str(uuid4()),
            "maturity": "FINAL",
            "specialist_results": {
                "funnel_performance": {
                    "agent_name": "funnel_performance",
                    "status": "SUCCEEDED",
                    "output": {"summary": "ok"},
                }
            },
            "data_quality": {"status": "PASS", "limitations": []},
        }
        errors = validate_chief_input(data)
        assert errors == []


# ---------------------------------------------------------------------------
# Chief Output Schema Tests
# ---------------------------------------------------------------------------

class TestChiefOutputSchema:
    def test_valid_chief_output(self):
        data = {
            "executive_summary": "Mixed day with positive short signals",
            "findings": [
                {
                    "section": "WHAT_HAPPENED",
                    "observation_code": "SHORT_POSITIVE_EXPECTANCY",
                    "statement": "Shorts were profitable",
                    "evidence_refs": ["metric:scanner:ME:SHORT:24h:pnl_r"],
                    "sample_size": 17,
                    "confidence": "MEDIUM",
                }
            ],
            "hypotheses": [
                {
                    "hypothesis": "Regime shift",
                    "falsifiable_experiment": "Check 7d persistence",
                    "required_data": ["7d metrics"],
                    "validation_criterion": "Consistent direction",
                }
            ],
            "proposed_experiments": [],
            "action_class": "MONITOR",
            "limitations": [],
            "evidence_refs": ["metric:scanner:ME:SHORT:24h:pnl_r"],
            "partial": False,
            "missing_agents": [],
        }
        errors = validate_chief_output(data)
        assert errors == []

    def test_forbidden_action_class(self):
        data = {
            "executive_summary": "test",
            "findings": [],
            "hypotheses": [],
            "proposed_experiments": [],
            "action_class": "CHANGE_CONFIG",  # FORBIDDEN
            "limitations": [],
            "evidence_refs": [],
            "partial": False,
            "missing_agents": [],
        }
        errors = validate_chief_output(data)
        assert len(errors) > 0


# ---------------------------------------------------------------------------
# Schema file existence & validity
# ---------------------------------------------------------------------------

class TestSchemaFilesExist:
    def test_all_schema_files_exist(self):
        base = Path(__file__).parent.parent / "app" / "analytics" / "agents" / "contracts" / "v1"
        assert (base / "agent_input.schema.json").exists()
        assert (base / "specialist_output.schema.json").exists()
        assert (base / "chief_input.schema.json").exists()
        assert (base / "chief_output.schema.json").exists()

    def test_all_schemas_are_valid_json(self):
        base = Path(__file__).parent.parent / "app" / "analytics" / "agents" / "contracts" / "v1"
        for name in ["agent_input", "specialist_output", "chief_input", "chief_output"]:
            path = base / f"{name}.schema.json"
            data = json.loads(path.read_text())
            assert "$schema" in data
            assert "properties" in data
