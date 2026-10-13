from app.research.me_reverse_long_v3.entry_policy import next_executable_entry_at
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.research.prospective_observer import ProspectiveOOSObserver
from app.research.me_reverse_long_v3.protocol import EXPERIMENT_ID, SOURCE_SCANNER

UTC = timezone.utc
FREEZE = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)

ENTRY = next_executable_entry_at(FREEZE + timedelta(seconds=1))


class FrozenObserverDatetime(datetime):
    capture_time = ENTRY - timedelta(microseconds=1)

    @classmethod
    def now(cls, tz=None):
        value = cls.capture_time
        return value if tz is None else value.astimezone(tz)


@pytest.fixture(autouse=True)
def fixed_observer_clock():
    # Preserve datetime isinstance/fromisoformat semantics.
    FrozenObserverDatetime.capture_time = ENTRY - timedelta(microseconds=1)
    with patch(
        "app.research.prospective_observer.datetime",
        FrozenObserverDatetime,
    ):
        yield FrozenObserverDatetime



def make_observer(activation_ready=True):
    observer = ProspectiveOOSObserver(None, {})
    observer._is_htf_db_activation_ready = MagicMock(
        return_value=activation_ready
    )
    return observer


def capture(
    *,
    exhaustion=0.3,
    signal_time=None,
    activation_ready=True,
    entry_zone_low=99.8,
    shadow=True,
    locked_started_at=FREEZE,
):
    observer = make_observer(activation_ready)
    cursor = MagicMock()
    cursor.fetchone.side_effect = [
        (locked_started_at,) if locked_started_at is not None else None,
        (123,),
    ]

    result = observer._observe_me_reverse_long_v3(
        cursor=cursor,
        exp_id=EXPERIMENT_ID,
        exp_spec={
            "scanner_name": SOURCE_SCANNER,
            "freeze_ts": FREEZE.isoformat(),
        },
        source_key=1001,
        symbol="BTCUSDT",
        direction="LONG",
        signal_time=signal_time or FREEZE + timedelta(seconds=1),
        reference_price=105.0,
        invalidation_price=97.5,
        target_1=103.0,
        target_2=None,
        score=50.0,
        features={"exhaustion_magnitude": exhaustion},
        parameters={
            "_v3_shadow_capture": shadow,
            "_v3_entry_zone_low": entry_zone_low,
            "_v3_scanner_version": "1.0.0",
            "_v3_trigger_open_at": (
                FREEZE - timedelta(minutes=5)
            ).isoformat(),
            "_v3_temporal_status": "TEMPORAL_VALIDATED",
            "_v3_source_timeframes_validated": True,
            "_v3_temporal_reason": "SOURCE_CANDLE_CLOSED_BEFORE_DETECTION",
            "_v3_trigger_close_at": FREEZE.isoformat(),
            "_v3_execution_entry_at": next_executable_entry_at(FREEZE + timedelta(seconds=1)).isoformat(),
        },
        market_regime="NORMAL",
    )
    return result, cursor, observer


@pytest.mark.parametrize("value,passed,reason", [
    (0.3, True, "candidate_threshold_pass"),
    (0.4, True, "candidate_threshold_pass"),
    (0.8, False, "candidate_threshold_reject"),
    (None, False, "exhaustion_magnitude_missing"),
])
def test_observation_classification(value, passed, reason):
    result, cursor, _ = capture(exhaustion=value)

    assert result == 123
    sql, args = cursor.execute.call_args.args

    assert "ON CONFLICT" in sql
    assert "variant_entry, variant_stop, variant_target" in sql
    assert args[0] == EXPERIMENT_ID
    assert args[10] is passed
    assert args[11] == reason
    assert args[12:15] == (None, None, None)


@pytest.mark.parametrize("timestamp", [
    FREEZE,
    FREEZE - timedelta(microseconds=1),
])
def test_pre_activation_rejected(timestamp):
    result, cursor, _ = capture(signal_time=timestamp)
    assert result is None
    cursor.execute.assert_not_called()


def test_db_activation_required():
    result, cursor, observer = capture(activation_ready=False)
    assert result is None
    cursor.execute.assert_not_called()
    observer._is_htf_db_activation_ready.assert_called_once()


