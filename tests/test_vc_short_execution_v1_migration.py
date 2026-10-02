from pathlib import Path
import json


MIGRATION = Path("sql/migrations/055_vc_short_execution_v1_registration.sql")


def test_vc_execution_migration_registration_values():
    sql = MIGRATION.read_text()
    assert "INSERT INTO research.prospective_experiment" in sql
    assert "'VC_SHORT_EXECUTION_V1', 1," in sql
    assert "'VOLATILITY_COMPRESSION', 'SHORT', 'EXECUTION_PROSPECTIVE_VALIDATION'" in sql
    assert "bb_width_percentile < 0.569723" in sql
    assert "reference_price" in sql
    assert "invalidation_price" in sql
    assert "target_1" in sql
    assert "0.569723" in sql
    assert "50, 10," in sql
    assert "'shadow_only'" in sql
    assert "'READY_TO_START', NULL" in sql
    assert "ON CONFLICT (experiment_id) DO NOTHING" in sql
    assert "BEGIN;" in sql
    assert "COMMIT;" in sql


def test_vc_execution_migration_does_not_modify_existing_runtime():
    sql = MIGRATION.read_text()
    assert "UPDATE research.prospective_experiment" not in sql
    assert "DELETE FROM research.prospective_experiment" not in sql
    assert "scanner_runner" not in sql
    assert "INSERT INTO paper" not in sql
    assert "UPDATE paper" not in sql
    assert "DELETE FROM paper" not in sql
    assert "INSERT INTO live" not in sql
    assert "UPDATE live" not in sql
    assert "DELETE FROM live" not in sql


def test_vc_execution_registry_and_protocol_remain_frozen():
    registry = json.loads(Path("app/research/prospective_registry.json").read_text())
    experiment = next(
        item for item in registry["experiments"]
        if item["experiment_id"] == "VC_SHORT_EXECUTION_V1"
    )
    assert experiment["freeze_ts"] == "2026-10-02T07:30:21Z"
    assert experiment["threshold"] == 0.569723
    assert experiment["max_hold_minutes"] == 120
    assert experiment["intrabar_policy"] == "STOP_FIRST"
