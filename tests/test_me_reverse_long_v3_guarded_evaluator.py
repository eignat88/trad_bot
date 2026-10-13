import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.research.me_reverse_long_v3.activation import V3ActivationIdentity
from app.research.me_reverse_long_v3.guarded_evaluator import GuardedV3Evaluator
from app.research.me_reverse_long_v3.protocol import EXPERIMENT_ID

FREEZE = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
STARTUP = V3ActivationIdentity(
    experiment_id=EXPERIMENT_ID,
    status="RUNNING",
    activation_ns=int(FREEZE.timestamp() * 1_000_000_000),
)


def registry(tmp_path, *, freeze=FREEZE, status="RUNNING"):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "experiments": [{
            "experiment_id": EXPERIMENT_ID,
            "status": status,
            "freeze_ts": freeze.isoformat(),
        }]
    }))
    return path


def test_disabled_never_calls_evaluator(tmp_path):
    evaluator = MagicMock()
    guard = GuardedV3Evaluator(
        conn=MagicMock(),
        evaluator=evaluator,
        registry_path=registry(tmp_path),
        explicitly_enabled=False,
    )

    result = guard.run_evaluation_cycle()

    assert result["skipped"] == 1
    evaluator.run_evaluation_cycle.assert_not_called()


def test_startup_active_evaluates_and_runtime_loss_is_terminal(tmp_path):
    evaluator = MagicMock()
    evaluator.run_evaluation_cycle.return_value = {
        "checked": 1, "finalized": 1, "errors": 0,
    }

    guard = GuardedV3Evaluator(
        conn=MagicMock(),
        evaluator=evaluator,
        registry_path=registry(tmp_path),
        explicitly_enabled=True,
        startup_activation=STARTUP,
    )

    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        side_effect=[("RUNNING", FREEZE), ("PAUSED", FREEZE), ("RUNNING", FREEZE)],
    ):
        first = guard.run_evaluation_cycle()
        second = guard.run_evaluation_cycle()
        third = guard.run_evaluation_cycle()

    assert first["finalized"] == 1
    assert second["skipped"] == 1
    assert third["skipped"] == 1
    assert evaluator.run_evaluation_cycle.call_count == 1


def test_running_to_paused_stops_next_cycle(tmp_path):
    evaluator = MagicMock()
    evaluator.run_evaluation_cycle.return_value = {
        "checked": 1, "finalized": 1, "errors": 0,
    }

    guard = GuardedV3Evaluator(
        conn=MagicMock(),
        evaluator=evaluator,
        registry_path=registry(tmp_path),
        explicitly_enabled=True,
        startup_activation=STARTUP,
    )

    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        side_effect=[("RUNNING", FREEZE), ("PAUSED", FREEZE)],
    ):
        first = guard.run_evaluation_cycle()
        second = guard.run_evaluation_cycle()

    assert first["finalized"] == 1
    assert second["skipped"] == 1
    assert evaluator.run_evaluation_cycle.call_count == 1


def test_freeze_transition_invalidates_permanently(tmp_path):
    evaluator = MagicMock()
    evaluator.run_evaluation_cycle.return_value = {
        "checked": 1, "finalized": 1, "errors": 0,
    }
    path = registry(tmp_path)
    guard = GuardedV3Evaluator(
        conn=MagicMock(),
        evaluator=evaluator,
        registry_path=path,
        explicitly_enabled=True,
        startup_activation=STARTUP,
    )

    changed = FREEZE.replace(second=1)
    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        side_effect=[("RUNNING", FREEZE), ("RUNNING", changed), ("RUNNING", FREEZE)],
    ):
        assert guard.run_evaluation_cycle()["finalized"] == 1
        assert guard.run_evaluation_cycle()["skipped"] == 1
        assert guard.run_evaluation_cycle()["skipped"] == 1

    assert evaluator.run_evaluation_cycle.call_count == 1


def test_terminal_status_never_reactivates(tmp_path):
    evaluator = MagicMock()
    evaluator.run_evaluation_cycle.return_value = {
        "checked": 1, "finalized": 1, "errors": 0,
    }
    guard = GuardedV3Evaluator(
        conn=MagicMock(),
        evaluator=evaluator,
        registry_path=registry(tmp_path),
        explicitly_enabled=True,
        startup_activation=STARTUP,
    )

    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        side_effect=[("RUNNING", FREEZE), ("COMPLETED", FREEZE), ("RUNNING", FREEZE)],
    ):
        assert guard.run_evaluation_cycle()["finalized"] == 1
        assert guard.run_evaluation_cycle()["skipped"] == 1
        assert guard.run_evaluation_cycle()["skipped"] == 1

    assert evaluator.run_evaluation_cycle.call_count == 1


