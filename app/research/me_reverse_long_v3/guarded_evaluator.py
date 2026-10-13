"""Fail-closed, process-lifetime V3 research evaluator activation."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .activation import (
    V3ActivationIdentity,
    is_v3_evaluator_activated,
    load_v3_db_activation,
    resolve_startup_v3_activation,
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
        startup_activation: V3ActivationIdentity | None = None,
    ):
        self.conn = conn
        self.evaluator = evaluator
        self.registry_path = registry_path
        self.explicitly_enabled = explicitly_enabled
        self._startup_activation = startup_activation
        self._latched = startup_activation is not None and explicitly_enabled
        self._invalidated = False

    def run_evaluation_cycle(self) -> dict[str, int]:
        if not self.explicitly_enabled:
            return {"checked": 0, "finalized": 0, "errors": 0, "skipped": 1}

        # Activation identity must be supplied at evaluator startup.
        # Never initialize an inactive process during an evaluation cycle.
        if not self._latched or self._startup_activation is None:
            logger.info("V3 evaluation skipped: invalid startup activation")
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
                enabled=self.explicitly_enabled,
                registry_spec=spec,
                db_status=db_status,
                db_started_at=db_started_at,
                expected_activation=self._startup_activation,
            )

            if not allowed:
                if self._invalidated:
                    logger.info(
                        "V3 evaluation skipped: process activation remains invalidated"
                    )
                else:
                    logger.info(
                        "V3 evaluation skipped: activation invalid; "
                        "controlled restart required for reactivation"
                    )
                    self._invalidated = True
                    self._latched = False
                return {
                    "checked": 0,
                    "finalized": 0,
                    "errors": 0,
                    "skipped": 1,
                }

            if self._invalidated:
                # No runtime transition may restore an invalidated process.
                return {"checked": 0, "finalized": 0, "errors": 0, "skipped": 1}

            return self.evaluator.run_evaluation_cycle()

        except Exception:
            logger.exception("V3 activation recheck failed")
            self._invalidated = True
            self._latched = False
            return {"checked": 0, "finalized": 0, "errors": 1, "skipped": 1}
