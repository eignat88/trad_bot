"""Issue #322: actual observer SQL parameters, offline only.

Exercises the ME_SHORT-scoped adapter (:class:`ScannerSignalIntegrityObserver`)
against a mock connection: the candle identity must be stable per signal candle,
change per new candle, fail closed on a missing candle lineage, and rely on the
existing ``ON CONFLICT (experiment_id, source_signal_id)`` uniqueness for dedup.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.research.scanner_signal_integrity_observer import (
    ScannerSignalIntegrityObserver,
)


# A ME_SHORT-scope experiment id that routes to the generic insert path
# (``_observe_standard``) so the shared source-key contract is exercised
# without invoking any frozen experiment-specific geometry.
EXPERIMENT = "ME_SHORT_INTEGRITY_V1"
CANDLE = 1795680000000
START = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)


def _observer():
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (7,)
    registry = {
        EXPERIMENT: {
            "scanner_name": "MOMENTUM_EXHAUSTION",
            "direction": "SHORT",
            "status": "RUNNING",
        }
    }
    return ScannerSignalIntegrityObserver(conn, registry), conn, cursor


def _observe(observer, detected_at, candle):
    return observer.observe(
        scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT",
        symbol="BTCUSDT",
        signal_time=detected_at,
        entry_timeframe="5m",
        signal_candle_open_time=candle,
        reference_price=100.0,
        invalidation_price=99.0,
        target_1=102.0,
        target_2=None,
        score=80.0,
        features={},
        parameters={},
        market_regime="RANGE",
    )


def _inserts(cursor):
    return [
        call
        for call in cursor.execute.call_args_list
        if "INSERT INTO research.prospective_observation" in call.args[0]
    ]


def test_sql_identity_stable_on_rescan_and_changes_on_new_candle():
    observer, conn, cursor = _observer()

    with (
        patch.object(observer, "_is_db_active", return_value=True),
        patch.object(observer, "_is_direction_active", return_value=True),
    ):
        _observe(observer, START, CANDLE)
        _observe(observer, START + timedelta(seconds=20), CANDLE)
        _observe(observer, START + timedelta(minutes=5), CANDLE + 300000)
        _observe(observer, START + timedelta(minutes=6), None)

    inserts = _inserts(cursor)

    # The missing-candle call (lineage = None) fails closed -> no 4th insert.
    assert len(inserts) == 3

    params = [call.args[1] for call in inserts]
    ids = [p[1] for p in params]

    assert ids[0] == ids[1]           # same candle -> same source id
    assert ids[0] != ids[2]           # new candle -> new source id
    assert all(isinstance(value, int) and value < 0 for value in ids)
    assert all(-(2**63) <= value < -(2**31) for value in ids)

    # Detection timestamp remains separate from source identity.
    assert params[0][5] != params[1][5]

    # Existing DB uniqueness remains the deduplication mechanism.
    assert all(
        "ON CONFLICT (experiment_id, source_signal_id) DO NOTHING"
        in call.args[0]
        for call in inserts
    )

    # One commit per real capture; the fail-closed call touches no DB state.
    assert conn.commit.call_count == 3


def test_missing_candle_lineage_never_inserts():
    observer, conn, cursor = _observer()

    with (
        patch.object(observer, "_is_db_active", return_value=True),
        patch.object(observer, "_is_direction_active", return_value=True),
    ):
        for bad in (None, 0, -1, True, "0"):
            _observe(observer, START, bad)

    assert _inserts(cursor) == []
    assert conn.commit.call_count == 0
