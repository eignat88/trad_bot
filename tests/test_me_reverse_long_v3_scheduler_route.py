from unittest.mock import MagicMock

from app.research.me_reverse_long_v3.protocol import EXPERIMENT_ID
from app.research.me_reverse_long_v3.scheduler_route import (
    route_prospective_experiment,
)


def test_v3_inactive_never_calls_generic():
    generic = MagicMock()

    result = route_prospective_experiment(
        EXPERIMENT_ID, generic, v3_enabled=False,
    )

    assert result["skipped_v3"] == 1
    generic.run_evaluation_cycle.assert_not_called()


def test_v3_missing_evaluator_never_falls_back():
    generic = MagicMock()

    result = route_prospective_experiment(
        EXPERIMENT_ID, generic, v3_enabled=True,
    )

    assert result["skipped_v3"] == 1
    generic.run_evaluation_cycle.assert_not_called()


def test_v3_enabled_uses_dedicated_evaluator():
    generic = MagicMock()
    v3 = MagicMock()
    v3.run_evaluation_cycle.return_value = {
        "checked": 4,
        "finalized": 2,
        "errors": 0,
    }

    result = route_prospective_experiment(
        EXPERIMENT_ID, generic, v3, v3_enabled=True,
    )

    assert result["signals_checked"] == 4
    assert result["finalized"] == 2
    generic.run_evaluation_cycle.assert_not_called()
    v3.run_evaluation_cycle.assert_called_once_with()


def test_other_experiments_remain_unchanged():
    generic = MagicMock()
    generic.run_evaluation_cycle.return_value = {
        "signals_checked": 3,
        "finalized": 1,
        "errors": 0,
    }

    result = route_prospective_experiment(
        "OTHER_OOS", generic, v3_enabled=False,
    )

    assert result["signals_checked"] == 3
    generic.run_evaluation_cycle.assert_called_once_with("OTHER_OOS")
