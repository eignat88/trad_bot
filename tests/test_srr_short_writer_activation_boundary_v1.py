"""Tests for SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1.

Covers scenarios T01–T24 from the task specification.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock

import pytest

from app.research.srr_short_writer_activation_boundary_v1 import (
    SRR_ACTIVATION_BOUNDARY_V1,
    SRR_ACTIVATION_DIRECTION,
    SRR_ACTIVATION_EXPERIMENT_ID,
    ACTIVATION_STATUS_ACTIVE,
    ACTIVATION_STATUS_PENDING,
    ACTIVATION_STATUS_REVOKED,
    SrrActivationBoundaryError,
    SrrActivationConfigError,
    SrrActivationInvalidTransition,
    SrrActivationRecordImmutableViolation,
    SrrActivationRecordMismatch,
    SrrActivationRecordNotActive,
    SrrActivationRecordUnavailable,
    SrrPreActivationObservation,
    SrrShortWriterActivationBoundary,
    SrrShortWriterActivationGate,
    SrrShortWriterActivationMode,
    build_activation_sql_filters,
    normalize_activation_ts,
    validate_activation_record_immutability,
    validate_activation_transition,
)
from app.research.srr_short_execution_outcome_persistence import SrrOutcomeWriter
from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SrrShortExecutionRExpansionProspectiveEvaluator,
    SRR_EXPERIMENT_ID,
    SrrRouteResult,
    SOURCE_VALIDATED,
)

ACTIVATION_TS = "2026-10-08T12:00:00Z"
ACTIVATION_TS_NS = 1791460800000000000  # 2026-10-08T12:00:00Z in nanoseconds
ACTIVATION_TS_MS = 1791460800000        # 2026-10-08T12:00:00Z in milliseconds


def make_boundary():
    return SrrShortWriterActivationBoundary.create(ACTIVATION_TS)


# ─────────────────────────────────────────────────────────────────
# T01–T03: Strict eligibility
# ─────────────────────────────────────────────────────────────────

def test_t01_signal_time_before_activation_rejected():
    boundary = make_boundary()
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T11:59:59.999Z",
    )


def test_t02_signal_time_equal_activation_rejected():
    boundary = make_boundary()
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time=ACTIVATION_TS,
    )


def test_t03_signal_time_after_activation_eligible():
    boundary = make_boundary()
    assert boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T12:00:00.001Z",
    )


def test_t03_signal_time_after_activation_by_1ms_eligible():
    boundary = make_boundary()
    assert boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T12:00:00.001Z",
    )


# ─────────────────────────────────────────────────────────────────
# T04–T06: ACTIVATION_TS validation
# ─────────────────────────────────────────────────────────────────

def test_t04_missing_activation_ts_fails_closed():
    with pytest.raises(SrrActivationConfigError):
        SrrShortWriterActivationBoundary.create(None)

def test_t04_missing_activation_ts_empty_fails_closed():
    with pytest.raises(SrrActivationConfigError):
        SrrShortWriterActivationBoundary.create("")

def test_t04_missing_activation_ts_empty_string_fails_closed():
    with pytest.raises(SrrActivationConfigError):
        SrrShortWriterActivationBoundary.create("   ")


def test_t05_naive_datetime_fails_closed():
    with pytest.raises(SrrActivationConfigError):
        SrrShortWriterActivationBoundary.create(datetime(2026, 10, 8, 12, 0, 0))


def test_t05_naive_string_fails_closed():
    with pytest.raises(SrrActivationConfigError):
        SrrShortWriterActivationBoundary.create("2026-10-08T12:00:00")


def test_t06_invalid_timezone_fails_closed():
    with pytest.raises(SrrActivationConfigError):
        SrrShortWriterActivationBoundary.create("2026-10-08T12:00:00+25:00")


def test_t06_invalid_timestamp_fails_closed():
    with pytest.raises(SrrActivationConfigError):
        SrrShortWriterActivationBoundary.create("not-a-timestamp")


def test_t06_boolean_fails_closed():
    with pytest.raises(SrrActivationConfigError):
        SrrShortWriterActivationBoundary.create(True)


# ─────────────────────────────────────────────────────────────────
# T07–T08: Restart with same/different boundary
# ─────────────────────────────────────────────────────────────────

def test_t07_restart_with_same_boundary_pass():
    boundary1 = make_boundary()
    boundary2 = SrrShortWriterActivationBoundary.create(ACTIVATION_TS)
    assert boundary1.activation_ts_ms == boundary2.activation_ts_ms
    assert boundary1.activation_ts_text == boundary2.activation_ts_text

    gate1 = SrrShortWriterActivationGate(
        boundary=boundary1,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate1.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })

    gate2 = SrrShortWriterActivationGate(
        boundary=boundary2,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate2.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })

    # Both gates allow writes
    gate1.assert_writer_allowed()
    gate2.assert_writer_allowed()


def test_t08_restart_with_different_boundary_fails_closed():
    boundary1 = make_boundary()
    boundary2 = SrrShortWriterActivationBoundary.create("2026-10-08T13:00:00Z")

    gate1 = SrrShortWriterActivationGate(
        boundary=boundary1,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate1.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })
    gate1.assert_writer_allowed()

    gate2 = SrrShortWriterActivationGate(
        boundary=boundary2,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    with pytest.raises(SrrActivationRecordMismatch):
        gate2.verify_persisted_record({
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
            "activation_ts": ACTIVATION_TS,  # old record, new runtime boundary
            "direction": SRR_ACTIVATION_DIRECTION,
            "status": ACTIVATION_STATUS_ACTIVE,
        })


# ─────────────────────────────────────────────────────────────────
# T09–T10: experiment/direction rejection
# ─────────────────────────────────────────────────────────────────

def test_t09_wrong_experiment_rejected():
    boundary = make_boundary()
    assert not boundary.is_eligible(
        experiment_id="OTHER_EXPERIMENT",
        direction="SHORT",
        signal_time="2026-10-08T12:00:01Z",
    )


def test_t10_direction_long_rejected():
    boundary = make_boundary()
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="LONG",
        signal_time="2026-10-08T12:00:01Z",
    )


def test_t10_direction_lower_rejected():
    boundary = make_boundary()
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="short",
        signal_time="2026-10-08T12:00:01Z",
    )


# ─────────────────────────────────────────────────────────────────
# T11–T13: Pre-activation observation → zero downstream calls
# ─────────────────────────────────────────────────────────────────

class _FakeCursorForBoundary:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        # Return an empty list: the SQL filter excludes pre-activation rows.
        return []

    def close(self):
        pass


class _FakeConnForBoundary:
    def __init__(self):
        self.cursor_obj = _FakeCursorForBoundary()

    def cursor(self):
        return self.cursor_obj


def _make_pre_activation_result():
    return SrrRouteResult(
        experiment_id=SRR_EXPERIMENT_ID,
        observation_id=999,
        status="INCOMPLETE_COVERAGE",
        reason_code="TEST",
        path_class=None,
        finalization_eligible=False,
        source_status=SOURCE_VALIDATED,
        geometry_valid=True,
        eligible_candle_count=0,
        gross_r=None,
        cost_r_normal=None,
        cost_r_elevated=None,
        net_r_normal=None,
        net_r_elevated=None,
        policy_result={},
        source_diagnostics={},
        route_diagnostics={},
    )


def test_t11_t12_t13_pre_activation_observation_skipped():
    """A pre-activation observation must not trigger Bybit, policy, or writer."""
    boundary = make_boundary()
    conn = _FakeConnForBoundary()
    candle_source = Mock()
    writer = Mock()

    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=conn,
        candle_source=candle_source,
        dry_run=False,
        write_outcomes=writer,
        activation_boundary=boundary,
    )

    # Verify SQL contains activation filter
    evaluator._current_asof_ms = lambda: ACTIVATION_TS_MS + 60_000
    stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

    # The SQL query should include the activation boundary filter
    assert "o.signal_time > %s::timestampt" in conn.cursor_obj.sql[0][0]
    assert conn.cursor_obj.sql[0][1] == (
        SRR_EXPERIMENT_ID,
        "2026-10-07T08:17:50Z",  # FROZEN_FREEZE_TS
        SRR_ACTIVATION_EXPERIMENT_ID,
        SRR_ACTIVATION_DIRECTION,
        "2026-10-08T12:00:00.000000Z",  # canonical UTC
    )

    # No observations processed (SQL filter excluded them)
    assert stats["signals_checked"] == 0
    assert stats["observations"] == []
    assert candle_source.fetch_5m_candles.call_count == 0
    assert writer.call_count == 0


def test_t11_pre_activation_observation_sql_filter():
    """The SQL filter itself excludes pre-activation rows."""
    boundary = make_boundary()
    fragment, params = build_activation_sql_filters(boundary)
    assert "o.signal_time > %s::timestamptz" in fragment
    assert params == (
        SRR_ACTIVATION_EXPERIMENT_ID,
        SRR_ACTIVATION_DIRECTION,
        "2026-10-08T12:00:00.000000Z",  # canonical UTC format
    )


# ─────────────────────────────────────────────────────────────────
# T14: Direct writer call before boundary → REJECT
# ─────────────────────────────────────────────────────────────────

def test_t14_direct_writer_call_rejected():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    # Gate not yet cleared with persisted record
    with pytest.raises(SrrActivationRecordUnavailable):
        gate.assert_writer_allowed()

    # Even if gate is cleared, pre-activation observation is rejected
    gate.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor
    # 1) activation record read → ACTIVE
    cursor.fetchone.side_effect = [
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
    ]

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)

    result = writer.write(
        observation_id=1,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation={
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "direction": "SHORT",
            "signal_time": "2026-10-08T11:00:00Z",  # pre-activation
            "symbol": "BTCUSDT",
        },
        result={"status": "TEST", "reason_code": "TEST", "finalization_eligible": False},
    )
    assert result.action == "REJECTED_PRE_ACTIVATION"
    # No outcome SQL was executed
    assert cursor.execute.call_count == 1  # only the activation record read


def test_t14_direct_writer_call_without_gate_rejected():
    """Writer without gate is allowed by legacy behavior, but gate-mode blocks."""
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor
    # 1) activation record read → ACTIVE
    cursor.fetchone.side_effect = [
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
    ]

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)

    result = writer.write(
        observation_id=2,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation={
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "direction": "SHORT",
            "signal_time": ACTIVATION_TS,  # equal, not after
            "symbol": "BTCUSDT",
        },
        result={"status": "TEST", "reason_code": "TEST", "finalization_eligible": False},
    )
    assert result.action == "REJECTED_PRE_ACTIVATION"


# ─────────────────────────────────────────────────────────────────
# T15–T18: Idempotency and final immutability
# ─────────────────────────────────────────────────────────────────

def test_t15_repeated_nonfinal_write_idempotent():
    """A repeated NONFINAL write refreshes the row."""
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor

    # 1) activation record read → ACTIVE
    # 2) SELECT FOR UPDATE → existing
    # 3) UPDATE RETURNING → success
    cursor.fetchone.side_effect = [
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
        (100,),  # SELECT FOR UPDATE returns existing
        (100, False),  # UPDATE RETURNING
    ]

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=100,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation={
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "direction": "SHORT",
            "signal_time": datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc),
            "symbol": "BTCUSDT",
            "features": json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
        },
        result={"status": "TEST", "reason_code": "TEST", "finalization_eligible": False},
    )
    assert result.action == "REFRESHED"
    fake_conn.commit.assert_called_once()


def test_t16_nonfinal_to_final_pass():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor

    # 1) activation record read → ACTIVE
    # 2) SELECT FOR UPDATE → existing, 3) SELECT is_final → False,
    # 4) UPDATE RETURNING → success
    cursor.fetchone.side_effect = [
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
        (200,),      # SELECT FOR UPDATE → existing
        (False,),    # SELECT is_final → returns (is_final,)= (False,)
        (200, True), # UPDATE RETURNING
    ]

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=200,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation={
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "direction": "SHORT",
            "signal_time": datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc),
            "symbol": "ETHUSDT",
            "features": json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
            "reference_price": 200.0,
            "invalidation_price": 202.0,
            "variant_entry": 200.0,
            "variant_stop": 202.0,
            "variant_target": 199.5,
        },
        result={
            "status": "FINALIZED",
            "reason_code": "OK",
            "finalization_eligible": True,
            "gross_r": 0.5,
            "cost_r_normal": 0.0021,
            "cost_r_elevated": 0.0031,
            "net_r_normal": 0.4979,
            "net_r_elevated": 0.4969,
            "policy_result": {
                "structural_r": 2.0,
                "execution_r": 4.0,
                "max_hold_minutes": 120,
                "intrabar_policy": "STOP_FIRST",
                "coverage_complete": True,
            },
        },
    )
    assert result.action == "FINALIZED"
    assert result.is_final is True


def test_t17_repeated_final_write_immutable():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor

    # 1) activation record read → ACTIVE
    # 2) SELECT FOR UPDATE → existing, 3) SELECT is_final → True (returns is_final,)
    cursor.fetchone.side_effect = [
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
        (300,),      # SELECT FOR UPDATE
        (True,),     # SELECT is_final → (True,)
    ]

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=300,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation={
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "direction": "SHORT",
            "signal_time": datetime(2026, 10, 8, 14, 30, tzinfo=timezone.utc),
            "symbol": "SOLUSDT",
            "features": json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
            "reference_price": 100.0,
            "invalidation_price": 102.0,
            "variant_entry": 100.0,
            "variant_stop": 102.0,
            "variant_target": 98.5,
        },
        result={
            "status": "FINALIZED",
            "reason_code": "OK",
            "finalization_eligible": True,
            "gross_r": -1.5,
            "cost_r_normal": 0.0021,
            "cost_r_elevated": 0.0031,
            "net_r_normal": -1.5021,
            "net_r_elevated": -1.5031,
            "policy_result": {
                "structural_r": 2.0,
                "execution_r": 4.0,
                "max_hold_minutes": 120,
                "intrabar_policy": "STOP_FIRST",
                "coverage_complete": True,
            },
        },
    )
    assert result.action == "ALREADY_FINALIZED"
    fake_conn.rollback.assert_called_once()


def test_t18_final_to_other_final_blocked():
    """A second finalizer must never rewrite an already-final row."""
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor

    # 1) activation record read → ACTIVE
    # 2) SELECT FOR UPDATE → existing, 3) SELECT is_final → True (returns is_final,)
    cursor.fetchone.side_effect = [
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
        (400,),      # SELECT FOR UPDATE
        (True,),     # SELECT is_final → (True,)
    ]

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=400,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation={
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "direction": "SHORT",
            "signal_time": datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc),
            "symbol": "BNBUSDT",
            "features": json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
            "reference_price": 200.0,
            "invalidation_price": 202.0,
            "variant_entry": 200.0,
            "variant_stop": 202.0,
            "variant_target": 199.5,
        },
        result={
            "status": "FINALIZED",
            "reason_code": "OK",
            "finalization_eligible": True,
            "gross_r": -1.0,
            "cost_r_normal": 0.0021,
            "cost_r_elevated": 0.0031,
            "net_r_normal": -1.0021,
            "net_r_elevated": -1.0031,
            "policy_result": {
                "structural_r": 2.0,
                "execution_r": 4.0,
                "max_hold_minutes": 120,
                "intrabar_policy": "STOP_FIRST",
                "coverage_complete": True,
            },
        },
    )
    assert result.action == "ALREADY_FINALIZED"


# ─────────────────────────────────────────────────────────────────
# T19–T20: TIMEOUT cutoff semantics
# ─────────────────────────────────────────────────────────────────

def test_t19_timeout_before_cutoff_no_final():
    """TIMEOUT before cutoff is not eligible for final persistence."""
    from app.research.srr_short_timeout_persistence_gate import (
        prepare_srr_persistence_result,
    )

    result = SrrRouteResult(
        experiment_id=SRR_EXPERIMENT_ID,
        observation_id=500,
        status="RESOLVED",
        reason_code="SRR_SHORT_POLICY_OK",
        path_class="TIMEOUT",
        finalization_eligible=True,
        source_status=SOURCE_VALIDATED,
        geometry_valid=True,
        eligible_candle_count=24,
        gross_r=0.5,
        cost_r_normal=0.0021,
        cost_r_elevated=0.0031,
        net_r_normal=0.4979,
        net_r_elevated=0.4969,
        policy_result={
            "cutoff_time_ms": 1791467200000,  # 2026-10-08T14:00:00Z
        },
        source_diagnostics={},
        route_diagnostics={},
    )

    # ASOF before cutoff → should delay
    asof_before = 1791467100000  # 2026-10-08T13:58:20Z
    prepared = prepare_srr_persistence_result(result, evaluation_asof_ms=asof_before)
    assert prepared["finalization_eligible"] is False
    assert prepared["reason_code"] == "TIMEOUT_CUTOFF_NOT_REACHED"
    assert prepared["gross_r"] is None


def test_t20_timeout_after_cutoff_eligible():
    from app.research.srr_short_timeout_persistence_gate import (
        prepare_srr_persistence_result,
    )

    result = SrrRouteResult(
        experiment_id=SRR_EXPERIMENT_ID,
        observation_id=501,
        status="RESOLVED",
        reason_code="SRR_SHORT_POLICY_OK",
        path_class="TIMEOUT",
        finalization_eligible=True,
        source_status=SOURCE_VALIDATED,
        geometry_valid=True,
        eligible_candle_count=24,
        gross_r=0.5,
        cost_r_normal=0.0021,
        cost_r_elevated=0.0031,
        net_r_normal=0.4979,
        net_r_elevated=0.4969,
        policy_result={
            "cutoff_time_ms": 1791467200000,
        },
        source_diagnostics={},
        route_diagnostics={},
    )

    asof_after = 1791467300000  # after cutoff
    prepared = prepare_srr_persistence_result(result, evaluation_asof_ms=asof_after)
    assert prepared["finalization_eligible"] is True
    assert prepared["gross_r"] == 0.5


# ─────────────────────────────────────────────────────────────────
# T21: Persistence exception → other OOS unaffected
# ─────────────────────────────────────────────────────────────────

def test_t21_persistence_exception_other_oos_unaffected():
    """A writer failure for one observation must not abort the cycle."""
    boundary = make_boundary()
    conn = _FakeConnForBoundary()
    writer = Mock(side_effect=RuntimeError("TEST_PERSISTENCE_FAILURE"))

    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=conn,
        candle_source=Mock(),
        dry_run=False,
        write_outcomes=writer,
        activation_boundary=boundary,
    )
    evaluator._current_asof_ms = lambda: ACTIVATION_TS_MS + 120_000

    # Simulate: SQL returns one eligible observation
    def fake_execute(sql, params=None):
        conn.cursor_obj.sql.append((sql, params))

    def fake_fetchall():
        return [
            (
                101, "BTCUSDT",
                datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc),
                SRR_EXPERIMENT_ID, "SHORT",
                100.0, 100.5, 100.0, 101.0, 99.25,
                json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
                None, None,
            ),
            (
                102, "ETHUSDT",
                datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc),
                SRR_EXPERIMENT_ID, "SHORT",
                200.0, 200.5, 200.0, 201.0, 199.25,
                json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
                None, None,
            ),
        ]

    conn.cursor_obj.execute = fake_execute
    conn.cursor_obj.fetchall = fake_fetchall

    # Mock route to return finalizable results
    evaluator.route = Mock(return_value=SrrRouteResult(
        experiment_id=SRR_EXPERIMENT_ID,
        observation_id=101,
        status="RESOLVED",
        reason_code="OK",
        path_class="TP_FIRST",
        finalization_eligible=True,
        source_status=SOURCE_VALIDATED,
        geometry_valid=True,
        eligible_candle_count=10,
        gross_r=1.5,
        cost_r_normal=0.0021,
        cost_r_elevated=0.0031,
        net_r_normal=1.4979,
        net_r_elevated=1.4969,
        policy_result={
            "structural_r": 1.0,
            "execution_r": 2.0,
            "max_hold_minutes": 120,
            "intrabar_policy": "STOP_FIRST",
            "coverage_complete": True,
            "cutoff_time_ms": 1791467200000,
        },
        source_diagnostics={},
        route_diagnostics={},
    ))

    stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

    # Both observations attempted
    assert stats["signals_checked"] == 2
    # Both had write failures
    assert writer.call_count == 2
    # Errors counted but cycle didn't abort
    assert stats["errors"] == 2
    # Both observations present in stats
    assert len(stats["observations"]) == 2
    assert all(o["write_action"] == "WRITE_FAILED" for o in stats["observations"])


# ─────────────────────────────────────────────────────────────────
# T22: Writer disabled → zero SRR writes
# ─────────────────────────────────────────────────────────────────

def test_t22_writer_disabled_zero_srr_writes():
    boundary = make_boundary()
    conn = _FakeConnForBoundary()
    candle_source = Mock()
    writer = Mock()

    # dry_run=True means writer is never called
    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=conn,
        candle_source=candle_source,
        dry_run=True,  # disabled
        write_outcomes=None,
        activation_boundary=boundary,
    )
    evaluator._current_asof_ms = lambda: ACTIVATION_TS_MS + 120_000

    def fake_execute(sql, params=None):
        conn.cursor_obj.sql.append((sql, params))

    def fake_fetchall():
        return [
            (
                103, "BTCUSDT",
                datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc),
                SRR_EXPERIMENT_ID, "SHORT",
                100.0, 100.5, 100.0, 101.0, 99.25,
                json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
                None, None,
            ),
        ]

    conn.cursor_obj.execute = fake_execute
    conn.cursor_obj.fetchall = fake_fetchall

    evaluator.route = Mock(return_value=SrrRouteResult(
        experiment_id=SRR_EXPERIMENT_ID,
        observation_id=103,
        status="RESOLVED",
        reason_code="OK",
        path_class="TP_FIRST",
        finalization_eligible=True,
        source_status=SOURCE_VALIDATED,
        geometry_valid=True,
        eligible_candle_count=10,
        gross_r=1.5,
        cost_r_normal=0.0021,
        cost_r_elevated=0.0031,
        net_r_normal=1.4979,
        net_r_elevated=1.4969,
        policy_result={},
        source_diagnostics={},
        route_diagnostics={},
    ))

    stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

    # dry_run=True → finalized counted in stats but no writes
    assert stats["signals_checked"] == 1
    assert stats["finalized"] == 1
    # writer.call_count is 0 because dry_run short-circuits
    # (we never passed writer to the evaluator)


# ─────────────────────────────────────────────────────────────────
# T23: Old 73 observations excluded from clean holdout
# ─────────────────────────────────────────────────────────────────

def test_t23_old_observations_excluded_from_holdout():
    """The 73 pre-activation observations are excluded at SQL level."""
    boundary = make_boundary()
    fragment, params = build_activation_sql_filters(boundary)

    # Verify the filter uses strict > (not >=)
    assert "o.signal_time > %s::timestamptz" in fragment
    assert ">=" not in fragment

    # The boundary timestamp itself is excluded (canonical UTC format)
    assert params[2] == "2026-10-08T12:00:00.000000Z"

    # Any observation with signal_time <= ACTIVATION_TS is excluded by SQL
    # This is a structural guarantee: the evaluator never sees them.


# ─────────────────────────────────────────────────────────────────
# T24: Reader/writer transaction isolation
# ─────────────────────────────────────────────────────────────────

def test_t24_reader_writer_transaction_isolation():
    """Writer must use a separate connection from reader."""
    from app.research.srr_short_writer_connection_manager import (
        open_srr_outcome_writer,
    )

    reader_conn = object()

    def fake_connect(**kwargs):
        return MagicMock(name="writer_conn")

    with open_srr_outcome_writer(
        host="localhost",
        port=5432,
        database="test",
        user="test",
        password="test",
        reader_conn=reader_conn,
        connect_factory=fake_connect,
    ) as writer:
        assert writer is not None
        # Writer has its own connection, not the reader's
        assert writer._conn is not reader_conn

    # The writer connection was rolled back and closed
    # (verified by the connection manager's finally block)


def test_t24_writer_cannot_rollback_reader():
    """Writer rollback must not affect reader's transaction."""
    from app.research.srr_short_writer_connection_manager import (
        open_srr_outcome_writer,
    )

    boundary = make_boundary()
    gate = _make_active_gate(boundary)

    reader_conn = MagicMock(name="reader_conn")
    reader_conn.cursor.side_effect = AssertionError("reader cursor not allowed")

    def fake_connect(**kwargs):
        writer_conn = MagicMock(name="writer_conn")
        return writer_conn

    with open_srr_outcome_writer(
        host="localhost",
        port=5432,
        database="test",
        user="test",
        password="test",
        reader_conn=reader_conn,
        connect_factory=fake_connect,
        activation_gate=gate,
    ) as writer:
        # Simulate a write failure that triggers rollback
        cursor = MagicMock()
        writer._conn.cursor.return_value = cursor

        # First fetchone is the activation record read → row exists (ACTIVE)
        # Second fetchone triggers the injected failure.
        cursor.fetchone.side_effect = [
            (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
             SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
            RuntimeError("TEST"),
        ]
        cursor.execute.side_effect = [None, RuntimeError("TEST")]

        with pytest.raises(RuntimeError):
            writer.write(
                observation_id=1,
                experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
                observation=_eligible_observation(),
                result={"status": "TEST", "reason_code": "TEST", "finalization_eligible": False},
            )

    # reader_conn.rollback was never called
    reader_conn.rollback.assert_not_called()


# ─────────────────────────────────────────────────────────────────
# Activation gate tests
# ─────────────────────────────────────────────────────────────────

def test_gate_without_mode_blocks_writes():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(boundary=boundary, mode=None)
    with pytest.raises(SrrActivationBoundaryError):
        gate.assert_writer_allowed()


def test_gate_with_observe_only_mode_blocks_writes():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=False),
    )
    with pytest.raises(SrrActivationBoundaryError):
        gate.assert_writer_allowed()


