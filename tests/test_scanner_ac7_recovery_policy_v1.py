import pytest

from app.scanners.recovery_policy_v1 import (
    RECOVERY_DELAY_SECONDS,
    REASON_NOT_BENEFICIAL,
    decide_recovery,
)


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
    {"limiter_cooldown_hint_seconds": -1},
    {"limiter_cooldown_hint_seconds": float("nan")},
    {"limiter_cooldown_hint_seconds": float("inf")},
])
def test_invalid_inputs_fail_closed(overrides):
    with pytest.raises(ValueError):
        decision(**overrides)


def test_default_recovery_delay_is_the_named_experimental_constant():
    # The 130 s value must stay a NAMED, documented experimental constant —
    # never a safe-looking bare literal hidden in the call site.
    assert RECOVERY_DELAY_SECONDS == 130.0
    result = decision()
    assert result.delay_seconds == RECOVERY_DELAY_SECONDS


def test_cooldown_hint_shorter_than_floor_keeps_floor():
    result = decision(limiter_cooldown_hint_seconds=30.0)
    assert result.expedite
    assert result.delay_seconds == 130.0


def test_cooldown_hint_longer_than_floor_clamps_delay():
    # A level-3 breaker with a longer configured cooldown: the rescan must
    # not wake before the breaker can actually grant again.
    result = decision(limiter_cooldown_hint_seconds=200.0)
    assert result.expedite
    assert result.delay_seconds == 200.0


def test_cooldown_hint_beyond_normal_delay_is_not_beneficial():
    # No acceleration is possible without waking into a closed breaker:
    # keep the normal schedule and say so explicitly.
    result = decision(limiter_cooldown_hint_seconds=400.0)
    assert not result.expedite
    assert result.delay_seconds == 260.0
    assert result.reason == REASON_NOT_BENEFICIAL


def test_cooldown_hint_equal_to_normal_delay_is_not_beneficial():
    result = decision(limiter_cooldown_hint_seconds=260.0)
    assert not result.expedite
    assert result.reason == REASON_NOT_BENEFICIAL


def test_zero_normal_delay_is_never_expedited():
    # A cycle that overran its interval starts the next one immediately —
    # recovery cannot beat it.
    result = decision(normal_delay_seconds=0.0)
    assert not result.expedite
    assert result.reason == REASON_NOT_BENEFICIAL
