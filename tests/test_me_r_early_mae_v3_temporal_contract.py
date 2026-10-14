from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.temporal import validate_detection_temporal

T = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

def test_299_seconds_allowed():
    x = validate_detection_temporal(T, T + timedelta(seconds=299), T + timedelta(seconds=299))
    assert x.delay_seconds == 299

def test_300_seconds_allowed():
    x = validate_detection_temporal(T, T + timedelta(seconds=300), T + timedelta(seconds=300))
    assert x.delay_seconds == 300

def test_301_seconds_rejected():
    try:
        validate_detection_temporal(T, T + timedelta(seconds=301), T + timedelta(seconds=301))
    except ValueError as exc:
        assert "300" in str(exc)
    else:
        raise AssertionError

def test_future_detection_rejected():
    try:
        validate_detection_temporal(T + timedelta(seconds=1), T, T)
    except ValueError as exc:
        assert "CLOSED" in str(exc)
    else:
        raise AssertionError
