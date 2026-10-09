"""Frozen close-location 0.70 boundary contract."""

from decimal import Decimal

import pytest


THRESHOLD = Decimal("0.70")


@pytest.mark.parametrize(
    "raw,expected_pass",
    [
        ("0.6999998", False),
        ("0.7000000", True),
        ("0.7000002", True),
        ("0.6999994", False),
        ("0.7000006", True),
    ],
)
def test_frozen_threshold_uses_unrounded_value(raw, expected_pass):
    value = Decimal(raw)
    assert (value >= THRESHOLD) is expected_pass


@pytest.mark.parametrize(
    "raw,expected_pass",
    [
        ("0.6999998", False),
        ("0.7000000", True),
        ("0.7000002", True),
    ],
)
def test_rounded_storage_cannot_reliably_reconstruct_decision(
    raw, expected_pass
):
    value = Decimal(raw)
    stored = round(value, 6)

    assert (value >= THRESHOLD) is expected_pass

    # Reproduce the existing scanner storage policy.
    assert stored == Decimal("0.700000")

    # Frozen decision must remain based on the unrounded value.
    assert (value >= THRESHOLD) is expected_pass


@pytest.mark.parametrize(
    "raw,expected_pass",
    [
        ("0.6999998", False),
        ("0.7000000", True),
        ("0.7000002", True),
    ],
)
def test_raw_ohlc_recovers_frozen_decision(raw, expected_pass):
    low = Decimal("100")
    high = Decimal("200")
    close = low + Decimal(raw) * (high - low)

    recovered = (close - low) / (high - low)

    assert recovered == Decimal(raw)
    assert (recovered >= THRESHOLD) is expected_pass
