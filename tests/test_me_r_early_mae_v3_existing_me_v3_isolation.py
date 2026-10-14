from pathlib import Path

def test_existing_me_reverse_long_v3_is_untouched():
    assert Path("app/research/me_reverse_long_v3").exists()
    for path in Path("app/research/me_r_long_early_mae_v3").glob("*.py"):
        assert "me_reverse_long_v3" not in path.read_text()
