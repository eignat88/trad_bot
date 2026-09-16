"""Versioned retry policy for Stage 3 agent orchestration.

Error classification:
- RETRYABLE: MODEL_TIMEOUT, MODEL_RATE_LIMIT, MODEL_PROVIDER_ERROR
  → automatic retry with exponential backoff + jitter
- REPAIRABLE: SCHEMA_VALIDATION_ERROR
  → one schema repair attempt before terminal failure
- TERMINAL: everything else
  → immediate failure, no retry

The ``V1`` suffix allows future policy versions with different thresholds
without breaking existing callers.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from app.analytics.agents.errors import AgentErrorCode


@dataclass(frozen=True)
class RetryPolicyV1:
    """Versioned retry policy with configurable thresholds.

    Parameters
    ----------
    max_attempts:
        Maximum number of execution attempts (including the initial one).
    base_delay_s:
        Base delay in seconds for exponential backoff.
    max_delay_s:
        Upper bound on the backoff delay.
    jitter_fraction:
        Fraction of the computed delay used as random jitter (0.0–1.0).
    schema_repair_attempts:
        Number of schema repair attempts allowed per error.

    Example::

        policy = RetryPolicyV1(max_attempts=3)
        if policy.should_retry(AgentErrorCode.MODEL_TIMEOUT, attempt=1):
            delay = policy.delay_for_attempt(attempt=1)
            await asyncio.sleep(delay)
    """

    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter_fraction: float = 0.25
    schema_repair_attempts: int = 1

    # Errors that trigger automatic retry
    _RETRYABLE: frozenset = frozenset({
        AgentErrorCode.MODEL_TIMEOUT,
        AgentErrorCode.MODEL_RATE_LIMIT,
        AgentErrorCode.MODEL_PROVIDER_ERROR,
    })

    # Errors that trigger schema repair (not retry)
    _REPAIRABLE: frozenset = frozenset({
        AgentErrorCode.SCHEMA_VALIDATION_ERROR,
    })

    def is_retryable(self, error_code: AgentErrorCode) -> bool:
        """Return True if the error code belongs to the retryable set."""
        return error_code in self._RETRYABLE

    def is_repairable(self, error_code: AgentErrorCode) -> bool:
        """Return True if the error code belongs to the repairable set."""
        return error_code in self._REPAIRABLE

    def delay_for_attempt(self, attempt: int) -> float:
        """Calculate delay in seconds for a given attempt number (1-based).

        Uses exponential backoff: ``base_delay * 2^(attempt-1)``,
        capped at ``max_delay_s``, with uniform random jitter.

        Parameters
        ----------
        attempt:
            The 1-based attempt number (1 = first retry delay).
        """
        delay = min(
            self.base_delay_s * (2 ** (attempt - 1)),
            self.max_delay_s,
        )
        jitter = delay * self.jitter_fraction * random.random()
        return delay + jitter

    def should_retry(self, error_code: AgentErrorCode, current_attempt: int) -> bool:
        """Determine if we should retry given the error and current attempt.

        Parameters
        ----------
        error_code:
            The classified error code from the failed execution.
        current_attempt:
            The 1-based attempt number that just failed.

        Returns
        -------
        bool
            True if another attempt should be made.
        """
        if not self.is_retryable(error_code):
            return False
        return current_attempt < self.max_attempts

    def should_repair(self, error_code: AgentErrorCode, repairs_attempted: int) -> bool:
        """Determine if we should attempt schema repair.

        Parameters
        ----------
        error_code:
            The classified error code from the failed execution.
        repairs_attempted:
            Number of repair attempts already made for this agent run.

        Returns
        -------
        bool
            True if a schema repair should be attempted.
        """
        if not self.is_repairable(error_code):
            return False
        return repairs_attempted < self.schema_repair_attempts
