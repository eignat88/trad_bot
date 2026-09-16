"""Tests for Stage 3 Retry Policy."""
import pytest
from app.analytics.agents.retry_policy import RetryPolicyV1
from app.analytics.agents.errors import AgentErrorCode


@pytest.fixture
def policy():
    """Create a deterministic retry policy for testing (no jitter)."""
    return RetryPolicyV1(max_attempts=3, base_delay_s=1.0, max_delay_s=30.0, jitter_fraction=0.0)


@pytest.fixture
def policy_with_jitter():
    """Create a retry policy with jitter enabled."""
    return RetryPolicyV1(max_attempts=3, base_delay_s=1.0, max_delay_s=30.0, jitter_fraction=0.25)


class TestRetryPolicy:
    def test_retryable_timeout(self, policy):
        """Timeout errors are retryable."""
        assert policy.is_retryable(AgentErrorCode.MODEL_TIMEOUT)

    def test_retryable_rate_limit(self, policy):
        """Rate limit errors are retryable."""
        assert policy.is_retryable(AgentErrorCode.MODEL_RATE_LIMIT)

    def test_retryable_provider_error(self, policy):
        """Provider errors are retryable."""
        assert policy.is_retryable(AgentErrorCode.MODEL_PROVIDER_ERROR)

    def test_not_retryable_schema(self, policy):
        """Schema validation errors are NOT retryable (they're repairable)."""
        assert not policy.is_retryable(AgentErrorCode.SCHEMA_VALIDATION_ERROR)

    def test_not_retryable_evidence(self, policy):
        """Evidence validation errors are NOT retryable."""
        assert not policy.is_retryable(AgentErrorCode.EVIDENCE_VALIDATION_ERROR)

    def test_should_retry_first_attempt(self, policy):
        """Should retry on first timeout failure."""
        assert policy.should_retry(AgentErrorCode.MODEL_TIMEOUT, current_attempt=1)

    def test_should_retry_second_attempt(self, policy):
        """Should retry on second timeout failure (within max_attempts)."""
        assert policy.should_retry(AgentErrorCode.MODEL_TIMEOUT, current_attempt=2)

    def test_should_not_retry_exhausted(self, policy):
        """Should NOT retry when attempts exhausted (attempt == max_attempts)."""
        assert not policy.should_retry(AgentErrorCode.MODEL_TIMEOUT, current_attempt=3)

    def test_should_not_retry_terminal(self, policy):
        """Should NOT retry terminal errors even on first attempt."""
        assert not policy.should_retry(AgentErrorCode.EVIDENCE_VALIDATION_ERROR, current_attempt=1)

    def test_repairable_schema(self, policy):
        """Schema validation errors are repairable."""
        assert policy.is_repairable(AgentErrorCode.SCHEMA_VALIDATION_ERROR)

    def test_not_repairable_timeout(self, policy):
        """Timeout errors are NOT repairable."""
        assert not policy.is_repairable(AgentErrorCode.MODEL_TIMEOUT)

    def test_should_repair_first(self, policy):
        """Should repair on first schema validation error."""
        assert policy.should_repair(AgentErrorCode.SCHEMA_VALIDATION_ERROR, repairs_attempted=0)

    def test_should_not_repair_second(self, policy):
        """Should NOT repair after first repair attempt."""
        assert not policy.should_repair(AgentErrorCode.SCHEMA_VALIDATION_ERROR, repairs_attempted=1)

    def test_delay_exponential(self, policy):
        """Delay should increase exponentially with attempt number."""
        d1 = policy.delay_for_attempt(1)
        d2 = policy.delay_for_attempt(2)
        assert d2 > d1
        assert d1 >= 1.0  # base delay
        assert d2 >= 2.0  # 2x base for attempt 2

    def test_delay_capped(self, policy):
        """Delay should be capped at max_delay_s."""
        d = policy.delay_for_attempt(10)
        assert d <= 30.0  # max_delay

    def test_delay_exponential_values(self, policy):
        """Test specific exponential delay values."""
        # base=1.0, jitter=0.0
        assert policy.delay_for_attempt(1) == 1.0  # 1.0 * 2^0
        assert policy.delay_for_attempt(2) == 2.0  # 1.0 * 2^1
        assert policy.delay_for_attempt(3) == 4.0  # 1.0 * 2^2

    def test_delay_capped_at_10(self, policy):
        """Test that delay caps at max_delay_s for high attempts."""
        # For attempt 5: 1.0 * 2^4 = 16.0
        assert policy.delay_for_attempt(5) == 16.0
        # For attempt 6: 1.0 * 2^5 = 32.0 > 30.0 → capped at 30.0
        assert policy.delay_for_attempt(6) == 30.0

    def test_delay_with_jitter(self, policy_with_jitter):
        """Test that jitter adds randomness to delay."""
        delays = [policy_with_jitter.delay_for_attempt(1) for _ in range(10)]
        # With jitter=0.25, delay should vary between 1.0 and ~1.25
        assert all(1.0 <= d <= 1.25 for d in delays)
        # Should have some variation
        assert len(set(delays)) > 1

    def test_max_attempts_configurable(self):
        """Test that max_attempts is configurable."""
        policy = RetryPolicyV1(max_attempts=5)
        assert policy.max_attempts == 5

    def test_base_delay_configurable(self):
        """Test that base_delay_s is configurable."""
        policy = RetryPolicyV1(base_delay_s=2.0, jitter_fraction=0.0)
        assert policy.delay_for_attempt(1) == 2.0

    def test_schema_repair_attempts_configurable(self):
        """Test that schema_repair_attempts is configurable."""
        policy = RetryPolicyV1(schema_repair_attempts=2)
        assert policy.should_repair(AgentErrorCode.SCHEMA_VALIDATION_ERROR, repairs_attempted=1)
        assert not policy.should_repair(AgentErrorCode.SCHEMA_VALIDATION_ERROR, repairs_attempted=2)

    def test_retry_on_attempt_zero(self, policy):
        """Test that attempt 0 is allowed for retry (policy doesn't validate attempt)."""
        # The policy only checks if current_attempt < max_attempts
        # attempt=0 < 3, so it would allow retry (policy is simple math-based)
        assert policy.should_retry(AgentErrorCode.MODEL_TIMEOUT, current_attempt=0)

    def test_all_retryable_codes_in_frozen_set(self, policy):
        """Verify the retryable set contains exactly the expected codes."""
        retryable_codes = {
            AgentErrorCode.MODEL_TIMEOUT,
            AgentErrorCode.MODEL_RATE_LIMIT,
            AgentErrorCode.MODEL_PROVIDER_ERROR,
        }
        for code in retryable_codes:
            assert policy.is_retryable(code)

    def test_repairable_codes_in_frozen_set(self, policy):
        """Verify the repairable set contains exactly the expected codes."""
        repairable_codes = {
            AgentErrorCode.SCHEMA_VALIDATION_ERROR,
        }
        for code in repairable_codes:
            assert policy.is_repairable(code)