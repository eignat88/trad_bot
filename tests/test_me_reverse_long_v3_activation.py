from datetime import datetime, timedelta, timezone

from app.research.me_reverse_long_v3.activation import (
    is_v3_evaluator_activated,
)
from app.research.me_reverse_long_v3.protocol import EXPERIMENT_ID

FREEZE = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)


def check(**overrides):
    data = dict(
        enabled=True,
        registry_spec={
            "experiment_id": EXPERIMENT_ID,
            "status": "RUNNING",
            "freeze_ts": FREEZE.isoformat(),
        },
        db_status="RUNNING",
        db_started_at=FREEZE,
    )
    data.update(overrides)
    return is_v3_evaluator_activated(**data)


def test_matching_activation():
    assert check()


def test_disabled():
    assert not check(enabled=False)


def test_inactive_registry():
    assert not check(registry_spec={
        "experiment_id": EXPERIMENT_ID,
        "status": "INACTIVE",
        "freeze_ts": FREEZE.isoformat(),
    })


def test_missing_freeze():
    assert not check(registry_spec={
        "experiment_id": EXPERIMENT_ID,
        "status": "RUNNING",
        "freeze_ts": None,
    })


def test_db_paused():
    assert not check(db_status="PAUSED")


def test_freeze_mismatch():
    assert not check(
        db_started_at=FREEZE + timedelta(seconds=1)
    )


def test_wrong_experiment():
    assert not check(registry_spec={
        "experiment_id": "OTHER_EXPERIMENT",
        "status": "RUNNING",
        "freeze_ts": FREEZE.isoformat(),
    })


def test_naive_db_time_rejected():
    assert not check(
        db_started_at=FREEZE.replace(tzinfo=None)
    )
