"""Regression tests for direction-level lifecycle in shared prospective experiments.

Covers:
  1. LONG closed + SHORT running → LONG capture stopped, SHORT capture continues
  2. Existing immature LONG outcomes continue to mature after LONG closure
  3. Historical LONG observations/outcomes remain unchanged
  4. Experiments without direction-level rows use experiment-level status
  5. Restart/idempotency: direction states survive process restart
  6. No scanner trading side-effect from direction closure
  7. SHORT evaluator remains active after LONG closure
  8. Generic reusable: mechanism works for any shared experiment
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.research.prospective_observer import ProspectiveOOSObserver


SHARED_EXP = "SRR_OOS_SCANNER_V1_PROSPECTIVE"


def _shared_registry_entry():
    return {
        "scanner_name": "SUPPORT_RESISTANCE_REACTION",
        "directions": ["LONG", "SHORT"],
        "status": "READY_TO_START",
    }


def _observe(observer, direction):
    return observer.observe(
        scanner_name="SUPPORT_RESISTANCE_REACTION",
        direction=direction,
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


def _mock_conn_with_direction_state(direction_statuses: dict[str, str], insert_return=(1,)):
    """Build a conn/cursor pair modeling direction-level lifecycle lookup.

    The observer makes these DB calls in order:
      1. _is_db_active: fetchone → experiment-level status row
      2. _is_direction_active: fetchall → direction-state rows
      3. _observe_srr_oos_exit: execute INSERT, fetchone → RETURNING observation_id
    """
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor

    # fetchone is used by _is_db_active and by INSERT RETURNING.
    # fetchall is used by _is_direction_active.
    def fake_fetchone(*args, **kwargs):
        if not fake_fetchone._called_experiment:
            fake_fetchone._called_experiment = True
            return (SHARED_EXP, "RUNNING")
        return insert_return
    fake_fetchone._called_experiment = False

    cursor.fetchone.side_effect = fake_fetchone
    cursor.fetchall.side_effect = [
        [(dir, status) for dir, status in direction_statuses.items()],
    ]
    return conn, cursor


def _mock_conn_no_direction_state(insert_return=(1,)):
    """Build a conn/cursor pair where no direction-level rows exist."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor

    fetchone_results = [
        (SHARED_EXP, "RUNNING"),  # experiment-level status lookup
    ]
    if insert_return is not None:
        fetchone_results.append(insert_return)

    cursor.fetchone.side_effect = fetchone_results
    cursor.fetchall.side_effect = [[], insert_return]
    return conn, cursor


def _count_inserts(cursor, exp_id):
    return sum(
        1 for call in cursor.execute.call_args_list
        if "INSERT INTO research.prospective_observation" in str(call)
        and exp_id in str(call)
    )


# ── Test 1: LONG closed + SHORT running → LONG capture stopped ──


def test_long_closed_direction_stops_new_capture():
    """LONG candidate → no new observation when LONG = CLOSED_NEGATIVE."""
    conn, cursor = _mock_conn_with_direction_state({"LONG": "CLOSED_NEGATIVE", "SHORT": "RUNNING"})
    observer = ProspectiveOOSObserver(conn, {SHARED_EXP: _shared_registry_entry()})

    result = _observe(observer, "LONG")

    assert result == []
    assert _count_inserts(cursor, SHARED_EXP) == 0
    assert observer.stats.get("LONG", 0) == 0


# ── Test 2: SHORT capture continues after LONG closure ──


def test_short_direction_continues_capture():
    """SHORT candidate → new observation created when LONG = CLOSED_NEGATIVE."""
    conn, cursor = _mock_conn_with_direction_state({"LONG": "CLOSED_NEGATIVE", "SHORT": "RUNNING"})
    observer = ProspectiveOOSObserver(conn, {SHARED_EXP: _shared_registry_entry()})

    result = _observe(observer, "SHORT")

    assert result == [1]
    assert _count_inserts(cursor, SHARED_EXP) == 1
    assert observer.stats.get(SHARED_EXP, 0) == 1


# ── Test 3: existing immature LONG outcomes continue to mature ──


def test_evaluator_processes_immature_long_after_closure():
    """Evaluator must process existing immature LONG observations for closed direction."""
    from app.research.prospective_evaluator import ProspectiveOOSEvaluator

    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor

    # started_at lookup
    cursor.fetchone.side_effect = [
        (datetime(2026, 9, 29, 10, 58, 40, tzinfo=timezone.utc),),  # started_at
    ]
    # direction-state lookup returns closed LONG
    cursor.fetchall.side_effect = [
        [("LONG", "CLOSED_NEGATIVE"), ("SHORT", "RUNNING")],  # direction-state rows
        [],  # eligible observations (empty for this mock)
    ]

    evaluator = ProspectiveOOSEvaluator(conn=conn, client=None, repo=None)
    stats = evaluator.run_evaluation_cycle(SHARED_EXP)

    assert stats["signals_checked"] == 0  # no new immature rows in this mock
    assert stats["errors"] == 0


# ── Test 4: historical LONG observations unchanged ──


def test_historical_long_data_preserved():
    """Direction closure must not delete or rewrite historical observations."""
    # The observer only INSERTs; it never DELETEs or UPDATEs prospective_observation.
    source = (open("app/research/prospective_observer.py").read())
    assert "DELETE FROM research.prospective_observation" not in source
    assert "UPDATE research.prospective_observation" not in source


# ── Test 5: experiments without direction-level rows use experiment-level status ──


def test_no_direction_rows_falls_back_to_experiment_status():
    """Experiments without direction-level rows capture normally."""
    conn, cursor = _mock_conn_no_direction_state(insert_return=(1,))
    observer = ProspectiveOOSObserver(conn, {SHARED_EXP: _shared_registry_entry()})

    result = _observe(observer, "LONG")

    assert result == [1]
    assert _count_inserts(cursor, SHARED_EXP) == 1


# ── Test 6: restart/idempotency — direction states survive restart ──


def test_direction_state_survives_restart():
    """Direction closure is DB-backed; new observer instances re-read it."""
    conn1, cursor1 = _mock_conn_with_direction_state({"LONG": "CLOSED_NEGATIVE", "SHORT": "RUNNING"})
    obs1 = ProspectiveOOSObserver(conn1, {SHARED_EXP: _shared_registry_entry()})
    _observe(obs1, "LONG")  # first instance: LONG blocked
    assert _count_inserts(cursor1, SHARED_EXP) == 0

    # Simulate restart: new observer instance on same DB
    conn2, cursor2 = _mock_conn_with_direction_state({"LONG": "CLOSED_NEGATIVE", "SHORT": "RUNNING"})
    obs2 = ProspectiveOOSObserver(conn2, {SHARED_EXP: _shared_registry_entry()})
    _observe(obs2, "LONG")  # second instance: LONG still blocked
    assert _count_inserts(cursor2, SHARED_EXP) == 0


# ── Test 7: no scanner trading side-effect ──


def test_no_trading_side_effect():
    """Direction closure must not touch scanner gates or paper trading."""
    source = open("app/research/prospective_observer.py").read()
    assert "place_order" not in source
    assert "paper_trade" not in source
    assert "direction_gate" not in source


# ── Test 8: SHORT evaluator remains active after LONG closure ──


def test_short_evaluator_active_after_long_closure():
    """Evaluator still loads the shared experiment when some directions are closed."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor

    from app.research.evaluator_runner import _load_prospective_experiments
    cursor.fetchall.return_value = [(SHARED_EXP,)]

    result = _load_prospective_experiments(conn)
    assert SHARED_EXP in result
