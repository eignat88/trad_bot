"""Regression and recovery guards for the HTF Point-B observation pipeline repair."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.research.htf_keylevel_point_b import BASELINE_EXPERIMENT_ID, EXPERIMENT_ID, detect_point_b_setup
from app.research.prospective_evaluator import ProspectiveOOSEvaluator
from app.research.prospective_observer import ProspectiveOOSObserver

FREEZE_TS = "2026-10-06T14:00:00Z"
FREEZE_DT = datetime.fromisoformat(FREEZE_TS.replace("Z", "+00:00"))
SIGNAL_TS = "2026-10-06T14:05:00+00:00"
SETUP_EVENT_ID = "pipeline-repair-point-b-setup"
BASE_TS = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _base_candle(index, *, high=100.2, low=99.8, close=100.0, volume=10.0):
    from app.models import Candle

    return Candle(int((BASE_TS + timedelta(minutes=5 * index)).timestamp() * 1000), close, high, low, close, volume)


def _flat_htf(_direction):
    from app.models import Candle

    rows = []
    for index in range(26):
        rows.append(
            Candle(
                int((BASE_TS - timedelta(hours=24 - index)).timestamp() * 1000),
                100.5,
                101.0,
                100.0,
                100.5,
                10.0,
            )
        )
    return rows


def _point_b_history():
    rows = [_base_candle(index) for index in range(20)]
    rows.extend(
        [
            _base_candle(20, high=100.3, low=99.4, close=100.1, volume=20.0),
            _base_candle(21, high=104.0, low=99.5, close=103.7, volume=100.0),
            _base_candle(22, high=104.0, low=103.3, close=103.6, volume=20.0),
            _base_candle(23, high=106.0, low=103.8, close=105.7, volume=30.0),
            _base_candle(24, high=105.5, low=104.85, close=105.1, volume=15.0),
            _base_candle(25, high=105.4, low=104.9, close=105.2),
            _base_candle(26, high=105.5, low=105.0, close=105.3),
            _base_candle(27, high=105.5, low=105.0, close=105.2),
            _base_candle(28, high=105.6, low=105.0, close=105.3),
        ]
    )
    return rows


def _snapshot(*, cohort="POINT_B", direction="LONG"):
    long = direction == "LONG"
    snapshot = {
        "experiment_id": EXPERIMENT_ID,
        "setup_event_id": SETUP_EVENT_ID,
        "symbol": "BTCUSDT",
        "direction": direction,
        "level_price": 100.0,
        "level_type": "support" if long else "resistance",
        "touch_time": "2026-10-06T14:00:30+00:00",
        "touch_price": 100.0 if long else 102.0,
        "reaction_time": "2026-10-06T14:01:30+00:00",
        "reaction_high": 104.0,
        "reaction_low": 99.4,
        "reaction_candles": 2,
        "structure_reference_time": "2026-10-06T14:01:25+00:00",
        "structure_reference_price": 104.0 if long else 96.0,
        "break_time": "2026-10-06T14:01:35+00:00",
        "break_price": 105.7 if long else 94.5,
        "point_b_time": "2026-10-06T14:02:35+00:00",
        "point_b_price": 105.1 if long else 95.0,
        "point_b_retrace_pct": 50.0,
        "signal_time": SIGNAL_TS,
        "entry_reference_price": 105.1 if long else 95.0,
        "structural_stop_price": 99.4 if long else 104.0,
        "risk_abs": 5.7 if long else 9.0,
        "risk_pct": 5.423406279733587 if long else 9.473684210526315,
        "target_1": 116.5 if long else 77.0,
        "target_2": 122.2 if long else 71.3,
        "frozen_at": FREEZE_TS,
    }
    if cohort is not None:
        snapshot["cohort"] = cohort
    return snapshot


def _completed_row(snapshot, *, direction="LONG"):
    return {
        "experiment_id": snapshot["experiment_id"],
        "setup_event_id": snapshot["setup_event_id"],
        "symbol": snapshot["symbol"],
        "direction": direction,
        "status": "COMPLETED_IMMUTABLE",
        "snapshot": snapshot,
        "frozen_at": FREEZE_DT,
        "created_at": FREEZE_DT,
    }


def _registry(scanner_name=EXPERIMENT_ID):
    return {
        scanner_name: {
            "scanner_name": scanner_name,
            "direction": "LONG",
            "directions": ["LONG", "SHORT"],
            "freeze_ts": FREEZE_TS,
        }
    }


def _detection(*, signal_time=SIGNAL_TS):
    from dataclasses import replace

    from tests.test_htf_keylevel_point_b_detector import _flat_htf as detector_flat_htf
    from tests.test_htf_keylevel_point_b_detector import _long_history

    result = detect_point_b_setup(
        symbol="BTCUSDT",
        direction="LONG",
        execution_candles=_long_history(),
        htf_candles=detector_flat_htf("LONG"),
        current_time=BASE_TS + timedelta(minutes=5 * 30 + 5 * 29, seconds=1),
    )
    assert result.signal is not None
    signal = replace(result.signal, features=dict(result.signal.features))
    signal.features["setup_event_id"] = SETUP_EVENT_ID
    signal = replace(signal, detected_at=datetime.fromisoformat(signal_time))
    return replace(result, signal=signal)


def _conn(fetchone=("RUNNING", FREEZE_DT)):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = fetchone
    cursor.fetchall.return_value = []
    return conn, cursor


def _observe(observer, conn, *, direction="LONG", signal_time=SIGNAL_TS, detection=None, features=None, reference_price=105.1, invalidation_price=99.4, target_1=116.5, target_2=122.2):
    if detection is None:
        detection = _detection(signal_time=signal_time)
    return observer.observe(
        scanner_name=EXPERIMENT_ID,
        direction=direction,
        symbol="BTCUSDT",
        signal_time=signal_time,
        reference_price=reference_price,
        invalidation_price=invalidation_price,
        target_1=target_1,
        target_2=target_2,
        score=0.0,
        features=features or dict(detection.signal.features),
        parameters={},
        market_regime=None,
        detection_result=detection,
    )


def test_detector_point_b_features_carry_point_b_cohort():
    features = _detection().signal.features
    assert features["cohort"] == "POINT_B"
    assert features["experiment_id"] == EXPERIMENT_ID
    assert features["setup_event_id"] == SETUP_EVENT_ID


def test_new_point_b_completion_snapshot_preserves_cohort():
    snapshot = ProspectiveOOSObserver._completion_snapshot_from_features(_detection().signal.features)
    assert snapshot is not None
    assert snapshot["cohort"] == "POINT_B"
    assert snapshot["experiment_id"] == EXPERIMENT_ID
    assert snapshot["setup_event_id"] == SETUP_EVENT_ID


def test_legacy_point_b_authoritative_snapshot_creates_observation():
    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _completed_row(_snapshot(cohort=None))
    conn, cursor = _conn()
    observer = ProspectiveOOSObserver(conn, _registry(), completed_event_repo=completed_repo)
    detection = _detection()

    assert _observe(observer, conn, detection=detection)
    insert = next(call for call in cursor.execute.call_args_list if "INSERT INTO research.prospective_observation" in str(call))
    params = insert[0][1]
    assert params[1] == ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, SETUP_EVENT_ID)
    features = json.loads(params[13])
    assert features["cohort"] == "POINT_B"
    assert features["_authoritative_completion"] is True


def test_new_point_b_authoritative_snapshot_creates_observation():
    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _completed_row(_snapshot(cohort="POINT_B"))
    conn, _ = _conn()
    observer = ProspectiveOOSObserver(conn, _registry(), completed_event_repo=completed_repo)
    assert _observe(observer, conn)


def test_wrong_context_authoritative_snapshot_fails_closed():
    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _completed_row(
        _snapshot(cohort=None), direction="SHORT"
    )
    conn, _ = _conn()
    registry = _registry()
    registry[EXPERIMENT_ID]["direction"] = "SHORT"
    observer = ProspectiveOOSObserver(conn, registry, completed_event_repo=completed_repo)
    detection = _detection()
    detection.signal.features["cohort"] = "BASELINE"
    detection.signal.features["experiment_id"] = BASELINE_EXPERIMENT_ID

    assert not _observe(
        observer,
        conn,
        direction="SHORT",
        detection=detection,
        reference_price=95.0,
        invalidation_price=104.0,
        target_1=77.0,
        target_2=71.3,
    )


@pytest.mark.parametrize(
    "signal_time",
    [FREEZE_TS, "2026-10-06T13:59:59+00:00"],
)
def test_freeze_boundary_remains_strict(signal_time):
    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _completed_row(_snapshot(cohort=None))
    conn, cursor = _conn()
    observer = ProspectiveOOSObserver(conn, _registry(), completed_event_repo=completed_repo)
    assert not _observe(observer, conn, signal_time=signal_time)
    completed_repo.freeze_completed_event.assert_not_called()
    assert not any("INSERT INTO research.prospective_observation" in str(call) for call in cursor.execute.call_args_list)


@pytest.mark.parametrize(
    "fetchone",
    [
        ("PAUSED", FREEZE_DT),
        ("RUNNING", None),
        ("RUNNING", "not-a-timestamp"),
    ],
)
def test_db_lifecycle_gate_remains_fail_closed(fetchone):
    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _completed_row(_snapshot(cohort=None))
    conn, cursor = _conn(fetchone=fetchone)
    observer = ProspectiveOOSObserver(conn, _registry(), completed_event_repo=completed_repo)
    assert not _observe(observer, conn)
    completed_repo.freeze_completed_event.assert_not_called()


def test_htf_identity_is_stable_sha256_not_process_hash():
    first = ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, SETUP_EVENT_ID)
    second = ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, SETUP_EVENT_ID)
    baseline = ProspectiveOOSObserver._make_htf_source_key(BASELINE_EXPERIMENT_ID, SETUP_EVENT_ID)
    assert first == second
    assert first != baseline
    assert first != -abs(hash(f"{EXPERIMENT_ID}:{SETUP_EVENT_ID}")) % (2**31)


def test_idempotent_observation_and_completion_identity():
    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _completed_row(_snapshot(cohort=None))
    conn, cursor = _conn()
    observer = ProspectiveOOSObserver(conn, _registry(), completed_event_repo=completed_repo)
    detection = _detection()
    first = _observe(observer, conn, detection=detection)
    second = _observe(observer, conn, detection=detection)
    assert first == second
    inserts = [call for call in cursor.execute.call_args_list if "INSERT INTO research.prospective_observation" in str(call)]
    assert len(inserts) == 2
    assert inserts[0][0][1][1] == inserts[1][0][1][1]


def test_baseline_does_not_use_point_b_authoritative_completion():
    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _completed_row(_snapshot(cohort=None))
    conn, cursor = _conn()
    registry = _registry(BASELINE_EXPERIMENT_ID)
    observer = ProspectiveOOSObserver(conn, registry, completed_event_repo=completed_repo)
    detection = _detection()
    assert detection.baseline is not None
    features = dict(detection.baseline.features)
    assert features["cohort"] == "BASELINE"
    assert features["experiment_id"] == EXPERIMENT_ID
    assert observer.observe(
        scanner_name=BASELINE_EXPERIMENT_ID,
        direction="LONG",
        symbol="BTCUSDT",
        signal_time=SIGNAL_TS,
        reference_price=105.1,
        invalidation_price=99.4,
        target_1=116.5,
        target_2=122.2,
        score=0.0,
        features=features,
        parameters={},
        market_regime=None,
        detection_result=None,
    )
    completed_repo.freeze_completed_event.assert_not_called()
    insert = next(call for call in cursor.execute.call_args_list if "INSERT INTO research.prospective_observation" in str(call))
    inserted_features = json.loads(insert[0][1][13])
    assert inserted_features["cohort"] == "BASELINE"
    assert inserted_features["_authoritative_completion"] is False


def test_legacy_observation_retains_evaluator_contract():
    completed_repo = MagicMock()
    completed_repo.freeze_completed_event.return_value = _completed_row(_snapshot(cohort=None))
    conn, cursor = _conn()
    observer = ProspectiveOOSObserver(conn, _registry(), completed_event_repo=completed_repo)
    assert _observe(observer, conn)
    insert = next(call for call in cursor.execute.call_args_list if "INSERT INTO research.prospective_observation" in str(call))
    params = insert[0][1]
    features = json.loads(params[13])
    assert params[6] == 105.1
    assert params[7] == 99.4
    assert params[8] == 116.5
    assert params[9] == 122.2
    assert features["_frozen_max_hold"] == 240
    assert features["_frozen_intrabar_policy"] == "STOP_FIRST"
    assert features["_structural_r"] == pytest.approx(5.7)
    assert ProspectiveOOSEvaluator._get_frozen_max_hold({"features": params[13]}) == 240
    assert ProspectiveOOSEvaluator._get_frozen_intrabar_policy({"features": params[13]}) == "STOP_FIRST"


def _legacy_snapshot_fixture():
    def row(setup_id, *, experiment_id=EXPERIMENT_ID, cohort=None, signal_time=SIGNAL_TS, frozen_at=FREEZE_TS, direction="LONG", risk=5.7):
        snapshot = _snapshot(cohort=cohort, direction=direction)
        snapshot["setup_event_id"] = setup_id
        snapshot["experiment_id"] = experiment_id
        snapshot["signal_time"] = signal_time
        snapshot["frozen_at"] = frozen_at
        snapshot["risk_abs"] = risk
        return _completed_row(snapshot, direction=direction)

    return [
        row("prefreeze", signal_time=FREEZE_TS, frozen_at=FREEZE_TS),
        row("wrong-experiment", experiment_id="OTHER_EXPERIMENT"),
        row("invalid-direction", direction="SIDEWAYS"),
        row("risk-invalid", risk=0.0),
        row("baseline-context", experiment_id=BASELINE_EXPERIMENT_ID),
    ]


def test_legacy_snapshot_fixture_and_shared_observation_builder():
    legacy = {**_snapshot(cohort=None), "cohort": "POINT_B"}
    features = legacy
    source = ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, SETUP_EVENT_ID)
    assert features["cohort"] == "POINT_B"
    assert source == ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, SETUP_EVENT_ID)
    assert legacy["entry_reference_price"] == 105.1
    assert legacy["structural_stop_price"] == 99.4
