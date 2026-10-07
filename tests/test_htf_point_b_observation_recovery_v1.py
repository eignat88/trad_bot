"""Dedicated completed-event-only recovery tests for HTF Point-B."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.research.htf_point_b_observation_recovery import (
    EXPERIMENT_ID,
    FREEZE_TS,
    classify_completed_event,
    recover_completed_events,
    recovery_source_signal_id,
)
from app.research.prospective_observer import ProspectiveOOSObserver


FREEZE_TS = "2026-10-06T14:00:00Z"
FREEZE_DT = datetime.fromisoformat(FREEZE_TS.replace("Z", "+00:00"))
SIGNAL_TS = "2026-10-06T14:05:00+00:00"
POST_FREEZE_TS = "2026-10-06T14:05:00+00:00"
POST_FREEZE_DT = datetime.fromisoformat(POST_FREEZE_TS)
SETUP_ID = "recovery-point-b-1"


def _row(
    setup_id=SETUP_ID,
    *,
    experiment_id=EXPERIMENT_ID,
    status="COMPLETED_IMMUTABLE",
    symbol="BTCUSDT",
    row_direction="LONG",
    frozen_at=POST_FREEZE_DT,
    cohort="POINT_B",
    snapshot_experiment_id=EXPERIMENT_ID,
    snapshot_setup_id=None,
    snapshot_symbol=None,
    snapshot_direction=None,
    signal_time=None,
    entry=105.1,
    stop=99.4,
    risk=5.7,
):
    snapshot_setup_id = snapshot_setup_id if snapshot_setup_id is not None else setup_id
    snapshot_symbol = snapshot_symbol if snapshot_symbol is not None else symbol
    snapshot_direction = snapshot_direction if snapshot_direction is not None else row_direction
    signal_time = signal_time if signal_time is not None else SIGNAL_TS
    snapshot = {
        "experiment_id": snapshot_experiment_id,
        "setup_event_id": snapshot_setup_id,
        "symbol": snapshot_symbol,
        "direction": snapshot_direction,
        "signal_time": signal_time,
        "entry_reference_price": entry,
        "structural_stop_price": stop,
        "risk_abs": risk,
        "risk_pct": risk / entry * 100 if entry else None,
        "target_1": 116.5 if snapshot_direction == "LONG" else 77.0,
        "target_2": 122.2 if snapshot_direction == "LONG" else 71.3,
        "point_b_time": "2026-10-06T14:02:35+00:00",
        "point_b_price": entry,
        "point_b_retrace_pct": 50.0,
        "frozen_at": FREEZE_TS,
    }
    if cohort is not None:
        snapshot["cohort"] = cohort
    return {
        "experiment_id": experiment_id,
        "setup_event_id": setup_id,
        "symbol": symbol,
        "direction": row_direction,
        "status": status,
        "snapshot": snapshot,
        "frozen_at": frozen_at,
        "created_at": frozen_at,
    }


class FakeConnection:
    def __init__(self, existing=(), execute_handler=None):
        self.existing = set(existing)
        self.inserts = []
        self.selects = []
        self.execute_handler = execute_handler
        self.commits = 0
        self.rollbacks = 0
        self._result = None

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        if "SELECT observation_id" in sql:
            self.selects.append((sql, params))
            identity = (params[0], params[1])
            self._result = (1,) if identity in self.existing else None
        elif "INSERT INTO research.prospective_observation" in sql:
            identity = (params[0], params[1])
            if identity in self.existing:
                self._result = None
            else:
                self.existing.add(identity)
                self.inserts.append(params)
                self._result = (100 + len(self.inserts),)
        else:
            self._result = None
        if self.execute_handler:
            self.execute_handler(sql, params)

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []

    def close(self):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_valid_new_and_legacy_snapshots_classify_insert():
    new = _row("new")
    legacy = _row("legacy", cohort=None)
    new_verdict = classify_completed_event(new)
    legacy_verdict = classify_completed_event(legacy)

    assert new_verdict["verdict"] == "WOULD_INSERT"
    assert legacy_verdict["verdict"] == "WOULD_INSERT"
    assert new_verdict["source_signal_id"] == recovery_source_signal_id(EXPERIMENT_ID, "new")
    assert legacy_verdict["source_signal_id"] == ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, "legacy")
    assert json.loads(legacy_verdict["payload"]["features"])["cohort"] == "POINT_B"
    assert json.loads(legacy_verdict["payload"]["features"])["_frozen_intrabar_policy"] == "STOP_FIRST"
    assert json.loads(legacy_verdict["payload"]["features"])["_frozen_max_hold"] == 240


def test_all_required_rejections_have_single_final_verdict():
    cases = {
        "prefreeze": _row("prefreeze", frozen_at=FREEZE_DT),
        "wrong-experiment": _row("wrong", experiment_id="OTHER"),
        "snapshot-experiment": _row("snapshot-experiment", snapshot_experiment_id="OTHER"),
        "setup-mismatch": _row("setup-mismatch", snapshot_setup_id="other"),
        "symbol-mismatch": _row("symbol-mismatch", snapshot_symbol="ETHUSDT"),
        "direction-mismatch": _row("direction-mismatch", snapshot_direction="SHORT"),
        "invalid-direction": _row("invalid-direction", row_direction="SIDEWAYS"),
        "risk-zero": _row("risk-zero", risk=0.0),
        "invalid-entry": _row("invalid-entry", entry=0.0),
        "invalid-stop": _row("invalid-stop", stop=0.0),
        "nan-entry": _row("nan-entry", entry=float("nan")),
        "inf-entry": _row("inf-entry", entry=float("inf")),
        "malformed-time": _row("malformed-time", signal_time="not-a-time"),
        "wrong-cohort": _row("wrong-cohort", cohort="BASELINE"),
    }
    cases["prefreeze"]["snapshot"]["signal_time"] = FREEZE_TS
    cases["prefreeze"]["frozen_at"] = datetime(2026, 10, 6, 14, 5, tzinfo=timezone.utc)
    expected = {
        "prefreeze": "prefreeze",
        "wrong-experiment": "experiment_mismatch",
        "snapshot-experiment": "snapshot_experiment_mismatch",
        "setup-mismatch": "setup_id_mismatch",
        "symbol-mismatch": "symbol_mismatch",
        "direction-mismatch": "direction_mismatch",
        "invalid-direction": "direction_mismatch",
        "risk-zero": "invalid_risk",
        "invalid-entry": "invalid_entry",
        "invalid-stop": "invalid_stop",
        "nan-entry": "invalid_entry",
        "inf-entry": "invalid_entry",
        "malformed-time": "malformed",
        "wrong-cohort": "wrong_cohort",
    }

    for key, verdict in expected.items():
        assert classify_completed_event(cases[key])["verdict"] == verdict, key


def test_already_observed_apply_idempotence_and_reconciliation():
    row = _row("already")
    source = recovery_source_signal_id(EXPERIMENT_ID, "already")
    conn = FakeConnection(existing={(EXPERIMENT_ID, source)})
    report = recover_completed_events(None, conn, [row], apply=True)

    assert report["scanned"] == report["eligible"] == 1
    assert report["already_observed"] == 1
    assert report["would_insert"] == report["inserted"] == 0
    assert report["prefreeze"] == 0
    assert report["experiment_mismatch"] == 0
    assert report["snapshot_experiment_mismatch"] == 0
    assert report["setup_id_mismatch"] == 0
    assert report["symbol_mismatch"] == 0
    assert report["direction_mismatch"] == 0
    assert report["invalid_signal_time"] == 0
    assert report["invalid_entry"] == 0
    assert report["invalid_stop"] == 0
    assert report["invalid_risk"] == 0
    assert report["wrong_cohort"] == 0
    assert report["malformed"] == 0
    assert report["identity_collision"] == 0
    assert conn.inserts == []


def test_first_apply_inserts_once_second_apply_zero_and_completed_unchanged():
    rows = [_row("legacy-1", cohort=None), _row("legacy-2", cohort=None)]
    before = deepcopy(rows)
    conn = FakeConnection()

    first = recover_completed_events(None, conn, rows, apply=True)
    second = recover_completed_events(None, conn, rows, apply=True)
    third_dry = recover_completed_events(None, conn, rows, apply=False)

    assert first["scanned"] == first["eligible"] == first["would_insert"] == first["inserted"] == 2
    assert second["scanned"] == second["eligible"] == second["already_observed"] == 2
    assert second["inserted"] == second["would_insert"] == 0
    assert third_dry["would_insert"] == 0
    assert third_dry["already_observed"] == 2
    assert rows == before
    assert conn.inserts[0][1] == ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, "legacy-1")
    assert conn.inserts[1][1] == ProspectiveOOSObserver._make_htf_source_key(EXPERIMENT_ID, "legacy-2")


def test_baseline_and_identity_collision_fail_closed():
    baseline = _row("baseline", experiment_id="HTF_KEYLEVEL_KEYLEVEL_BASELINE_V1_PROSPECTIVE")
    first = _row("collision")
    second = deepcopy(first)
    conn = FakeConnection()

    report = recover_completed_events(None, conn, [baseline, first, second], apply=True)

    assert report["experiment_mismatch"] == 1
    assert report["identity_collision"] == 1
    assert report["eligible"] == 1
    assert len(conn.inserts) == 1


def test_dry_run_makes_zero_writes_and_reconciles_all_rows():
    rows = [
        _row("valid"),
        _row("legacy", cohort=None),
        _row("bad-risk", risk=-1),
        _row("bad-experiment", experiment_id="OTHER"),
    ]
    conn = FakeConnection()

    report = recover_completed_events(None, conn, rows, apply=False)

    assert report["scanned"] == 4
    assert report["eligible"] == report["would_insert"] == 2
    assert report["invalid_risk"] == 1
    assert report["experiment_mismatch"] == 1
    assert (
        report["scanned"]
        == report["eligible"]
        + report["invalid_risk"]
        + report["experiment_mismatch"]
    )
    assert conn.inserts == []
    assert conn.commits == 0
    assert conn.rollbacks == 1

def test_report_does_not_create_uppercase_would_insert_counter():
    rows = [_row("report-key")]
    conn = FakeConnection()

    report = recover_completed_events(
        None,
        conn,
        rows,
        apply=False,
    )

    assert report["would_insert"] == 1
    assert report["eligible"] == 1
    assert "WOULD_INSERT" not in report

def test_default_connection_uses_project_load_settings(monkeypatch, tmp_path):
    import argparse
    import types
    import app.research.htf_point_b_observation_recovery as recovery

    captured = {}

    fake_settings = types.SimpleNamespace(
        db_host="db.example",
        db_port=5439,
        db_name="configured_db",
        db_user="configured_user",
        db_password="configured_password",
    )

    def fake_load_settings(*, path, env_file):
        captured["path"] = path
        captured["env_file"] = env_file
        return fake_settings

    fake_conn = object()

    class FakeScannerRepository:
        def __init__(self, host, port, database, user, password):
            captured["repo"] = (host, port, database, user, password)
            self._conn = fake_conn

    monkeypatch.setattr("app.config.settings.load_settings", fake_load_settings)
    monkeypatch.setattr("app.db.repository.ScannerRepository", FakeScannerRepository)

    args = argparse.Namespace(
        host=None,
        port=5432,
        database=None,
        user=None,
        password=None,
    )

    conn = recovery._load_connection(args)

    assert conn is fake_conn
    assert captured["path"].name == "config.yaml"
    assert captured["env_file"].name == ".env"
    assert captured["repo"] == (
        "db.example",
        5439,
        "configured_db",
        "configured_user",
        "configured_password",
    )
