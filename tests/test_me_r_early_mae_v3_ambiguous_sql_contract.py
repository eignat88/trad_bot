from pathlib import Path

MIGRATION = Path(
    "sql/migrations/069_me_r_early_mae_v3_ambiguous_nonfinal.sql"
)


def sql():
    assert MIGRATION.is_file(), "MIGRATION_069_REQUIRED"
    return MIGRATION.read_text()


def test_new_terminal_source_status():
    text = sql()
    assert "AMBIGUOUS_NONFINAL" in text
    assert "CHECK" in text


def test_finalized_variant_cannot_be_ambiguous():
    text = sql()
    assert "ambiguous_ordering" in text
    assert "FINALIZED" in text
    assert "RAISE EXCEPTION" in text


def test_diagnostic_tuple_validated_at_commit():
    text = sql()
    assert "DEFERRABLE INITIALLY DEFERRED" in text
    assert "AMBIGUOUS_NONFINAL_TUPLE_INVALID" in text


def test_terminal_diagnostic_rows_immutable():
    text = sql()
    assert "AMBIGUOUS_NONFINAL_SOURCE_IMMUTABLE" in text
    assert "AMBIGUOUS_NONFINAL_VARIANT_IMMUTABLE" in text


def test_exactly_seven_diagnostic_variants():
    text = sql()
    assert "count(*)" in text
    assert "AMBIGUOUS" in text
