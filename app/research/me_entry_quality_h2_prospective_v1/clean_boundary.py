"""Immutable, microsecond-exact clean prospective boundary (H2 only).

The #305 freeze remains unchanged. Before H2 first activation an operator
must pin six candidate policy fingerprints and a fresh UTC boundary.
Never evaluate a source event with signal_time <= boundary or captured_at
< boundary. No historic #305 event is reclassified as clean OOS.
"""
from __future__ import annotations

from datetime import datetime, timezone

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def activation_timestamp_us(value: str) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("H2_CLEAN_ACTIVATION_TS_REQUIRED")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError("H2_CLEAN_ACTIVATION_TS_INVALID") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("H2_CLEAN_ACTIVATION_TS_REQUIRES_OFFSET")
    delta = dt.astimezone(timezone.utc) - EPOCH
    us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    if us <= 0:
        raise ValueError("H2_CLEAN_ACTIVATION_TS_INVALID")
    return us


def eligible_for_clean_holdout(row: dict, start_us: int) -> bool:
    from app.research.me_entry_quality_h1c_v1.event_adapter import parse_utc_us

    return (
        parse_utc_us(row.get("signal_time"), field_name="signal_time") > start_us
        and parse_utc_us(row.get("captured_at"), field_name="captured_at") >= start_us
    )
