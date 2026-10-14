from pathlib import Path

def test_new_package_has_no_paper_live_imports():
    for path in Path("app/research/me_r_long_early_mae_v3").glob("*.py"):
        text = path.read_text().lower()
        assert "paper_trade" not in text
        assert "live_trade" not in text
        assert "paper_runner" not in text
        assert "live_runner" not in text

def test_migration_has_no_paper_live_foreign_keys():
    text = Path("sql/migrations/068_me_r_early_mae_v3_prospective.sql").read_text().lower()
    assert "dds.paper" not in text
    assert "live" not in text.replace("research-only", "")
