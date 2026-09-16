"""Chief eligibility and partial report policy for Stage 3.

Determines whether the Chief Trading Analyst agent can run based on
the outcomes of the specialist agents.  The Chief synthesizes specialist
findings into a daily trading report, so it needs sufficient data.

Eligibility rules:
- All 3 required specialists SUCCEEDED/DEGRADED → full report
- 1 required specialist FAILED/SKIPPED but ≥2 available → partial report
- 2+ required specialists FAILED/SKIPPED → Chief NOT eligible
"""
from __future__ import annotations

from dataclasses import dataclass

# Required specialist agents for Chief
_REQUIRED_SPECIALISTS = frozenset({
    "FUNNEL_AND_PERFORMANCE",
    "EXECUTION_QUALITY",
    "DRIFT_AND_ANOMALY",
})

# Optional specialists (Chief can work without them, but report is partial)
_OPTIONAL_SPECIALISTS: frozenset = frozenset()


@dataclass(frozen=True)
class ChiefEligibility:
    """Result of Chief eligibility check.

    Attributes
    ----------
    eligible:
        True if Chief may run (full or partial report).
    partial:
        True if the report will be partial (missing specialist data).
    available_agents:
        Names of specialists that completed successfully.
    missing_agents:
        Names of specialists that failed or were skipped.
    blockers:
        If not eligible, the reason(s) why.
    """

    eligible: bool
    partial: bool
    available_agents: tuple[str, ...]
    missing_agents: tuple[str, ...]
    blockers: tuple[str, ...]


def check_chief_eligibility(
    specialist_statuses: dict[str, str],
) -> ChiefEligibility:
    """Determine if Chief Trading Analyst can run.

    Parameters
    ----------
    specialist_statuses:
        Mapping of ``agent_name -> status`` string.
        Valid status values: ``SUCCEEDED``, ``DEGRADED``, ``FAILED``, ``SKIPPED``.

    Returns
    -------
    ChiefEligibility
        Frozen result with eligibility, partial flag, and diagnostic detail.

    Examples
    --------

    All specialists succeeded::

        >>> result = check_chief_eligibility({
        ...     "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
        ...     "EXECUTION_QUALITY": "SUCCEEDED",
        ...     "DRIFT_AND_ANOMALY": "SUCCEEDED",
        ... })
        >>> result.eligible, result.partial
        (True, False)

    One specialist failed (partial report)::

        >>> result = check_chief_eligibility({
        ...     "FUNNEL_AND_PERFORMANCE": "SUCCEEDED",
        ...     "EXECUTION_QUALITY": "FAILED",
        ...     "DRIFT_AND_ANOMALY": "SUCCEEDED",
        ... })
        >>> result.eligible, result.partial
        (True, True)

    Two specialists failed (not eligible)::

        >>> result = check_chief_eligibility({
        ...     "FUNNEL_AND_PERFORMANCE": "FAILED",
        ...     "EXECUTION_QUALITY": "FAILED",
        ...     "DRIFT_AND_ANOMALY": "SUCCEEDED",
        ... })
        >>> result.eligible
        False
    """
    available: list[str] = []
    missing: list[str] = []
    blockers: list[str] = []

    # Evaluate required specialists
    for name in sorted(_REQUIRED_SPECIALISTS):
        status = specialist_statuses.get(name, "SKIPPED")
        if status in ("SUCCEEDED", "DEGRADED"):
            available.append(name)
        else:
            missing.append(name)

    # Evaluate optional specialists (included if available, no penalty if missing)
    for name in sorted(_OPTIONAL_SPECIALISTS):
        status = specialist_statuses.get(name, "SKIPPED")
        if status in ("SUCCEEDED", "DEGRADED"):
            available.append(name)

    # ── Eligibility logic ─────────────────────────────────────────────
    all_present = len(missing) == 0
    partial_allowed = len(missing) <= 1  # at most 1 missing for partial

    if all_present:
        return ChiefEligibility(
            eligible=True,
            partial=False,
            available_agents=tuple(available),
            missing_agents=tuple(missing),
            blockers=(),
        )

    if partial_allowed and len(available) >= 2:
        return ChiefEligibility(
            eligible=True,
            partial=True,
            available_agents=tuple(available),
            missing_agents=tuple(missing),
            blockers=(),
        )

    # Not eligible — too many specialists failed
    blockers.append(
        f"Insufficient specialist data: "
        f"{len(missing)}/{len(_REQUIRED_SPECIALISTS)} required agents failed"
    )
    return ChiefEligibility(
        eligible=False,
        partial=False,
        available_agents=tuple(available),
        missing_agents=tuple(missing),
        blockers=tuple(blockers),
    )