def test_db_error_invalidates_process_and_recovers_connection(tmp_path):
    evaluator = MagicMock()
    evaluator.run_evaluation_cycle.return_value = {
        "checked": 1, "finalized": 1, "errors": 0,
    }
    conn = MagicMock()
    guard = GuardedV3Evaluator(
        conn=conn,
        evaluator=evaluator,
        registry_path=registry(tmp_path),
        explicitly_enabled=True,
        startup_activation=STARTUP,
    )

    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        side_effect=[("RUNNING", FREEZE), RuntimeError("db down"), ("RUNNING", FREEZE)],
    ):
        assert guard.run_evaluation_cycle()["finalized"] == 1
        assert guard.run_evaluation_cycle()["errors"] == 1
        assert guard.run_evaluation_cycle()["skipped"] == 1

    assert evaluator.run_evaluation_cycle.call_count == 1


def test_new_process_with_approved_activation_may_resume(tmp_path):
    path = registry(tmp_path)
    evaluator = MagicMock()
    evaluator.run_evaluation_cycle.return_value = {
        "checked": 1, "finalized": 1, "errors": 0,
    }

    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        return_value=("RUNNING", FREEZE),
    ):
        guard = GuardedV3Evaluator(
            conn=MagicMock(),
            evaluator=evaluator,
            registry_path=path,
            explicitly_enabled=True,
            startup_activation=STARTUP,
        )
        result = guard.run_evaluation_cycle()

    assert result["finalized"] == 1
    assert evaluator.run_evaluation_cycle.call_count == 1



def test_invalid_startup_identity_never_evaluates(tmp_path):
    evaluator = MagicMock()
    guard = GuardedV3Evaluator(
        conn=MagicMock(),
        evaluator=evaluator,
        registry_path=registry(tmp_path),
        explicitly_enabled=True,
        startup_activation=V3ActivationIdentity(
            experiment_id=EXPERIMENT_ID,
            status="RUNNING",
            activation_ns=STARTUP.activation_ns + 1,
        ),
    )

    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        return_value=("RUNNING", FREEZE),
    ):
        result = guard.run_evaluation_cycle()

    assert result["skipped"] == 1
    evaluator.run_evaluation_cycle.assert_not_called()


@pytest.mark.parametrize("freeze,status,db_status,db_freeze", [
    (FREEZE, "PAUSED", "RUNNING", FREEZE),
    (FREEZE, "RUNNING", "PAUSED", FREEZE),
    (FREEZE, "RUNNING", "COMPLETED", FREEZE),
    (FREEZE, "RUNNING", "RUNNING", FREEZE.replace(second=1)),
    (FREEZE.replace(second=1), "RUNNING", "RUNNING", FREEZE),
    (FREEZE, "RUNNING", "RUNNING", None),
])
def test_all_mismatch_matrix_never_evaluates(
    tmp_path, freeze, status, db_status, db_freeze,
):
    evaluator = MagicMock()
    guard = GuardedV3Evaluator(
        conn=MagicMock(),
        evaluator=evaluator,
        registry_path=registry(tmp_path, freeze=freeze, status=status),
        explicitly_enabled=True,
        startup_activation=STARTUP,
    )

    with patch(
        "app.research.me_reverse_long_v3.guarded_evaluator.load_v3_db_activation",
        return_value=(db_status, db_freeze),
    ):
        result = guard.run_evaluation_cycle()

    assert result["skipped"] == 1
    evaluator.run_evaluation_cycle.assert_not_called()


def test_inactive_at_startup_cannot_activate_on_later_cycle(tmp_path):
    """An inactive process must never self-activate after DB resumes."""
    evaluator = MagicMock()

    guard = GuardedV3Evaluator(
        conn=MagicMock(),
        evaluator=evaluator,
        registry_path=registry(tmp_path),
        explicitly_enabled=True,
        startup_activation=None,
    )

    with (
        patch(
            "app.research.me_reverse_long_v3.guarded_evaluator."
            "load_v3_db_activation",
            return_value=("RUNNING", FREEZE),
        ) as load_activation,
        patch(
            "app.research.me_reverse_long_v3.guarded_evaluator."
            "resolve_startup_v3_activation",
            return_value=STARTUP,
        ) as resolve_activation,
    ):
        first = guard.run_evaluation_cycle()
        second = guard.run_evaluation_cycle()

    assert first["skipped"] == 1
    assert second["skipped"] == 1
    evaluator.run_evaluation_cycle.assert_not_called()
    load_activation.assert_not_called()
    resolve_activation.assert_not_called()