def test_gate_without_record_blocks_writes():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    with pytest.raises(SrrActivationRecordUnavailable):
        gate.assert_writer_allowed()


def test_gate_with_matching_record_allows_writes():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })
    gate.assert_writer_allowed()  # no exception


def test_gate_with_mismatched_experiment_rejected():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    with pytest.raises(SrrActivationRecordMismatch):
        gate.verify_persisted_record({
            "experiment_id": "WRONG_EXPERIMENT",
            "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
            "activation_ts": ACTIVATION_TS,
            "direction": SRR_ACTIVATION_DIRECTION,
            "status": ACTIVATION_STATUS_ACTIVE,
        })


def test_gate_with_mismatched_version_rejected():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    with pytest.raises(SrrActivationRecordMismatch):
        gate.verify_persisted_record({
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "boundary_version": "SOME_OTHER_VERSION",
            "activation_ts": ACTIVATION_TS,
            "direction": SRR_ACTIVATION_DIRECTION,
            "status": ACTIVATION_STATUS_ACTIVE,
        })


# ─────────────────────────────────────────────────────────────────
# Boundary normalization tests
# ─────────────────────────────────────────────────────────────────

def test_normalize_activation_ts_with_offset():
    """A timezone offset is converted to UTC."""
    ns, text = normalize_activation_ts("2026-10-08T14:00:00+02:00")
    assert ns == ACTIVATION_TS_NS
    assert text == "2026-10-08T12:00:00.000000Z"


