import os
from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.guards import require_runtime_configuration
from app.research.me_r_long_early_mae_v3.protocol import calculate_protocol_fingerprint, protocol_payload

T = datetime(2026, 1, 1, tzinfo=timezone.utc)

def test_disabled_by_default():
    os.environ.pop("ME_EARLY_MAE_V3_CAPTURE_ENABLED", None)
    try:
        require_runtime_configuration(T)
    except RuntimeError as exc:
        assert "DISABLED" in str(exc)
    else:
        raise AssertionError

def test_fingerprint_is_deterministic():
    payload = protocol_payload(T, T + timedelta(minutes=1))
    assert calculate_protocol_fingerprint(payload) == calculate_protocol_fingerprint(payload)

def test_unapproved_fingerprint_fails_closed():
    os.environ["ME_EARLY_MAE_V3_CAPTURE_ENABLED"] = "true"
    os.environ["ME_EARLY_MAE_V3_FROZEN_AT"] = "2026-01-01T00:00:00Z"
    os.environ["ME_EARLY_MAE_V3_ACTIVATION_AT"] = "2026-01-01T00:01:00Z"
    os.environ.pop("ME_EARLY_MAE_V3_PROTOCOL_FINGERPRINT", None)
    try:
        require_runtime_configuration(T + timedelta(minutes=2))
    except RuntimeError as exc:
        assert "FINGERPRINT" in str(exc)
    else:
        raise AssertionError
