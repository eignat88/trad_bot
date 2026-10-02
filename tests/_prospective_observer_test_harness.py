"""Helpers for prospective OOS observer test harnesses.

Models the actual call sequence of ProspectiveOOSObserver.observe():
  1. lifecycle-check SELECT per experiment
  2. observation INSERT per routed experiment
  3. optional RETURNING observation_id per INSERT

Tests must select SQL calls by semantic content rather than positional index.
"""
from __future__ import annotations

from unittest.mock import MagicMock


INSERT_MARK = "INSERT INTO research.prospective_observation"
LIFECYCLE_MARK = "SELECT status FROM research.prospective_experiment"


def _execute_sql(call) -> str:
    if call.args:
        return str(call.args[0])
    return str(call)


def _execute_params(call):
    if len(call.args) >= 2:
        return call.args[1]
    return None


def _all_calls(cursor):
    return list(cursor.execute.call_args_list)


def _insert_calls(cursor):
    return [c for c in _all_calls(cursor) if INSERT_MARK in _execute_sql(c)]


def _lifecycle_calls(cursor):
    return [c for c in _all_calls(cursor) if LIFECYCLE_MARK in _execute_sql(c)]


def _insert_params(cursor):
    for call in _insert_calls(cursor):
        params = _execute_params(call)
        if params is not None:
            return list(params)
    return []


def _insert_params_for_exp(cursor, exp_id: str):
    for call in _insert_calls(cursor):
        params = _execute_params(call)
        if params and params[0] == exp_id:
            return list(params)
    return []


def _all_insert_params(cursor):
    return [_execute_params(call) for call in _insert_calls(cursor)]


def _insert_params_with_exp(cursor, exp_id: str):
    """Return all INSERT params tuples whose experiment_id matches exp_id."""
    out = []
    for call in _insert_calls(cursor):
        params = _execute_params(call)
        if params and params[0] == exp_id:
            out.append(list(params))
    return out


def _mock_conn_for_single_experiment(status: str = "RUNNING", insert_return=(1,)):
    """Build a MagicMock conn/cursor for a single-experiment observe() call.

    fetchone call order:
      1. lifecycle SELECT (returns status row)
      2. INSERT RETURNING (returns insert_return, or None for conflict)
    """
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    results = [(status,)]
    if insert_return is not None:
        results.append(insert_return)
    else:
        results.append(None)
    cursor.fetchone.side_effect = results
    return conn, cursor


def _mock_conn_for_multi_experiment(
    lifecycle_statuses: dict[str, str],
    insert_returns: list | None = None,
):
    """Build a MagicMock conn/cursor for multi-experiment observe() calls.

    lifecycle_statuses: mapping experiment_id -> DB lifecycle status for the
      lifecycle SELECT calls in registry order. "DEFAULT" is the fallback.
    insert_returns: list of values returned by INSERT RETURNING fetchone calls,
      in routing order. If None, INSERT fetchone returns None (conflict).
    """
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor

    fetchone_results: list = []

    # Lifecycle SELECT results in registry iteration order.
    for exp_id, status in lifecycle_statuses.items():
        if exp_id == "DEFAULT":
            continue
        fetchone_results.append((status,))

    # INSERT RETURNING results.
    if insert_returns is not None:
        fetchone_results.extend(insert_returns)
    else:
        fetchone_results.append(None)

    cursor.fetchone.side_effect = fetchone_results
    return conn, cursor
