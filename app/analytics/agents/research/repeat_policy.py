"""Repeat policy for research findings.

Evaluates whether a finding should transition based on its occurrence
history.  The policy is purely computational — no DB writes, no
side effects.  The caller (ingestion service) is responsible for
executing the recommended transition via the repository.

Transition rules (FindingRepeatPolicyV1):
    OPEN → REPEATED       when independent occurrences ≥ repeated_min_independent_occurrences
    REPEATED → RESEARCH_REQUIRED when independent occurrences ≥ research_required_min_occurrences
                              AND distinct business dates ≥ min_distinct_business_dates
    No shortcut OPEN → RESEARCH_REQUIRED (must pass through REPEATED first)
"""
from __future__ import annotations

from dataclasses import dataclass


# ======================================================================
# Confidence ranking for threshold comparison
# ======================================================================

_CONFIDENCE_RANK: dict[str, int] = {
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
}


def _confidence_meets_threshold(confidence: str, minimum: str) -> bool:
    """Return True if confidence meets or exceeds the minimum threshold."""
    rank = _CONFIDENCE_RANK.get(confidence.upper(), 0)
    min_rank = _CONFIDENCE_RANK.get(minimum.upper(), 0)
    return rank >= min_rank


# ======================================================================
# Configuration
# ======================================================================

@dataclass(frozen=True)
class FindingRepeatPolicyConfig:
    """Tunable thresholds for the repeat policy.

    Defaults match the DOC4 specification.
    """

    repeated_min_independent_occurrences: int = 3
    research_required_min_occurrences: int = 5
    min_distinct_business_dates: int = 2
    min_confidence: str = "MEDIUM"


# ======================================================================
# Policy evaluation
# ======================================================================

# Possible return values
KEEP_OPEN = "KEEP_OPEN"
MARK_REPEATED = "MARK_REPEATED"
MARK_RESEARCH_REQUIRED = "MARK_RESEARCH_REQUIRED"
NO_CHANGE = "NO_CHANGE"


class FindingRepeatPolicyV1:
    """Deterministic repeat-policy evaluator.

    Each call receives the *complete* occurrence list for a single finding
    and its current status, and returns a recommended action.

    Parameters:
        config:         Tunable thresholds
        occurrences:    List of dicts, each with at least:
                          - confidence: str (LOW/MEDIUM/HIGH)
                          - business_date: str | None (YYYY-MM-DD)
        current_status: Current finding status (OPEN, REPEATED, RESEARCH_REQUIRED, CLOSED)

    Returns:
        One of: KEEP_OPEN, MARK_REPEATED, MARK_RESEARCH_REQUIRED, NO_CHANGE
    """

    @classmethod
    def evaluate(
        cls,
        config: FindingRepeatPolicyConfig,
        occurrences: list[dict],
        current_status: str,
    ) -> str:
        """Evaluate the repeat policy and return a recommended action."""
        # Terminal state — no transitions from CLOSED
        if current_status == "CLOSED":
            return NO_CHANGE

        # Already at RESEARCH_REQUIRED — no further promotion
        if current_status == "RESEARCH_REQUIRED":
            return NO_CHANGE

        # Filter occurrences by confidence threshold
        qualifying = [
            occ for occ in occurrences
            if _confidence_meets_threshold(
                occ.get("confidence", "LOW"),
                config.min_confidence,
            )
        ]

        # Count independent occurrences (distinct business_date values)
        business_dates: set[str] = set()
        for occ in qualifying:
            bd = occ.get("business_date")
            if bd:
                business_dates.add(str(bd))

        qualifying_count = len(qualifying)
        distinct_dates = len(business_dates)

        # ------------------------------------------------------------------
        # Evaluation logic
        # ------------------------------------------------------------------

        if current_status == "OPEN":
            # Check if we should promote to REPEATED
            if qualifying_count >= config.repeated_min_independent_occurrences:
                return MARK_REPEATED
            return KEEP_OPEN

        if current_status == "REPEATED":
            # Check if we should promote to RESEARCH_REQUIRED
            if (
                qualifying_count >= config.research_required_min_occurrences
                and distinct_dates >= config.min_distinct_business_dates
            ):
                return MARK_RESEARCH_REQUIRED
            return NO_CHANGE

        # Unknown status — no action
        return NO_CHANGE