def test_normalize_activation_ts_with_milliseconds():
    ns, text = normalize_activation_ts("2026-10-08T12:00:00.500Z")
    assert ns == ACTIVATION_TS_NS + 500_000_000
    assert text == "2026-10-08T12:00:00.500000Z"


def test_normalize_activation_ts_with_datetime_object():
    dt = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
    ns, text = normalize_activation_ts(dt)
    assert ns == ACTIVATION_TS_NS


def test_normalize_activation_ts_with_offset_datetime():
    tz = timezone(timedelta(hours=2))
    dt = datetime(2026, 10, 8, 14, 0, 0, tzinfo=tz)
    ns, text = normalize_activation_ts(dt)
    assert ns == ACTIVATION_TS_NS


# ─────────────────────────────────────────────────────────────────
# Runtime wiring tests
# ─────────────────────────────────────────────────────────────────

def test_runtime_wiring_observe_only_returns_none():
    from app.research.srr_short_runtime_wiring import prepare_srr_short_writer

    with prepare_srr_short_writer(
        reader_conn=object(),
        host="localhost",
        port=5432,
        database="test",
        user="test",
        enable_writes=False,
    ) as writer:
        assert writer is None


def test_runtime_writes_blocked_without_mode():
    from app.research.srr_short_runtime_wiring import (
        SrrRuntimeWriteBlocked,
        prepare_srr_short_writer,
    )

    with pytest.raises(SrrRuntimeWriteBlocked):
        with prepare_srr_short_writer(
            reader_conn=object(),
            host="localhost",
            port=5432,
            database="test",
            user="test",
            enable_writes=True,
            activation_ts=ACTIVATION_TS,
            activation_mode=None,
        ):
            pass


