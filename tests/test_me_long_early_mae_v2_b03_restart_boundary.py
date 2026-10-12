from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.research.me_r_long_early_mae_v2.evaluator import MeEarlyMaeV2Evaluator


def test_restart_preserves_source_availability_boundary():
    available = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    source = {
        "source_event_id": 123,
        "source_status": "DETECTED",
        "source_available_at": available,
    }

    repository = MagicMock()
    evaluator = MeEarlyMaeV2Evaluator(repository)
    evaluator._attempt_fill = MagicMock()

    for minutes in (5, 10, 15):
        evaluator._process_source(
            dict(source),
            available + timedelta(minutes=minutes),
            {},
        )

    calls = evaluator._attempt_fill.call_args_list
    assert len(calls) == 3

    boundaries = [
        call.args[0]["source_available_at"]
        for call in calls
    ]

    assert boundaries == [available, available, available]
    repository.mark_entry_observable.assert_not_called()