def test_geometry_fail_closed():
    result, cursor, _ = capture(entry_zone_low=110.0)
    assert result is None
    cursor.execute.assert_not_called()


def test_unmarked_capture_rejected():
    result, cursor, _ = capture(shadow=False)
    assert result is None
    cursor.execute.assert_not_called()


def test_duplicate_insert_does_not_return_new_observation():
    observer = make_observer()
    cursor = MagicMock()
    cursor.fetchone.return_value = None

    result = observer._observe_me_reverse_long_v3(
        cursor, EXPERIMENT_ID,
        {"scanner_name": SOURCE_SCANNER, "freeze_ts": FREEZE.isoformat()},
        1001, "BTCUSDT", "LONG", FREEZE + timedelta(seconds=1),
        105.0, 97.5, 103.0, None, 50.0,
        {"exhaustion_magnitude": 0.3},
        {
            "_v3_shadow_capture": True,
            "_v3_entry_zone_low": 99.8,
            "_v3_scanner_version": "1.0.0",
            "_v3_trigger_open_at": (
                FREEZE - timedelta(minutes=5)
            ).isoformat(),
            "_v3_temporal_status": "TEMPORAL_VALIDATED",
            "_v3_source_timeframes_validated": True,
            "_v3_trigger_close_at": FREEZE.isoformat(),
            "_v3_execution_entry_at": next_executable_entry_at(FREEZE + timedelta(seconds=1)).isoformat(),
        },
        "NORMAL",
    )

    assert result is None
    cursor.execute.assert_called_once()


def test_missing_temporal_proof_rejected():
    observer = make_observer()
    cursor = MagicMock()

    result = observer._observe_me_reverse_long_v3(
        cursor, EXPERIMENT_ID,
        {"scanner_name": SOURCE_SCANNER, "freeze_ts": FREEZE.isoformat()},
        1001, "BTCUSDT", "LONG", FREEZE + timedelta(seconds=1),
        105.0, 97.5, 103.0, None, 50.0,
        {"exhaustion_magnitude": 0.3},
        {"_v3_shadow_capture": True, "_v3_entry_zone_low": 99.8},
        "NORMAL",
    )

    assert result is None
    cursor.execute.assert_not_called()


def test_trigger_close_after_detection_rejected():
    observer = make_observer()
    cursor = MagicMock()

    result = observer._observe_me_reverse_long_v3(
        cursor, EXPERIMENT_ID,
        {"scanner_name": SOURCE_SCANNER, "freeze_ts": FREEZE.isoformat()},
        1001, "BTCUSDT", "LONG", FREEZE + timedelta(seconds=1),
        105.0, 97.5, 103.0, None, 50.0,
        {"exhaustion_magnitude": 0.3},
        {
            "_v3_shadow_capture": True,
            "_v3_entry_zone_low": 99.8,
            "_v3_scanner_version": "1.0.0",
            "_v3_trigger_open_at": (
                FREEZE - timedelta(minutes=5)
            ).isoformat(),
            "_v3_temporal_status": "TEMPORAL_VALIDATED",
            "_v3_source_timeframes_validated": True,
            "_v3_trigger_close_at": (
                FREEZE + timedelta(minutes=5)
            ).isoformat(),
            "_v3_execution_entry_at": (
                FREEZE + timedelta(minutes=5)
            ).isoformat(),
        },
        "NORMAL",
    )

    assert result is None
    cursor.execute.assert_not_called()


def test_unverified_source_timeframes_rejected():
    observer = make_observer()
    cursor = MagicMock()

    result = observer._observe_me_reverse_long_v3(
        cursor, EXPERIMENT_ID,
        {"scanner_name": SOURCE_SCANNER, "freeze_ts": FREEZE.isoformat()},
        1001, "BTCUSDT", "LONG", FREEZE + timedelta(seconds=1),
        105.0, 97.5, 103.0, None, 50.0,
        {"exhaustion_magnitude": 0.3},
        {
            "_v3_shadow_capture": True,
            "_v3_entry_zone_low": 99.8,
            "_v3_scanner_version": "1.0.0",
            "_v3_trigger_open_at": (
                FREEZE - timedelta(minutes=5)
            ).isoformat(),
            "_v3_temporal_status": "TEMPORAL_VALIDATED",
            "_v3_source_timeframes_validated": False,
            "_v3_trigger_close_at": FREEZE.isoformat(),
            "_v3_execution_entry_at": next_executable_entry_at(FREEZE + timedelta(seconds=1)).isoformat(),
        },
        "NORMAL",
    )

    assert result is None
    cursor.execute.assert_not_called()


