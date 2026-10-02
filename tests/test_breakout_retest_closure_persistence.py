"""Regression tests for BREAKOUT_RETEST_LONG_CLOSURE_PERSISTENCE_V1.

These tests prove that terminal prospective experiment states cannot be
reanimated by registry reload, scanner restart, evaluator restart, or a
future DB bootstrap/migration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.research.prospective_observer import ProspectiveOOSObserver


CLOSED_ID = "BREAKOUT_RETEST_LONG_EXPECTANCY_REJECT_OOS_V1"


def _registry_entry():
    return {
        "scanner_name": "BREAKOUT_RETEST",
        "direction": "LONG",
        "status": "READY_TO_START",
    }


def _mock_conn_with_status(status: str):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = (status,)
    return conn, cursor


def _observe(observer: ProspectiveOOSObserver):
    return observer.observe(
        scanner_name="BREAKOUT_RETEST",
        direction="LONG",
        symbol="BTCUSDT",
        signal_time=datetime.now(timezone.utc),
        reference_price=100.0,
        invalidation_price=99.0,
        target_1=102.0,
        target_2=103.0,
        score=50.0,
        features={},
        parameters={},
        market_regime="RANGE",
    )


def test_cancelled_experiment_is_not_reactivated_on_bootstrap():
    """DB CANCELLED state must override an otherwise active registry entry."""
    conn, cursor = _mock_conn_with_status("CANCELLED")
    observer = ProspectiveOOSObserver(conn, {CLOSED_ID: _registry_entry()})

    assert _observe(observer) == []
    assert observer.stats.get(CLOSED_ID, 0) == 0
    assert observer.stats.get("promoted", 0) == 0
    assert not any("INSERT INTO research.prospective_observation" in str(call) for call in cursor.execute.call_args_list)


def test_completed_experiment_is_not_reactivated_on_bootstrap():
    """DB COMPLETED state must also prevent prospective capture."""
    conn, cursor = _mock_conn_with_status("COMPLETED")
    observer = ProspectiveOOSObserver(conn, {CLOSED_ID: _registry_entry()})

    assert _observe(observer) == []
    assert observer.stats.get(CLOSED_ID, 0) == 0
    assert not any("INSERT INTO research.prospective_observation" in str(call) for call in cursor.execute.call_args_list)


def test_inactive_registry_entry_is_not_reactivated():
    """Registry-level terminal/inactive status is also respected."""
    conn, cursor = _mock_conn_with_status(None)
    spec = _registry_entry()
    spec["status"] = "CANCELLED"
    observer = ProspectiveOOSObserver(conn, {CLOSED_ID: spec})

    assert _observe(observer) == []
    assert observer.stats.get(CLOSED_ID, 0) == 0
    assert cursor.execute.call_args_list == []


def test_terminal_experiment_does_not_affect_other_experiments():
    """A terminal closed experiment must not affect other active experiments."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    # Per-call sequence: CLOSED_ID lifecycle lookup -> CANCELLED;
    # FVG lifecycle lookup -> active status; FVG INSERT RETURNING -> id.
    cursor.fetchone.side_effect = [("CANCELLED",), ("READY_TO_START",), (1,)]
    registry = {
        CLOSED_ID: _registry_entry(),
        "FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1": {
            "scanner_name": "BREAKOUT_RETEST",
            "direction": "LONG",
            "status": "READY_TO_START",
        },
    }
    observer = ProspectiveOOSObserver(conn, registry)

    assert _observe(observer) == [1]
    # Only the active experiment may reach the observation INSERT.
    insert_sql = [
        str(call) for call in cursor.execute.call_args_list
        if "INSERT INTO research.prospective_observation" in str(call)
    ]
    assert any("FVG_REACTION_LONG_EXPECTANCY_REJECT_OOS_V1" in sql for sql in insert_sql)
    assert not any(CLOSED_ID in sql for sql in insert_sql)


def test_terminal_status_lookup_failure_fails_open_without_mutation():
    """Lifecycle lookup errors are fail-open and do not mutate the cache."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    # Per-call sequence: lifecycle lookup raises; INSERT RETURNING returns id.
    cursor.fetchone.side_effect = [RuntimeError("db unavailable"), (1,)]
    observer = ProspectiveOOSObserver(conn, {CLOSED_ID: _registry_entry()})

    assert _observe(observer) == [1]
    assert observer.stats.get(CLOSED_ID, 0) == 1

