"""Terminal lifecycle rules for complete V3 seven-variant tuples."""
from __future__ import annotations

from typing import Any

from .protocol import VARIANTS

COMPLETE_STATUSES = frozenset({"EVALUATED", "AMBIGUOUS", "BASELINE_EXITED_EARLY"})


def can_finalize(evaluations: list[dict[str, Any]]) -> bool:
    expected = {v.variant_id for v in VARIANTS}
    actual = [row.get("variant_id") for row in evaluations]
    if len(actual) != 7 or set(actual) != expected:
        return False
    return all(
        row.get("variant_status") in COMPLETE_STATUSES
        and row.get("coverage_reason") is None
        and row.get("net_r_normal") is not None
        and row.get("net_r_elevated") is not None
        and row.get("delta_net_r_normal") is not None
        and row.get("delta_net_r_elevated") is not None
        and row.get("variant_exit_at") is not None
        for row in evaluations
    )
