"""Issue #154: static fail-closed deploy-unit contract; no service starts."""
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "deploy/systemd/trad-bot-me-r-v3-oos.service"
TIMER = Path(__file__).resolve().parents[1] / "deploy/systemd/trad-bot-me-r-v3-oos.timer"


def test_isolated_release_paths_and_no_production_python():
    content = UNIT.read_text(encoding="utf-8")
    assert "WorkingDirectory=/opt/trad_bot_me_r_v3_release" in content
    assert "ExecStart=/opt/trad_bot_me_r_v3_release/.venv/bin/python " in content
    assert "/opt/trad_bot/.venv" not in content
    assert "WorkingDirectory=/opt/trad_bot\n" not in content
    assert "User=trad_bot_me_r_v3_oos" in content
    assert "Group=trad_bot_me_r_v3_oos" in content


def test_mandatory_env_and_disabled_default():
    content = UNIT.read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/trad-bot/me-r-v3-oos.env" in content
    assert "EnvironmentFile=-" not in content
    assert "Environment=ME_EARLY_MAE_V3_CAPTURE_ENABLED=false" in content
    assert "Restart=no" in content
    assert "Type=oneshot" in content
    assert "ProtectSystem=strict" in content
    assert "NoNewPrivileges=yes" in content


def test_no_implicit_activation():
    content = TIMER.read_text(encoding="utf-8")
    assert "Persistent=false" in content
    assert "OnCalendar=*-*-* *:0/5:30 UTC" in content
    assert "trad-bot-me-r-v3-oos.service" in content
