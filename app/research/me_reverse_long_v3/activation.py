"""Fail-closed activation policy for V3 research evaluation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .protocol import EXPERIMENT_ID


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("INVALID_ACTIVATION_TIMESTAMP")

    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("NAIVE_ACTIVATION_TIMESTAMP")

    return result.astimezone(timezone.utc)


def is_v3_evaluator_activated(
    *,
    enabled: bool,
    registry_spec: dict | None,
    db_status: str | None,
    db_started_at: datetime | None,
) -> bool:
    if not enabled or not isinstance(registry_spec, dict):
        return False

    if registry_spec.get("experiment_id") != EXPERIMENT_ID:
        return False

    if registry_spec.get("status") != "RUNNING":
        return False

    if db_status != "RUNNING" or db_started_at is None:
        return False

    try:
        return _utc(registry_spec.get("freeze_ts")) == _utc(db_started_at)
    except (TypeError, ValueError, OverflowError):
        return False


def load_v3_db_activation(conn: Any) -> tuple[str | None, datetime | None]:
    """Read V3 activation state. Fail closed on missing DB data."""
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT status, started_at
            FROM research.prospective_experiment
            WHERE experiment_id = %s
            """,
            (EXPERIMENT_ID,),
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else (None, None)
    except Exception:
        # PostgreSQL requires rollback after a failed statement.
        # Prevent the shared evaluator connection remaining aborted.
        conn.rollback()
        return None, None
    finally:
        if cursor is not None:
            cursor.close()
