"""Durable persistence for immutable prospective research completions.

This repository intentionally supports INSERT-only semantics.  It exists so
low-level detector geometry remains causal while lifecycle state is frozen
above the detector in PostgreSQL.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Mapping

logger = logging.getLogger(__name__)

COMPLETED_IMMUTABLE = "COMPLETED_IMMUTABLE"

REQUIRED_SNAPSHOT_FIELDS = (
    "symbol",
    "direction",
    "level_price",
    "level_type",
    "touch_time",
    "touch_price",
    "reaction_time",
    "reaction_high",
    "reaction_low",
    "reaction_candles",
    "structure_reference_time",
    "structure_reference_price",
    "break_time",
    "break_price",
    "point_b_time",
    "point_b_price",
    "point_b_retrace_pct",
    "signal_time",
    "entry_reference_price",
    "structural_stop_price",
    "risk_abs",
    "risk_pct",
    "target_1",
)

_FREEZE_SQL = """
    INSERT INTO research.prospective_completed_event (
        experiment_id,
        setup_event_id,
        symbol,
        direction,
        status,
        snapshot,
        frozen_at,
        created_at
    ) VALUES (
        %s,
        %s,
        %s,
        %s,
        %s,
        %s,
        %s,
        %s
    )
    ON CONFLICT (experiment_id, setup_event_id) DO NOTHING
    RETURNING
        experiment_id,
        setup_event_id,
        symbol,
        direction,
        status,
        snapshot,
        frozen_at,
        created_at
"""

_EXISTING_SQL = """
    SELECT
        experiment_id,
        setup_event_id,
        symbol,
        direction,
        status,
        snapshot,
        frozen_at,
        created_at
    FROM research.prospective_completed_event
    WHERE experiment_id = %s
      AND setup_event_id = %s
"""


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _decode_snapshot(snapshot: Any) -> dict[str, Any]:
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    if isinstance(snapshot, (bytes, bytearray)):
        snapshot = bytes(snapshot).decode("utf-8")
    if isinstance(snapshot, str):
        if not snapshot:
            return {}
        try:
            decoded = json.loads(snapshot)
        except (TypeError, ValueError):
            return {"snapshot_text": snapshot}
        return decoded if isinstance(decoded, dict) else {"snapshot": decoded}
    if snapshot is None:
        return {}
    return {"snapshot": snapshot}


def _row_to_dict(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        snapshot = row.get("snapshot")
        frozen_at = row.get("frozen_at")
        created_at = row.get("created_at")
        return {
            "experiment_id": row.get("experiment_id"),
            "setup_event_id": row.get("setup_event_id"),
            "symbol": row.get("symbol"),
            "direction": row.get("direction"),
            "status": row.get("status"),
            "snapshot": _decode_snapshot(snapshot),
            "frozen_at": frozen_at,
            "created_at": created_at,
        }

    if len(row) != 8:
        raise ValueError(f"unexpected prospective_completed_event row width: {len(row)}")
    return {
        "experiment_id": row[0],
        "setup_event_id": row[1],
        "symbol": row[2],
        "direction": row[3],
        "status": row[4],
        "snapshot": _decode_snapshot(row[5]),
        "frozen_at": row[6],
        "created_at": row[7],
    }


class ProspectiveCompletedEventRepository:
    """First-writer-wins repository for immutable completion events."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @staticmethod
    def validate_completion(
        *,
        experiment_id: str,
        setup_event_id: str,
        snapshot: Mapping[str, Any],
    ) -> None:
        """Reject incomplete completion records before any DB write."""
        if not experiment_id:
            raise ValueError("experiment_id is required")
        if not setup_event_id:
            raise ValueError("setup_event_id is required")
        if direction := snapshot.get("direction"):
            if direction not in {"LONG", "SHORT"}:
                raise ValueError(f"unsupported completion direction: {direction}")
        missing = [
            field for field in REQUIRED_SNAPSHOT_FIELDS
            if field not in snapshot or snapshot[field] is None
        ]
        if missing:
            raise ValueError(
                "completion snapshot missing required geometry fields: "
                + ", ".join(missing)
            )

    def get_completed_event(
        self,
        experiment_id: str,
        setup_event_id: str,
    ) -> dict[str, Any] | None:
        """Read the authoritative frozen row without mutating it."""
        if not self._conn or not experiment_id or not setup_event_id:
            return None
        cursor = self._conn.cursor()
        try:
            cursor.execute(_EXISTING_SQL, (experiment_id, setup_event_id))
            row = cursor.fetchone()
            return _row_to_dict(row) if row is not None else None
        except Exception:
            self._conn.rollback()
            logger.exception(
                "prospective completed event lookup failed for %s/%s",
                experiment_id,
                setup_event_id,
            )
            return None
        finally:
            cursor.close()

    def freeze_completed_event(
        self,
        *,
        experiment_id: str,
        setup_event_id: str,
        snapshot: Mapping[str, Any],
        frozen_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Atomically freeze the first valid Point-B completion.

        Semantics:
          1. INSERT the immutable snapshot with status COMPLETED_IMMUTABLE.
          2. ON CONFLICT (experiment_id, setup_event_id) DO NOTHING.
          3. SELECT and return the authoritative row.

        There is intentionally no UPDATE path.  The first writer wins, later
        candidates lose, and existing frozen geometry remains authoritative.
        """
        if not self._conn:
            return None
        snapshot_dict = dict(snapshot)
        self.validate_completion(
            experiment_id=experiment_id,
            setup_event_id=setup_event_id,
            snapshot=snapshot_dict,
        )
        if frozen_at is None:
            frozen_at = snapshot_dict.get("frozen_at")
        if frozen_at is None:
            raise ValueError("frozen_at is required for a completed event")
        snapshot_dict.setdefault("frozen_at", frozen_at.isoformat() if hasattr(frozen_at, "isoformat") else frozen_at)
        symbol = str(snapshot_dict["symbol"])
        direction = str(snapshot_dict["direction"])
        frozen_value = frozen_at
        payload = json.dumps(
            snapshot_dict,
            default=_json_default,
            separators=(",", ":"),
            ensure_ascii=False,
        )

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                _FREEZE_SQL,
                (
                    experiment_id,
                    setup_event_id,
                    symbol,
                    direction,
                    COMPLETED_IMMUTABLE,
                    payload,
                    frozen_value,
                    frozen_value,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                self._conn.commit()
                logger.debug(
                    "prospective completion froze new authoritative row %s/%s",
                    experiment_id,
                    setup_event_id,
                )
                return _row_to_dict(row)

            # DO NOTHING returned no row: an authoritative frozen row already
            # exists.  SELECT it and never mutate it.
            cursor.execute(_EXISTING_SQL, (experiment_id, setup_event_id))
            row = cursor.fetchone()
            if row is None:
                self._conn.rollback()
                logger.exception(
                    "prospective completion conflict did not return authoritative row "
                    "for %s/%s",
                    experiment_id,
                    setup_event_id,
                )
                return None
            self._conn.commit()
            logger.debug(
                "prospective completion kept first authoritative row %s/%s",
                experiment_id,
                setup_event_id,
            )
            return _row_to_dict(row)
        except Exception:
            self._conn.rollback()
            logger.exception(
                "prospective completion freeze failed for %s/%s",
                experiment_id,
                setup_event_id,
            )
            raise
        finally:
            cursor.close()
