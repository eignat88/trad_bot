"""Terminal lifecycle policy for Early MAE V2 paired outcomes."""

from __future__ import annotations

from typing import Any

from .protocol import VARIANTS


COMPLETE_STATUSES = frozenset({
    "EVALUATED_TRIGGERED",
    "EVALUATED_NO_TRIGGER",
    "BASELINE_EXITED_EARLY",
    "AMBIGUOUS",
})


def can_finalize_source(evaluations: list[dict[str, Any]]) -> bool:
    """Finalize only a complete and unique frozen seven-variant tuple."""
    expected = {variant.variant_id for variant in VARIANTS}

    if len(evaluations) != len(expected):
        return False

    actual = [row.get("variant_id") for row in evaluations]

    if len(set(actual)) != len(expected) or set(actual) != expected:
        return False

    return all(
        row.get("variant_status") in COMPLETE_STATUSES
        and row.get("variant_net_r") is not None
        and row.get("delta_net_r") is not None
        and row.get("variant_exit_at") is not None
        for row in evaluations
    )
