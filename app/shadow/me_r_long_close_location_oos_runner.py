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

        # Strict clean observer contract: no proxy candle data.
        features = candidate.features

        if features.get("oos_clean_observer_version") != "1.2.0":
            logger.warning(
                "ME_R_LONG_CL_OOS: skipping non-clean candidate %s",
                candidate.symbol,
            )
            return

        required = (
            "oos_signal_open",
            "oos_signal_high",
            "oos_signal_low",
            "oos_signal_close",
            "oos_signal_volume",
            "oos_signal_entry_price",
            "oos_signal_candle_open_ms",
        )

        if any(features.get(key) is None for key in required):
            logger.error(
                "ME_R_LONG_CL_OOS: missing clean candle fields for %s",
                candidate.symbol,
            )
            return

        open_price = float(features["oos_signal_open"])
        high_price = float(features["oos_signal_high"])
        low_price = float(features["oos_signal_low"])
        close_price = float(features["oos_signal_close"])
        volume = float(features["oos_signal_volume"])
        signal_price = float(features["oos_signal_entry_price"])

        decision_time = candidate.detected_at
        signal_candle_open_time = datetime.fromtimestamp(
            int(features["oos_signal_candle_open_ms"]) / 1000,
            tz=timezone.utc,
        )

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
            decision_time=decision_time,
            signal_candle_open_time=signal_candle_open_time,
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
