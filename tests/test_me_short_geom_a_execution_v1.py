from datetime import datetime, timezone
import json
from unittest.mock import MagicMock

from app.research.prospective_observer import ProspectiveOOSObserver


EXP_ID = "ME_SHORT_GEOM_A_EXECUTION_V1"
FREEZE_TS = "2026-10-19T08:26:00Z"
FREEZE_DT = datetime(2026, 10, 19, 8, 26, tzinfo=timezone.utc)

SPEC = {
    "scanner_name": "MOMENTUM_EXHAUSTION",
    "direction": "SHORT",
    "version": 1,
    "freeze_ts": FREEZE_TS,
    "max_hold_minutes": 240,
    "intrabar_policy": "STOP_FIRST",
    "cost_model": {
        "taker_fee_per_side": 0.00055,
        "normal_slippage_per_side": 0.0005,
        "elevated_slippage_per_side": 0.001,
        "primary_scenario": "NORMAL_COST",
    },
}


def _conn(*, lifecycle="RUNNING", started_at=FREEZE_DT, insert_row=(42,)):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.side_effect = [
        (lifecycle,),                 # _is_db_active
        (lifecycle, started_at),      # exact activation gate
        insert_row,                   # INSERT RETURNING
    ]
    cursor.fetchall.return_value = [] # no direction-level closure
    return conn, cursor


def _observe(conn, **overrides):
    kwargs = dict(
        scanner_name="MOMENTUM_EXHAUSTION",
        direction="SHORT",
        symbol="BTCUSDT",
        signal_time="2026-10-19T08:26:01Z",
        reference_price=100.0,
        invalidation_price=100.2,
        target_1=95.0,
        target_2=None,
        score=0.7,
        features={"atr": 1.0},
        parameters={},
        market_regime="HIGH_VOLATILITY",
    )
    kwargs.update(overrides)
    return ProspectiveOOSObserver(conn, {EXP_ID: SPEC}).observe(**kwargs)


def _insert_params(cursor):
    for call in cursor.execute.call_args_list:
        sql = str(call.args[0]) if call.args else ""
        if "INSERT INTO research.prospective_observation" in sql:
            return list(call.args[1])
    return None


def test_me_short_a_execution_is_strictly_post_freeze():
    conn, cursor = _conn()
    assert _observe(conn, signal_time="2026-10-19T08:25:59Z") == []
    assert _insert_params(cursor) is None

    conn, cursor = _conn()
    assert _observe(conn, signal_time=FREEZE_TS) == []
    assert _insert_params(cursor) is None

    conn, cursor = _conn()
    assert _observe(conn, signal_time="2026-10-19T08:26:01Z") == [42]
    assert _insert_params(cursor) is not None


def test_me_short_a_execution_requires_exact_db_activation_boundary():
    conn, cursor = _conn(lifecycle="READY_TO_START", started_at=None)
    # READY registry/DB presence must not activate prospective capture.
    cursor.fetchone.side_effect = [
        ("READY_TO_START",),
        ("READY_TO_START", None),
    ]
    assert _observe(conn) == []
    assert _insert_params(cursor) is None

    conn, cursor = _conn(started_at=datetime(2026, 10, 19, 8, 25, tzinfo=timezone.utc))
    assert _observe(conn) == []
    assert _insert_params(cursor) is None


def test_me_short_a_execution_persists_complete_frozen_tuple():
    conn, cursor = _conn()
    assert _observe(conn) == [42]

    params = _insert_params(cursor)
    assert params is not None
    assert params[0] == EXP_ID
    assert params[4] == "SHORT"
    assert params[6] == 100.0
    assert params[7] == 100.2
    assert params[8] == 95.0
    assert params[11] == "me_short_geom_a_execution_frozen"

    # Atomic execution tuple.
    assert params[12] == 100.0
    assert params[13] == 100.2
    assert params[14] == 95.0

    frozen = json.loads(params[15])
    assert frozen["_frozen_entry"] == "reference_price"
    assert frozen["_frozen_stop"] == "invalidation_price"
    assert frozen["_frozen_target"] == "target_1"
    assert frozen["_frozen_max_hold"] == 240
    assert frozen["_frozen_intrabar_policy"] == "STOP_FIRST"
    assert frozen["_frozen_freeze_ts"] == FREEZE_TS
    assert frozen["_frozen_cost_model"]["taker_fee_per_side"] == 0.00055


def test_me_short_a_execution_rejects_invalid_short_geometry():
    conn, cursor = _conn()
    assert _observe(conn, direction="LONG") == []
    assert _insert_params(cursor) is None

    conn, cursor = _conn()
    assert _observe(conn, invalidation_price=99.9) == []
    assert _insert_params(cursor) is None

    conn, cursor = _conn()
    assert _observe(conn, target_1=100.1) == []
    assert _insert_params(cursor) is None
