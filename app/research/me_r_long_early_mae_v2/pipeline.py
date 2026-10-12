"""High-level V2 prospective observer wiring.

This module intentionally stays separate from ``scanner_runner`` and
``paper_runner``. Runtime wiring can instantiate it with a dedicated
PostgreSQL connection after deployment review; no production process is
changed by importing this module.
"""
from __future__ import annotations

from typing import Any

from app.scanners.models import SetupCandidate

from .capture import MeEarlyMaeV2SourceAdapter
from .evaluator import MeEarlyMaeV2Evaluator
from .repository import MeEarlyMaeV2Repository


class MeEarlyMaeV2Pipeline:
    def __init__(self, connection: Any) -> None:
        self.repository = MeEarlyMaeV2Repository(connection)
        self.source_adapter = MeEarlyMaeV2SourceAdapter(self.repository)
        self.evaluator = MeEarlyMaeV2Evaluator(self.repository)

    def initialize(self) -> None:
        self.repository.register_protocol()

    def capture_candidate(self, candidate: SetupCandidate) -> int | None:
        return self.source_adapter.capture(candidate)

    def evaluate_cycle(self, now: Any = None) -> dict[str, int]:
        return self.evaluator.run_cycle(now)
