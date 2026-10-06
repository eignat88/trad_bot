"""Focused persistence test for HTF Point-B first-writer-wins lifecycle storage."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.research.prospective_completed_event_repository import (
    COMPLETED_IMMUTABLE,
    ProspectiveCompletedEventRepository,
)
from app.research.prospective_observer import ProspectiveOOSObserver


EXPERIMENT_ID = "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE"
SETUP_EVENT_ID = "touch-only-identity"
FROZEN_AT = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _snapshot(direction: str = "LONG") -> dict:
    return {
        "symbol": "BTCUSDT",
        "direction": direction,
        "level_price": 100.0,
        "level_type": "support" if direction == "LONG" else "resistance",
        "touch_time": "2026-10-03T11:00:00+00:00",
        "touch_price": 100.0 if direction == "LONG" else 101.0,
        "reaction_time": "2026-10-03T11:05:00+00:00",
        "reaction_high": 104.0,
        "reaction_low": 99.4,
        "reaction_candles": 2,
        "structure_reference_time": "2026-10-03T11:10:00+00:00",
        "structure_reference_price": 104.0 if direction == "LONG" else 96.0,
        "break_time": "2026-10-03T11:15:00+00:00",
        "break_price": 105.7 if direction == "LONG" else 94.5,
        "point_b_time": "2026-10-03T11:20:00+00:00",
        "point_b_price": 105.1 if direction == "LONG" else 95.0,
        "point_b_retrace_pct": 50.0,
        "signal_time": "2026-10-03T11:20:00+00:00",
        "entry_reference_price": 105.1 if direction == "LONG" else 95.0,
        "structural_stop_price": 99.4 if direction == "LONG" else 104.0,
        "risk_abs": 5.7 if direction == "LONG" else 9.0,
        "risk_pct": 5.423406279733587 if direction == "LONG" else 9.473684210526315,
        "target_1": 116.5 if direction == "LONG" else 77.0,
        "target_2": 122.2 if direction == "LONG" else 71.3,
        "experiment_id": EXPERIMENT_ID,
        "setup_event_id": SETUP_EVENT_ID,
        "frozen_at": FROZEN_AT.isoformat(),
    }


def _row(snapshot_json: str | dict) -> tuple:
    return (
        EXPERIMENT_ID,
        SETUP_EVENT_ID,
        "BTCUSDT",
        "LONG",
        COMPLETED_IMMUTABLE,
        snapshot_json,
        FROZEN_AT,
        FROZEN_AT,
    )


def test_freeze_completed_event_insert_wins_first():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    payload = _snapshot()
    cursor.fetchone.return_value = _row(payload)

    result = ProspectiveCompletedEventRepository(conn).freeze_completed_event(
        experiment_id=EXPERIMENT_ID,
        setup_event_id=SETUP_EVENT_ID,
        snapshot=payload,
    )

    assert result is not None
    assert result["status"] == COMPLETED_IMMUTABLE
    assert result["snapshot"]["point_b_price"] == 105.1
    assert result["snapshot"]["target_1"] == 116.5
    assert result["snapshot"]["target_2"] == 122.2
    insert_call = cursor.execute.call_args_list[0]
    assert "INSERT INTO research.prospective_completed_event" in insert_call[0][0]
    assert "ON CONFLICT (experiment_id, setup_event_id) DO NOTHING" in insert_call[0][0]
    assert not any("UPDATE research.prospective_completed_event" in str(call) for call in cursor.execute.call_args_list)
    conn.commit.assert_called_once()


def test_freeze_completed_event_conflict_keeps_authoritative_snapshot():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    authoritative_snapshot = _snapshot()
    cursor.fetchone.side_effect = [
        None,
        _row(authoritative_snapshot),
    ]

    result = ProspectiveCompletedEventRepository(conn).freeze_completed_event(
        experiment_id=EXPERIMENT_ID,
        setup_event_id=SETUP_EVENT_ID,
        snapshot=_snapshot(),
        frozen_at=FROZEN_AT,
    )

    assert result is not None
    assert result["snapshot"]["point_b_price"] == 105.1
    assert cursor.execute.call_args_list[0][0][1][4] == COMPLETED_IMMUTABLE
    assert "ON CONFLICT (experiment_id, setup_event_id) DO NOTHING" in cursor.execute.call_args_list[0][0][0]
    assert cursor.execute.call_args_list[1][0][1] == (EXPERIMENT_ID, SETUP_EVENT_ID)
    assert not any("UPDATE research.prospective_completed_event" in str(call) for call in cursor.execute.call_args_list)
    conn.commit.assert_called_once()


def test_observer_freezes_first_valid_point_b_completion():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = (
        "RUNNING",
        datetime.fromisoformat("2026-10-03T11:19:00+00:00"),
    )

    features = _snapshot()
    features["key_level_price"] = features.pop("level_price")
    features["key_level_type"] = features.pop("level_type")
    features["cohort"] = "POINT_B"
    result = MagicMock()
    result.signal.features = features
    result.signal.direction = "LONG"
    result.signal.symbol = "BTCUSDT"
    result.signal.reference_price = 105.1
    result.signal.invalidation_price = 99.4
    result.signal.target_1 = 116.5
    result.signal.target_2 = 122.2
    result.signal.score = 0.0
    result.signal.detected_at = FROZEN_AT

    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _row(_snapshot())
    observer = ProspectiveOOSObserver(
        conn,
        {
            EXPERIMENT_ID: {
                "scanner_name": EXPERIMENT_ID,
                "direction": "LONG",
                "freeze_ts": "2026-10-03T11:19:00+00:00",
            }
        },
        completed_event_repo=completed_repo,
    )
    observer.observe(
        scanner_name=EXPERIMENT_ID,
        direction="LONG",
        symbol="BTCUSDT",
        signal_time=FROZEN_AT,
        reference_price=105.1,
        invalidation_price=99.4,
        target_1=116.5,
        target_2=122.2,
        score=0.0,
        features=dict(features),
        parameters={},
        market_regime=None,
        detection_result=result,
    )

    completed_repo.freeze_completed_event.assert_called_once()
    call = completed_repo.freeze_completed_event.call_args
    assert call.kwargs["experiment_id"] == EXPERIMENT_ID
    assert call.kwargs["setup_event_id"] == SETUP_EVENT_ID
    assert call.kwargs["snapshot"]["level_price"] == 100.0
    assert call.kwargs["snapshot"]["reaction_high"] == 104.0
    assert call.kwargs["snapshot"]["reaction_low"] == 99.4
    assert call.kwargs["snapshot"]["point_b_retrace_pct"] == 50.0
    assert call.kwargs["snapshot"]["signal_time"] == "2026-10-03T11:20:00+00:00"



def _make_point_b_detection_result():
    features = _snapshot()
    features["key_level_price"] = features.pop("level_price")
    features["key_level_type"] = features.pop("level_type")
    features["cohort"] = "POINT_B"

    result = MagicMock()
    result.signal.features = features
    result.signal.direction = "LONG"
    result.signal.symbol = "BTCUSDT"
    result.signal.reference_price = 105.1
    result.signal.invalidation_price = 99.4
    result.signal.target_1 = 116.5
    result.signal.target_2 = 122.2
    result.signal.score = 0.0
    result.signal.detected_at = FROZEN_AT
    return result, features


def _observe_point_b(
    *,
    registry,
    signal_time="2026-10-03T11:20:00+00:00",
    scanner_name=EXPERIMENT_ID,
    db_status="RUNNING",
    detection_result=None,
):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    freeze_ts = registry.get(scanner_name, {}).get("freeze_ts")
    started_at = (
        datetime.fromisoformat(str(freeze_ts).replace("Z", "+00:00"))
        if db_status == "RUNNING" and freeze_ts
        else None
    )
    cursor.fetchone.return_value = (db_status, started_at)
    cursor.fetchall.return_value = []

    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _row(_snapshot())

    if detection_result is None:
        detection_result, features = _make_point_b_detection_result()
    else:
        features = dict(detection_result.signal.features)

    observer = ProspectiveOOSObserver(
        conn,
        registry,
        completed_event_repo=completed_repo,
    )

    observation_ids = observer.observe(
        scanner_name=scanner_name,
        direction="LONG",
        symbol="BTCUSDT",
        signal_time=signal_time,
        reference_price=105.1,
        invalidation_price=99.4,
        target_1=116.5,
        target_2=122.2,
        score=0.0,
        features=dict(features),
        parameters={},
        market_regime=None,
        detection_result=detection_result,
    )

    return observation_ids, completed_repo


def test_htf_completion_not_frozen_at_or_before_freeze_ts():
    freeze_ts = "2026-10-03T11:20:00+00:00"
    registry = {
        EXPERIMENT_ID: {
            "scanner_name": EXPERIMENT_ID,
            "direction": "LONG",
            "freeze_ts": freeze_ts,
        }
    }

    _, repo_equal = _observe_point_b(
        registry=registry,
        signal_time="2026-10-03T11:20:00+00:00",
    )
    repo_equal.freeze_completed_event.assert_not_called()

    _, repo_before = _observe_point_b(
        registry=registry,
        signal_time="2026-10-03T11:19:59+00:00",
    )
    repo_before.freeze_completed_event.assert_not_called()


def test_htf_completion_frozen_strictly_after_freeze_ts():
    registry = {
        EXPERIMENT_ID: {
            "scanner_name": EXPERIMENT_ID,
            "direction": "LONG",
            "freeze_ts": "2026-10-03T11:20:00+00:00",
        }
    }

    _, completed_repo = _observe_point_b(
        registry=registry,
        signal_time="2026-10-03T11:20:01+00:00",
    )

    completed_repo.freeze_completed_event.assert_called_once()


def test_htf_completion_not_frozen_for_wrong_scanner():
    registry = {
        EXPERIMENT_ID: {
            "scanner_name": EXPERIMENT_ID,
            "direction": "LONG",
            "freeze_ts": "2026-10-03T11:19:00+00:00",
        }
    }

    _, completed_repo = _observe_point_b(
        registry=registry,
        scanner_name="SOME_OTHER_SCANNER",
    )

    completed_repo.freeze_completed_event.assert_not_called()


def test_htf_completion_not_frozen_for_terminal_experiment():
    registry = {
        EXPERIMENT_ID: {
            "scanner_name": EXPERIMENT_ID,
            "direction": "LONG",
            "freeze_ts": "2026-10-03T11:19:00+00:00",
        }
    }

    _, completed_repo = _observe_point_b(
        registry=registry,
        db_status="CANCELLED",
    )

    completed_repo.freeze_completed_event.assert_not_called()


def test_htf_authoritative_snapshot_drives_point_b_observation():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.side_effect = [
        ("RUNNING",),
        (
            "RUNNING",
            datetime.fromisoformat("2026-10-03T11:19:00+00:00"),
        ),
        (77,),
        None,
    ]

    frozen_snapshot = _snapshot()
    frozen_snapshot["cohort"] = "POINT_B"
    frozen_snapshot["point_b_retrace_pct"] = 33.2653
    frozen_snapshot["target_1"] = 116.5
    frozen_snapshot["target_2"] = 122.2
    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = {"snapshot": frozen_snapshot}

    current_result, features = _make_point_b_detection_result()
    features["point_b_retrace_pct"] = 66.8581
    features["entry_reference_price"] = 999.0
    features["structural_stop_price"] = 1.0
    features["target_1"] = 999.0
    features["target_2"] = 888.0
    current_result.signal.features = features

    registry = {
        EXPERIMENT_ID: {
            "scanner_name": EXPERIMENT_ID,
            "direction": "LONG",
            "freeze_ts": "2026-10-03T11:19:00+00:00",
        }
    }
    observer = ProspectiveOOSObserver(conn, registry, completed_event_repo=completed_repo)
    assert observer.observe(
        scanner_name=EXPERIMENT_ID,
        direction="LONG",
        symbol="BTCUSDT",
        signal_time=FROZEN_AT,
        reference_price=999.0,
        invalidation_price=1.0,
        target_1=999.0,
        target_2=888.0,
        score=0.0,
        features=dict(features),
        parameters={},
        market_regime=None,
        detection_result=current_result,
    ) == [77]

    observation_call = next(
        call for call in cursor.execute.call_args_list
        if "INSERT INTO research.prospective_observation" in str(call)
    )
    params = observation_call[0][1]
    assert params[1] == ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, SETUP_EVENT_ID)
    assert params[3:5] == ("BTCUSDT", "LONG")
    assert params[6] == frozen_snapshot["entry_reference_price"]
    assert params[7] == frozen_snapshot["structural_stop_price"]
    assert params[8] == frozen_snapshot["target_1"]
    assert params[9] == frozen_snapshot["target_2"]
    assert '"point_b_retrace_pct": 33.2653' in params[13]
    assert "999.0" not in params[13]
    assert "66.8581" not in params[13]


def test_htf_completion_requires_freeze_ts():
    registry = {
        EXPERIMENT_ID: {
            "scanner_name": EXPERIMENT_ID,
            "direction": "LONG",
        }
    }

    _, completed_repo = _observe_point_b(registry=registry)

    completed_repo.freeze_completed_event.assert_not_called()


def test_htf_baseline_never_freezes_point_b_completion():
    baseline_id = "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE"
    registry = {
        baseline_id: {
            "scanner_name": baseline_id,
            "direction": "LONG",
            "freeze_ts": "2026-10-03T11:19:00+00:00",
        }
    }

    detection_result, features = _make_point_b_detection_result()
    features["cohort"] = "BASELINE"
    detection_result.signal.features = features

    _, completed_repo = _observe_point_b(
        registry=registry,
        scanner_name=baseline_id,
        detection_result=detection_result,
    )

    completed_repo.freeze_completed_event.assert_not_called()


def test_htf_source_identity_is_restart_stable_and_experiment_scoped():
    first = ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, SETUP_EVENT_ID)
    second = ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, SETUP_EVENT_ID)
    baseline_id = "HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE"
    baseline = ProspectiveOOSObserver._make_htf_source_key(baseline_id, SETUP_EVENT_ID)
    other_setup = ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, "other-touch")

    payload = f"{EXPERIMENT_ID}:{SETUP_EVENT_ID}".encode("utf-8")
    expected = -(int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % (2**31 - 1)) - 1
    assert first == second == expected
    assert first != baseline
    assert first != other_setup
    assert all(-2**31 < key < -1 for key in (first, second, baseline, other_setup))
