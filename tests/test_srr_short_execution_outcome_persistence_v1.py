from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.research.srr_short_execution_outcome_persistence import (
    SRR_ADAPTER_VERSION,
    SRR_FROZEN_POLICY_VERSION,
    SrrOutcomeWriter,
)

OBS_ID = 424242
EXP_ID = "SRR_SHORT_EXECUTION_R_EXPANSION_PROSPECTIVE_VALIDATION_V1"
FREEZE = "2026-10-07T08:17:50Z"
SIGNAL_TIME = datetime(2026, 10, 7, 9, 20, tzinfo=timezone.utc)
ACTIVATION_TS = "2026-10-07T09:00:00Z"


def make_activation_gate():
    """Helper: an ACTIVE gate valid for OBS_ID's signal time."""
    from app.research.srr_short_writer_activation_boundary_v1 import (
        ACTIVATION_STATUS_ACTIVE,
        SRR_ACTIVATION_BOUNDARY_V1,
        SrrShortWriterActivationBoundary,
        SrrShortWriterActivationGate,
        SrrShortWriterActivationMode,
    )

    boundary = SrrShortWriterActivationBoundary.create(ACTIVATION_TS)
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate.verify_persisted_record({
        "experiment_id": EXP_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": "SHORT",
        "status": ACTIVATION_STATUS_ACTIVE,
    })
    return gate


class Cursor:
    def __init__(self, rows=None, fail_on=None):
        self.rows = list(rows or [])
        self.fail_on = fail_on
        self.executed = []
        self.closed = False
        self._next = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("injected persistence failure")
        if "srr_short_writer_activation" in sql:
            # Activation record read → return an ACTIVE row
            self._next = (
                EXP_ID, "SHORT",
                "SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1",
                ACTIVATION_TS, "ACTIVE",
            )
        elif sql.strip().startswith("SELECT observation_id"):
            self._next = self.rows.pop(0) if self.rows else None
        elif sql.strip().startswith("SELECT is_final"):
            self._next = self.rows.pop(0) if self.rows else None
        elif sql.strip().startswith("INSERT"):
            self._next = (OBS_ID, params[params.index(True)] if True in params else False)
        elif sql.strip().startswith("UPDATE"):
            self._next = (OBS_ID, True)

    def fetchone(self):
        return self._next

    def close(self):
        self.closed = True


class Conn:
    def __init__(self, rows=None, fail_on=None):
        self.cursor_obj = Cursor(rows, fail_on)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def observation():
    return {
        "observation_id": OBS_ID,
        "experiment_id": EXP_ID,
        "symbol": "AAAUSDT",
        "direction": "SHORT",
        "signal_time": SIGNAL_TIME,
        "reference_price": 100.0,
        "invalidation_price": 100.5,
        "variant_entry": 100.0,
        "variant_stop": 101.0,
        "variant_target": 99.25,
        "features": json.dumps({"_frozen_freeze_ts": FREEZE}),
    }


def final_result():
    return {
        "status": "RESOLVED",
        "reason_code": "SRR_SHORT_POLICY_OK",
        "path_class": "TP_FIRST",
        "finalization_eligible": True,
        "source_status": "SOURCE_VALIDATED",
        "eligible_candle_count": 23,
        "gross_r": 0.75,
        "cost_r_normal": 0.21,
        "cost_r_elevated": 0.31,
        "net_r_normal": 0.54,
        "net_r_elevated": 0.44,
        "policy_result": {
            "structural_r": 0.5,
            "execution_r": 1.0,
            "max_hold_minutes": 120,
            "intrabar_policy": "STOP_FIRST",
            "coverage_complete": True,
            "source_confidence": "ARCHIVE_VALIDATED_LOCAL",
            "source_provenance": {
                "source_kind": "OFFLINE_ARCHIVE",
                "source_id": "archive-1",
                "retrieval_method": "archive",
                "notes": "fixture",
            },
            "data_quality": {"input_candle_count": 23},
            "evaluation_asof_ms": 1791450000000,
        },
        "source_diagnostics": {"validated_open_count": 23},
        "route_diagnostics": {"adapter_id": SRR_ADAPTER_VERSION},
    }


def nonfinal_result(status="SOURCE_UNVERIFIABLE", reason="SOURCE_FETCH_EXCEPTION"):
    result = final_result()
    result.update(
        {
            "status": status,
            "reason_code": reason,
            "path_class": None,
            "finalization_eligible": False,
            "source_status": "SOURCE_FETCH_ERROR",
            "eligible_candle_count": 0,
            "gross_r": None,
            "cost_r_normal": None,
            "cost_r_elevated": None,
            "net_r_normal": None,
            "net_r_elevated": None,
        }
    )
    result["policy_result"] = {
        "structural_r": 0.5,
        "execution_r": 1.0,
        "max_hold_minutes": 120,
        "intrabar_policy": "STOP_FIRST",
        "coverage_complete": False,
        "source_confidence": "UNPROVEN",
        "source_provenance": {"source_kind": "BYBIT", "source_id": "source"},
        "data_quality": {},
        "evaluation_asof_ms": 1791450000000,
    }
    return result


def test_insert_is_atomic_idempotent_and_preserves_frozen_snapshot():
    conn = Conn(rows=[None])
    writer = SrrOutcomeWriter(conn, activation_gate=make_activation_gate())
    result = writer.write(
        observation_id=OBS_ID,
        experiment_id=EXP_ID,
        observation=observation(),
        result=final_result(),
    )
    assert result.action == "INSERTED"
    assert result.is_final is True
    assert conn.commits == 1
    assert conn.rollbacks == 0
    sql, params = conn.cursor_obj.executed[-1]
    assert "INSERT INTO research.srr_short_execution_prospective_outcome" in sql
    assert "research.research_outcome" not in sql
    assert "research.prospective_outcome" not in sql
    assert params[params.index("TP_FIRST")] == "TP_FIRST"
    assert params[params.index(0.75)] == 0.75
    assert params[params.index("archive-1")] == "archive-1"
    assert params[params.index(SRR_FROZEN_POLICY_VERSION)] == SRR_FROZEN_POLICY_VERSION


