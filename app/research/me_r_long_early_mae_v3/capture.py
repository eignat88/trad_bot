"""Fail-closed, nonproduction source adapter for V3."""
from __future__ import annotations

from datetime import timedelta

from .guards import require_runtime_configuration
from .protocol import (
    ACTIVATION_AT,
    SCANNER_NAME,
    SCANNER_VERSION,
    SOURCE_DIRECTION,
    execution_geometry,
    parse_utc,
    validate_measured_source_availability,
)
from .temporal import validate_detection_temporal


class MeEarlyMaeV3SourceAdapter:
    def __init__(self, repository) -> None:
        self.repository = repository

    def capture(self, source: dict, *, commit: bool = True) -> int | None:
        import datetime as _dt

        require_runtime_configuration(_dt.datetime.now(_dt.timezone.utc))
        if source.get("scanner_name") != SCANNER_NAME:
            return None
        if source.get("scanner_version") != SCANNER_VERSION:
            return None
        if source.get("direction") != SOURCE_DIRECTION:
            return None
        close_at = parse_utc(source["source_candle_close_at"])
        detected_at = parse_utc(source["detected_at"])
        if close_at <= parse_utc(ACTIVATION_AT):
            return None
        if detected_at <= parse_utc(ACTIVATION_AT):
            return None
        if "source_available_at" not in source or source["source_available_at"] is None:
            raise ValueError("MEASURED_SOURCE_AVAILABILITY_REQUIRED")
        measured_available = parse_utc(source["source_available_at"])
        validate_measured_source_availability(
            close_at, measured_available, detected_at
        )
        fetch_completed_raw = source.get("source_fetch_completed_at")
        if fetch_completed_raw is None:
            raise ValueError("SOURCE_FETCH_COMPLETION_PROVENANCE_REQUIRED")

        fetch_started = parse_utc(source["source_fetch_started_at"])
        fetch_completed = parse_utc(fetch_completed_raw)

        if fetch_completed < fetch_started:
            raise ValueError("FETCH_COMPLETED_BEFORE_START")

        temporal = validate_detection_temporal(
            close_at, detected_at, fetch_completed
        )

        if measured_available < fetch_completed:
            raise ValueError("SOURCE_AVAILABILITY_BEFORE_FETCH_COMPLETION")
        entry_price, stop_price, target_price = execution_geometry(
            float(source["reference_price"])
        )
        instrument_id = source.get("instrument_id")
        if instrument_id is None:
            instrument_id = self.repository.ensure_instrument(source["symbol"])
        if instrument_id is None:
            raise ValueError("INSTRUMENT_NOT_FOUND")
        payload = {
            **source,
            "instrument_id": instrument_id,
            "source_available_at": measured_available,
            "entry_eligible_at": max(
                temporal.source_available_at,
                detected_at,
                parse_utc(ACTIVATION_AT),
            ),
            "stop_price": stop_price,
            "target_price": target_price,
            "risk_distance": entry_price - stop_price,
        }
        if commit:
            return self.repository.insert_source_with_variants(payload)
        return self.repository.insert_source_with_variants(payload, commit=False)
