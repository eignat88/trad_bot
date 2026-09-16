"""Tests for Stage 3 Confidence Policy v1."""
import pytest
from app.analytics.agents.policies.confidence_v1 import ConfidencePolicyV1
from app.analytics.agents.models import ConfidenceLevel


@pytest.fixture
def policy():
    return ConfidencePolicyV1()


class TestConfidencePolicy:
    def test_high_confidence(self, policy):
        result = policy.evaluate(
            sample_size=30, maturity="FINAL",
            data_quality_status="PASS", windows_with_effect=3, has_gaps=False
        )
        assert result == ConfidenceLevel.HIGH

    def test_high_requires_final(self, policy):
        result = policy.evaluate(
            sample_size=30, maturity="PROVISIONAL",
            data_quality_status="PASS", windows_with_effect=3, has_gaps=False
        )
        assert result != ConfidenceLevel.HIGH

    def test_high_requires_no_gaps(self, policy):
        result = policy.evaluate(
            sample_size=30, maturity="FINAL",
            data_quality_status="PASS", windows_with_effect=3, has_gaps=True
        )
        assert result != ConfidenceLevel.HIGH

    def test_medium_confidence(self, policy):
        result = policy.evaluate(
            sample_size=10, maturity="PROVISIONAL",
            data_quality_status="PASS", windows_with_effect=2, has_gaps=False
        )
        assert result == ConfidenceLevel.MEDIUM

    def test_medium_min_sample(self, policy):
        result = policy.evaluate(
            sample_size=9, maturity="FINAL",
            data_quality_status="PASS", windows_with_effect=2, has_gaps=False
        )
        assert result == ConfidenceLevel.LOW

    def test_low_small_sample(self, policy):
        result = policy.evaluate(
            sample_size=2, maturity="FINAL",
            data_quality_status="PASS", windows_with_effect=1, has_gaps=False
        )
        assert result == ConfidenceLevel.LOW

    def test_low_degraded_quality(self, policy):
        result = policy.evaluate(
            sample_size=30, maturity="FINAL",
            data_quality_status="DEGRADED", windows_with_effect=3, has_gaps=False
        )
        assert result == ConfidenceLevel.MEDIUM  # Degraded prevents HIGH

    def test_single_window_only_low(self, policy):
        result = policy.evaluate(
            sample_size=20, maturity="FINAL",
            data_quality_status="PASS", windows_with_effect=1, has_gaps=False
        )
        assert result == ConfidenceLevel.LOW

    def test_n2_cannot_be_high(self, policy):
        """n=2 must never produce HIGH — hard requirement from spec."""
        result = policy.evaluate(
            sample_size=2, maturity="FINAL",
            data_quality_status="PASS", windows_with_effect=3, has_gaps=False
        )
        assert result != ConfidenceLevel.HIGH
