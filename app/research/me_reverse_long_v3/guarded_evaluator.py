"""Per-cycle fail-closed V3 research evaluator activation."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .activation import (
    is_v3_evaluator_activated,
    load_v3_db_activation,
)
from .protocol import EXPERIMENT_ID

logger = logging.getLogger(__name__)


class GuardedV3Evaluator:
    def __init__(
        self,
        *,
        conn: Any,
        evaluator: Any,
        registry_path: Path,
        explicitly_enabled: bool = False,
    ):
        self.conn = conn
        self.evaluator = evaluator
        self.registry_path = registry_path
        self.explicitly_enabled = explicitly_enabled

    def run_evaluation_cycle(self) -> dict[str, int]:
        if not self.explicitly_enabled:
            return {"checked": 0, "finalized": 0, "errors": 0, "skipped": 1}

        try:
            registry = json.loads(self.registry_path.read_text())
            specs = registry.get("experiments", [])

            spec = next(
                (
                    item for item in specs
                    if item.get("experiment_id") == EXPERIMENT_ID
                ),
                None,
            )

            db_status, db_started_at = load_v3_db_activation(self.conn)

            allowed = is_v3_evaluator_activated(
                enabled=True,
                registry_spec=spec,
                db_status=db_status,
                db_started_at=db_started_at,
            )

            if not allowed:
                logger.info("V3 evaluation skipped: activation mismatch")
                return {
                    "checked": 0,
                    "finalized": 0,
                    "errors": 0,
                    "skipped": 1,
                }

            return self.evaluator.run_evaluation_cycle()

        except Exception:
            logger.exception("V3 activation recheck failed")
            return {"checked": 0, "finalized": 0, "errors": 1, "skipped": 1}
