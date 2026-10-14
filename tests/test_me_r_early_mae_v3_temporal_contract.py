from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.temporal import (
    require_fetch_completion_provenance,
    validate_detection_temporal,
)

T = datetime(2026, 1, 1, tzinfo=timezone.utc)

def test_299_300_and_301_seconds():
    for seconds, allowed in ((299, True), (300, True), (301, False)):
        try:
            validate_detection_temporal(T, T + timedelta(seconds=seconds), T + timedelta(seconds=seconds))
        except ValueError as exc:
            assert allowed is False and "300" in str(exc)
        else:
            assert allowed

def test_fetch_completion_is_required_or_measured():
    try:
        require_fetch_completion_provenance(None)
    except ValueError as exc:
        assert "PROVENANCE_REQUIRED" in str(exc)
    else:
        raise AssertionError
    assert require_fetch_completion_provenance(T) == T

def test_detection_before_fetch_completion_rejected():
    try:
        validate_detection_temporal(T, T, T + timedelta(seconds=1))
    except ValueError as exc:
        assert "COMPLETION" in str(exc)
    else:
        raise AssertionError
