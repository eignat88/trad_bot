"""Regression guards for terminal ME A/B/C and live A execution isolation."""
import json
from pathlib import Path
from unittest.mock import MagicMock

from app.research.prospective_observer import ProspectiveOOSObserver

OLD = ("ME_SHORT_GEOM_A_V1", "ME_SHORT_GEOM_B_V1", "ME_SHORT_GEOM_C_V1")
NEW = "ME_SHORT_GEOM_A_EXECUTION_V1"


def test_retired_abc_rejected_even_if_registry_running_or_db_unavailable():
    conn = MagicMock()
    observer = ProspectiveOOSObserver(conn, {
        key: {"scanner_name": "MOMENTUM_EXHAUSTION", "direction": "SHORT",
              "status": "RUNNING"} for key in OLD
    })
    for key in OLD:
        assert observer._is_db_active(key) is False
    conn.cursor.assert_not_called()


def test_registry_is_terminal_for_abc_but_not_execution():
    data = json.loads(
        (Path(__file__).resolve().parents[1]
         / "app/research/prospective_registry.json").read_text(encoding="utf-8")
    )
    specs = {item["experiment_id"]: item for item in data["experiments"]}
    for key in OLD:
        assert specs[key]["status"] == "CANCELLED"
    assert specs[NEW]["status"] not in {"CANCELLED", "COMPLETED", "INACTIVE"}


def test_migration_contains_db_fuse_and_execution_guard():
    sql = (Path(__file__).resolve().parents[1]
           / "sql/migrations/073_me_short_abc_terminal_lifecycle.sql").read_text()
    for key in OLD:
        assert key in sql
    assert "CREATE TRIGGER trg_guard_retired_me_short_abc_v1" in sql
    assert "BEFORE UPDATE" in sql
    assert "AFTER INSERT" in sql
    assert "READY_TO_START" in sql
    assert "RAISE EXCEPTION" in sql
    assert "status='CANCELLED'" in sql
    assert "ME_SHORT_GEOM_A_EXECUTION_V1" in sql
    assert "2026-10-19 08:26:00+00" in sql
