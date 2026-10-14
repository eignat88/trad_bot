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
    # Finalization is validated transactionally, after all seven
    # variant rows have been updated.
    for token in (
        "fn_validate_finalization_commit",
        "fn_validate_source_commit",
        "CREATE CONSTRAINT TRIGGER trg_me_mae_v3_variant_commit_check",
        "CREATE CONSTRAINT TRIGGER trg_me_mae_v3_source_commit_check",
        "DEFERRABLE INITIALLY DEFERRED",
        "FINALIZED_COMMIT_TUPLE_INVALID",
        "FINALIZED_SOURCE_COMMIT_INVALID",
        "FINALIZED_VARIANT_WITHOUT_FINALIZED_SOURCE",
    ):
        assert token in text

    # The old per-row cross-variant gate must not be reintroduced.
    assert "AND variant_id <> NEW.variant_id" not in text
    assert "RESEARCH_PRICE_PROXY_NOT_VERIFIED_EXECUTABLE" in text
    assert "BASELINE_EXECUTION_PROXY" in text

def test_repository_protocol_registration_inserts_variants():
    text = open("app/research/me_r_long_early_mae_v3/repository.py").read()
    registration = text[text.index("def register_protocol"):text.index("def ensure_instrument")]
    assert "INSERT INTO {SOURCE_SCHEMA}.variant" in registration
    assert "ON CONFLICT (protocol_version, variant_id) DO NOTHING" in registration

def test_repository_uses_driver_compatible_placeholders_in_paired_updates():
    text = open("app/research/me_r_long_early_mae_v3/repository.py").read()
    update = text[text.index("def save_paired_outcome"):text.index("def mark_source_not_filled")]
    assert "%(" not in update
    assert "WHERE protocol_version=%s" in update


def test_repository_source_variant_query_orders_before_lock():
    text = open("app/research/me_r_long_early_mae_v3/repository.py").read()
    assert "ORDER BY variant_id FOR UPDATE" in text


def test_repository_entry_update_uses_driver_compatible_placeholders():
    text = open("app/research/me_r_long_early_mae_v3/repository.py").read()
    entry = text[text.index("def update_source_entry"):text.index("def save_paired_outcome")]
    assert "entry_filled_at=%s" in entry
    assert "source_event_id=%s" in entry

def test_repository_paired_outcome_uses_driver_compatible_placeholders():
    text = open("app/research/me_r_long_early_mae_v3/repository.py").read()
    section = text[text.index("def save_paired_outcome"):text.index("def mark_source_not_filled")]
    assert "%(" not in section
    assert "variant_exit_at=%s" in section
    assert "INVALID_FINALIZED_VARIANT_STATUS" in section
    assert "SET variant_status='FINALIZED'\n                        WHERE protocol_version=%s" in section
    assert "baseline_exit_at=%s" in section

def test_repository_paired_outcome_complete():
    text = open("app/research/me_r_long_early_mae_v3/repository.py").read()
    section = text[text.index("def save_paired_outcome"):text.index("def mark_source_not_filled")]
    for key in ("trigger_result","variant_exit_at","net_r_normal","delta_net_r_elevated","execution_price_semantics","variant_id","source_event_id","protocol_version"):
        assert key in section


def test_variant_definitions_immutable():
    from pathlib import Path

    sql = Path(
        "sql/migrations/068_me_r_early_mae_v3_prospective.sql"
    ).read_text()

    for token in (
        "fn_block_variant_definition_mutation",
        "REGISTERED_VARIANT_DEFINITION_IMMUTABLE",
        "trg_me_mae_v3_variant_definition_immutable_update",
        "trg_me_mae_v3_variant_definition_immutable_delete",
        "BEFORE UPDATE ON research_me_early_mae_v3.variant",
        "BEFORE DELETE ON research_me_early_mae_v3.variant",
    ):
        assert token in sql


def test_migration_exact_variant_whitelist():
    from pathlib import Path

    sql = Path(
        "sql/migrations/068_me_r_early_mae_v3_prospective.sql"
    ).read_text()

    assert "chk_me_mae_v3_exact_variant_definitions" in sql

    for variant_id in (
        "MAE_10m_040",
        "MAE_10m_050",
        "MAE_15m_040",
        "MAE_15m_050",
        "MAE_15m_060",
        "MAE_15m_075",
        "MAE_30m_050",
    ):
        assert f"variant_id = '{variant_id}'" in sql


def test_direct_finalized_source_insert_forbidden():
    from pathlib import Path

    sql = Path(
        "sql/migrations/068_me_r_early_mae_v3_prospective.sql"
    ).read_text()

    for token in (
        "fn_block_direct_finalized_source_insert",
        "DIRECT_FINALIZED_SOURCE_INSERT_FORBIDDEN",
        "trg_me_mae_v3_source_block_direct_finalized_insert",
        "BEFORE INSERT ON research_me_early_mae_v3.source_event",
        "IF NEW.source_status = 'FINALIZED' THEN",
    ):
        assert token in sql