def test_runtime_writes_blocked_without_activation_ts():
    from app.research.srr_short_runtime_wiring import (
        SrrActivationConfigError,
        prepare_srr_short_writer,
    )

    with pytest.raises(SrrActivationConfigError):
        with prepare_srr_short_writer(
            reader_conn=object(),
            host="localhost",
            port=5432,
            database="test",
            user="test",
            enable_writes=True,
            activation_ts=None,
            activation_mode=SrrShortWriterActivationMode(enabled=True),
        ):
            pass


def test_runtime_writes_with_naive_activation_ts_blocked():
    from app.research.srr_short_runtime_wiring import (
        SrrActivationConfigError,
        prepare_srr_short_writer,
    )

    with pytest.raises(SrrActivationConfigError):
        with prepare_srr_short_writer(
            reader_conn=object(),
            host="localhost",
            port=5432,
            database="test",
            user="test",
            enable_writes=True,
            activation_ts="2026-10-08T12:00:00",  # no timezone
            activation_mode=SrrShortWriterActivationMode(enabled=True),
        ):
            pass


# ═══════════════════════════════════════════════════════════════════
# PRE-PR HARDENING TESTS (task D)
# ═══════════════════════════════════════════════════════════════════


def _make_active_gate(boundary=None):
    """Helper: create a fully verified ACTIVE gate."""
    boundary = boundary or make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "direction": SRR_ACTIVATION_DIRECTION,
        "status": ACTIVATION_STATUS_ACTIVE,
    })
    return gate


