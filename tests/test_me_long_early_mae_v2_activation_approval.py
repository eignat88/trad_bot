from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.research.me_r_long_early_mae_v2 import protocol

freeze = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
activation = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)


def test_missing_approval_rejected():
    with patch.object(protocol, "PROTOCOL_FINGERPRINT", None):
        with pytest.raises(RuntimeError, match="FINGERPRINT_NOT_APPROVED"):
            protocol.require_protocol_fingerprint(freeze, activation)


def test_incorrect_approval_rejected():
    with patch.object(protocol, "PROTOCOL_FINGERPRINT", "0" * 64):
        with pytest.raises(RuntimeError, match="FINGERPRINT_MISMATCH"):
            protocol.require_protocol_fingerprint(freeze, activation)


def test_correct_approval_accepted():
    expected = protocol.calculate_protocol_fingerprint(
        protocol.protocol_payload(freeze, activation)
    )
    with patch.object(protocol, "PROTOCOL_FINGERPRINT", expected):
        assert protocol.require_protocol_fingerprint(freeze, activation) == expected
