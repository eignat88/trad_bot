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
import math
from datetime import datetime, timedelta, timezone
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


class MERLongCLoOosAck:
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    ERROR = "ERROR"
    INVALID_SOURCE = "INVALID_SOURCE"
    SKIPPED_VERSION = "SKIPPED_VERSION"


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
        self._invalid_source = 0
        self._skipped_version = 0

    def observe(self, candidate: SetupCandidate) -> str:
        """Observe a single candidate and persist if new.

        Both PASS and REJECT candidates are saved for OOS analysis.
        """
        # Extract close_location from features
        close_location = candidate.features.get("close_location")
        close_location_threshold = candidate.features.get("close_location_threshold", 0.70)
        filter_passed = candidate.features.get("close_location_passed", False)

        # Strict clean observer contract: no proxy candle data.
        features = candidate.features

        if (
            features.get("oos_clean_observer_version") != "1.2.0"
            or candidate.scanner_version != "1.2.0"
        ):
            logger.warning(
                "ME_R_LONG_CL_OOS: skipping non-clean candidate %s",
                candidate.symbol,
            )
            self._skipped_version += 1
            return MERLongCLoOosAck.SKIPPED_VERSION

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
            self._invalid_source += 1
            return MERLongCLoOosAck.INVALID_SOURCE

        try:
            numeric_keys = (
                "oos_signal_open",
                "oos_signal_high",
                "oos_signal_low",
                "oos_signal_close",
                "oos_signal_volume",
                "oos_signal_entry_price",
            )
            values = {key: float(features[key]) for key in numeric_keys}

            if not all(math.isfinite(value) for value in values.values()):
                raise ValueError("non-finite candle numeric field")

            candle_open_ms = int(features["oos_signal_candle_open_ms"])
            signal_candle_open_time = datetime.fromtimestamp(
                candle_open_ms / 1000,
                tz=timezone.utc,
            )

            raw_timestamp = features.get("close_location_source_timestamp")
            if not isinstance(raw_timestamp, str) or not raw_timestamp:
                raise ValueError("missing signal source timestamp")

            signal_time = datetime.fromisoformat(
                raw_timestamp.replace("Z", "+00:00")
            )
            if signal_time.tzinfo is None:
                raise ValueError("naive signal source timestamp")

            decision_time = candidate.detected_at
            if (
                not isinstance(decision_time, datetime)
                or decision_time.tzinfo is None
            ):
                raise ValueError("invalid decision timestamp")

            source_utc = signal_time.astimezone(timezone.utc)
            candle_utc = signal_candle_open_time.astimezone(timezone.utc)
            decision_utc = decision_time.astimezone(timezone.utc)

            if source_utc != candle_utc:
                raise ValueError("signal source and candle open mismatch")

            if decision_utc < candle_utc + timedelta(minutes=5):
                raise ValueError("signal candle not closed at decision time")

        except (TypeError, ValueError, OverflowError, OSError) as exc:
            self._invalid_source += 1
            logger.error(
                "ME_R_LONG_CL_OOS invalid source: symbol=%s reason=%s",
                candidate.symbol,
                exc,
            )
            return MERLongCLoOosAck.INVALID_SOURCE

        open_price = values["oos_signal_open"]
        high_price = values["oos_signal_high"]
        low_price = values["oos_signal_low"]
        close_price = values["oos_signal_close"]
        volume = values["oos_signal_volume"]
        signal_price = values["oos_signal_entry_price"]

        close_location = features.get("close_location")
        close_location_threshold = features.get(
            "close_location_threshold", 0.70
        )
        filter_passed = features.get("close_location_passed", False)

        # Preserve geometrically invalid but representable candles
        # for SQL INVALID classification.
        rsi = features.get("rsi")
        atr = features.get("atr")

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
                "ME_R_LONG_CL_OOS inserted: symbol=%s signal_time=%s "
                "close_location=%s filter_passed=%s signal_id=%s",
                candidate.symbol,
                signal_time,
                close_location,
                filter_passed,
                result.signal_id,
            )
            return MERLongCLoOosAck.INSERTED

        if result.status == MERLongCLoOosSaveStatus.DUPLICATE:
            self._duplicates += 1
            logger.debug(
                "ME_R_LONG_CL_OOS duplicate: symbol=%s signal_time=%s",
                candidate.symbol,
                signal_time,
            )
            return MERLongCLoOosAck.DUPLICATE

        self._errors += 1
        logger.error(
            "ME_R_LONG_CL_OOS save error: symbol=%s signal_time=%s",
            candidate.symbol,
            signal_time,
        )
        return MERLongCLoOosAck.ERROR

    def get_stats(self) -> dict[str, int]:
        """Get observer statistics."""
        return {
            "inserted": self._inserted,
            "duplicates": self._duplicates,
            "errors": self._errors,
            "invalid_source": self._invalid_source,
            "skipped_version": self._skipped_version,
        }


def observe_oos_candidate(
    repo: MERLongCLoOosRepository,
    candidate: SetupCandidate,
) -> str:
    """Observe one candidate and return its persistence ACK."""
    observer = MERLongCLoOosObserver(repo)
    return observer.observe(candidate)


def observe_oos_candidates(
    repo: MERLongCLoOosRepository,
    candidates: list[SetupCandidate],
) -> dict[str, int]:
    """Observe multiple OOS candidates and return stats."""
    observer = MERLongCLoOosObserver(repo)
    for candidate in candidates:
        observer.observe(candidate)
    return observer.get_stats()