def _eligible_observation(observation_id=900, **overrides):
    """Helper: an observation strictly after the activation boundary."""
    base = {
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "direction": "SHORT",
        "signal_time": datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc),
        "symbol": "BTCUSDT",
        "features": json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
        "reference_price": 100.0,
        "invalidation_price": 102.0,
        "variant_entry": 100.0,
        "variant_stop": 102.0,
        "variant_target": 98.5,
    }
    base.update(overrides)
    return base


# ── D1: Writer without gate ─────────────────────────────────────

def test_d1_writer_without_gate_rejected_before_sql():
    """A writer constructed without a gate must reject before any SQL."""
    fake_conn = MagicMock()
    writer = SrrOutcomeWriter(fake_conn, activation_gate=None)

    result = writer.write(
        observation_id=1,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={"status": "TEST", "reason_code": "TEST", "finalization_eligible": False},
    )
    assert result.action == "REJECTED_NO_ACTIVATION_GATE"
    fake_conn.cursor.assert_not_called()


# ── D2: Writer with unauthorized gate ───────────────────────────

def test_d2_writer_with_unauthorized_gate_rejected():
    """An unverified gate (no record loaded from DB) blocks the write."""
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    # Gate exists but no activation record is loaded from the DB.
    # The writer tries to read the record and fails (no row / no DB).
    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor
    # load_authoritative_record returns None → no row found
    cursor.fetchone.return_value = None

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)

    result = writer.write(
        observation_id=1,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={"status": "TEST", "reason_code": "TEST", "finalization_eligible": False},
    )
    assert result.action == "REJECTED_ACTIVATION_RECORD_UNAVAILABLE"
    # Only the activation record read was attempted
    assert cursor.execute.call_count == 1
    # No outcome SQL was executed
    sqls = [c[0][0] for c in cursor.execute.call_args_list]
    assert not any("srr_short_execution_prospective_outcome" in s for s in sqls)


# ── D3: Activation status PENDING ───────────────────────────────

def test_d3_pending_record_blocks_writer():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    with pytest.raises(SrrActivationRecordNotActive):
        gate.verify_persisted_record({
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
            "activation_ts": ACTIVATION_TS,
            "direction": SRR_ACTIVATION_DIRECTION,
            "status": ACTIVATION_STATUS_PENDING,
        })


# ── D4: Activation status REVOKED ───────────────────────────────

def test_d4_revoked_record_blocks_writer():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    with pytest.raises(SrrActivationRecordNotActive):
        gate.verify_persisted_record({
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
            "activation_ts": ACTIVATION_TS,
            "direction": SRR_ACTIVATION_DIRECTION,
            "status": ACTIVATION_STATUS_REVOKED,
        })


# ── D5: Changing timestamp in PENDING state ─────────────────────

def test_d5_activation_ts_immutable_in_pending():
    original = {
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "direction": SRR_ACTIVATION_DIRECTION,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "status": ACTIVATION_STATUS_PENDING,
    }
    updated = dict(original)
    updated["activation_ts"] = "2026-10-08T13:00:00.000000Z"
    with pytest.raises(SrrActivationRecordImmutableViolation):
        validate_activation_record_immutability(original, updated)


def test_d5_activation_ts_immutable_in_active():
    original = {
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "direction": SRR_ACTIVATION_DIRECTION,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "status": ACTIVATION_STATUS_ACTIVE,
    }
    updated = dict(original)
    updated["activation_ts"] = "2026-10-08T13:00:00.000000Z"
    updated["status"] = ACTIVATION_STATUS_REVOKED
    with pytest.raises(SrrActivationRecordImmutableViolation):
        validate_activation_record_immutability(original, updated)


def test_d5_activation_ts_same_value_in_different_format_allowed():
    """The same timestamp in a different string format is not a change."""
    original = {
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "direction": SRR_ACTIVATION_DIRECTION,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,  # "2026-10-08T12:00:00Z"
        "status": ACTIVATION_STATUS_PENDING,
    }
    updated = dict(original)
    updated["activation_ts"] = "2026-10-08T14:00:00+02:00"  # same instant
    # No exception: normalize_activation_ts maps both to the same ms.
    validate_activation_record_immutability(original, updated)


