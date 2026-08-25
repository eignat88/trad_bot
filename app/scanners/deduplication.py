from __future__ import annotations

from app.scanners.models import SetupCandidate


class DeduplicationEngine:
    def __init__(self, price_tolerance: float = 0.002) -> None:
        self._seen: dict[str, SetupCandidate] = {}
        self.price_tolerance = price_tolerance

    def _key(self, candidate: SetupCandidate) -> str:
        return (
            f"{candidate.scanner_name}|{candidate.symbol}|{candidate.direction}|"
            f"{candidate.setup_timeframe}"
        )

    def _is_duplicate(self, candidate: SetupCandidate) -> bool:
        key = self._key(candidate)
        existing = self._seen.get(key)
        if existing is None:
            return False
        if candidate.scanner_name != existing.scanner_name:
            return False
        if candidate.symbol != existing.symbol:
            return False
        if candidate.direction != existing.direction:
            return False
        if existing.reference_price > 0:
            diff = abs(candidate.reference_price - existing.reference_price) / existing.reference_price
            if diff < self.price_tolerance:
                return True
        if candidate.setup_started_at == existing.setup_started_at:
            return True
        return False

    def filter_new(self, candidates: list[SetupCandidate]) -> list[SetupCandidate]:
        result: list[SetupCandidate] = []
        for c in candidates:
            if not self._is_duplicate(c):
                self._seen[self._key(c)] = c
                result.append(c)
        return result

    def cleanup(self, max_age_seconds: int = 7200) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        expired = [
            key for key, c in self._seen.items()
            if (now - c.detected_at).total_seconds() > max_age_seconds
        ]
        for key in expired:
            del self._seen[key]

    def clear(self) -> None:
        self._seen.clear()
