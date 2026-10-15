from pathlib import Path


def source_insert_section():
    text = Path(
        "app/research/me_r_long_early_mae_v3/repository.py"
    ).read_text(encoding="utf-8")
    return text[
        text.index("def insert_source_with_variants"):
        text.index("def _verify_existing_source")
    ]


def test_source_insert_conflict_safe():
    section = source_insert_section()

    assert "ON CONFLICT DO NOTHING" in section
    assert "RETURNING source_event_id" in section
    assert "SOURCE_CANDLE_IDENTITY_COLLISION" in section
    assert "self._verify_existing_source(" in section


def test_source_replay_is_locked_and_verified():
    section = source_insert_section()

    assert section.count("FOR UPDATE") >= 3
    assert "source_setup_id=%s" in section
    assert "self.connection.commit()" in section
    assert "self.connection.rollback()" in section


def test_source_and_variants_same_transaction():
    section = source_insert_section()

    assert "INSERT INTO {SOURCE_SCHEMA}.source_event" in section
    assert "INSERT INTO {SOURCE_SCHEMA}.variant_outcome" in section
    assert (
        "ON CONFLICT (protocol_version, source_event_id, variant_id)"
        in section
    )


def test_registration_uses_valid_bigint_advisory_lock():
    text = Path(
        "app/research/me_r_long_early_mae_v3/repository.py"
    ).read_text(encoding="utf-8")

    section = text[
        text.index("def register_protocol"):
        text.index("def ensure_instrument")
    ]

    assert "pg_advisory_xact_lock(%s::bigint)" in section
    assert "hashtext(%s)" not in section
    assert "PROTOCOL_LOCK_KEY = 732188603" in text