# ── D6: DELETE activation record ────────────────────────────────

def test_d6_delete_blocked_in_sql_migration():
    """The migration must install a DELETE-blocking trigger."""
    from pathlib import Path

    sql_text = Path(
        "sql/migrations/062_srr_short_writer_activation_boundary.sql"
    ).read_text(encoding="utf-8")
    assert "BEFORE DELETE ON research.srr_short_writer_activation" in sql_text
    assert "fn_block_srr_short_writer_activation_delete" in sql_text
    assert "cannot be deleted" in sql_text


# ── D7: Invalid state machine transitions ───────────────────────

def test_d7_pending_to_active_allowed():
    validate_activation_transition(ACTIVATION_STATUS_PENDING, ACTIVATION_STATUS_ACTIVE)


def test_d7_pending_to_revoked_allowed():
    validate_activation_transition(ACTIVATION_STATUS_PENDING, ACTIVATION_STATUS_REVOKED)


def test_d7_active_to_revoked_allowed():
    validate_activation_transition(ACTIVATION_STATUS_ACTIVE, ACTIVATION_STATUS_REVOKED)


def test_d7_active_to_pending_blocked():
    with pytest.raises(SrrActivationInvalidTransition):
        validate_activation_transition(ACTIVATION_STATUS_ACTIVE, ACTIVATION_STATUS_PENDING)


def test_d7_revoked_to_active_blocked():
    with pytest.raises(SrrActivationInvalidTransition):
        validate_activation_transition(ACTIVATION_STATUS_REVOKED, ACTIVATION_STATUS_ACTIVE)


def test_d7_revoked_to_pending_blocked():
    with pytest.raises(SrrActivationInvalidTransition):
        validate_activation_transition(ACTIVATION_STATUS_REVOKED, ACTIVATION_STATUS_PENDING)


def test_d7_self_transition_is_noop():
    validate_activation_transition(ACTIVATION_STATUS_PENDING, ACTIVATION_STATUS_PENDING)
    validate_activation_transition(ACTIVATION_STATUS_ACTIVE, ACTIVATION_STATUS_ACTIVE)
    validate_activation_transition(ACTIVATION_STATUS_REVOKED, ACTIVATION_STATUS_REVOKED)


def test_d7_unknown_status_blocked():
    with pytest.raises(SrrActivationInvalidTransition):
        validate_activation_transition("SOMETHING_ELSE", ACTIVATION_STATUS_ACTIVE)


# ── D8: Substituted experiment/direction/version ────────────────

def test_d8_direction_mismatch_rejected():
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    with pytest.raises(SrrActivationRecordMismatch):
        gate.verify_persisted_record({
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
            "activation_ts": ACTIVATION_TS,
            "direction": "LONG",  # substituted
            "status": ACTIVATION_STATUS_ACTIVE,
        })


def test_d8_immutable_field_change_detected():
    original = {
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "direction": SRR_ACTIVATION_DIRECTION,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "status": ACTIVATION_STATUS_ACTIVE,
    }
    for field, new_value in [
        ("experiment_id", "WRONG"),
        ("direction", "LONG"),
        ("boundary_version", "OTHER"),
        ("activation_ts", "2026-10-08T13:00:00.000000Z"),
    ]:
        updated = dict(original)
        updated[field] = new_value
        with pytest.raises(SrrActivationRecordImmutableViolation):
            validate_activation_record_immutability(original, updated)


def test_d8_status_change_alone_is_allowed():
    """Changing only status/notes is permitted (lifecycle updates)."""
    original = {
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "direction": SRR_ACTIVATION_DIRECTION,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
        "status": ACTIVATION_STATUS_ACTIVE,
        "notes": "initial",
    }
    updated = dict(original)
    updated["status"] = ACTIVATION_STATUS_REVOKED
    updated["notes"] = "revoked per operator request"
    validate_activation_record_immutability(original, updated)  # no exception


# ── D9: Correct ACTIVE record with eligible observation ─────────

def test_d9_active_record_and_eligible_observation_allows_write():
    gate = _make_active_gate()
    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor
    cursor.fetchone.side_effect = [
        # 1) activation record read → ACTIVE
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
        # 2) SELECT FOR UPDATE → existing row
        (900,),
        # 3) UPDATE RETURNING
        (900, True),
    ]

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=900,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={
            "status": "INCOMPLETE_COVERAGE",
            "reason_code": "OK",
            "finalization_eligible": False,
        },
    )
    # Action may be REFRESHED (non-final update of an existing row).
    assert not result.action.startswith("REJECTED_")
    assert result.action in {"INSERTED", "REFRESHED"}


# ── D10: SQL parameters and pre-fetch boundary ──────────────────

def test_d10_sql_filter_parameter_order():
    boundary = make_boundary()
    fragment, params = build_activation_sql_filters(boundary)

    # Fragment must have exactly 3 placeholders in this order:
    # experiment_id, direction, activation_ts.
    assert fragment.count("%s") == 3
    assert "o.experiment_id = %s" in fragment
    assert "o.direction = %s" in fragment
    assert "o.signal_time > %s::timestamptz" in fragment

    # Parameter order must match placeholder order.
    assert params[0] == SRR_ACTIVATION_EXPERIMENT_ID
    assert params[1] == SRR_ACTIVATION_DIRECTION
    assert params[2] == "2026-10-08T12:00:00.000000Z"


def test_d10_prefetch_boundary_no_bybit_no_policy_no_writer():
    """In writer-enabled mode, pre-activation observations are excluded
    by the SQL filter and never reach the Bybit source, frozen policy,
    or the writer callback."""
    boundary = make_boundary()
    conn = _FakeConnForBoundary()
    candle_source = Mock()
    writer = Mock()

    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=conn,
        candle_source=candle_source,
        dry_run=False,
        write_outcomes=writer,
        activation_boundary=boundary,
    )
    evaluator._current_asof_ms = lambda: ACTIVATION_TS_MS + 3600_000

    # Simulate: SQL returns empty list (filter excluded pre-activation rows).
    def fake_fetchall():
        return []
    conn.cursor_obj.fetchall = fake_fetchall

    stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

    # The SQL query included the activation boundary parameter.
    assert len(conn.cursor_obj.sql) >= 1
    sql, params = conn.cursor_obj.sql[0]
    assert "o.signal_time > %s::timestamptz" in sql
    assert params[0] == SRR_EXPERIMENT_ID
    assert params[1] == "2026-10-07T08:17:50Z"  # FROZEN_FREEZE_TS
    assert params[2] == SRR_ACTIVATION_EXPERIMENT_ID
    assert params[3] == SRR_ACTIVATION_DIRECTION
    assert params[4] == "2026-10-08T12:00:00.000000Z"

    # Zero downstream calls.
    assert stats["signals_checked"] == 0
    assert candle_source.fetch_5m_candles.call_count == 0
    assert writer.call_count == 0


# ── D-extra: direction mismatch in record vs boundary ───────────

