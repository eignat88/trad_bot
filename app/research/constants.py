"""Generic research framework constants.

Shared across all experiments and evaluators.
"""
from __future__ import annotations


# Evaluation horizons — (label, minutes) pairs
# Used by all research evaluators regardless of experiment.
HORIZONS: list[tuple[str, int]] = [
    ("15m", 15),
    ("30m", 30),
    ("60m", 60),
    ("120m", 120),
    ("240m", 240),
]


# Observation status constants (rejection chain)
class ObservationStatus:
    """Final status of a research observation in the rejection chain."""
    DETECTED = "DETECTED"
    SCORE_REJECTED = "SCORE_REJECTED"
    GEOMETRY_REJECTED = "GEOMETRY_REJECTED"
    DEDUP_REJECTED = "DEDUP_REJECTED"
    GATE_REJECTED = "GATE_REJECTED"
    EXPECTANCY_REJECTED = "EXPECTANCY_REJECTED"
    REGIME_REJECTED = "REGIME_REJECTED"
    SETUP_READY = "SETUP_READY"
    OOS_REJECTED = "OOS_REJECTED"


# Valid observation statuses (for CHECK constraint documentation)
VALID_STATUSES = frozenset({
    ObservationStatus.DETECTED,
    ObservationStatus.SCORE_REJECTED,
    ObservationStatus.GEOMETRY_REJECTED,
    ObservationStatus.DEDUP_REJECTED,
    ObservationStatus.GATE_REJECTED,
    ObservationStatus.EXPECTANCY_REJECTED,
    ObservationStatus.REGIME_REJECTED,
    ObservationStatus.SETUP_READY,
    ObservationStatus.OOS_REJECTED,
})
