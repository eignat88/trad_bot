"""Tests for Stage 3 Data Readiness Gate."""
import pytest
from uuid import uuid4
from datetime import datetime, timezone

from app.analytics.agents.readiness import DataReadinessGate, DataReadinessResult


@pytest.fixture
def gate():
    return DataReadinessGate()


class TestDataReadinessGate:
    def test_ready_with_pass(self, gate):
        """Test readiness with all conditions met and quality PASS."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc123",
            quality_status="PASS",
            publication_status="READY",
            canonical_build_json={"trade_fact_built": True, "setup_fact_built": True},
        )
        assert r.ready is True
        assert r.dataset_state == "READY"
        assert r.quality_status == "PASS"
        assert r.dataset_version == "20260916.F.abc123"
        assert r.maturity == "FINAL"
        assert len(r.blockers) == 0
        assert len(r.limitations) == 0

    def test_ready_with_degraded(self, gate):
        """Test readiness with DEGRADED quality still allows analysis."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="PROVISIONAL",
            dataset_version="20260916.P.def456",
            quality_status="DEGRADED",
            publication_status="READY",
            canonical_build_json={"trade_fact_built": True, "setup_fact_built": True},
        )
        assert r.ready is True
        assert r.quality_status == "DEGRADED"
        assert r.dataset_state == "READY"

    def test_not_ready_quality_fail(self, gate):
        """Test that quality FAIL blocks readiness."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status="FAIL",
            publication_status="READY",
            canonical_build_json={"trade_fact_built": True, "setup_fact_built": True},
        )
        assert r.ready is False
        assert any("quality gate FAILED" in b for b in r.blockers)

    def test_not_ready_canonical_missing(self, gate):
        """Test that missing canonical build blocks readiness."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status="PASS",
            canonical_build_json={"trade_fact_built": False, "setup_fact_built": True},
        )
        assert r.ready is False
        assert any("trade_fact" in b for b in r.blockers)

    def test_not_ready_dataset_version_missing(self, gate):
        """Test that missing dataset_version blocks readiness."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version=None,
            quality_status="PASS",
        )
        assert r.ready is False
        assert any("dataset_version is missing" in b for b in r.blockers)

    def test_not_ready_stub_version(self, gate):
        """Test that stub dataset_version blocks readiness."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="00000000.0",
            quality_status="PASS",
        )
        assert r.ready is False
        assert any("stub" in b for b in r.blockers)

    def test_not_ready_status_not_succeeded(self, gate):
        """Test that non-SUCCEEDED status blocks readiness."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="RUNNING",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status="PASS",
        )
        assert r.ready is False
        assert any("RUNNING" in b for b in r.blockers)

    def test_not_ready_invalid_maturity(self, gate):
        """Test that invalid maturity blocks readiness."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="INVALID",
            dataset_version="20260916.F.abc",
            quality_status="PASS",
        )
        assert r.ready is False
        assert any("invalid maturity" in b for b in r.blockers)

    def test_not_ready_quality_missing(self, gate):
        """Test that missing quality_status blocks readiness."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status=None,
        )
        assert r.ready is False
        assert any("quality_status is missing" in b for b in r.blockers)

    def test_limitations_propagated(self, gate):
        """Test that quality limitations are propagated to result."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="v1",
            quality_status="DEGRADED",
            publication_status="READY",
            canonical_build_json={"trade_fact_built": True, "setup_fact_built": True},
            quality_limitations=["missing 7d candles for ETHUSDT"],
        )
        assert r.ready is True
        assert "missing 7d candles for ETHUSDT" in r.limitations

    def test_multiple_blockers(self, gate):
        """Test that multiple issues produce multiple blockers."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="FAILED",
            maturity="INVALID",
            dataset_version=None,
            quality_status=None,
        )
        assert r.ready is False
        assert len(r.blockers) >= 3

    def test_analysis_window_propagated(self, gate):
        """Test that analysis window timestamps are propagated."""
        from_time = datetime(2026, 9, 16, tzinfo=timezone.utc)
        to_time = datetime(2026, 9, 17, tzinfo=timezone.utc)
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status="PASS",
            analysis_window_from=from_time,
            analysis_window_to=to_time,
        )
        assert r.analysis_window_from == from_time
        assert r.analysis_window_to == to_time

    def test_run_id_preserved(self, gate):
        """Test that run_id is used (though not stored in result)."""
        run_id = uuid4()
        r = gate.evaluate(
            run_id=run_id,
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status="PASS",
            publication_status="READY",
            canonical_build_json={"trade_fact_built": True, "setup_fact_built": True},
        )
        assert r.ready is True

    def test_quality_status_none_becomes_fail(self, gate):
        """Test that None quality_status becomes FAIL in effective quality."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status=None,
        )
        assert r.quality_status == "FAIL"  # effective quality when None

    def test_frozen_result(self, gate):
        """Test that DataReadinessResult is frozen (immutable)."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status="PASS",
        )
        with pytest.raises(AttributeError):
            r.ready = False

    def test_setup_fact_built_false_blocks(self, gate):
        """Test that setup_fact not built blocks readiness."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status="PASS",
            canonical_build_json={"trade_fact_built": True, "setup_fact_built": False},
        )
        assert r.ready is False
        assert any("setup_fact" in b for b in r.blockers)

    def test_both_canonical_facts_missing_blocks(self, gate):
        """Test that both missing canonical facts produce two blockers."""
        r = gate.evaluate(
            run_id=uuid4(),
            status="SUCCEEDED",
            maturity="FINAL",
            dataset_version="20260916.F.abc",
            quality_status="PASS",
            canonical_build_json={"trade_fact_built": False, "setup_fact_built": False},
        )
        assert r.ready is False
        assert len([b for b in r.blockers if "fact" in b]) == 2