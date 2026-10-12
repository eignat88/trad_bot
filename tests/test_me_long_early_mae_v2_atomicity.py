"""Atomic persistence failure-injection tests for Early MAE V2."""
from unittest.mock import MagicMock

import pytest

from app.research.me_r_long_early_mae_v2.protocol import VARIANTS
from app.research.me_r_long_early_mae_v2.repository import MeEarlyMaeV2Repository


def test_fourth_variant_update_rolls_back_all():
    connection = MagicMock()
    cursor = connection.cursor.return_value

    # First fetchone: locked source, second fetchall: seven locked variants.
    cursor.fetchone.return_value = ("ENTRY_FILLED",)
    cursor.fetchall.return_value = [
        (
            v.variant_id, "PENDING",
            None, None, None, None, "PENDING",
            None, None, None, None, None, None,
            False, None,
        )
        for v in VARIANTS
    ]

    update_calls = 0

    def execute(sql, params=None):
        nonlocal update_calls
        if "UPDATE" in sql and "variant_outcome" in sql:
            update_calls += 1
            if update_calls == 4:
                raise RuntimeError("Injected fourth variant failure")
        cursor.rowcount = 1

    cursor.execute.side_effect = execute

    repository = MeEarlyMaeV2Repository(connection)

    baseline = {
        "exit_at": None,
        "exit_price": 100.0,
        "exit_reason": "FIXED_HORIZON",
        "gross_r": 0.0,
        "net_r": -0.084,
        "coverage": "COMPLETE",
    }

    evaluations = [
        {
            "variant_id": v.variant_id,
            "variant_status": "EVALUATED_NO_TRIGGER",
            "mae_r": 0.0,
            "mfe_r": 0.0,
            "window_low": 100.0,
            "window_high": 100.0,
            "trigger_result": "NO_TRIGGER",
            "variant_exit_at": None,
            "variant_exit_price": 100.0,
            "variant_exit_reason": "FIXED_HORIZON",
            "variant_gross_r": 0.0,
            "variant_net_r": -0.084,
            "delta_net_r": 0.0,
            "ambiguous_ordering": False,
            "coverage_reason": None,
        }
        for v in VARIANTS
    ]

    with pytest.raises(RuntimeError, match="Injected fourth variant failure"):
        repository.save_paired_outcome(1, baseline, evaluations)

    assert update_calls == 4
    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