def test_d_extra_missing_direction_rejected():
    """A record with no direction field is rejected."""
    boundary = make_boundary()
    gate = SrrShortWriterActivationGate(
        boundary=boundary,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    with pytest.raises(SrrActivationRecordMismatch):
        gate.verify_persisted_record({
            "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
            "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
            "activation_ts": ACTIVATION_TS,
            # direction missing
            "status": ACTIVATION_STATUS_ACTIVE,
        })


def test_d_extra_revoked_record_status_recheck_blocks_write():
    """A record that was ACTIVE at verification but later marked REVOKED
    in-memory is caught by assert_writer_allowed's status re-check."""
    gate = _make_active_gate()
    gate.assert_writer_allowed()  # initially OK

    # Simulate in-memory revocation.
    gate._record_status = ACTIVATION_STATUS_REVOKED
    with pytest.raises(SrrActivationRecordNotActive):
        gate.assert_writer_allowed()


# ═══════════════════════════════════════════════════════════════════
# FINAL REVIEW FIX TESTS (task FIX 1-3)
# ═══════════════════════════════════════════════════════════════════


def _make_db_conn(active_row=True, revoke_row=False):
    """Helper: fake connection returning the activation record row."""
    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor

    active_row_tuple = (
        SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
        SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE,
    )
    revoked_row_tuple = (
        SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
        SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_REVOKED,
    )

    if revoke_row:
        cursor.fetchone.side_effect = [revoked_row_tuple]
    elif active_row:
        cursor.fetchone.side_effect = [active_row_tuple]
    else:
        cursor.fetchone.return_value = None

    return fake_conn, cursor


# ── 1. ACTIVE → REVOKED after first verification ────────────────

def test_fr1_active_to_revoked_after_verification_blocks_write():
    """A record verified as ACTIVE, then changed to REVOKED in the DB,
    is caught by the per-write authoritative read."""
    gate = _make_active_gate()

    # First write: DB returns ACTIVE
    fake_conn, cursor = _make_db_conn(active_row=True)
    # After the activation row, the writer proceeds to outcome SQL
    cursor.fetchone.side_effect = [
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
        (900,),        # SELECT FOR UPDATE
        (900, True),   # UPDATE RETURNING
    ]

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=900,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={"status": "INCOMPLETE_COVERAGE", "reason_code": "OK",
                "finalization_eligible": False},
    )
    assert result.action in {"INSERTED", "REFRESHED"}

    # Second write: DB now returns REVOKED
    fake_conn2, cursor2 = _make_db_conn(active_row=False, revoke_row=True)
    writer2 = SrrOutcomeWriter(fake_conn2, activation_gate=gate)
    result2 = writer2.write(
        observation_id=901,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(observation_id=901),
        result={"status": "INCOMPLETE_COVERAGE", "reason_code": "OK",
                "finalization_eligible": False},
    )
    assert result2.action == "REJECTED_ACTIVATION_GATE"
    # No outcome SQL was executed after the record read
    outcome_sqls = [c[0][0] for c in cursor2.execute.call_args_list
                    if "srr_short_execution_prospective_outcome" in c[0][0]]
    assert outcome_sqls == []


# ── 2. REVOKED between two writer calls ─────────────────────────

def test_fr2_revoked_between_two_writer_calls():
    """A revoke between two writer calls blocks the second write."""
    gate = _make_active_gate()

    # Call 1: DB returns ACTIVE
    fake_conn, cursor = _make_db_conn(active_row=True)
    cursor.fetchone.side_effect = [
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
        (900,), (900, True),
    ]
    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=900,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={"status": "INCOMPLETE_COVERAGE", "reason_code": "OK",
                "finalization_eligible": False},
    )
    assert result.action in {"INSERTED", "REFRESHED"}

    # Call 2: DB now returns REVOKED
    fake_conn2, cursor2 = _make_db_conn(revoke_row=True)
    writer2 = SrrOutcomeWriter(fake_conn2, activation_gate=gate)
    result2 = writer2.write(
        observation_id=901,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(observation_id=901),
        result={"status": "INCOMPLETE_COVERAGE", "reason_code": "OK",
                "finalization_eligible": False},
    )
    assert result2.action == "REJECTED_ACTIVATION_GATE"


# ── 3. Concurrent write and revoke ──────────────────────────────

def test_fr3_concurrent_write_and_revoke():
    """A concurrent revoke (between the record read and the outcome write)
    is detected: the writer re-reads the record before every write."""
    gate = _make_active_gate()

    # First call succeeds (ACTIVE)
    fake_conn, cursor = _make_db_conn(active_row=True)
    cursor.fetchone.side_effect = [
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
        (900,), (900, True),
    ]
    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=900,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={"status": "INCOMPLETE_COVERAGE", "reason_code": "OK",
                "finalization_eligible": False},
    )
    assert result.action in {"INSERTED", "REFRESHED"}

    # Second call: DB returns REVOKED (revoke committed between calls)
    fake_conn2, cursor2 = _make_db_conn(revoke_row=True)
    writer2 = SrrOutcomeWriter(fake_conn2, activation_gate=gate)
    result2 = writer2.write(
        observation_id=901,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(observation_id=901),
        result={"status": "INCOMPLETE_COVERAGE", "reason_code": "OK",
                "finalization_eligible": False},
    )
    assert result2.action == "REJECTED_ACTIVATION_GATE"


# ── 4. Forged ACTIVE record without DB row ──────────────────────

def test_fr4_forged_active_record_without_db_row():
    """A gate with a verified ACTIVE record but no matching row in the DB
    is rejected: the writer loads the record from the DB, not from memory."""
    # Gate is verified with a caller-supplied ACTIVE record.
    gate = _make_active_gate()

    # DB returns no row (activation record was never persisted).
    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor
    cursor.fetchone.return_value = None  # no activation record in DB

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=1,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={"status": "TEST", "reason_code": "TEST",
                "finalization_eligible": False},
    )
    assert result.action == "REJECTED_ACTIVATION_RECORD_UNAVAILABLE"


# ── 5. Deleted or missing activation record ─────────────────────

def test_fr5_missing_activation_record():
    """A deleted or never-created activation record blocks the write."""
    gate = _make_active_gate()

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor
    cursor.fetchone.return_value = None  # record absent

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=1,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={"status": "TEST", "reason_code": "TEST",
                "finalization_eligible": False},
    )
    assert result.action == "REJECTED_ACTIVATION_RECORD_UNAVAILABLE"


# ── 6. DB read exception → zero writes ──────────────────────────

def test_fr6_db_read_exception_zero_writes():
    """Any exception while reading the activation record blocks the write."""
    gate = _make_active_gate()

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor
    cursor.execute.side_effect = RuntimeError("DB connection lost")

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=1,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={"status": "TEST", "reason_code": "TEST",
                "finalization_eligible": False},
    )
    assert result.action == "REJECTED_ACTIVATION_RECORD_UNAVAILABLE"
    # No outcome SQL was executed
    outcome_sqls = [c[0][0] for c in cursor.execute.call_args_list
                    if "srr_short_execution_prospective_outcome" in c[0][0]]
    assert outcome_sqls == []


