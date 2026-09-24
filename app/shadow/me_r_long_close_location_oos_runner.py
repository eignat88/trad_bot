"""ME_R_LONG_CLOSE_LOCATION_OOS prospective OOS runner.

Scans ME_R_LONG base candidates, applies close_location >= 0.70 filter,
and persists both PASS and REJECT candidates to dds.me_r_long_close_location_oos_signal.

Architecture:
  - Uses existing scanner_runner infrastructure
  - Observes candidates from ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1 scanner
  - Persists to dedicated OOS tables (not scanner_setup)

Only signals from deploy time forward are recorded (prospective).
No backfill of historical observations.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.scanners.me_r_long_close_location_oos_validation import (
    MERLongCloseLocationOOSValidationV1Scanner,
)
from app.scanners.models import SetupCandidate
from app.shadow.me_r_long_close_location_oos_repository import (
    MERLongCLoOosRepository,
    MERLongCLoOosSaveStatus,
)

logger = logging.getLogger(__name__)

EXPERIMENT_ID = "ME_R_LONG_CLOSE_LOCATION_OOS_VALIDATION_V1"


class MERLongCLoOosObserver:
    """Observer for ME_R_LONG_CLOSE_LOCATION_OOS signals.

    Captures every candidate from the OOS scanner (both PASS and REJECT)
    and persists them to the dedicated OOS tables.
    """

    def __init__(self, repo: MERLongCLoOosRepository) -> None:
        self.repo = repo
        self._inserted = 0
        self._duplicates = 0
        self._errors = 0

    def observe(self, candidate: SetupCandidate) -> None:
        """Observe a single candidate and persist if new.

        Both PASS and REJECT candidates are saved for OOS analysis.
        """
        # Extract close_location from features
        close_location = candidate.features.get("close_location")
        close_location_threshold = candidate.features.get("close_location_threshold", 0.70)
        filter_passed = candidate.features.get("close_location_passed", False)

        # Extract signal candle OHLCV from features or candidate fields
        # The OOS scanner doesn't store raw OHLCV in features,
        # so we use reference_price as signal_price and entry_zone for range
        signal_price = candidate.reference_price or candidate.entry_zone_high

        # For now, use entry_zone as proxy for candle range
        # In production, this should come from the actual candle data
        open_price = candidate.entry_zone_low
        high_price = candidate.entry_zone_high
        low_price = candidate.invalidation_price
        close_price = candidate.entry_zone_high  # Approximation

        # Calculate candle range for volume proxy
        volume = 1000.0  # Placeholder - not critical for OOS analysis

        # Extract indicators from features if available
        rsi = candidate.features.get("rsi")
        atr = candidate.features.get("atr")

        # Get signal time from features or use detected_at
        signal_time_str = candidate.features.get("close_location_source_timestamp")
        if signal_time_str:
            try:
                signal_time = datetime.fromisoformat(signal_time_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                signal_time = candidate.detected_at
        else:
            signal_time = candidate.detected_at

        result = self.repo.save_signal(
            symbol=candidate.symbol,
            signal_time=signal_time,
            signal_price=signal_price,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            close_location=close_location,
            close_location_threshold=close_location_threshold,
            filter_passed=filter_passed,
            rsi=rsi,
            atr=atr,
            signal_version=candidate.scanner_version or "1.0.0",
        )

        if result.status == MERLongCLoOosSaveStatus.INSERTED:
            self._inserted += 1
            logger.info(
                "ME_R_LONG_CL_OOS observation saved: symbol=%s signal_time=%s "
                "close_location=%.4f filter_passed=%s signal_id=%d",
                candidate.symbol, signal_time,
                close_location if close_location is not None else 0.0,
                filter_passed,
                result.signal_id or 0,
            )
        elif result.status == MERLongCLoOosSaveStatus.DUPLICATE:
            self._duplicates += 1
            logger.debug(
                "ME_R_LONG_CL_OOS duplicate: symbol=%s signal_time=%s",
                candidate.symbol, signal_time,
            )
        else:
            self._errors += 1
            logger.error(
                "ME_R_LONG_CL_OOS save error: symbol=%s signal_time=%s",
                candidate.symbol, signal_time,
            )

    def get_stats(self) -> dict[str, int]:
        """Get observer statistics."""
        return {
            "inserted": self._inserted,
            "duplicates": self._duplicates,
            "errors": self._errors,
        }


def observe_oos_candidate(
    repo: MERLongCLoOosRepository,
    candidate: SetupCandidate,
) -> None:
    """Convenience function to observe a single OOS candidate."""
    observer = MERLongCLoOosObserver(repo)
    observer.observe(candidate)


def observe_oos_candidates(
    repo: MERLongCLoOosRepository,
    candidates: list[SetupCandidate],
) -> dict[str, int]:
    """Observe multiple OOS candidates and return stats."""
    observer = MERLongCLoOosObserver(repo)
    for candidate in candidates:
        observer.observe(candidate)
    return observer.get_stats()
