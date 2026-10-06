"""Regression guards for HTF Point-B Phase B activation lifecycle."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from app.research.prospective_observer import ProspectiveOOSObserver


FREEZE_TS = "2026-10-06T14:00:00Z"


def _observer_with_db_row(row):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = row
    return ProspectiveOOSObserver(conn, {})


def test_htf_activation_rejects_ready_to_start():
    observer = _observer_with_db_row(("READY_TO_START", None))
    assert not observer._is_htf_db_activation_ready(
        "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE",
        FREEZE_TS,
    )


def test_htf_activation_rejects_paused():
    started = datetime.fromisoformat(FREEZE_TS.replace("Z", "+00:00"))
    observer = _observer_with_db_row(("PAUSED", started))
    assert not observer._is_htf_db_activation_ready(
        "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE",
        FREEZE_TS,
    )


def test_htf_activation_rejects_conflicting_boundary():
    started = datetime.fromisoformat(FREEZE_TS.replace("Z", "+00:00"))
    started = started.replace(minute=(started.minute + 1) % 60)
    observer = _observer_with_db_row(("RUNNING", started))
    assert not observer._is_htf_db_activation_ready(
        "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE",
        FREEZE_TS,
    )


def test_htf_activation_accepts_running_exact_boundary():
    started = datetime.fromisoformat(FREEZE_TS.replace("Z", "+00:00"))
    observer = _observer_with_db_row(("RUNNING", started))
    assert observer._is_htf_db_activation_ready(
        "HTF_KEYLEVEL_SR_BREAK_POINT_B_V1_PROSPECTIVE",
        FREEZE_TS,
    )


def test_migration_059_accepts_ready_and_exact_running_states():
    sql = Path(
        "sql/migrations/059_htf_point_b_prospective_activation.sql"
    ).read_text(encoding="utf-8")

    assert "status = 'READY_TO_START' AND started_at IS NULL" in sql
    assert "status = 'RUNNING'" in sql
    assert "started_at = '" + FREEZE_TS + "'::timestamptz" in sql
    assert "activation conflict" in sql
    assert "activation verification failed" in sql
