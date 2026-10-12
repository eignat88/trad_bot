"""Source adapter for the independent V2 prospective observer."""
from __future__ import annotations

from typing import Any

from app.scanners.models import SetupCandidate

from . import protocol as _protocol

from .protocol import (
    ENTRY_EXPIRY_MINUTES,
    SCANNER_NAME,
    SCANNER_VERSION,
    SOURCE_DIRECTION,
    require_activation_at,
)


class MeEarlyMaeV2SourceAdapter:
    """Capture natural V1 LONG candidates before paper execution gates.

    The adapter is intentionally narrow: it accepts only the frozen scanner
    identity, rejects pre-activation events, and delegates durable idempotent
    persistence to the V2 repository.
    """

    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self._captured = 0
        self._rejected = 0

    def capture(self, candidate: SetupCandidate) -> int | None:
        _protocol.validate_activation_freeze(
            _protocol.FROZEN_AT,
            _protocol.ACTIVATION_AT,
            _protocol.datetime.now(_protocol.timezone.utc),
        )
        if candidate.scanner_name != SCANNER_NAME:
            self._rejected += 1
            return None
        if candidate.scanner_version != SCANNER_VERSION:
            self._rejected += 1
            return None
        if candidate.direction != SOURCE_DIRECTION:
            self._rejected += 1
            return None
        if candidate.detected_at <= require_activation_at():
            self._rejected += 1
            return None
        if not candidate.signal_candle_open_time:
            self._rejected += 1
            return None
        if candidate.target_1 is None:
            self._rejected += 1
            return None

        source = {
            "source_setup_id": candidate.setup_id,
            "instrument_id": self.repository.ensure_instrument(candidate.symbol),
            "symbol": candidate.symbol,
            "detected_at": candidate.detected_at,
            "signal_candle_open_at": self._from_ms(candidate.signal_candle_open_time),
            "entry_zone_low": candidate.entry_zone_low,
            "entry_zone_high": candidate.entry_zone_high,
            "reference_price": candidate.reference_price,
            "invalidation_price": candidate.invalidation_price,
            "target_1": candidate.target_1,
            "features": dict(candidate.features or {}),
        }
        if source["instrument_id"] is None:
            self._rejected += 1
            return None

        source_id = self.repository.insert_source_with_variants(source)
        if source_id is not None:
            self._captured += 1
        return source_id

    @staticmethod
    def _from_ms(value: int):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)

    @property
    def stats(self) -> dict[str, int]:
        return {"captured": self._captured, "rejected": self._rejected}
