"""Freeze the approved ME reverse LONG V3 registry identity after recovery.

Research-only: this test does not connect to production PostgreSQL.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.research.me_reverse_long_v3.activation import is_v3_evaluator_activated
from app.research.me_reverse_long_v3.protocol import EXPERIMENT_ID

ROOT = Path(__file__).resolve().parents[1]
FROZEN_AT = datetime(2026, 10, 13, 12, 37, 3, tzinfo=timezone.utc)


def _registry():
    payload = json.loads(
        (ROOT / "app/research/prospective_registry.json").read_text(encoding="utf-8")
    )
    specs = [row for row in payload["experiments"] if row["experiment_id"] == EXPERIMENT_ID]
    assert len(specs) == 1
    return specs[0]


def test_me_v3_recovered_registry_identity_is_frozen():
    spec = _registry()
    assert spec["status"] == "RUNNING"
    assert spec["started_at"] == "2026-10-13T12:37:03Z"
    assert spec["freeze_ts"] == "2026-10-13T12:37:03Z"
    assert spec["threshold"] == 0.4
    assert spec["position_sizing_rule"] == "shadow_only"


def test_me_v3_activation_requires_original_db_boundary():
    spec = _registry()
    assert is_v3_evaluator_activated(
        enabled=True, registry_spec=spec,
        db_status="RUNNING", db_started_at=FROZEN_AT,
    )
    assert not is_v3_evaluator_activated(
        enabled=True, registry_spec=spec,
        db_status="RUNNING", db_started_at=FROZEN_AT.replace(second=4),
    )
    assert not is_v3_evaluator_activated(
        enabled=True, registry_spec=spec,
        db_status="PAUSED", db_started_at=FROZEN_AT,
    )
    assert not is_v3_evaluator_activated(
        enabled=False, registry_spec=spec,
        db_status="RUNNING", db_started_at=FROZEN_AT,
    )
