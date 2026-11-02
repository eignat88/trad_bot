"""Offline issue #248 regression: SRR activation boundary is fail-closed."""
from datetime import datetime, timezone

import pytest

from app.research.srr_short_evaluator_activation_wiring_v1 import (
    resolve_srr_evaluator_activation_boundary,
)
from app.research.srr_short_writer_activation_boundary_v1 import (
    SRR_ACTIVATION_BOUNDARY_V1,
    SRR_ACTIVATION_EXPERIMENT_ID,
    SrrActivationConfigError,
    SrrActivationRecordMismatch,
    SrrActivationRecordNotActive,
    SrrActivationRecordUnavailable,
    build_activation_sql_filters,
)

TS = "2026-10-09T11:00:00+00:00"


class Cursor:
    def __init__(self, row=None, error=False):
        self.row = row
        self.error = error
        self.executed = []
        self.closed = False

    def execute(self, sql, params):
        self.executed.append((sql, params))
        if self.error:
            raise RuntimeError("db unavailable")

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


class Conn:
    def __init__(self, cursor):
        self.cur = cursor
        self.calls = 0

    def cursor(self):
        self.calls += 1
        return self.cur


def record(status="ACTIVE", ts=TS, version=SRR_ACTIVATION_BOUNDARY_V1):
    return (SRR_ACTIVATION_EXPERIMENT_ID, "SHORT", version, datetime.fromisoformat(ts), status)


def test_active_matching_boundary_is_strict_and_query_parameterized():
    c = Cursor(record())
    boundary = resolve_srr_evaluator_activation_boundary(
        Conn(c), writer_enabled=True, activation_ts=TS,
    )
    assert c.closed
    assert len(c.executed) == 1
    assert boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time="2026-10-09T11:00:00.000001+00:00",
    )
    assert not boundary.is_eligible(
        experiment_id=SRR_ACTIVATION_EXPERIMENT_ID,
        direction="SHORT",
        signal_time=TS,
    )
    fragment, params = build_activation_sql_filters(boundary)
    assert "o.signal_time > %s::timestamptz" in fragment
    assert params[-1] == boundary.activation_ts_text


@pytest.mark.parametrize(
    "row,activation_ts,error",
    [
        (None, TS, SrrActivationRecordUnavailable),
        (record(status="REVOKED"), TS, SrrActivationRecordNotActive),
        (record(status="PENDING"), TS, SrrActivationRecordNotActive),
        (record(ts="2026-10-09T12:00:00+00:00"), TS, SrrActivationRecordMismatch),
        (record(version="INVALID"), TS, SrrActivationRecordMismatch),
        (record(), None, SrrActivationConfigError),
    ],
)
def test_enabled_writer_never_allows_unfiltered_queue(row, activation_ts, error):
    c = Cursor(row)
    with pytest.raises(error):
        resolve_srr_evaluator_activation_boundary(
            Conn(c), writer_enabled=True, activation_ts=activation_ts,
        )


def test_database_error_fails_closed_and_closes_cursor():
    c = Cursor(error=True)
    with pytest.raises(SrrActivationRecordUnavailable):
        resolve_srr_evaluator_activation_boundary(
            Conn(c), writer_enabled=True, activation_ts=TS,
        )
    assert c.closed


def test_writer_disabled_keeps_observe_only_and_does_not_read_db():
    conn = Conn(Cursor(error=True))
    assert resolve_srr_evaluator_activation_boundary(
        conn, writer_enabled=False, activation_ts=None,
    ) is None
    assert conn.calls == 0
