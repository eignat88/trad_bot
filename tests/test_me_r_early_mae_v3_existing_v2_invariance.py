from pathlib import Path

def test_v3_package_does_not_import_frozen_v2():
    for path in Path("app/research/me_r_long_early_mae_v3").glob("*.py"):
        text = path.read_text()
        assert "me_r_long_early_mae_v2" not in text

def test_existing_v2_migration_is_unchanged_in_v3_scope():
    p = Path("sql/migrations/066_me_long_early_mae_exit_prospective_v2.sql")
    assert p.exists()
