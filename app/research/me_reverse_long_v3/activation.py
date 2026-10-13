"""Fail-closed activation policy for V3 research evaluation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import EXPERIMENT_ID


@dataclass(frozen=True)
class V3ActivationIdentity:
    experiment_id: str
    status: str
    activation_ns: int


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        if not value.strip() or value != value.strip():
            raise ValueError("INVALID_ACTIVATION_TIMESTAMP")
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("INVALID_ACTIVATION_TIMESTAMP")

    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("NAIVE_ACTIVATION_TIMESTAMP")

    return result.astimezone(timezone.utc)


def _activation_ns(value: Any) -> int:
    value_utc = _utc(value)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value_utc - epoch
    return (
        delta.days * 86_400_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def is_v3_evaluator_activated(
    *,
    enabled: bool,
    registry_spec: dict | None,
    db_status: str | None,
    db_started_at: datetime | None,
    expected_activation: V3ActivationIdentity | None = None,
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
        registry_ns = _activation_ns(registry_spec.get("freeze_ts"))
        db_ns = _activation_ns(db_started_at)
    except (TypeError, ValueError, OverflowError):
        return False

    if registry_ns != db_ns:
        return False
    if expected_activation is None:
        return True

    return (
        expected_activation.experiment_id == EXPERIMENT_ID
        and expected_activation.status == "RUNNING"
        and expected_activation.activation_ns == registry_ns
    )


def resolve_startup_v3_activation(
    *,
    conn: Any,
    registry_path: Path,
    explicitly_enabled: bool,
) -> V3ActivationIdentity | None:
    """Resolve and validate the one activation identity for this process."""
    if not explicitly_enabled:
        return None

    try:
        registry = json.loads(registry_path.read_text())
        specs = registry.get("experiments", [])
        spec = next(
            (
                item for item in specs
                if item.get("experiment_id") == EXPERIMENT_ID
            ),
            None,
        )
        db_status, db_started_at = load_v3_db_activation(conn)
        if not is_v3_evaluator_activated(
            enabled=True,
            registry_spec=spec,
            db_status=db_status,
            db_started_at=db_started_at,
        ):
            return None

        return V3ActivationIdentity(
            experiment_id=EXPERIMENT_ID,
            status="RUNNING",
            activation_ns=_activation_ns(db_started_at),
        )
    except Exception:
        return None


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
