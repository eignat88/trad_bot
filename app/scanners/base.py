from __future__ import annotations

from typing import Protocol

from app.scanners.models import MarketContext, SetupCandidate


class MarketScanner(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def scan(self, context: MarketContext) -> list[SetupCandidate]: ...
