"""Versioned confidence policy for analytical agent observations.

This module provides a deterministic, testable confidence evaluation
policy that assigns a confidence level (HIGH / MEDIUM / LOW) to an
observation based on quantifiable criteria extracted from the data
pipeline — *not* from LLM prompts.

Policy versioning ensures that historical analyses can be traced back
to the exact confidence logic that produced them.
"""

from __future__ import annotations

from app.analytics.agents.models import ConfidenceLevel


class ConfidencePolicyV1:
    """Confidence evaluation policy — version 1.

    Thresholds are explicit class constants so they can be inspected
    and tested without depending on any external state.

    Decision table (evaluated top → bottom; first match wins):

    ┌──────────┬─────────────┬─────────────────┬──────────┬──────────┬───────────┐
    │  Level   │ sample_size │ windows_with_effect │ has_gaps │ maturity │ data_qos  │
    ├──────────┼─────────────┼─────────────────┼──────────┼──────────┼───────────┤
    │  HIGH    │    >= 30    │      >= 3       │  False   │  FINAL   │   any     │
    │  MEDIUM  │    >= 10    │      >= 2       │   any    │   any    │   any     │
    │  LOW     │    any      │      any        │   any    │   any    │   any     │
    └──────────┴─────────────┴─────────────────┴──────────┴──────────┴───────────┘
    """

    # --- Threshold constants ---------------------------------------------------

    HIGH_MIN_SAMPLE_SIZE: int = 30
    HIGH_MIN_WINDOWS: int = 3
    HIGH_REQUIRES_NO_GAPS: bool = True
    HIGH_REQUIRES_FINAL: bool = True

    MEDIUM_MIN_SAMPLE_SIZE: int = 10
    MEDIUM_MIN_WINDOWS: int = 2

    # --- Public API -----------------------------------------------------------

    @classmethod
    def evaluate(
        cls,
        sample_size: int,
        maturity: str,
        data_quality_status: str,
        windows_with_effect: int,
        has_gaps: bool,
    ) -> ConfidenceLevel:
        """Evaluate confidence for a single observation.

        Parameters
        ----------
        sample_size:
            Number of data points backing the observation.
        maturity:
            Dataset maturity — ``"PROVISIONAL"`` or ``"FINAL"``.
        data_quality_status:
            Quality gate result — ``"PASS"`` or ``"DEGRADED"``.
            Currently informational; reserved for future policy versions.
        windows_with_effect:
            Number of distinct analysis windows where the effect was
            observed.
        has_gaps:
            Whether the underlying data has temporal gaps.

        Returns
        -------
        ConfidenceLevel
            The assigned confidence level.
        """
        # --- HIGH gate ---------------------------------------------------------
        if (
            sample_size >= cls.HIGH_MIN_SAMPLE_SIZE
            and windows_with_effect >= cls.HIGH_MIN_WINDOWS
            and (not cls.HIGH_REQUIRES_NO_GAPS or not has_gaps)
            and (not cls.HIGH_REQUIRES_FINAL or maturity == "FINAL")
            and data_quality_status == "PASS"
        ):
            return ConfidenceLevel.HIGH

        # --- MEDIUM gate -------------------------------------------------------
        if (
            sample_size >= cls.MEDIUM_MIN_SAMPLE_SIZE
            and windows_with_effect >= cls.MEDIUM_MIN_WINDOWS
        ):
            return ConfidenceLevel.MEDIUM

        # --- LOW (everything else) --------------------------------------------
        return ConfidenceLevel.LOW
