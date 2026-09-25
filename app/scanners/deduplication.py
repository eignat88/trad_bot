from __future__ import annotations

from app.scanners.models import SetupCandidate


class DeduplicationEngine:
    def __init__(self, price_tolerance: float = 0.002) -> None:
        self._seen: dict[str, SetupCandidate] = {}
        self.price_tolerance = price_tolerance

    def key(self, candidate: SetupCandidate) -> str:
        """Return the dedup identity key for a candidate.

        Public API — used by orchestrator to check whether a candidate was
        already kept by ``filter_new`` without touching private internals.
        """
        return (
            f"{candidate.scanner_name}|{candidate.symbol}|{candidate.direction}|"
            f"{candidate.entry_timeframe}|{candidate.signal_candle_open_time}"
        )

    # Internal alias kept so existing internal callers don't break.
    _key = key

    def _is_duplicate(self, candidate: SetupCandidate) -> bool:
        key = self._key(candidate)
        existing = self._seen.get(key)
        if existing is None:
            return False
        # A key represents one strategy/side/LTF candle. Price movement within
        # an unclosed candle must update/skip that setup, never create another.
        if candidate.signal_candle_open_time:
            return True
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
                self._seen[self.key(c)] = c
                result.append(c)
        return result

    def contains_key(self, candidate: SetupCandidate) -> bool:
        """Return True if this candidate's key was already accepted by ``filter_new``.

        Public API — avoids exposing the internal ``_seen`` dict.
        """
        return self.key(candidate) in self._seen

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
