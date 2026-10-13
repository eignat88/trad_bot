from unittest.mock import MagicMock

import pytest

from app.research.me_reverse_long_v3.evaluator import (
    MEReverseLongV3ExecutionEvaluator,
)


@pytest.mark.parametrize("failure_at", ["execute", "fetchall"])
def test_failed_eligibility_rolls_back_and_closes_cursor(failure_at):
    conn = MagicMock()
    cursor = conn.cursor.return_value

    if failure_at == "execute":
        cursor.execute.side_effect = RuntimeError("SQL statement failed")
    else:
        cursor.fetchall.side_effect = RuntimeError("SQL fetch failed")

    evaluator = MEReverseLongV3ExecutionEvaluator(
        conn=conn,
        candle_source=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="SQL"):
        evaluator._eligible()

    conn.rollback.assert_called_once()
    cursor.close.assert_called_once()


def test_successful_eligibility_does_not_rollback():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchall.return_value = []

    evaluator = MEReverseLongV3ExecutionEvaluator(
        conn=conn,
        candle_source=MagicMock(),
    )

    assert evaluator._eligible() == []

    conn.rollback.assert_not_called()
    cursor.close.assert_called_once()
