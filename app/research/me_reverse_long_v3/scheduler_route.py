"""Fail-closed scheduler routing for ME Reverse Long V3."""
from __future__ import annotations

from typing import Any

from .protocol import EXPERIMENT_ID


def route_prospective_experiment(
    experiment_id: str,
    generic_evaluator: Any,
    v3_evaluator: Any | None = None,
    *,
    v3_enabled: bool = False,
) -> dict:
    """V3 must never fall back to generic prospective evaluation."""
    if experiment_id == EXPERIMENT_ID:
        if not v3_enabled or v3_evaluator is None:
            return {
                "signals_checked": 0,
                "finalized": 0,
                "errors": 0,
                "skipped_v3": 1,
            }

        stats = v3_evaluator.run_evaluation_cycle()
        return {
            "signals_checked": stats.get("checked", 0),
            "finalized": stats.get("finalized", 0),
            "errors": stats.get("errors", 0),
            "skipped_v3": 0,
        }

    return generic_evaluator.run_evaluation_cycle(experiment_id)
