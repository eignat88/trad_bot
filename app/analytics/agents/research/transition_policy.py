"""Versioned transition policy for research lifecycle entities.

Defines legal state transitions for finding, hypothesis, experiment, etc.
Invalid transitions raise ValueError — the repository calls this BEFORE
any DB write, ensuring atomic transition + history consistency.
"""
from __future__ import annotations


# ── Legal transitions ──────────────────────────────────────────────────
# Source: DOC4 lifecycle model

# Finding lifecycle:
#   OPEN → REPEATED
#   REPEATED → RESEARCH_REQUIRED
#   OPEN / REPEATED / RESEARCH_REQUIRED → CLOSED
FINDING_TRANSITIONS: dict[str, set[str]] = {
    "OPEN":              {"REPEATED", "CLOSED"},
    "REPEATED":          {"RESEARCH_REQUIRED", "CLOSED"},
    "RESEARCH_REQUIRED": {"CLOSED"},
}

# Hypothesis lifecycle (linear with terminal branches):
#   DRAFT → RESEARCH_REQUIRED → EXPERIMENT_DESIGNED → BACKTESTING
#   → OOS_VALIDATION → VALIDATED / REJECTED / INCONCLUSIVE
#   DRAFT, RESEARCH_REQUIRED → REJECTED (early abort)
HYPOTHESIS_TRANSITIONS: dict[str, set[str]] = {
    "DRAFT":              {"RESEARCH_REQUIRED", "REJECTED"},
    "RESEARCH_REQUIRED":  {"EXPERIMENT_DESIGNED", "REJECTED"},
    "EXPERIMENT_DESIGNED": {"BACKTESTING", "REJECTED"},
    "BACKTESTING":        {"OOS_VALIDATION", "REJECTED", "INCONCLUSIVE"},
    "OOS_VALIDATION":     {"VALIDATED", "REJECTED", "INCONCLUSIVE"},
}

# Experiment lifecycle:
#   PROPOSED → FROZEN → RUNNING → COMPLETED / CANCELLED
EXPERIMENT_TRANSITIONS: dict[str, set[str]] = {
    "PROPOSED":  {"FROZEN", "CANCELLED"},
    "FROZEN":    {"RUNNING", "CANCELLED"},
    "RUNNING":   {"COMPLETED", "CANCELLED"},
}

# Experiment run lifecycle:
#   PENDING → RUNNING → COMPLETED / FAILED
EXPERIMENT_RUN_TRANSITIONS: dict[str, set[str]] = {
    "PENDING":   {"RUNNING", "FAILED"},
    "RUNNING":   {"COMPLETED", "FAILED"},
}

# Change candidate lifecycle:
#   PROPOSED → APPROVED / REJECTED
#   APPROVED → IMPLEMENTED
CHANGE_CANDIDATE_TRANSITIONS: dict[str, set[str]] = {
    "PROPOSED":  {"APPROVED", "REJECTED"},
    "APPROVED":  {"IMPLEMENTED"},
}

# Production change: terminal by design (no lifecycle transitions)
PRODUCTION_CHANGE_TRANSITIONS: dict[str, set[str]] = {}

# Monitoring result lifecycle:
#   PENDING → PASS / FAIL / INCONCLUSIVE
MONITORING_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"PASS", "FAIL", "INCONCLUSIVE"},
}


class ResearchTransitionPolicyV1:
    """Deterministic transition validation. Versioned for future schema evolution."""

    POLICIES: dict[str, dict[str, set[str]]] = {
        "finding":         FINDING_TRANSITIONS,
        "hypothesis":      HYPOTHESIS_TRANSITIONS,
        "experiment":      EXPERIMENT_TRANSITIONS,
        "experiment_run":  EXPERIMENT_RUN_TRANSITIONS,
        "change_candidate": CHANGE_CANDIDATE_TRANSITIONS,
        "production_change": PRODUCTION_CHANGE_TRANSITIONS,
        "monitoring_result": MONITORING_TRANSITIONS,
    }

    @classmethod
    def validate(cls, entity_type: str, current_status: str, new_status: str) -> None:
        """Raise ValueError if the transition is illegal.

        Called BEFORE any DB write. If this raises, no UPDATE or
        INSERT INTO transition_history should occur.
        """
        if entity_type not in cls.POLICIES:
            raise ValueError(
                f"Unknown entity_type '{entity_type}' — cannot validate transition"
            )

        allowed = cls.POLICIES[entity_type].get(current_status, set())

        if not allowed:
            raise ValueError(
                f"No transitions allowed from '{current_status}' "
                f"on entity '{entity_type}' — terminal state"
            )

        if new_status not in allowed:
            raise ValueError(
                f"Illegal transition: {entity_type} "
                f"'{current_status}' → '{new_status}'. "
                f"Allowed: {sorted(allowed)}"
            )
