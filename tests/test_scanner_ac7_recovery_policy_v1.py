import pytest

from app.scanners.recovery_policy_v1 import decide_recovery


def decision(**overrides):
    values = {
        "enabled": True,
        "expected_symbols": 50,
        "processed_symbols": 0,
        "limiter_pre_send_skips": 50,
        "previous_recovery_attempted": False,
        "normal_delay_seconds": 260.0,
    }
    values.update(overrides)
    return decide_recovery(**values)


def test_incident_297_recovery():
    result = decision()
    assert result.expedite
    assert result.delay_seconds == 130.0


def test_feature_disabled():
    result = decision(enabled=False)
    assert not result.expedite
    assert result.delay_seconds == 260.0


def test_no_recovery_storm():
    result = decision(previous_recovery_attempted=True)
    assert not result.expedite
    assert result.reason == "RECOVERY_ALREADY_ATTEMPTED"


def test_complete_cycle():
    result = decision(
        processed_symbols=50,
        limiter_pre_send_skips=0,
    )
    assert not result.expedite


def test_non_limiter_failure():
    result = decision(
        processed_symbols=44,
        limiter_pre_send_skips=0,
    )
    assert not result.expedite


def test_partial_limiter_failure():
    result = decision(
        processed_symbols=44,
        limiter_pre_send_skips=6,
    )
    assert result.expedite


def test_never_extends_normal_delay():
    result = decision(normal_delay_seconds=60.0)
    assert not result.expedite
    assert result.delay_seconds == 60.0


@pytest.mark.parametrize("overrides", [
    {"normal_delay_seconds": -1},
    {"normal_delay_seconds": float("nan")},
    {"recovery_delay_seconds": 0},
    {"processed_symbols": 51},
    {"limiter_pre_send_skips": 51},
    {"expected_symbols": -1},
])
def test_invalid_inputs_fail_closed(overrides):
    with pytest.raises(ValueError):
        decision(**overrides)
