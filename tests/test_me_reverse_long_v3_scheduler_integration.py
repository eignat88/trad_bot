from unittest.mock import MagicMock, patch

import pytest

from app.research.evaluator_runner import _run_single_cycle
from app.research.me_reverse_long_v3.protocol import EXPERIMENT_ID


@pytest.mark.parametrize("enabled", [False, True])
def test_once_v3_does_not_raise_missing_horizons(enabled):
    generic = MagicMock()
    v3 = MagicMock() if enabled else None

    if v3 is not None:
        v3.run_evaluation_cycle.return_value = {
            "checked": 2,
            "finalized": 1,
            "errors": 0,
        }

    with (
        patch(
            "app.research.evaluator_runner._load_active_experiments",
            return_value=[],
        ),
        patch(
            "app.research.evaluator_runner._load_prospective_experiments",
            return_value=[EXPERIMENT_ID],
        ),
    ):
        _run_single_cycle(
            MagicMock(), MagicMock(), generic, v3
        )

    generic.run_evaluation_cycle.assert_not_called()

    if enabled:
        v3.run_evaluation_cycle.assert_called_once()


def test_repository_locks_experiment_lifecycle():
    from app.research.me_reverse_long_v3.repository import (
        MEReverseLongV3OutcomeRepository,
        V3OutcomeRecord,
    )
    from datetime import datetime, timezone

    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = None

    record = V3OutcomeRecord(
        observation_id=999,
        status="PENDING",
        filter_group="PASS",
        execution_entry_at=datetime.now(timezone.utc),
        evaluated_at=datetime.now(timezone.utc),
    )

    result = MEReverseLongV3OutcomeRepository(conn).upsert(record)

    sql = conn.cursor.return_value.execute.call_args.args[0]

    assert result == "NOT_ELIGIBLE"
    assert "FOR UPDATE OF o" in sql
    assert "FOR SHARE OF e" in sql
    conn.rollback.assert_called_once()
