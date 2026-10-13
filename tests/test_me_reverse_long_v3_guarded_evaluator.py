import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.research.me_reverse_long_v3.guarded_evaluator import (
    GuardedV3Evaluator,
)
from app.research.me_reverse_long_v3.protocol import EXPERIMENT_ID

FREEZE = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)


def test_disabled_never_calls_evaluator(tmp_path):
    evaluator = MagicMock()
    guard = GuardedV3Evaluator(
        conn=MagicMock(),
        evaluator=evaluator,
        registry_path=tmp_path / "missing.json",
        explicitly_enabled=False,
    )

    result = guard.run_evaluation_cycle()

    assert result["skipped"] == 1
    evaluator.run_evaluation_cycle.assert_not_called()


def test_running_to_paused_stops_next_cycle(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "experiments": [{
            "experiment_id": EXPERIMENT_ID,
            "status": "RUNNING",
            "freeze_ts": FREEZE.isoformat(),
        }]
    }))

    evaluator = MagicMock()
    evaluator.run_evaluation_cycle.return_value = {
        "checked": 1, "finalized": 1, "errors": 0,
    }

    conn = MagicMock()

    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        side_effect=[
            ("RUNNING", FREEZE),
            ("PAUSED", FREEZE),
        ],
    ):
        guard = GuardedV3Evaluator(
            conn=conn,
            evaluator=evaluator,
            registry_path=path,
            explicitly_enabled=True,
        )

        first = guard.run_evaluation_cycle()
        second = guard.run_evaluation_cycle()

    assert first["finalized"] == 1
    assert second["skipped"] == 1
    assert evaluator.run_evaluation_cycle.call_count == 1


def test_freeze_mismatch_never_runs(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "experiments": [{
            "experiment_id": EXPERIMENT_ID,
            "status": "RUNNING",
            "freeze_ts": FREEZE.isoformat(),
        }]
    }))

    evaluator = MagicMock()

    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        return_value=("RUNNING", FREEZE.replace(second=1)),
    ):
        guard = GuardedV3Evaluator(
            conn=MagicMock(),
            evaluator=evaluator,
            registry_path=path,
            explicitly_enabled=True,
        )

        assert guard.run_evaluation_cycle()["skipped"] == 1

    evaluator.run_evaluation_cycle.assert_not_called()
