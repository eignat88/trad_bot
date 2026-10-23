"""Issue 189: V3-only DRAINING routing and immutable lifecycle safeguards."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.research.evaluator_runner import _load_prospective_experiments
from app.research.me_reverse_long_v3.activation import is_v3_evaluator_activated, V3ActivationIdentity
from datetime import datetime, timezone
from app.research.me_reverse_long_v3.protocol import EXPERIMENT_ID

FREEZE = datetime(2026, 10, 13, 12, 37, 3, tzinfo=timezone.utc)
REGISTRY = {"experiment_id": EXPERIMENT_ID, "status": "RUNNING", "freeze_ts": FREEZE.isoformat()}
IDENTITY = V3ActivationIdentity(EXPERIMENT_ID, "RUNNING", int(FREEZE.timestamp()) * 1_000_000_000)


def test_activation_allows_draining_only_for_same_frozen_identity():
    for status in ("RUNNING", "DRAINING"):
        assert is_v3_evaluator_activated(
            enabled=True, registry_spec=REGISTRY, db_status=status,
            db_started_at=FREEZE, expected_activation=IDENTITY
        )
    for status in ("CANCELLED", "PAUSED", "COMPLETED"):
        assert not is_v3_evaluator_activated(
            enabled=True, registry_spec=REGISTRY, db_status=status,
            db_started_at=FREEZE, expected_activation=IDENTITY
        )


def test_scheduler_query_only_allows_v3_draining_not_other_experiments():
    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = [(EXPERIMENT_ID,)]
    assert _load_prospective_experiments(conn) == [EXPERIMENT_ID]
    sql = conn.cursor.return_value.execute.call_args.args[0]
    assert "experiment_id = 'MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS'" in sql
    assert "status = 'DRAINING'" in sql
    assert "status = 'RUNNING'" in sql


def test_v3_capture_requires_running_and_evaluator_allows_draining():
    base = Path(__file__).resolve().parents[1] / "app/research"
    observer = (base / "prospective_observer.py").read_text(encoding="utf-8")
    evaluator = (base / "me_reverse_long_v3/evaluator.py").read_text(encoding="utf-8")
    writer = (base / "me_reverse_long_v3/repository.py").read_text(encoding="utf-8")
    assert "if exp_id == \"MOMENTUM_EXHAUSTION_REVERSE_LONG_V3_OOS\"" in observer
    assert 'str(row[0]) == "RUNNING"' in observer
    assert "AND status = 'RUNNING'" in observer
    assert "AND e.status IN ('RUNNING', 'DRAINING')" in evaluator
    assert "AND e.status IN ('RUNNING', 'DRAINING')" in writer
    assert "FOR UPDATE OF o" in writer and "FOR SHARE OF e" in writer


def test_sql_is_install_only_and_blocks_premature_cancel_or_reactivation():
    sql = (Path(__file__).resolve().parents[1] /
           "sql/migrations/076_me_reverse_long_v3_draining_lifecycle.sql").read_text()
    assert "CREATE TRIGGER trg_guard_me_reverse_long_v3_drain" in sql
    assert "OLD.status = 'RUNNING' AND NEW.status = 'CANCELLED'" in sql
    assert "OLD.status = 'DRAINING' AND NEW.status = 'CANCELLED'" in sql
    assert "r.status IS DISTINCT FROM 'FINALIZED'" in sql
    assert "IF unfinished <> 0 THEN" in sql
    assert "UPDATE research.prospective_experiment SET" not in sql