def test_next_open_protocol_and_source_geometry():
    import json

    result, cursor, _ = capture(exhaustion=0.3)

    assert result == 123
    _, args = cursor.execute.call_args.args

    features = json.loads(args[15])
    parameters = json.loads(args[16])

    assert parameters["_v3_execution_model"] == "NEXT_OPEN_5M"
    assert features["_frozen_entry"] == "NEXT_OPEN_5M"
    assert features["_v3_source_entry"] == pytest.approx(100.0)
    assert features["_v3_source_stop"] == pytest.approx(97.5)
    assert features["_v3_source_target"] == pytest.approx(103.0)
    assert args[12:15] == (None, None, None)


def test_premature_execution_entry_rejected():
    observer = make_observer()
    cursor = MagicMock()

    signal_time = FREEZE + timedelta(seconds=1)

    result = observer._observe_me_reverse_long_v3(
        cursor=cursor,
        exp_id=EXPERIMENT_ID,
        exp_spec={
            "scanner_name": SOURCE_SCANNER,
            "freeze_ts": FREEZE.isoformat(),
        },
        source_key=1001,
        symbol="BTCUSDT",
        direction="LONG",
        signal_time=signal_time,
        reference_price=105.0,
        invalidation_price=97.5,
        target_1=103.0,
        target_2=None,
        score=50.0,
        features={"exhaustion_magnitude": 0.3},
        parameters={
            "_v3_shadow_capture": True,
            "_v3_entry_zone_low": 99.8,
            "_v3_scanner_version": "1.0.0",
            "_v3_trigger_open_at": (
                FREEZE - timedelta(minutes=5)
            ).isoformat(),
            "_v3_temporal_status": "TEMPORAL_VALIDATED",
            "_v3_source_timeframes_validated": True,
            "_v3_trigger_close_at": FREEZE.isoformat(),
            "_v3_execution_entry_at": FREEZE.isoformat(),
        },
        market_regime="NORMAL",
    )

    assert result is None
    cursor.execute.assert_not_called()


def test_capture_at_execution_entry_rejected(fixed_observer_clock):
    fixed_observer_clock.capture_time = ENTRY

    result, cursor, _ = capture()

    assert result is None
    cursor.execute.assert_not_called()


def test_capture_after_execution_entry_rejected(fixed_observer_clock):
    fixed_observer_clock.capture_time = ENTRY + timedelta(seconds=1)

    result, cursor, _ = capture()

    assert result is None
    cursor.execute.assert_not_called()


def test_capture_just_before_execution_entry_allowed(fixed_observer_clock):
    fixed_observer_clock.capture_time = ENTRY - timedelta(microseconds=1)

    result, cursor, _ = capture()

    assert result == 123
    assert cursor.execute.call_count == 2
    lock_sql = cursor.execute.call_args_list[0].args[0]
    insert_sql = cursor.execute.call_args_list[1].args[0]
    assert "FOR SHARE" in lock_sql
    assert "INSERT INTO research.prospective_observation" in insert_sql



def test_v3_capture_requires_lifecycle_lock_before_insert():
    result, cursor, _ = capture()

    assert result == 123
    assert cursor.execute.call_count == 2
    assert "FOR SHARE" in cursor.execute.call_args_list[0].args[0]
    assert "INSERT INTO research.prospective_observation" in (
        cursor.execute.call_args_list[1].args[0]
    )


@pytest.mark.parametrize("locked_started_at", [
    None,
    FREEZE + timedelta(seconds=1),
    123,
])
def test_v3_invalid_locked_lifecycle_never_inserts(locked_started_at):
    result, cursor, _ = capture(
        locked_started_at=locked_started_at,
    )

    assert result is None
    assert cursor.execute.call_count == 1
    assert "FOR SHARE" in cursor.execute.call_args_list[0].args[0]
    assert all(
        "INSERT INTO research.prospective_observation" not in call.args[0]
        for call in cursor.execute.call_args_list
    )
