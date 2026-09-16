"""Tests for Stage 3 Chief Eligibility Policy."""
import pytest
from app.analytics.agents.chief_policy import check_chief_eligibility, ChiefEligibility


class TestChiefEligibility:
    def test_all_success_full(self):
        """All 3 specialists SUCCEEDED → full report."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SUCCEEDED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        assert r.eligible is True
        assert r.partial is False
        assert len(r.missing_agents) == 0
        assert len(r.available_agents) == 3
        assert len(r.blockers) == 0

    def test_all_degraded_full(self):
        """All 3 specialists DEGRADED → still full report."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "DEGRADED",
            "EXECUTION_QUALITY": "DEGRADED",
            "DRIFT_AND_ANOMALY": "DEGRADED",
        })
        assert r.eligible is True
        assert r.partial is False
        assert len(r.missing_agents) == 0
        assert len(r.available_agents) == 3

    def test_one_failed_partial(self):
        """One FAILED specialist → partial report (2/3 available)."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "FAILED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        assert r.eligible is True
        assert r.partial is True
        assert "EXECUTION_QUALITY" in r.missing_agents
        assert len(r.available_agents) == 2
        assert len(r.blockers) == 0

    def test_one_skipped_partial(self):
        """One SKIPPED specialist → partial report."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SKIPPED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        assert r.eligible is True
        assert r.partial is True
        assert "EXECUTION_QUALITY" in r.missing_agents

    def test_two_failed_not_eligible(self):
        """Two FAILED specialists → Chief NOT eligible."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "FAILED",
            "EXECUTION_QUALITY": "FAILED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        assert r.eligible is False
        assert r.partial is False
        assert len(r.blockers) > 0
        assert "Insufficient specialist data" in r.blockers[0]

    def test_all_missing_not_eligible(self):
        """Empty status dict → Chief NOT eligible."""
        r = check_chief_eligibility({})
        assert r.eligible is False
        assert r.partial is False
        assert len(r.blockers) > 0
        assert len(r.missing_agents) == 3

    def test_mixed_success_and_skipped(self):
        """Mixed SUCCEEDED and SKIPPED → partial report."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SKIPPED",
            "DRIFT_AND_ANOMALY": "DEGRADED",
        })
        assert r.eligible is True
        assert r.partial is True
        assert len(r.available_agents) == 2
        assert "EXECUTION_QUALITY" in r.missing_agents

    def test_two_skipped_not_eligible(self):
        """Two SKIPPED specialists → Chief NOT eligible."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SKIPPED",
            "DRIFT_AND_ANOMALY": "SKIPPED",
        })
        assert r.eligible is False
        assert len(r.missing_agents) == 2

    def test_unknown_status_treated_as_skipped(self):
        """Unknown status values are treated as SKIPPED (not available)."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "UNKNOWN_STATUS",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        assert r.eligible is True
        assert r.partial is True
        assert "EXECUTION_QUALITY" in r.missing_agents

    def test_all_degraded_and_one_skipped_partial(self):
        """Two DEGRADED and one SKIPPED → partial report."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "DEGRADED",
            "EXECUTION_QUALITY": "DEGRADED",
            "DRIFT_AND_ANOMALY": "SKIPPED",
        })
        assert r.eligible is True
        assert r.partial is True
        assert len(r.available_agents) == 2
        assert "DRIFT_AND_ANOMALY" in r.missing_agents

    def test_available_agents_tuple(self):
        """Verify available_agents is a sorted tuple."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SUCCEEDED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        assert isinstance(r.available_agents, tuple)
        # Sorted alphabetically
        assert r.available_agents == (
            "DRIFT_AND_ANOMALY",
            "EXECUTION_QUALITY",
            "FUNNEL_AND_PERFORMANCE",
        )

    def test_missing_agents_tuple(self):
        """Verify missing_agents is a sorted tuple."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "FAILED",
            "DRIFT_AND_ANOMALY": "FAILED",
        })
        assert isinstance(r.missing_agents, tuple)
        # Sorted alphabetically
        assert r.missing_agents == (
            "DRIFT_AND_ANOMALY",
            "EXECUTION_QUALITY",
        )

    def test_frozen_dataclass(self):
        """Verify ChiefEligibility is immutable."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "SUCCEEDED",
            "DRIFT_AND_ANOMALY": "SUCCEEDED",
        })
        with pytest.raises(AttributeError):
            r.eligible = False

    def test_one_failed_one_skipped_not_eligible(self):
        """One FAILED and one SKIPPED → only 1 available → not eligible."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
            "EXECUTION_QUALITY": "FAILED",
            "DRIFT_AND_ANOMALY": "SKIPPED",
        })
        assert r.eligible is False
        assert len(r.available_agents) == 1
        assert len(r.missing_agents) == 2

    def test_blockers_message_format(self):
        """Verify blocker message includes count."""
        r = check_chief_eligibility({
            "FUNNEL_AND_PERFORMANCE": "FAILED",
            "EXECUTION_QUALITY": "FAILED",
            "DRIFT_AND_ANOMALY": "FAILED",
        })
        assert "3/3" in r.blockers[0]  # 3 out of 3 failed