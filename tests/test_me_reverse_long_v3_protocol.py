from datetime import datetime, timedelta, timezone

import pytest

from app.research.me_reverse_long_v3.protocol import (
    EXPERIMENT_ID,
    classify_exhaustion,
    is_post_activation,
    is_v3_source,
    reconstruct_geometry,
)

UTC = timezone.utc


def test_experiment_identity():
    assert EXPERIMENT_ID == (
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS"
    )


@pytest.mark.parametrize("value,expected", [
    (0.0, "PASS"),
    (0.399, "PASS"),
    (0.4, "PASS"),
    (0.400001, "REJECT"),
    (0.8, "REJECT"),
    (None, "MISSING_FEATURE"),
    ("invalid", "MISSING_FEATURE"),
    (float("nan"), "MISSING_FEATURE"),
    (float("inf"), "MISSING_FEATURE"),
])
def test_classification(value, expected):
    assert classify_exhaustion(value).group == expected


def test_v2_geometry_reconstruction():
    current_price = 100.0

    geometry = reconstruct_geometry(
        entry_zone_low=current_price * 0.998,
        invalidation_price=97.5,
        target_1=103.0,
    )

    assert geometry.entry == pytest.approx(100.0)
    assert geometry.stop == pytest.approx(97.5)
    assert geometry.target == pytest.approx(103.0)


def test_reference_price_not_used():
    geometry = reconstruct_geometry(99.8, 97.5, 103.0)

    assert geometry.entry == pytest.approx(100.0)
    assert geometry.entry != 105.0


@pytest.mark.parametrize("low,stop,target", [
    (None, 97.5, 103),
    (99.8, None, 103),
    (99.8, 97.5, None),
    (99.8, 101, 103),
    (99.8, 97.5, 99),
    (99.8, 99, 103),
])
def test_invalid_geometry_rejected(low, stop, target):
    with pytest.raises(ValueError):
        reconstruct_geometry(low, stop, target)


def test_strict_post_activation():
    activation = datetime(2026, 10, 12, 17, 0, tzinfo=UTC)

    assert not is_post_activation(activation, activation)
    assert not is_post_activation(
        activation - timedelta(microseconds=1), activation
    )
    assert is_post_activation(
        activation + timedelta(microseconds=1), activation
    )
    assert not is_post_activation(activation, None)


def test_source_isolation():
    assert is_v3_source(
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "LONG"
    )
    assert not is_v3_source(
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V1", "LONG"
    )
    assert not is_v3_source(
        "MOMENTUM_EXHAUSTION_REVERSE_LONG_V2", "SHORT"
    )
