from datetime import datetime, timedelta, timezone
from app.research.me_r_long_early_mae_v3.protocol import (
    ACTIVATION_AT, FROZEN_AT, PROTOCOL_VERSION, VARIANTS, protocol_payload,
    calculate_protocol_fingerprint, validate_measured_source_availability,
)
from app.research.me_r_long_early_mae_v3.repository import MeEarlyMaeV3Repository

T = datetime(2026, 1, 1, tzinfo=timezone.utc)

def test_protocol_payload_and_fingerprint_are_deterministic():
    p1 = protocol_payload(T, T + timedelta(minutes=1))
    p2 = protocol_payload(T, T + timedelta(minutes=1))
    assert p1 == p2
    assert calculate_protocol_fingerprint(p1) == calculate_protocol_fingerprint(p2)
    assert len(VARIANTS) == 7
    assert sum(v.is_primary for v in VARIANTS) == 1

def test_measured_availability_rejects_missing_and_misordered_values():
    validate_measured_source_availability(T, T + timedelta(seconds=1), T + timedelta(seconds=2))
    for args in ((T, T - timedelta(seconds=1), T), (T, T + timedelta(seconds=2), T + timedelta(seconds=1)), (T, T + timedelta(seconds=301), T + timedelta(seconds=301))):
        try:
            validate_measured_source_availability(*args)
        except ValueError:
            pass
        else:
            raise AssertionError(args)

def test_migration_contains_finalization_immutability_guards():
    text = open("sql/migrations/068_me_r_early_mae_v3_prospective.sql").read()
    for token in (
        "fn_block_finalized_source_update",
        "fn_block_finalized_source_delete",
        "fn_block_finalized_variant_update",
        "fn_block_finalized_variant_delete",
        "fn_validate_finalized_variant_batch",
        "fn_validate_source_finalization",
        "fn_lock_variant_tuple",
    ):
        assert token in text
    assert "RESEARCH_PRICE_PROXY_NOT_VERIFIED_EXECUTABLE" in text
    assert "BASELINE_EXECUTION_PROXY" in text

def test_repository_uses_positional_placeholders_in_paired_updates():
    text = open("app/research/me_r_long_early_mae_v3/repository.py").read()
    update = text[text.index("def save_paired_outcome"):text.index("def mark_source_not_filled")]
    assert "%(" in update
    assert "WHERE protocol_version=%(protocol_version)s" in update
