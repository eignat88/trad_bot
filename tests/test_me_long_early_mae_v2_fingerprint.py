from copy import deepcopy
from datetime import datetime, timezone

from app.research.me_r_long_early_mae_v2.protocol import (
    PROTOCOL_FINGERPRINT,
    require_protocol_fingerprint,
    calculate_protocol_fingerprint,
    protocol_payload,
)

TEST_FROZEN_AT = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
TEST_ACTIVATION_AT = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)


def test_fingerprint_matches_full_payload():
    assert PROTOCOL_FINGERPRINT is None
    assert require_protocol_fingerprint(
        TEST_FROZEN_AT, TEST_ACTIVATION_AT
    ) == calculate_protocol_fingerprint(
        protocol_payload(TEST_FROZEN_AT, TEST_ACTIVATION_AT)
    )


def test_fingerprint_changes_with_activation_boundary():
    payload = protocol_payload(TEST_FROZEN_AT, TEST_ACTIVATION_AT)
    changed = deepcopy(payload)
    changed["activation_at"] = "2026-10-20T00:00:00+00:00"

    assert calculate_protocol_fingerprint(payload) != (
        calculate_protocol_fingerprint(changed)
    )


def test_fingerprint_changes_with_variant_geometry():
    payload = protocol_payload(TEST_FROZEN_AT, TEST_ACTIVATION_AT)
    changed = deepcopy(payload)
    changed["variants"][0]["threshold_mae_r"] = 0.99

    assert calculate_protocol_fingerprint(payload) != (
        calculate_protocol_fingerprint(changed)
    )


def test_fingerprint_is_independent_of_dict_key_order():
    payload = protocol_payload(TEST_FROZEN_AT, TEST_ACTIVATION_AT)

    assert calculate_protocol_fingerprint(payload) == (
        calculate_protocol_fingerprint(dict(reversed(list(payload.items()))))
    )