def test_nonfinal_source_error_persists_without_fictitious_economics():
    conn = Conn(rows=[None])
    result = SrrOutcomeWriter(conn, activation_gate=make_activation_gate()).write(
        observation_id=OBS_ID,
        experiment_id=EXP_ID,
        observation=observation(),
        result=nonfinal_result(),
    )
    assert result.action == "INSERTED"
    assert result.is_final is False
    sql, params = conn.cursor_obj.executed[-1]
    assert params[params.index("SOURCE_UNVERIFIABLE")] == "SOURCE_UNVERIFIABLE"
    assert 0.75 not in params
    assert 0.54 not in params


def test_finalization_conflict_is_not_overwritten():
    conn = Conn(rows=[(OBS_ID, False), (OBS_ID, True)])
    result = SrrOutcomeWriter(conn, activation_gate=make_activation_gate()).write(
        observation_id=OBS_ID,
        experiment_id=EXP_ID,
        observation=observation(),
        result=final_result(),
    )
    assert result.action == "ALREADY_FINALIZED"
    assert result.is_final is True
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_conditional_finalization_updates_only_nonfinal_rows():
    conn = Conn(rows=[(OBS_ID, False), (False,)])
    result = SrrOutcomeWriter(conn, activation_gate=make_activation_gate()).write(
        observation_id=OBS_ID,
        experiment_id=EXP_ID,
        observation=observation(),
        result=final_result(),
    )
    assert result.action == "FINALIZED"
    assert conn.commits == 1
    sql = conn.cursor_obj.executed[-1][0]
    assert "WHERE observation_id = %s" in sql
    assert "AND is_final = FALSE" in sql


def test_write_failure_rolls_back_transaction():
    conn = Conn(rows=[None], fail_on="INSERT INTO research.srr_short_execution_prospective_outcome")
    with pytest.raises(RuntimeError):
        SrrOutcomeWriter(conn, activation_gate=make_activation_gate()).write(
            observation_id=OBS_ID,
            experiment_id=EXP_ID,
            observation=observation(),
            result=final_result(),
        )
    assert conn.rollbacks == 1
    assert conn.commits == 0


def test_final_result_with_missing_economics_is_rejected():
    bad = final_result()
    bad["net_r_normal"] = None
    conn = Conn(rows=[None])
    with pytest.raises(ValueError):
        SrrOutcomeWriter(conn, activation_gate=make_activation_gate()).write(
            observation_id=OBS_ID,
            experiment_id=EXP_ID,
            observation=observation(),
            result=bad,
        )


def test_nonfinal_result_with_economics_is_rejected():
    conn = Conn(rows=[None])
    bad = nonfinal_result()
    bad["gross_r"] = 0.75
    with pytest.raises(ValueError):
        SrrOutcomeWriter(conn, activation_gate=make_activation_gate()).write(
            observation_id=OBS_ID,
            experiment_id=EXP_ID,
            observation=observation(),
            result=bad,
        )


def test_prefreeze_observation_is_rejected():
    """A pre-freeze observation is rejected either by the activation boundary
    (pre-activation) or by the writer's own freeze check."""
    from app.research.srr_short_writer_activation_boundary_v1 import (
        SrrShortWriterActivationBoundary,
    )

    early = observation()
    early["signal_time"] = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    # Boundary before the freeze ts, so the pre-freeze check is reached.
    boundary = SrrShortWriterActivationBoundary.create("2026-10-07T00:00:00Z")
    from app.research.srr_short_writer_activation_boundary_v1 import (
        ACTIVATION_STATUS_ACTIVE,
        SRR_ACTIVATION_BOUNDARY_V1,
        SrrShortWriterActivationGate,
        SrrShortWriterActivationMode,
    )
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate.verify_persisted_record({
        "experiment_id": EXP_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": "2026-10-07T00:00:00Z",
        "direction": "SHORT",
        "status": ACTIVATION_STATUS_ACTIVE,
    })

    # Override the Cursor's activation record read to return this boundary.
    class EarlyCursor(Cursor):
        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            if "srr_short_writer_activation" in sql:
                self._next = (
                    EXP_ID, "SHORT",
                    "SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1",
                    "2026-10-07T00:00:00Z", "ACTIVE",
                )
            else:
                super().execute(sql, params)

    class EarlyConn(Conn):
        def __init__(self):
            self.cursor_obj = EarlyCursor([None])
            self.commits = 0
            self.rollbacks = 0

    conn = EarlyConn()
    with pytest.raises(ValueError, match="after freeze"):
        SrrOutcomeWriter(conn, activation_gate=gate).write(
            observation_id=OBS_ID,
            experiment_id=EXP_ID,
            observation=early,
            result=final_result(),
        )


def test_writer_without_gate_is_rejected_before_sql():
    """Regression: writer with no gate must fail closed before any SQL."""
    conn = Conn(rows=[None])
    result = SrrOutcomeWriter(conn).write(
        observation_id=OBS_ID,
        experiment_id=EXP_ID,
        observation=observation(),
        result=final_result(),
    )
    assert result.action == "REJECTED_NO_ACTIVATION_GATE"
    assert conn.cursor_obj.executed == []
    assert conn.commits == 0
    assert conn.rollbacks == 0