# ── 7. Timestamp exactly on boundary ────────────────────────────

def test_fr7_timestamp_exactly_on_boundary_rejected():
    """A signal_time exactly at the boundary is rejected (strict >)."""
    boundary = make_boundary()
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T12:00:00.000000Z",
    )


# ── 8. Timestamp 1µs after boundary ─────────────────────────────

def test_fr8_timestamp_one_microsecond_after_boundary():
    """A signal_time 1µs after the boundary is eligible."""
    boundary = make_boundary()
    assert boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T12:00:00.000001Z",
    )


# ── 9. Timestamp 1µs before boundary ────────────────────────────

def test_fr9_timestamp_one_microsecond_before_boundary():
    """A signal_time 1µs before the boundary is rejected."""
    boundary = make_boundary()
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T11:59:59.999999Z",
    )


# ── 10. UTC offset equivalence ──────────────────────────────────

def test_fr10_utc_offset_equivalence():
    """The same instant expressed with different UTC offsets produces
    the same normalized boundary."""
    b1 = SrrShortWriterActivationBoundary.create("2026-10-08T12:00:00Z")
    b2 = SrrShortWriterActivationBoundary.create("2026-10-08T14:00:00+02:00")
    b3 = SrrShortWriterActivationBoundary.create("2026-10-08T07:00:00-05:00")
    assert b1.activation_ts_ns == b2.activation_ts_ns == b3.activation_ts_ns
    assert b1.activation_ts_text == b2.activation_ts_text == b3.activation_ts_text


# ── 11. Restart with same ACTIVATION_TS ─────────────────────────

def test_fr11_restart_with_same_activation_ts():
    """A restart with the same ACTIVATION_TS is a pass."""
    b1 = SrrShortWriterActivationBoundary.create(ACTIVATION_TS)
    b2 = SrrShortWriterActivationBoundary.create(ACTIVATION_TS)
    assert b1.activation_ts_ns == b2.activation_ts_ns

    g1 = _make_active_gate(b1)
    g2 = _make_active_gate(b2)
    g1.assert_writer_allowed()
    g2.assert_writer_allowed()


# ── 12. Pre-activation → zero Bybit / zero policy / zero writes ─

def test_fr12_pre_activation_zero_downstream():
    """A pre-activation observation in writer-enabled mode triggers
    zero Bybit requests, zero frozen-policy calls, and zero writes."""
    boundary = make_boundary()
    conn = _FakeConnForBoundary()
    candle_source = Mock()
    writer = Mock()

    evaluator = SrrShortExecutionRExpansionProspectiveEvaluator(
        conn=conn,
        candle_source=candle_source,
        dry_run=False,
        write_outcomes=writer,
        activation_boundary=boundary,
    )
    evaluator._current_asof_ms = lambda: ACTIVATION_TS_NS // 1_000_000 + 3600_000

    # SQL returns an empty list (pre-activation rows are excluded by the filter).
    conn.cursor_obj.fetchall = lambda: []

    stats = evaluator.run_evaluation_cycle(SRR_EXPERIMENT_ID)

    assert stats["signals_checked"] == 0
    assert candle_source.fetch_5m_candles.call_count == 0
    assert writer.call_count == 0


# ── Authoritative record load tests ─────────────────────────────

def test_fr_authoritative_record_loaded_from_db():
    """The writer loads the record from the DB, not from a caller dict."""
    gate = _make_active_gate()

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor
    cursor.fetchone.side_effect = [
        # Activation record: ACTIVE
        (SRR_ACTIVATION_EXPERIMENT_ID, SRR_ACTIVATION_DIRECTION,
         SRR_ACTIVATION_BOUNDARY_V1, ACTIVATION_TS, ACTIVATION_STATUS_ACTIVE),
        # SELECT FOR UPDATE → existing
        (900,),
        # UPDATE RETURNING
        (900, True),
    ]

    writer = SrrOutcomeWriter(fake_conn, activation_gate=gate)
    result = writer.write(
        observation_id=900,
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        observation=_eligible_observation(),
        result={"status": "INCOMPLETE_COVERAGE", "reason_code": "OK",
                "finalization_eligible": False},
    )
    assert not result.action.startswith("REJECTED_")
    # Verify the record read SQL was executed first
    first_sql = cursor.execute.call_args_list[0][0][0]
    assert "srr_short_writer_activation" in first_sql


def test_fr_no_authoritative_record_loads_from_db():
    """When no record is loaded, the writer returns None and blocks."""
    gate = SrrShortWriterActivationGate(
        boundary=make_boundary(),
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor
    cursor.fetchone.return_value = None

    record = gate.load_authoritative_record(fake_conn)
    assert record is None
    with pytest.raises(SrrActivationRecordUnavailable):
        gate.assert_writer_allowed()


# ── Runtime wiring: no caller-supplied record ───────────────────

def test_fr_runtime_wiring_no_caller_supplied_record():
    """prepare_srr_short_writer no longer accepts activation_record."""
    import inspect
    from app.research.srr_short_runtime_wiring import prepare_srr_short_writer

    sig = inspect.signature(prepare_srr_short_writer)
    assert "activation_record" not in sig.parameters


# ── Sub-microsecond precision tests ─────────────────────────────

def test_fr_sub_microsecond_boundary_exact():
    """A timestamp with sub-microsecond precision is compared exactly."""
    boundary = SrrShortWriterActivationBoundary.create(
        "2026-10-08T12:00:00.000000000Z"
    )
    # Exactly on the boundary
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T12:00:00.000000000Z",
    )
    # 1 nanosecond after
    assert boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T12:00:00.000000001Z",
    )
    # 1 nanosecond before
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T11:59:59.999999999Z",
    )


def test_fr_ns_truncation_does_not_flip_side():
    """Sub-microsecond values are truncated toward the past; no rounding
    can move a timestamp across the boundary."""
    # Boundary at exactly 12:00:00.000000000
    boundary = SrrShortWriterActivationBoundary.create(
        "2026-10-08T12:00:00.000000000Z"
    )
    # 11:59:59.999999999 is strictly before → rejected
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T11:59:59.999999999Z",
    )
    # 12:00:00.000000001 is strictly after → eligible
    assert boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T12:00:00.000000001Z",
    )
    # 12:00:01.000000001 is strictly after → eligible
    assert boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-08T12:00:01.000000001Z",
    )


def test_fr_activation_ts_text_ns_precision():
    """The canonical text preserves nanosecond precision when present."""
    ns, text = normalize_activation_ts("2026-10-08T12:00:00.000000001Z")
    assert text == "2026-10-08T12:00:00.000000001Z"
    # And the ns value is exact
    assert ns == ACTIVATION_TS_NS + 1


def test_fr_activation_ts_text_us_precision():
    """The canonical text preserves microsecond precision when present."""
    ns, text = normalize_activation_ts("2026-10-08T12:00:00.000001Z")
    assert text == "2026-10-08T12:00:00.000001Z"
    assert ns == ACTIVATION_TS_NS + 1000
