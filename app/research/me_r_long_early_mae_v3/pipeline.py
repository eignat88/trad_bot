"""Isolated V3 pipeline facade with no paper/live route."""
from __future__ import annotations

from .capture import MeEarlyMaeV3SourceAdapter
from .evaluator import MeEarlyMaeV3Evaluator
from .repository import MeEarlyMaeV3Repository


class MeEarlyMaeV3Pipeline:
    def __init__(self, connection) -> None:
        self.repository = MeEarlyMaeV3Repository(connection)
        self.source_adapter = MeEarlyMaeV3SourceAdapter(self.repository)
        self.evaluator = MeEarlyMaeV3Evaluator(self.repository)

    def initialize(self) -> None:
        self.repository.register_protocol()

    def capture_source(self, source: dict, *, commit: bool = True) -> int | None:
        return self.source_adapter.capture(source, commit=commit)

    def evaluate_cycle(self, now=None) -> dict[str, int]:
        return self.evaluator.run_cycle(now)
