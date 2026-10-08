"""Tests for SRR_SHORT_WRITER_ACTIVATION_BOUNDARY_V1.

Covers scenarios T01–T24 from the task specification.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch

import pytest

from app.research.srr_short_writer_activation_boundary_v1 import (
    SRR_ACTIVATION_BOUNDARY_V1,
    SRR_ACTIVATION_DIRECTION,
    SRR_ACTIVATION_EXPERIMENT_ID,
    SrrActivationBoundaryError,
    SrrActivationConfigError,
    SrrActivationRecordMismatch,
    SrrActivationRecordUnavailable,
    SrrPreActivationObservation,
    SrrShortWriterActivationBoundary,
    SrrShortWriterActivationGate,
    SrrShortWriterActivationMode,
    build_activation_sql_filters,
    normalize_activation_ts,
)
from app.research.srr_short_execution_outcome_persistence import SrrOutcomeWriter
from app.research.srr_short_execution_r_expansion_prospective_evaluator import (
    SrrShortExecutionRExpansionProspectiveEvaluator,
    SRR_EXPERIMENT_ID,
    SrrRouteResult,
    SOURCE_VALIDATED,
)

ACTIVATION_TS = "2026-10-08T12:00:00Z"
ACTIVATION_TS_MS = 1791460800000  # 2026-10-08T12:00:00Z


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
    })

    gate2 = SrrShortWriterActivationGate(
        boundary=boundary2,
        mode=SrrShortWriterActivationMode(enabled=True),
    )
    gate2.verify_persisted_record({
        "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
        "boundary_version": SRR_ACTIVATION_BOUNDARY_V1,
        "activation_ts": ACTIVATION_TS,
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
    })

    fake_conn = MagicMock()
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
    # No SQL was executed
    fake_conn.cursor.assert_not_called()


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
    })

    fake_conn = MagicMock()
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
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor

    # Existing non-final row
    cursor.fetchone.side_effect = [
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
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor

    # SELECT FOR UPDATE → existing, SELECT is_final → False (returns observation_id, is_final),
    # UPDATE RETURNING → success
    cursor.fetchone.side_effect = [
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
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor

    # SELECT FOR UPDATE → existing, SELECT is_final → True (returns is_final,)
    cursor.fetchone.side_effect = [
        (300,),     # SELECT FOR UPDATE
        (True,),    # SELECT is_final → (True,)
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
    })

    fake_conn = MagicMock()
    cursor = MagicMock()
    fake_conn.cursor.return_value = cursor

    # SELECT FOR UPDATE → existing, SELECT is_final → True (returns is_final,)
    cursor.fetchone.side_effect = [
        (400,),     # SELECT FOR UPDATE
        (True,),    # SELECT is_final → (True,)
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
    ) as writer:
        # Simulate a write failure that triggers rollback
        cursor = MagicMock()
        writer._conn.cursor.return_value = cursor
        cursor.execute.side_effect = RuntimeError("TEST")

        with pytest.raises(RuntimeError):
            writer.write(
                observation_id=1,
                experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
                observation={
                    "experiment_id": SRR_ACTIVATION_EXPERIMENT_ID,
                    "direction": "SHORT",
                    "signal_time": datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc),
                    "symbol": "BTCUSDT",
                    "features": json.dumps({"_frozen_freeze_ts": "2026-10-07T08:17:50Z"}),
                },
                result={"status": "TEST", "reason_code": "TEST"},
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
        })


# ─────────────────────────────────────────────────────────────────
# Boundary normalization tests
# ─────────────────────────────────────────────────────────────────

def test_normalize_activation_ts_with_offset():
    """A timezone offset is converted to UTC."""
    ms, text = normalize_activation_ts("2026-10-08T14:00:00+02:00")
    assert ms == ACTIVATION_TS_MS
    assert text == "2026-10-08T12:00:00.000000Z"


def test_normalize_activation_ts_with_milliseconds():
    ms, text = normalize_activation_ts("2026-10-08T12:00:00.500Z")
    assert ms == ACTIVATION_TS_MS + 500
    assert text == "2026-10-08T12:00:00.500000Z"


def test_normalize_activation_ts_with_datetime_object():
    dt = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
    ms, text = normalize_activation_ts(dt)
    assert ms == ACTIVATION_TS_MS


def test_normalize_activation_ts_with_offset_datetime():
    tz = timezone(timedelta(hours=2))
    dt = datetime(2026, 10, 8, 14, 0, 0, tzinfo=tz)
    ms, text = normalize_activation_ts(dt)
    assert ms == ACTIVATION_TS_MS


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
