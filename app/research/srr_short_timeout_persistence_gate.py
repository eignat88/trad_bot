"""Conservative SRR SHORT TIMEOUT persistence boundary.

Frozen policy is authoritative for economics.
This adapter only delays final persistence until the 120m cutoff.
"""

from dataclasses import asdict, replace
from typing import Any

from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SrrRouteResult,
)


def prepare_srr_persistence_result(
    result: SrrRouteResult,
    *,
    evaluation_asof_ms: int,
) -> dict[str, Any]:
    """Return a writer-safe result without modifying frozen policy output."""

    original = asdict(result)

    if not result.finalization_eligible or result.path_class != "TIMEOUT":
        return original

    policy = result.policy_result or {}
    cutoff_ms = policy.get("cutoff_time_ms")

    if cutoff_ms is None:
        raise ValueError("SRR TIMEOUT requires frozen cutoff_time_ms")

    if evaluation_asof_ms >= int(cutoff_ms):
        return original

    # A pre-cutoff TIMEOUT is only a provisional diagnostic.
    # No economics may be persisted in a non-final outcome.
    delayed = replace(
        result,
        status="INCOMPLETE_COVERAGE",
        reason_code="TIMEOUT_CUTOFF_NOT_REACHED",
        path_class=None,
        finalization_eligible=False,
        gross_r=None,
        cost_r_normal=None,
        cost_r_elevated=None,
        net_r_normal=None,
        net_r_elevated=None,
        policy_result=None,
    )

    return asdict(delayed)
