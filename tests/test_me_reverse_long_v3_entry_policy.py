from datetime import datetime, timezone, timedelta

import pytest

from app.research.me_reverse_long_v3.entry_policy import (
    next_executable_entry_at,
    execution_geometry,
)

UTC = timezone.utc


@pytest.mark.parametrize("detected,expected", [
    ("2026-10-13T17:05:03+00:00", "2026-10-13T17:10:00+00:00"),
    ("2026-10-13T17:05:00+00:00", "2026-10-13T17:10:00+00:00"),
    ("2026-10-13T17:09:59+00:00", "2026-10-13T17:10:00+00:00"),
    ("2026-10-13T17:59:59+00:00", "2026-10-13T18:00:00+00:00"),
])
def test_next_entry_strictly_after_detection(detected, expected):
    result = next_executable_entry_at(datetime.fromisoformat(detected))
    assert result == datetime.fromisoformat(expected)
    assert result > datetime.fromisoformat(detected)


def test_timezone_normalization():
    detected = datetime(
        2026, 10, 13, 20, 5, 3,
        tzinfo=timezone(timedelta(hours=3)),
    )
    assert next_executable_entry_at(detected) == datetime(
        2026, 10, 13, 17, 10, tzinfo=UTC,
    )


def test_geometry_from_actual_entry():
    entry, stop, target = execution_geometry(100)
    assert entry == 100
    assert stop == pytest.approx(97.5)
    assert target == pytest.approx(103)


def test_naive_detection_rejected():
    with pytest.raises(ValueError):
        next_executable_entry_at(datetime(2026, 10, 13, 17, 5))


def test_invalid_price_rejected():
    with pytest.raises(ValueError):
        execution_geometry(float("nan"))
